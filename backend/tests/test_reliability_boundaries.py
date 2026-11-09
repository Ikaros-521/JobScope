"""Boundary self-checks for the company reliability feature.

这些测试不评价功能好不好用，只钉住「不能做歪的地方」：形态红线（没有公开/分享/排名面）、
法律红线（每份报告都是主观参考、每条依据都有出处、material 类不出数字）、
密钥红线（响应全文不回显原文）、算分红线（分数只能来自 rubric，模型碰不到数字）。
任何一条变红都说明有人把功能往「对外评价他人商誉」的方向推了一步，应当直接返工。
"""
import ast
import json
import string
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobscope.main import create_app
from jobscope.repository import Repository, now
from jobscope.reliability import rubric, wording
from jobscope.reliability.models import DISCLAIMER, DISCLAIMER_VERSION, RuleFinding, SignalInput
from jobscope.reliability.service import ReliabilityService
from jobscope.reliability.sources import PAID_SOURCES, SIGNAL_SOURCES, SignalSourceStore

RELIABILITY_DIR = Path(__file__).resolve().parents[1] / 'jobscope' / 'reliability'
# 一次性哨兵：只有它出现在任何响应全文里才算事故，所以它不能和任何真实配置或文案撞车。
SECRET = 'sk-boundary-sentinel-4f9c71e2a8'
PAID_URL = 'https://vendor.example/qcc/company'
PAID_PAYLOAD = {'data': {'establish_date': '2015-04-01', 'insured_num': 240, 'insured_num_last': 312, 'case_num': 3}}

# 对外形态的词：本项目是本地单人自用情报整理，路径里出现任何一条都等于把它变成公开评价工具。
PUBLIC_SHAPE_WORDS = ('share', 'rank', 'public', 'leaderboard', 'blacklist', 'rating', 'scoreboard', 'compare')


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv('JOBSCOPE_LLM_URL', raising=False)
    with TestClient(create_app(tmp_path)) as client:
        yield client


def add_signal(tmp_path, company, rule, value, evidence_id, source_class='official'):
    """信号只能由采集或抽取产生；这里按契约直接落库，好让红线测试拿到一份真有依据的报告。"""
    repo = Repository(tmp_path)
    identity = repo.resolve_company(company)
    spec = rubric.RULES[rule]
    return repo.save_signal(SignalInput(company_id=identity['id'], dimension=spec.dimension, rule=rule,
                                        value=value, unit=spec.unit, source_class=source_class,
                                        evidence_id=evidence_id, observed_at='', collected_at=now()))


def add_all_kinds(tmp_path, company='甲公司'):
    """在册规则全量落库：assessment/material、official/platform/employee 三类依据都要被扫到。"""
    seeds = [('company_age', {'years': 8.0}, 'E-' + 'a' * 24, 'official'),
             ('insured_headcount_trend', {'before': 312, 'after': 240}, 'E-' + 'b' * 24, 'official'),
             ('judicial_case_ratio', {'ratio': 12.5}, 'E-' + 'c' * 24, 'official'),
             ('salary_quote_coverage', {'known': 9, 'total': 10}, 'E-' + 'd' * 24, 'platform'),
             ('disclosure_available', {'count': 4}, 'E-' + 'e' * 24, 'official'),
             ('employee_review_sample', {'samples': 3}, 'E-' + 'f' * 24, 'employee')]
    for rule, value, evidence_id, source_class in seeds:
        add_signal(tmp_path, company, rule, value, evidence_id, source_class)
    return [item[2] for item in seeds]


def all_findings(report):
    return [item for line in report['dimensions'] for item in line['findings']]


def stored_reports(tmp_path):
    """红线要覆盖「任何一份报告」，所以直接扫库表，不只看刚返回的那一条。"""
    with Repository(tmp_path).connection() as conn:
        return [json.loads(row[0]) for row in conn.execute('SELECT payload FROM company_reliability').fetchall()]


