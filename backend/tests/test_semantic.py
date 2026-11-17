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


def item(term, dimension='skill', hard=False):
    return dict(requirement=term, jd_quote=term, dimension=dimension, hard=hard)


def met(rationale='依据所列个人材料，需面试核实深度。'):
    return ('met', ['F-1'], rationale)


def mock_model(monkeypatch, items, matches=None, suggestions=None):
    """按 system prompt 区分拆解/匹配调用。

    items 为拆解返回的要求条目；matches 是全局 index → (status, fact_ids, rationale)，
    None 表示匹配组调用一律超时失败（整组转未知）。返回 (calls, seen_facts)：
    calls 记每次请求的 system 提示，seen_facts 记匹配调用实际收到的确认事实 ID。
    """
    calls, seen_facts = [], []

    def post(self, url, **kwargs):
        body = kwargs['json']
        system = body['messages'][0]['content']
        calls.append(system)
        if '拆解员' in system:
            content = json.dumps(dict(requirements=items, suggestions=suggestions or []), ensure_ascii=False)
        elif matches is None:
            raise httpx.ReadTimeout('timed out')
        else:
            payload = json.loads(body['messages'][1]['content'])
            seen_facts.extend(fact['id'] for fact in payload['facts'])
            content = json.dumps(dict(
                matches=[dict(index=index, status=status, fact_ids=refs, rationale=rationale)
                         for index, (status, refs, rationale) in sorted(matches.items())]),
                ensure_ascii=False)
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]}, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.Client, 'post', post)
    return calls, seen_facts


def test_semantic_score_separates_unknown_and_hard_conditions(context, monkeypatch):
    calls, seen_facts = mock_model(
        monkeypatch,
        [item('熟悉 Python'), item('要求 SQL'), item('必须本科', 'experience', True)],
        {0: met(), 1: ('unknown', [], '证据不足'), 2: ('unknown', [], '证据不足')},
        suggestions=['面试时核实工作内容与固定薪资。'])
    report = semantic().analyze_semantically(*context, [])
    assert report['score'] is None
    assert report['coverage'] < 0.6
    assert report['method'] == 'model_assisted'
    assert report['hard_conditions'][0]['status'] == 'unknown'
    assert report['requirements'][0]['evidence'] == ['F-1']
    assert report['questions'] == ['面试时核实工作内容与固定薪资。']
    assert seen_facts == ['F-1']  # 未确认事实不发给模型
    assert any('拆解员' in call for call in calls) and any('映射员' in call for call in calls)


def test_semantic_score_is_calculated_by_code_not_model(context, monkeypatch):
    context[0]['description'] = 'Python\nSQL'
    mock_model(monkeypatch, [item('Python'), item('SQL')],
               {0: met(), 1: ('partial', ['F-1'], '部分')})
    report = semantic().analyze_semantically(*context, [])
    assert report['coverage'] == 1
    assert report['score'] == 75


@pytest.mark.parametrize('match', [
    ('met', ['F-2'], '引用了未确认事实'),
    ('met', [], '无事实却称满足'),
    ('unknown', ['F-1'], 'unknown 却带事实'),
])
def test_invalid_match_downgrades_to_unknown_not_fatal(context, monkeypatch, match):
    context[0]['description'] = 'Python'
    mock_model(monkeypatch, [item('Python')], {0: match})
    report = semantic().analyze_semantically(*context, [])
    row = report['requirements'][0]
    assert row['status'] == 'unknown'
    assert row['evidence'] == []
    assert '已按未知处理' in row['rationale']
    assert any('未通过校验' in note for note in report['limitations'])


