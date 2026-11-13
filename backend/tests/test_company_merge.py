import json
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope import company_merge
from jobscope.main import create_app
from jobscope.repository import Repository


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv('JOBSCOPE_LLM_URL', raising=False)
    with TestClient(create_app(tmp_path)) as value:
        yield value


def save_job(repo, company, index, salary='15-20K'):
    repo.save_job(dict(company=company, title=f'后端工程师{index}', location='杭州', salary=salary,
                       description_status='listing', description='负责服务端开发 经验：3-5年 学历：本科',
                       source='boss', url=f'https://jobs.example.com/m{index}'), 'boss')


def test_merge_companies_unions_aliases_and_migrates_children(tmp_path):
    """合并 = 别名并集 + signals/报告迁移 + 删被并方实体行。"""
    repo = Repository(tmp_path)
    save_job(repo, '甲公司', 1)
    save_job(repo, '甲公司', 2)
    repo.resolve_company('甲公司', aliases=['甲旧称'])
    save_job(repo, '乙公司', 3)
    save_job(repo, '乙别称', 4)
    repo.resolve_company('乙公司', aliases=['乙别称'])
    a = repo.find_company('甲公司')
    b = repo.find_company('乙公司')
    # 信号 id 与 identity 都是全表唯一，双方不可能持有同键行；INSERT OR IGNORE 只是并发时序的保险。
    # 这里验证正常迁移：B 名下的信号与报告搬家到 A，一条不丢。
    with repo.connection() as conn:
        def add_signal(row_id, company_id, identity, owner):
            conn.execute('''INSERT INTO company_signals
                (id, company_id, identity, rule, dimension, source_class, evidence_id, observed_at, payload, created_at)
                VALUES (?, ?, ?, 'salary_quote_coverage', 'pay', 'platform', 'E-1', '', ?, '2026-11-13T00:00:00Z')''',
                (row_id, company_id, identity, json.dumps({'owner': owner})))
        add_signal('S-keep', a['id'], 'I1', '甲')
        add_signal('S-move', b['id'], 'I2', '乙')
        add_signal('S-move2', b['id'], 'I3', '乙2')
        conn.execute('INSERT INTO company_reliability VALUES (?, ?, ?, ?)',
                     (b['id'], 'HASH-1', '2026-11-13T00:00:00Z', json.dumps({'score': 50})))

    merged = repo.merge_companies(a['id'], b['id'])
    assert merged['aliases'] == sorted(['乙公司', '乙别称', '甲旧称'])
    assert repo.find_company('乙公司') is None, '被并方实体行应删除'
    with repo.connection() as conn:
        rows = conn.execute("SELECT id, json_extract(payload,'$.owner') FROM company_signals WHERE company_id=? "
                            'ORDER BY id', (a['id'],)).fetchall()
        assert [tuple(row) for row in rows] == [('S-keep', '甲'), ('S-move', '乙'), ('S-move2', '乙2')], '被并方信号一条不丢地搬家'
        reports = conn.execute('SELECT count(*) FROM company_reliability WHERE company_id=?', (a['id'],)).fetchone()[0]
        assert reports == 1, '被并方报告迁移到主名'
        assert conn.execute('SELECT count(*) FROM company_signals WHERE company_id=?', (b['id'],)).fetchone()[0] == 0
    # 实体口径的岗位数在合并后立即聚合（别名名下的岗位计入主名）：甲 2 条 + 乙系 2 条。
    entities = {item['name']: item for item in repo.list_merge_entities()}
    assert entities['甲公司']['jobs_count'] == 4
    with pytest.raises(ValueError):
        repo.merge_companies(a['id'], a['id'])


def test_split_alias_rebuilds_independent_entity(tmp_path):
    repo = Repository(tmp_path)
    save_job(repo, '甲公司', 1)
    save_job(repo, '甲别称', 2)
    repo.resolve_company('甲公司', aliases=['甲别称'])
    host = repo.find_company('甲公司')
    reborn = repo.split_alias(host['id'], '甲别称')
    assert reborn['name'] == '甲别称' and reborn['aliases'] == []
    host_after = repo.find_company('甲公司')
    assert '甲别称' not in host_after['aliases']
    with pytest.raises(ValueError):
        repo.split_alias(host['id'], '不存在的别名')