def module_tree(name):
    return ast.parse((RELIABILITY_DIR / f'{name}.py').read_text(encoding='utf-8'), filename=f'{name}.py')


def imported_names(tree):
    """把 `from ..chat import model_configured` 这类导入摊平成模块名 + 被导入符号名。"""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split('.')[0])
                names.add(alias.name.split('.')[-1])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split('.')[0])
                names.add(node.module.split('.')[-1])
            names.update(alias.name for alias in node.names)
    return names


def imported_modules(tree):
    """只看导入了哪些模块，不含被导入的符号名。"""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split('.')[-1])
    return names


def called_names(tree):
    return {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}


# --- 形态红线：没有公开面、没有分享页、没有榜单 ---

def test_app_exposes_no_sharing_ranking_or_public_route(tmp_path):
    paths = {route.path for route in create_app(tmp_path / 'boundary_routes').routes}
    offenders = sorted(path for path in paths if any(word in path.lower() for word in PUBLIC_SHAPE_WORDS))
    assert offenders == []


def test_reliability_surface_is_exactly_the_five_local_paths(tmp_path):
    """本功能对外只有这五个路径；多一条都得先过法律红线这条关。"""
    app = create_app(tmp_path / 'boundary_surface')
    surface = {}
    for route in app.routes:
        path = getattr(route, 'path', '')
        if 'reliability' in path or '/signals/' in path:
            surface.setdefault(path, set()).update(getattr(route, 'methods', set()))
    assert set(surface) == {'/api/companies/{company}/reliability',
                            '/api/companies/{company}/signals/collect',
                            '/api/companies/{company}/signals/extract',
                            '/api/reliability/sources',
                            '/api/reliability/sources/{source_id}'}
    # 读只有 GET、写只有 POST/PUT：没有删除类动词，也没有对外匿名访问的入口。
    assert surface['/api/companies/{company}/reliability'] == {'GET', 'POST'}
    assert surface['/api/companies/{company}/signals/collect'] == {'POST'}
    assert surface['/api/companies/{company}/signals/extract'] == {'POST'}
    assert surface['/api/reliability/sources'] == {'GET'}
    assert surface['/api/reliability/sources/{source_id}'] == {'PUT'}


# --- 法律红线：主观参考 + 免责声明 + 限制条件，每一份报告都要有 ---

def test_every_report_is_marked_subjective_reference(client, tmp_path):
    add_all_kinds(tmp_path)
    client.post('/api/companies/甲公司/reliability', json=dict())
    client.post('/api/companies/乙公司/reliability', json=dict())  # 零信号的那一份也要有声明
    assert len(stored_reports(tmp_path)) == 2

    responses = [client.post('/api/companies/甲公司/reliability', json=dict()).json(),
                 client.get('/api/companies/甲公司/reliability').json()['report'],
                 client.get('/api/companies/乙公司/reliability').json()['report']] + stored_reports(tmp_path)
    for report in responses:
        assert report['nature'] == 'subjective_reference'
        assert report['disclaimer'] == DISCLAIMER and report['disclaimer'].strip()
        assert report['disclaimer_version'] == DISCLAIMER_VERSION
        assert report['limitations'] and all(line.strip() for line in report['limitations'])
        # 三个版本号跟着分数一起落库，否则事后无法复现当初那句话是怎么写的。
        assert report['rubric_version'] == rubric.RUBRIC_VERSION
        assert report['wording_version'] == wording.WORDING_VERSION


# --- 密钥红线：设置过的原文绝不出现在响应全文里 ---