def test_extraction_drops_unlocatable_and_duplicate_quotes(context, monkeypatch):
    context[0]['description'] = 'Python\nSQL'
    mock_model(monkeypatch, [item('Python'), item('Python'),
                             dict(requirement='幻觉要求', jd_quote='不存在的句子', dimension='skill', hard=False)],
               {0: met()})
    report = semantic().analyze_semantically(*context, [])
    assert sum(1 for r in report['requirements'] if r['jd_quote'] == 'Python') == 1
    assert not any(r['requirement'] == '幻觉要求' for r in report['requirements'])


def test_extraction_all_unlocatable_is_fatal(context, monkeypatch):
    mock_model(monkeypatch, [dict(requirement='幻觉', jd_quote='不存在', dimension='skill', hard=False)])
    with pytest.raises(ValueError, match='没有一条能定位'):
        semantic().analyze_semantically(*context, [])


def test_group_failure_downgrades_to_unknown_not_fatal(context, monkeypatch):
    context[0]['description'] = 'Python\nSQL'
    mock_model(monkeypatch, [item('Python'), item('SQL')], matches=None)
    report = semantic().analyze_semantically(*context, [])
    assert all(r['status'] == 'unknown' for r in report['requirements'])
    assert all('模型调用失败' in r['rationale'] for r in report['requirements'])


def test_requirements_split_into_groups(context, monkeypatch):
    from jobscope import semantic as semantic_module
    monkeypatch.setattr(semantic_module, 'GROUP_SIZE', 2)
    context[0]['description'] = 'Python\nSQL\nGo'
    calls, _ = mock_model(monkeypatch, [item('Python'), item('SQL'), item('Go')],
                          {0: met(), 1: met(), 2: met()})
    semantic().analyze_semantically(*context, [])
    assert sum(1 for call in calls if '映射员' in call) == 2  # 3 条按每组 2 条拆两组


def test_cancel_raises_at_stage_boundary(context, monkeypatch):
    mock_model(monkeypatch, [item('Python')], {0: met()})
    from jobscope.semantic import RunCancelled
    with pytest.raises(RunCancelled):
        semantic().analyze_pipeline(*context, [], should_stop=lambda: True)


def test_extract_model_http_error_detail_reaches_failure_message(context, monkeypatch):
    def post(self, url, **kwargs):
        return httpx.Response(401, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.Client, 'post', post)
    with pytest.raises(ValueError, match=r'岗位要求拆解失败：.*401 Unauthorized'):
        semantic().analyze_semantically(*context, [])


def test_extract_silent_transport_error_falls_back_to_class_name(context, monkeypatch):
    def post(self, url, **kwargs):
        raise httpx.ReadTimeout('')
    monkeypatch.setattr(httpx.Client, 'post', post)
    with pytest.raises(ValueError, match='岗位要求拆解失败：ReadTimeout'):
        semantic().analyze_semantically(*context, [])


def test_null_model_content_has_clear_error(context, monkeypatch):
    def post(self, url, **kwargs):
        return httpx.Response(200, json={'choices': [{'message': {'content': None}}]}, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.Client, 'post', post)
    with pytest.raises(ValueError, match='拆解失败'):
        semantic().analyze_semantically(*context, [])


def test_omitted_hard_requirements_cannot_produce_high_score(context, monkeypatch):
    job, resume = context
    job['description'] = '熟悉Python\n必须博士学历\n必须10年经验'
    mock_model(monkeypatch, [item('熟悉Python')], {0: met()})
    report = semantic().analyze_semantically(job, resume, [])
    assert report['score'] is None
    assert len(report['hard_conditions']) == 2
    assert all(item['status'] == 'unknown' for item in report['hard_conditions'])
    assert report['coverage'] < .6


def test_long_unmapped_jd_paragraph_cannot_be_silently_excluded(context, monkeypatch):
    job, resume = context
    job['description'] = 'Python\n' + '负责系统开发与维护，' * 180 + '必须博士学历，必须10年经验'
    mock_model(monkeypatch, [item('Python')], {0: met()})
    report = semantic().analyze_semantically(job, resume, [])
    assert report['score'] is None
    assert report['hard_conditions']
