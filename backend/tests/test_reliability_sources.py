import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from jobscope.reliability import paid
from jobscope.reliability.rubric import RULES
from jobscope.reliability.sources import PAID_SOURCES, SIGNAL_SOURCES, SignalSourceStore, SignalSourceInput
from jobscope.repository import Repository


@pytest.fixture
def store(tmp_path):
    return SignalSourceStore(Repository(tmp_path))


def _walk(node):
    """每一个 (key, value) 对，嵌套层照样展开：泄漏往往藏在下一层里。"""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _walk(value)


def test_paid_sources_start_disabled_and_free_ones_enabled(store):
    public = store.public()
    assert public['gsxt']['tier'] == 'free' and public['gsxt']['mode'] == 'enabled'
    assert public['qcc']['tier'] == 'paid' and public['qcc']['mode'] == 'disabled'


def test_keys_are_never_returned(store):
    store.save('qcc', SignalSourceInput(mode='enabled', key='secret-token-value'))
    public = store.public()['qcc']
    assert public['key_set'] is True and 'secret' not in str(public)


def test_enabling_a_paid_source_without_a_key_is_rejected(store):
    with pytest.raises(ValueError):
        store.save('qcc', SignalSourceInput(mode='enabled', key=''))


def test_switching_endpoint_requires_a_fresh_key(store):
    store.save('qcc', SignalSourceInput(mode='enabled', key='first', url='https://a.example/qcc'))
    with pytest.raises(ValueError):
        store.save('qcc', SignalSourceInput(mode='enabled', key='', url='https://b.example/qcc'))


def test_disabled_source_never_provides_a_client(store):
    store.save('qcc', SignalSourceInput(mode='enabled', key='first'))
    store.save('qcc', SignalSourceInput(mode='disabled'))
    assert store.client('qcc') is None


def test_free_sources_have_no_client_requirement(store):
    assert store.client('gsxt') is None and store.enabled('gsxt') is True


def test_public_leaks_neither_a_key_field_nor_the_secret_value(store):
    secret = 'sk-qcc-8f2a6b'
    store.save('qcc', SignalSourceInput(mode='enabled', key=secret, url='https://api.qcc.example/v1'))
    public = store.public()
    assert [key for key, _ in _walk(public) if key == 'key'] == []
    assert all(secret not in str(value) for _, value in _walk(public))
    assert json.dumps(public, ensure_ascii=False).count('key_set') == len(SIGNAL_SOURCES)
    assert public['qcc']['key_set'] is True and public['qcc']['url'] == 'https://api.qcc.example/v1'


def test_the_paid_client_disappears_the_moment_the_source_is_disabled(store):
    store.save('qcc', SignalSourceInput(mode='enabled', key='first', url='https://api.qcc.example/v1'))
    assert store.client('qcc') is not None and store.enabled('qcc') is True
    store.save('qcc', SignalSourceInput(mode='disabled'))
    assert store.client('qcc') is None and store.enabled('qcc') is False


def test_a_free_source_can_be_switched_off_and_back_on(store):
    assert store.enabled('gsxt') is True
    store.save('gsxt', SignalSourceInput(mode='disabled'))
    assert store.enabled('gsxt') is False and store.public()['gsxt']['mode'] == 'disabled'
    store.save('gsxt', SignalSourceInput(mode='enabled'))
    assert store.enabled('gsxt') is True


def test_every_registered_source_points_at_a_real_rubric_rule():
    for source_id, spec in SIGNAL_SOURCES.items():
        assert spec['tier'] in {'free', 'paid'}, source_id
        assert spec['transport'] in {'browser_page', 'http_api', 'local'}, source_id
        assert set(spec['rules']) <= set(RULES), source_id


def test_every_paid_source_declares_a_field_mapping_for_every_rule():
    """付费来源必须自带 fields：没有声明表就没有取数通路，也不许在代码里写死厂商 schema。"""
    for source_id, spec in SIGNAL_SOURCES.items():
        if spec['tier'] != 'paid':
            assert 'fields' not in spec, source_id
            continue
        assert set(spec['fields']) == set(spec['rules']), source_id
        for rule, recipes in spec['fields'].items():
            assert recipes and all(recipe for recipe in recipes), rule
            for slots in recipes:
                for name, candidates in slots.items():
                    assert candidates and all(str(item).strip() for item in candidates), (source_id, rule, name)
                    assert len(set(candidates)) == len(candidates), (source_id, rule, name)


def test_both_paid_sources_keep_their_own_copy_of_the_mapping():
    """两家随时会被用户按各自文档改成不一样，所以表必须各存一份而不是共享同一个对象。"""
    first, second = (SIGNAL_SOURCES[item]['fields'] for item in PAID_SOURCES)
    assert first.keys() == second.keys()
    assert first is not second