def test_no_reliability_response_echoes_the_configured_secret(client, tmp_path, monkeypatch):
    from jobscope.reliability.sources import PaidClient

    monkeypatch.setattr(PaidClient, 'fetch', lambda self, company: PAID_PAYLOAD)
    client.post('/api/evidence', json=dict(title='年报摘要', company='甲公司', kind='disclosure',
                                           text='2024 年度报告：参保人数 240 人'))
    calls = [client.put('/api/reliability/sources/qcc', json=dict(mode='enabled', key=SECRET, url=PAID_URL)),
             client.put('/api/reliability/sources/tyc', json=dict(mode='disabled', key=SECRET)),
             client.get('/api/reliability/sources'),
             client.post('/api/companies/甲公司/signals/collect', json=dict(source='qcc')),
             client.post('/api/companies/甲公司/signals/collect', json=dict(source='jobscope_jobs')),
             client.post('/api/companies/甲公司/signals/collect', json=dict(source='gsxt')),
             client.post('/api/companies/甲公司/signals/extract', json=dict()),
             client.post('/api/companies/甲公司/reliability', json=dict()),
             client.get('/api/companies/甲公司/reliability')]
    # 顺带把会被这些端点写进去的读接口也扫一遍：证据正文与任务记录同样是对外文本。
    calls += [client.get('/api/evidence'), client.get('/api/tasks'), client.get('/api/companies')]
    assert all(result.status_code == 200 for result in calls), [(result.status_code, result.text) for result in calls]
    for result in calls:
        assert SECRET not in result.text, result.request.url.path
        assert SECRET.casefold() not in result.text.casefold()
        assert 'Bearer' not in result.text
        assert '"key"' not in result.text
    # 密钥只许待在 signal_sources 这一张表里；证据、信号、报告任何一处落进原文都算留档外泄。
    with Repository(tmp_path).connection() as conn:
        outward = [row[0] for table in ('evidence', 'company_signals', 'company_reliability', 'tasks')
                   for row in conn.execute(f'SELECT payload FROM {table}')]
    assert outward
    assert all(SECRET not in text for text in outward)


# --- 付费默认关闭 ---

def test_paid_sources_start_disabled_and_free_ones_enabled(client, tmp_path):
    items = client.get('/api/reliability/sources').json()['sources']
    assert set(items) == set(SIGNAL_SOURCES)
    paid = [key for key, spec in SIGNAL_SOURCES.items() if spec['tier'] == 'paid']
    free = [key for key, spec in SIGNAL_SOURCES.items() if spec['tier'] == 'free']
    assert paid and set(paid) == set(PAID_SOURCES)
    assert all(items[key]['mode'] == 'disabled' for key in paid)
    assert all(items[key]['mode'] == 'enabled' for key in free)
    assert all(items[key]['key_set'] is False for key in items)
    # 默认值只能由 tier 推导：登记表里写死 mode 就等于偷偷把某个付费来源默认打开。
    assert all('mode' not in spec for spec in SIGNAL_SOURCES.values())
    store = SignalSourceStore(Repository(tmp_path))
    assert all(not store.enabled(key) for key in paid)
    assert all(store.enabled(key) for key in free)
    # 付费来源必须走 http_api + 自备密钥，浏览器采集通路拿不到它们。
    assert all(SIGNAL_SOURCES[key]['transport'] == 'http_api' for key in paid)


# --- 每条依据都有出处；material 类永远不出数字 ---

