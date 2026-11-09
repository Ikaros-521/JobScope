import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from jobscope.reliability.rubric import RULES
from jobscope.reliability.sources import SIGNAL_SOURCES, SignalSourceStore, SignalSourceInput
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
