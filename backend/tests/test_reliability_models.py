import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from jobscope.repository import Repository


def test_company_identity_is_stable_and_alias_resolved(tmp_path):
    repo = Repository(tmp_path)
    first = repo.resolve_company('北京字节跳动科技有限公司', credit_code='91110000MA01ABCD7X')
    again = repo.resolve_company('字节跳动', credit_code='91110000MA01ABCD7X')
    assert first['id'] == again['id']
    assert first['name'] == '北京字节跳动科技有限公司'
    # 别名在第二次调用时才产生，first 是首次调用的返回值，只能断言 again 里的持久化状态。
    assert set(again['aliases']) == {'字节跳动'}


def test_fullwidth_and_space_noise_normalise_to_the_same_company(tmp_path):
    repo = Repository(tmp_path)
    a = repo.resolve_company('  明日    之星 ')
    b = repo.resolve_company('明日之星')
    assert a['id'] == b['id']


def test_similar_names_stay_separate_rather_than_fuzzy_merged(tmp_path):
    repo = Repository(tmp_path)
    a = repo.resolve_company('杭州明日之星科技有限公司')
    b = repo.resolve_company('杭州明日之星电子商务有限责任公司')
    assert a['id'] != b['id']


def test_aliases_are_stored_normalised(tmp_path):
    repo = Repository(tmp_path)
    company = repo.resolve_company('甲公司', aliases=['  甲 集团 '])
    assert company['aliases'] == ['甲集团']


def test_spaces_are_noise_for_latin_names_too(tmp_path):
    # 删空白这个决定对中英文一致生效：Remotive 的海外公司名同样带多余空格。
    repo = Repository(tmp_path)
    assert repo.resolve_company('Automattic  Inc')['id'] == repo.resolve_company('Automattic Inc')['id']


def test_signal_requires_evidence_and_rejects_unlisted_dimension():
    from pydantic import ValidationError
    from jobscope.reliability.models import SignalInput
    base = dict(company_id='CO-x', dimension='stability', rule='insured_headcount_trend',
                value={'before': 312, 'after': 240}, unit='人', source_class='official',
                evidence_id='E-1', observed_at='2025-06-30', collected_at='2026-11-09T00:00:00+00:00')
    assert SignalInput(**base).dimension == 'stability'
    for field, bad in (('dimension', 'vibes'), ('source_class', 'rumour')):
        with pytest.raises(ValidationError):
            SignalInput(**{**base, field: bad})
    with pytest.raises(ValidationError):
        SignalInput(**{**base, 'evidence_id': ''})


def test_employee_signals_cannot_stand_alone_as_negative():
    from jobscope.reliability.models import can_stand_alone
    assert can_stand_alone('employee') is False
    assert can_stand_alone('official') is True
