"""生存跑道：月支出/存款/社保/失业金 → 推导值。

重点两件事：推导口径正确（跑道月数、断缴整月数、失业金提示），以及隐私硬底线——
跑道数据绝不泄漏到任何模型素材路径或其他页面响应里（黑盒泄漏测试：标记值全站搜不到）。
"""
import sys
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.main import create_app
from jobscope.repository import Repository

import pytest

MARKER = '机密标记XYZ'


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBSCOPE_LLM_URL", raising=False)
    with TestClient(create_app(tmp_path)) as client:
        yield client


def test_empty_state(client):
    data = client.get('/api/runway').json()
    assert data['profile'] is None and data['derived'] is None


def test_roundtrip_and_derived(client):
    response = client.put('/api/runway', json=dict(
        monthly_expense=8000, savings=100000, social_security_last_month='2026-08',
        unemployment_claim='not_claimed', notes='房租年底到期'))
    assert response.status_code == 200
    payload = response.json()
    assert payload['profile']['savings'] == 100000 and payload['profile']['updated_at']
    derived = payload['derived']
    assert derived['runway_months'] == 12.5          # 100000 / 8000
    today = date.today()
    assert derived['paused_months'] == (today.year - 2026) * 12 + (today.month - 8)  # 整月数
    assert '尚未申领' in derived['claim_hint'] and '以当地人社部门口径为准' in derived['claim_hint']
    assert '医保' in derived['medical_hint']
    # GET 读回一致
    again = client.get('/api/runway').json()
    assert again['profile']['notes'] == '房租年底到期' and again['derived'] == derived


def test_derived_edge_cases(client):
    # 只填存款不填支出 → 跑道算不出（None），断缴同月算 0，claim 无提示
    payload = client.put('/api/runway', json=dict(
        savings=50000, social_security_last_month=f'{date.today().year}-{date.today().month:02d}',
        unemployment_claim='claimed')).json()
    assert payload['derived']['runway_months'] is None
    assert payload['derived']['paused_months'] == 0
    assert '认证' in payload['derived']['claim_hint'] and payload['derived']['medical_hint'] == ''
    # 不符合条件不再唠叨；未来的缴纳月按 0 处理
    payload = client.put('/api/runway', json=dict(
        unemployment_claim='ineligible', social_security_last_month='2099-01')).json()
    assert payload['derived']['claim_hint'] == ''
    assert payload['derived']['paused_months'] == 0


def test_input_validation(client):
    assert client.put('/api/runway', json=dict(monthly_expense=-1)).status_code == 422
    assert client.put('/api/runway', json=dict(social_security_last_month='2026-13')).status_code == 422
    assert client.put('/api/runway', json=dict(unemployment_claim='随便')).status_code == 422


def test_put_replaces_whole_profile(client):
    client.put('/api/runway', json=dict(monthly_expense=8000, savings=100000, notes='A'))
    payload = client.put('/api/runway', json=dict(monthly_expense=6000)).json()
    # PUT 是整档替换：没带的字段清空——跑道页语义就是「保存当前页的值」
    assert payload['profile']['savings'] is None and payload['profile']['notes'] == ''
    assert payload['profile']['monthly_expense'] == 6000


def test_privacy_no_leak_into_model_paths_or_other_pages(client):
    """黑盒泄漏测试：跑道里的标记值与金额，不得出现在任何模型素材路径或其他页面响应里。"""
    client.put('/api/runway', json=dict(
        monthly_expense=8000, savings=123456, unemployment_claim='not_claimed', notes=MARKER))
    # 模型素材池入口：资料库条目、偏好事实池、空窗叙事素材
    entries_text = str(client.get('/api/profile-entries').json())
    assert MARKER not in entries_text and '123456' not in entries_text
    repo = Repository(client.app.state.repo.data_dir)
    assert MARKER not in str(repo.preference_statements())
    from jobscope.gap_narrative import entries_block
    assert MARKER not in entries_block(repo)
    # 其他页面响应：仪表盘/管线回顾/漏斗/处方 全站扫一遍
    for path in ('/api/dashboard', '/api/jobs/pipeline/review', '/api/jobs/pipeline/prescriptions'):
        assert MARKER not in str(client.get(path).json()), path
        assert '123456' not in str(client.get(path).json()), path