def test_every_finding_cites_evidence_and_material_never_shows_a_number(client, tmp_path):
    add_all_kinds(tmp_path)
    built = client.post('/api/companies/甲公司/reliability', json=dict())
    assert built.status_code == 200
    reports = [built.json(), client.get('/api/companies/甲公司/reliability').json()['report']] + stored_reports(tmp_path)
    for report in reports:
        items = all_findings(report)
        assert items
        assert all(str(item['evidence_id']).strip() for item in items)
        material = [item for item in items if item['kind'] == 'material']
        assert material, '这份样本必须真的含 material 行，否则下面的断言是空转'
        assert [item['score'] for item in material] == [None] * len(material)
        # 序列化后必须是 null：字段被省略或写成 0 都会被前端读成一个数字。
        assert '"score":null' in json.dumps(report, ensure_ascii=False, separators=(',', ':'))
        for item in items:
            assert set(item) == {'text', 'rule', 'kind', 'score', 'source_class', 'evidence_id'}

    # 逐条 material 规则单独建报告：覆盖率数字外露就是这条红线的事故现场。
    samples = {'salary_quote_coverage': {'known': 9, 'total': 10},
               'disclosure_available': {'count': 4},
               'employee_review_sample': {'samples': 3}}
    materials = {name for name, spec in rubric.RULES.items() if spec.kind == rubric.MATERIAL}
    assert materials <= set(samples), '新增了 material 规则却没给它一个取值样本'
    for rule in sorted(materials):
        company = f'只有材料-{rule}'
        add_signal(tmp_path, company, rule, samples[rule], f'E-material-{rule}'[:24])
        report = ReliabilityService(Repository(tmp_path)).recompute(company)
        assert report['score'] is None
        line = next(item for item in report['dimensions'] if item['dimension'] == rubric.RULES[rule].dimension)
        assert [item['kind'] for item in line['findings']] == ['material']
        assert [item['score'] for item in line['findings']] == [None]
        assert line['score'] is None and line['confidence'] == 0


# --- 分数只能来自 rubric ---

# 计划正文的字面写法是「除 rubric.py 外没有任何模块 import model_transport/get_model_config」。
# 这条按字面实现不了：`extract.py` 的本职就是「证据正文 → 结构化信号」，它必须调用模型。
# 真正要钉住的是「谁都不能拿模型算分」，所以下面把它写成两层：
#   1. 除 extract 外，reliability/ 里任何模块不许导入模型层；
#   2. extract 只能交信号，交不出分数（契约禁字段 + 不许碰措辞 + 见下条 mint 检查）。
MODEL_ACCESS = {'model_transport', 'get_model_config', 'model_configured', 'chat', 'complete', 'stream_turn',
                'tool_turn', 'openai', 'anthropic', 'dashscope'}
MODEL_ALLOWED = {'extract'}


def test_only_the_extractor_may_reach_the_model_layer():
    """谁把评分改成让模型直接打分，这条就先红。"""
    for path in sorted(RELIABILITY_DIR.glob('*.py')):
        module = path.stem
        names = imported_names(module_tree(module))
        offenders = sorted(names & MODEL_ACCESS)
        if module in MODEL_ALLOWED:
            # 唯一被放行去调模型的模块仍然只交信号：它不认识措辞，也不在任何地方调用评分函数。
            assert 'wording' not in names
            assert 'score' not in called_names(module_tree(module))
            continue
        assert offenders == [], f'{module}.py 不该接触模型层，却导入了 {offenders}'


def test_rubric_and_wording_cannot_reach_the_outside_world():
    """算分与措辞两处都是纯函数：没有网络、没有时钟、没有模型、没有文件。"""
    assert imported_modules(module_tree('rubric')) == {'dataclasses', 'models'}
    assert imported_modules(module_tree('wording')) == set()


def test_only_the_rubric_can_mint_a_scored_finding():
    """带分数的依据只由 rubric 构造；别处冒出一个 RuleFinding 就等于多了一条算分通路。"""
    minters = {path.stem for path in RELIABILITY_DIR.glob('*.py') if 'RuleFinding' in called_names(module_tree(path.stem))}
    assert minters == {'rubric'}, 'RuleFinding 只能由 rubric 构造'


def test_the_extractor_contract_has_no_room_for_a_verdict():
    from pydantic import ValidationError

    from jobscope.reliability import extract
    for model in (extract.RawSignal, extract.ExtractInput):
        assert model.model_config.get('extra') == 'forbid'
        assert not ({'score', 'grade', 'rating', 'verdict'} & set(model.model_fields))
    with pytest.raises(ValidationError):
        extract.ExtractInput.model_validate({'signals': [{'rule': 'company_age', 'dimension': 'stability',
                                                          'value': {'years': 8}, 'evidence_id': 'E-1', 'score': 90}]})
    with pytest.raises(ValidationError):
        extract.ExtractInput.model_validate({'signals': [], 'overall_rating': 'bad'})
    prompt = extract.prompt_text()
    for word in ('score', 'grade', 'rating', 'verdict'):
        assert word in prompt
    assert '分数由本项目的固定规则计算' in prompt
    # 抽取器不许碰措辞，也不许调用评分函数。
    names = imported_names(module_tree('extract'))
    assert 'wording' not in names and 'score' not in names


