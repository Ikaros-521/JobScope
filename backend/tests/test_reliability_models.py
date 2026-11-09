import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