def test_the_paid_mapping_only_uses_slots_the_reader_understands():
    """槽位名是 paid.py 与映射表之间唯一的契约：写错槽位必须在测试里红，而不是静默不取数。"""
    for source_id in PAID_SOURCES:
        for rule, recipes in SIGNAL_SOURCES[source_id]['fields'].items():
            for slots in recipes:
                assert slots and set(slots) <= paid.SLOTS, (source_id, rule, sorted(slots))


def test_public_listing_shows_the_mapping_so_the_user_can_correct_it(store):
    secret = 'sk-qcc-9d1c'
    store.save('qcc', SignalSourceInput(mode='enabled', key=secret, url='https://api.qcc.example/v1'))
    spec = store.public()['qcc']
    assert 'establish_date' in json.dumps(spec['fields'], ensure_ascii=False)
    assert secret not in json.dumps(spec, ensure_ascii=False)


def test_implementation_and_readiness_are_separate_from_enabled_settings(store):
    public = store.public()
    for source in ('credit_china', 'court_records'):
        assert public[source]['implemented'] is False
        assert public[source]['status'] == 'unsupported'
        assert public[source]['mode'] == 'disabled'
        assert not public[source]['ready'] and public[source]['unavailable_reason']
        with pytest.raises(ValueError, match='尚未实现'):
            store.save(source, SignalSourceInput(mode='enabled'))
    assert public['gsxt']['implemented'] and public['gsxt']['ready']
    assert public['gsxt']['status'] == 'needs_verification'
    assert '待实测' in public['gsxt']['description']
    assert public['jobscope_jobs']['ready']
    saved = store.save('qcc', SignalSourceInput(mode='enabled', key='vendor-key'))
    assert saved['mode'] == 'enabled' and not saved['ready']
    assert '接口地址' in saved['unavailable_reason']
    assert store.client('qcc') is None
    assert store.save('qcc', SignalSourceInput(mode='enabled', url='http://localhost:9000/company',
                                             key='vendor-key'))['ready']


@pytest.mark.parametrize('url', [
    'ftp://api.example/x', 'https:///company', 'https://user:pass@api.example/x',
    'https://api.example/x?api_key=secret', 'https://api.example/x?access_token=secret',
    'https://api.example/x?signature=secret', 'https://api.example/x#secret',
    'http://localhost:invalid/x', 'https://api.example/a b',
])
def test_unsafe_urls_are_rejected_without_persisting(store, url):
    with pytest.raises(ValueError):
        store.save('qcc', SignalSourceInput(mode='enabled', key='vendor-key', url=url))
    assert store.stored('qcc') == {}


def test_advanced_settings_preserve_omitted_values_and_validate_complete_slot_sets(store):
    fields = json.loads(json.dumps(SIGNAL_SOURCES['qcc']['fields']))
    fields['company_age'][0]['founded_date'] = ['incorporated_on']
    saved = store.save('qcc', SignalSourceInput(mode='enabled', key='vendor-key',
                       url='http://127.0.0.1:9010/company', auth_header='X-Api-Key', auth_prefix='',
                       company_param='keyword', fields=fields))
    assert saved['fields'] == fields and saved['auth_prefix'] == ''
    assert saved['auth_header'] == 'X-Api-Key' and saved['company_param'] == 'keyword'
    saved = store.save('qcc', SignalSourceInput(mode='disabled'))
    assert saved['fields'] == fields and saved['auth_prefix'] == ''
    assert saved['url'] == 'http://127.0.0.1:9010/company' and saved['key_set']
    fields['insured_headcount_trend'][0].pop('insured_previous')
    with pytest.raises(ValueError, match='完整槽位'):
        store.save('qcc', SignalSourceInput(mode='enabled', fields=fields))
    assert store.stored('qcc')['mode'] == 'disabled'


@pytest.mark.parametrize('advanced', [
    {'auth_header': 'X-Key\r\nInjected'}, {'auth_header': 'Host'},
    {'auth_prefix': 'Bearer\n'}, {'company_param': 'company&token'},
    {'fields': {'company_age': [{'founded_date': []}]}},
])
def test_invalid_advanced_configuration_is_rejected(store, advanced):
    with pytest.raises(ValueError):
        store.save('qcc', SignalSourceInput(mode='enabled', key='vendor-key',
                   url='https://api.example/company', **advanced))
    assert store.stored('qcc') == {}


def test_last_run_is_signal_task_only_and_not_limited_by_recent_task_page(store):
    assert store.public()['qcc']['last_run'] is None
    signal = store.repo.create_task('signal_qcc', '甲公司')
    signal.update(status='partial', message='字段未全部匹配', count=2, updated_at='2026-11-13T12:00:00Z')
    store.repo.update_task(signal)
    for _ in range(105):
        store.repo.create_task('qcc', '公开网页查询')
    assert store.public()['qcc']['last_run'] == signal


def test_key_cannot_be_smuggled_into_public_advanced_configuration(store):
    secret = 'vendor-key-secret'
    with pytest.raises(ValueError, match='密钥'):
        store.save('qcc', SignalSourceInput(mode='enabled', key=secret,
                   url='https://api.example/company', auth_prefix=secret))
    assert secret not in json.dumps(store.public())