def test_scores_are_reproducible_with_the_model_layer_refusing_to_answer(tmp_path, monkeypatch):
    from jobscope import chat, model_transport, settings

    def boom(*args, **kwargs):
        raise AssertionError('评分路径不该调用模型')

    for target, attribute in ((model_transport, 'complete'), (model_transport, 'stream_turn'),
                              (model_transport, 'tool_turn'), (settings, 'get_model_config'),
                              (chat, 'model_configured')):
        monkeypatch.setattr(target, attribute, boom)

    add_all_kinds(tmp_path)
    report = ReliabilityService(Repository(tmp_path)).recompute('甲公司')
    identity = Repository(tmp_path).resolve_company('甲公司')
    stored = Repository(tmp_path).signals(identity['id'],
                                          ttl_days={name: spec.ttl_days for name, spec in rubric.RULES.items()})
    parsed = [SignalInput(**{key: value for key, value in item.items() if key in SignalInput.model_fields})
              for item in stored]
    expected = rubric.score(parsed, stale_rules={item['rule'] for item in stored if item['stale']})
    assert report['score'] == expected.score
    assert expected.score is not None, '样本里该有能算分的依据，否则这条复现断言是空转'
    assert [line['score'] for line in report['dimensions']] == [item.score for item in expected.dimensions]
    assert [line['confidence'] for line in report['dimensions']] == [item.confidence for item in expected.dimensions]


# --- 禁用词表对所有在册规则生效 ---

def test_banned_words_are_unreachable_from_every_registered_template():
    values = (0, 1, 2, 3, 7, 10, 25, 45, 55, 60, 75, 100, 240, 312, 999999, 0.0, 12.5, -1, 1e9)
    for rule, spec in rubric.RULES.items():
        template = wording.TEMPLATES[spec.template]
        fields = [name for _, name, _, _ in string.Formatter().parse(template) if name]
        assert fields, f'{rule} 的模板没有可替换字段，取值口径不可能成立'
        for value in values:
            assert wording.lint(template.format(**dict.fromkeys(fields, value))) == [], (rule, value)
        assert wording.lint(template.format(**{name: values[index % len(values)]
                                               for index, name in enumerate(fields)})) == []
        for source_class in ('official', 'platform', 'employee', 'media', 'user_material', 'paid_api'):
            finding = RuleFinding(rule=rule, dimension=spec.dimension, score=50.0, template=spec.template,
                                  data=dict.fromkeys(fields, 240), evidence_id='E-1',
                                  source_class=source_class, weight=1.0)
            assert wording.lint(wording.render(finding)) == []


def test_banned_list_covers_the_words_the_plan_named_and_lint_is_not_vacuous():
    for word in ('欠薪', '裁员', '高风险', '骗子', '跑路'):
        assert word in wording.BANNED
    assert wording.lint('这家公司长期欠薪，已经跑路') == ['欠薪', '跑路']
    assert wording.lint('参保人数由 312 人变为 240 人') == []


def test_every_outward_sentence_of_a_real_report_lints_clean(client, tmp_path):
    add_all_kinds(tmp_path)
    report = client.post('/api/companies/甲公司/reliability', json=dict()).json()
    texts = [report['summary'], report['disclaimer'], *report['limitations']]
    for line in report['dimensions']:
        texts += [line['label'], *[item['text'] for item in line['findings']], *line['missing']]
    for text in texts:
        assert wording.lint(str(text)) == []
