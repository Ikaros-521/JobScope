"""AI 行业判断的机制测试：模型只判断，分数折算与护栏全在代码里。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest

from jobscope.reliability import llm_industry


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv('JOBSCOPE_LLM_URL', 'https://example.com/v1/chat/completions')
    monkeypatch.setenv('JOBSCOPE_LLM_MODEL', 'test')


def reply(monkeypatch, content):
    def post(self, url, **kwargs):
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]},
                              request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.Client, 'post', post)


def judgment(trend='上行', summary='通信设备行业需求保持增长。', basis='依据已存材料中的经营范围。'):
    return json.dumps({'industry': '通信设备', 'trend': trend, 'summary': summary, 'basis': basis}, ensure_ascii=False)


MATERIALS = [dict(title='企业信息快照', kind='web_company_profile', text='经营范围：通信设备制造。')]


@pytest.mark.parametrize('trend,direction', [('上行', 1), ('平稳', 0), ('下行', -1)])
def test_three_trends_map_to_direction_and_one_signal(monkeypatch, trend, direction):
    reply(monkeypatch, judgment(trend=trend))
    outcome = llm_industry.collect('华为技术有限公司', MATERIALS)
    assert outcome['status'] == 'completed'
    [signal] = outcome['signals']
    assert signal['rule'] == 'industry_trend_llm' and signal['value'] == {'direction': direction}
    assert signal['source_class'] == 'llm' and signal['dimension'] == 'industry'
    # 判断全文进证据正文：行业、趋势、说明、依据、模型声明一段可溯源。
    assert trend in signal['text'] and '通信设备' in signal['text'] and '模型判断，非实时数据' in signal['text']


def test_without_a_model_the_run_is_skipped_not_failed(monkeypatch):
    monkeypatch.delenv('JOBSCOPE_LLM_URL', raising=False)
    outcome = llm_industry.collect('甲公司', MATERIALS)
    assert outcome['status'] == 'skipped' and outcome['signals'] == []


def test_transport_errors_return_failed_without_raising(monkeypatch):
    def boom(self, url, **kwargs):
        raise httpx.ConnectError('refused')
    monkeypatch.setattr(httpx.Client, 'post', boom)
    outcome = llm_industry.collect('甲公司', MATERIALS)
    assert outcome['status'] == 'failed' and outcome['signals'] == []


def test_unparseable_output_is_degraded_with_raw_kept(monkeypatch):
    reply(monkeypatch, '这不是 JSON，只是聊天。')
    outcome = llm_industry.collect('甲公司', MATERIALS)
    assert outcome['status'] == 'degraded' and outcome['signals'] == []
    assert '这不是 JSON' in outcome['raw']


def test_unknown_trend_word_is_rejected(monkeypatch):
    reply(monkeypatch, judgment(trend='暴涨'))
    outcome = llm_industry.collect('甲公司', MATERIALS)
    assert outcome['status'] == 'degraded' and outcome['signals'] == []


def test_banned_word_in_the_summary_voids_the_whole_judgement(monkeypatch):
    """判断文字命中禁用词＝对商誉下断言：整条作废，绝不截掉词以后照用。"""
    reply(monkeypatch, judgment(summary='该行业正在大规模裁员。'))
    outcome = llm_industry.collect('甲公司', MATERIALS)
    assert outcome['status'] == 'degraded' and outcome['signals'] == []
    assert '禁用词' in outcome['message']


def test_fenced_json_still_parses(monkeypatch):
    reply(monkeypatch, '```json\n' + judgment() + '\n```')
    outcome = llm_industry.collect('甲公司', MATERIALS)
    assert outcome['status'] == 'completed' and outcome['signals'][0]['value'] == {'direction': 1}


def test_the_prompt_forbids_scores_and_injection():
    assert '不得改成对某家公司评分' in llm_industry.SYSTEM
    assert '不要输出分数' in llm_industry.SYSTEM
    assert '上行' in llm_industry.SYSTEM and '平稳' in llm_industry.SYSTEM and '下行' in llm_industry.SYSTEM