def test_merge_proposals_lifecycle_and_dedup(tmp_path):
    repo = Repository(tmp_path)
    assert repo.add_merge_proposal('甲公司', '乙公司', 'rule:form 测试') is not None
    assert repo.add_merge_proposal('乙公司', '甲公司', '反方向同对') is None, '任一方向已有 pending 则跳过'
    assert repo.add_merge_proposal('甲公司', '甲公司', '自合并') is None
    proposal = repo.list_merge_proposals()[0]
    accepted = repo.resolve_merge_proposal(proposal['id'], 'accepted')
    assert accepted['status'] == 'accepted' and accepted['resolved_at']
    with pytest.raises(ValueError):
        repo.resolve_merge_proposal(proposal['id'], 'dismissed')  # 已处理的建议不能二次流转
    # accepted 之后同对不再重提；dismissed 的另一对也不再进候选。
    assert repo.add_merge_proposal('甲公司', '乙公司', '再来') is None
    other = repo.add_merge_proposal('丙公司', '丁公司', 'rule:substring')
    dismissed = repo.resolve_merge_proposal(other['id'], 'dismissed')
    assert dismissed['status'] == 'dismissed'
    assert repo.add_merge_proposal('丙公司', '丁公司', '重提') is None


def test_rule_candidates_form_and_substring():
    entities = [
        dict(id='1', name='北京字节跳动科技有限公司', jobs_count=5),
        dict(id='2', name='字节跳动科技', jobs_count=2),
        dict(id='3', name='蚂蚁科技集团股份有限公司', jobs_count=1),
        dict(id='4', name='杭州蚂蚁科技有限公司', jobs_count=4),
        dict(id='5', name='阿里云计算有限公司', jobs_count=3),
        dict(id='6', name='阿里', jobs_count=1),
        dict(id='7', name='无关公司', jobs_count=9),
    ]
    pairs = company_merge.candidates(entities)
    keys = {(pair['a'], pair['b']) for pair in pairs}
    assert ('北京字节跳动科技有限公司', '字节跳动科技') in keys, '城市前缀+后缀差异 → form 候选'
    assert any('蚂蚁' in a and '蚂蚁' in b for a, b in keys), '后缀叠加+城市前缀差异 → form 候选'
    assert not any(a == '阿里' or b == '阿里' for a, b in keys), '字号 <3 字不做子串候选（宁漏勿错）'
    assert not any('无关公司' in key for key in keys)
    note = next(pair['note'] for pair in pairs if pair['rule'] == 'form')
    assert note


def test_scan_auto_merges_same_and_quarantines_subsidiary(tmp_path, monkeypatch):
    """LLM 判定 same+高置信 → 自动合并（岗位多者为主名）；subsidiary → 进待审隔离。"""
    repo = Repository(tmp_path)
    for index in range(3):
        save_job(repo, '北京字节跳动科技有限公司', index)
    save_job(repo, '字节跳动科技', 9)
    save_job(repo, '某某控股集团', 10)
    save_job(repo, '某某控股集团杭州分公司', 11)

    calls = {}

    def fake_complete(config, system, user, **kwargs):
        calls['payload'] = json.loads(user)
        verdicts = []
        for index, pair in enumerate(calls['payload']):
            names = (pair['a'], pair['b'])
            verdict = ('same', 0.95, '全称与简称') if '字节' in names[0] + names[1] else \
                      ('subsidiary', 0.99, '分公司主体需隔离')
            verdicts.append(dict(index=index, **{'verdict': verdict[0], 'confidence': verdict[1], 'reason': verdict[2]}))
        return json.dumps({'verdicts': verdicts}, ensure_ascii=False), {}

    monkeypatch.setattr('jobscope.settings.purpose_configured', lambda purpose: True)
    monkeypatch.setattr('jobscope.settings.get_purpose_config', lambda purpose: {'url': 'x', 'model': 'm'})
    monkeypatch.setattr('jobscope.model_transport.complete', fake_complete)

    summary = company_merge.scan(repo)
    assert summary['merged'] == 1 and summary['proposed'] >= 1
    # 岗位多者（全称 3 条）做主名：简称实体消失、成为全称的别名。
    assert repo.find_company('字节跳动科技') is None
    survivor = repo.find_company('北京字节跳动科技有限公司')
    assert survivor and '字节跳动科技' in survivor['aliases']
    pending = [item for item in repo.list_merge_proposals() if item['status'] == 'pending']
    assert any('分公司' in item['company_a'] + item['company_b'] for item in pending), '子公司/分公司判定进待审，绝不自动合并'
    assert all('llm:' in item['reason'] for item in pending)


