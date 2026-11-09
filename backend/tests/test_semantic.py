import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def semantic():
    assert importlib.util.find_spec('jobscope.semantic'), '语义适配分析尚未实现'
    from jobscope import semantic
    return semantic


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setenv('JOBSCOPE_LLM_URL', 'https://example.com/v1/chat/completions')
    monkeypatch.setenv('JOBSCOPE_LLM_MODEL', 'test')
    job = dict(id='J-1', title='后端开发', company='甲', description='熟悉 Python\n要求 SQL\n必须本科', evidence_ids=['E-1'], salary='20-30K')
    resume = dict(text='facts:\n  - id: F-1\n    status: confirmed\n    statement: 使用Python开发服务\n  - id: F-2\n    status: unconfirmed\n    statement: 本科毕业\n', format='facts_yaml', revision=1)
    return job, resume


def mock_model(monkeypatch, requirements):
    def post(self, url, **kwargs):
        payload = json.loads(kwargs['json']['messages'][1]['content'])
        assert all(f['id'] != 'F-2' for f in payload['facts'])
        content = json.dumps(dict(requirements=requirements, suggestions=['面试时核实工作内容与固定薪资。']), ensure_ascii=False)
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]}, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.Client, 'post', post)


def req(term, status='met', refs=None, dimension='skill', hard=False):
    return dict(requirement=term, jd_quote=term, status=status, fact_ids=refs if refs is not None else ['F-1'], dimension=dimension, hard=hard, rationale='依据所列个人材料，需面试核实深度。')


def test_semantic_score_separates_unknown_and_hard_conditions(context, monkeypatch):
    mock_model(monkeypatch, [req('Python'), req('SQL', 'unknown', []), req('必须本科', 'unknown', [], 'experience', True)])
    report = semantic().analyze_semantically(*context, [])
    assert report['score'] is None
    assert report['coverage'] < 0.6
    assert report['method'] == 'model_assisted'
    assert report['hard_conditions'][0]['status'] == 'unknown'
    assert report['requirements'][0]['evidence'] == ['F-1']


def test_semantic_score_is_calculated_by_code_not_model(context, monkeypatch):
    context[0]['description'] = 'Python\nSQL'
    mock_model(monkeypatch, [req('Python'), req('SQL', 'partial')])
    report = semantic().analyze_semantically(*context, [])
    assert report['coverage'] == 1
    assert report['score'] == 75


@pytest.mark.parametrize('requirements', [
    [req('Python', refs=['F-2'])],
    [req('不存在的岗位要求')],
    [req('Python', 'met', [])],
    [req('Python', 'unknown', ['F-1'])],
    [req('Python'), req('Python')],
])
def test_semantic_rejects_unconfirmed_facts_invented_jd_and_invalid_mapping(context, monkeypatch, requirements):
    mock_model(monkeypatch, requirements)
    with pytest.raises(ValueError):
        semantic().analyze_semantically(*context, [])


def test_omitted_hard_requirements_cannot_produce_high_score(context, monkeypatch):
    job, resume = context
    job['description'] = '熟悉Python\n必须博士学历\n必须10年经验'
    mock_model(monkeypatch, [req('Python')])
    report = semantic().analyze_semantically(job, resume, [])
    assert report['score'] is None
    assert len(report['hard_conditions']) == 2
    assert all(item['status'] == 'unknown' for item in report['hard_conditions'])
    assert report['coverage'] < .6


def test_null_model_content_has_clear_error(context, monkeypatch):
    def post(self, url, **kwargs):
        return httpx.Response(200, json={'choices': [{'message': {'content': None}}]}, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.Client, 'post', post)
    with pytest.raises(ValueError, match='结构'):
        semantic().analyze_semantically(*context, [])


def test_long_unmapped_jd_paragraph_cannot_be_silently_excluded(context, monkeypatch):
    job, resume = context
    job['description'] = 'Python\n' + '负责系统开发与维护，' * 180 + '必须博士学历，必须10年经验'
    mock_model(monkeypatch, [req('Python')])
    report = semantic().analyze_semantically(job, resume, [])
    assert report['score'] is None
    assert report['hard_conditions']