def test_scan_without_model_falls_back_to_rule_proposals(tmp_path, monkeypatch):
    """模型未配置：候选全部转待审（reason 标规则来源），扫描不失败。"""
    monkeypatch.setattr('jobscope.settings.purpose_configured', lambda purpose: False)
    repo = Repository(tmp_path)
    save_job(repo, '北京字节跳动科技有限公司', 1)
    save_job(repo, '字节跳动科技', 2)
    summary = company_merge.scan(repo)
    assert summary['merged'] == 0 and summary['proposed'] == 1
    proposal = repo.list_merge_proposals()[0]
    assert proposal['reason'].startswith('rule:')


def test_merge_api_manual_scan_accept_split(client, tmp_path):
    repo = Repository(tmp_path)
    for index in range(2):
        save_job(repo, '甲公司', index)
    save_job(repo, '乙公司', 7)
    # 两对可成候选的公司：丙（后缀/城市前缀差异）与丁（子串），扫描后进待审。
    save_job(repo, '丙科技', 20)
    save_job(repo, '杭州丙科技有限公司', 21)
    save_job(repo, '某某科技', 22)
    save_job(repo, '北京某某科技有限公司', 23)

    # 手动合并：甲吸收乙，报告重算不炸，overview 立即反映。
    merged = client.post('/api/companies/merge', json={'company_a': '甲公司', 'company_b': '乙公司'})
    assert merged.status_code == 200, merged.text
    assert merged.json()['company']['name'] == '甲公司'
    assert any(item['name'] == '乙公司' for item in merged.json()['entities']) is False
    # 合并不存在的主体 → 422。
    assert client.post('/api/companies/merge', json={'company_a': '甲公司', 'company_b': '没有这家'}).status_code == 422

    # 拆分：把别名拆回独立实体。
    host = repo.find_company('甲公司')
    split = client.post(f'/api/companies/{host["id"]}/split', json={'alias': '乙公司'})
    assert split.status_code == 200 and split.json()['company']['name'] == '乙公司'
    assert '乙公司' not in repo.find_company('甲公司')['aliases']

    # 扫描端点：模型未配置（默认 fixture 环境），后台线程跑完规则候选进待审。
    scan = client.post('/api/companies/merge/scan')
    assert scan.status_code == 200 and scan.json()['task']['id']
    task_id = scan.json()['task']['id']
    for _ in range(50):
        task = client.get(f'/api/tasks/{task_id}').json()['task']
        if task['status'] not in ('running', 'pending'):
            break
        time.sleep(0.2)
    assert task['status'] == 'completed', task['message']

    # 待审建议：接受（auto 方向按岗位数取主名）与忽略。
    proposals = client.get('/api/companies/merge/overview').json()['proposals']
    pending = [item for item in proposals if item['status'] == 'pending']
    assert pending
    accepted = client.post(f'/api/companies/merge/proposals/{pending[0]["id"]}/accept', json={'primary': 'auto'})
    assert accepted.status_code == 200, accepted.text
    again = client.post(f'/api/companies/merge/proposals/{pending[0]["id"]}/accept', json={'primary': 'auto'})
    assert again.status_code == 422, '已处理的建议不能重复接受'
    if len(pending) > 1:
        dismissed = client.post(f'/api/companies/merge/proposals/{pending[1]["id"]}/dismiss')
        assert dismissed.status_code == 200


def test_compare_layers_count_alias_jobs_after_merge(client, tmp_path):
    """compare 分层模式按实体口径（名称+别名）聚岗位：归并后别名名下的岗位立即计入分层。"""
    repo = Repository(tmp_path)
    save_job(repo, '甲公司', 1)
    save_job(repo, '甲公司', 2)
    save_job(repo, '甲别称', 3)
    repo.resolve_company('甲公司', aliases=['甲别称'])
    rows = client.get('/api/reliability/compare?companies=甲公司').json()['companies']
    assert rows[0]['total_jobs'] == 3, '别名名下的岗位计入分层概要'
    # 未登记实体的名字退回单名匹配（不 500）。
    unknown = client.get('/api/reliability/compare?companies=没登记过的公司').json()['companies']
    assert unknown[0]['total_jobs'] == 0
