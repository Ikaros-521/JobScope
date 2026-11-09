"""Signal sources. Paid tiers stay off until the user opts in; keys never echo back."""
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..repository import now

SIGNAL_SOURCES = {
    'gsxt': dict(name='国家企业信用信息公示系统', tier='free', transport='browser_page',
                 status='needs_verification', rules=['company_age', 'insured_headcount_trend'],
                 description='登记信息、年报参保人数、行政处罚。公开可核实，页面校验时请人工完成。'),
    'credit_china': dict(name='信用中国', tier='free', transport='browser_page',
                         status='needs_verification', rules=['judicial_case_ratio'],
                         description='失信被列举等公共信用记录，按页面口径原样记录。'),
    'court_records': dict(name='中国裁判文书网', tier='free', transport='browser_page',
                          status='needs_verification', rules=['judicial_case_ratio'],
                          description='公开裁判文书检索；只登记数量与案号，不转述内容。'),
    'jobscope_jobs': dict(name='已保存岗位与证据', tier='free', transport='local',
                          status='available', rules=['salary_quote_coverage', 'disclosure_available'],
                          description='不联网，使用本项目已有数据计算。默认启用。'),
    'qcc': dict(name='企查查开放平台', tier='paid', transport='http_api', status='opt_in',
                rules=['company_age', 'insured_headcount_trend', 'judicial_case_ratio'],
                description='需你自备企业、密钥与授权额度；JobScope 不代持密钥、不默认调用。'),
    'tyc': dict(name='天眼查开放平台', tier='paid', transport='http_api', status='opt_in',
                rules=['company_age', 'insured_headcount_trend', 'judicial_case_ratio'],
                description='同上，接口口径与计费以服务商协议为准。'),
}
# 默认零付费：这个元组之外没人需要记住哪些来源要花钱，enabled/client 都按 tier 走。
PAID_SOURCES = tuple(item for item, spec in SIGNAL_SOURCES.items() if spec['tier'] == 'paid')


class SignalSourceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['disabled', 'enabled']
    key: str = Field(default='', max_length=4000)
    url: str = Field(default='', max_length=2000)


class SignalSourceStore:
    def __init__(self, repo):
        self.repo = repo
        with repo.connection() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS signal_sources(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')

    def stored(self, source_id):
        with self.repo.connection() as conn:
            row = conn.execute('SELECT payload FROM signal_sources WHERE id=?', (source_id,)).fetchone()
        return json.loads(row['payload']) if row else {}

    def effective(self):
        """tier 决定默认值，存过的设置覆盖它；这里的 payload 带密钥，只能内部用。
        写成字典合并而不是 `dict(mode=..., **stored)`：后者在库里已有 mode 时会抛 TypeError。"""
        return {item: {'mode': 'enabled' if SIGNAL_SOURCES[item]['tier'] == 'free' else 'disabled', **self.stored(item)} for item in SIGNAL_SOURCES}

    def enabled(self, source_id):
        spec = SIGNAL_SOURCES[source_id]
        state = self.effective()[source_id]
        if state['mode'] != 'enabled':
            return False
        # 免费来源同样尊重用户的关闭操作；付费来源没有密钥就不算启用，哪怕 mode 被写成了 enabled。
        return spec['tier'] == 'free' or bool(state.get('key'))

    def public(self):
        """照 ModelSettings.public() 的做法：`key` 字段整个不出现在返回结构里，只留 key_set 布尔。"""
        return {item: dict(SIGNAL_SOURCES[item], **{key: value for key, value in self.effective()[item].items() if key != 'key'},
                           key_set=bool(self.stored(item).get('key'))) for item in SIGNAL_SOURCES}

    def save(self, source_id, data: SignalSourceInput):
        spec = SIGNAL_SOURCES[source_id]
        current = self.stored(source_id)
        key = data.key.strip()
        # 空 url 表示「这次不改地址」，沿用已存的；与 ModelSettings 里空字段表示不变的写法一致。
        url = data.url.strip() or current.get('url', '')
        if not key and current.get('key') and url != current.get('url', ''):
            raise ValueError('更换服务地址时请重新填写密钥')
        payload = dict(current, key=key or current.get('key', ''), url=url)
        if data.mode == 'enabled' and spec['tier'] == 'paid' and not payload['key']:
            raise ValueError('付费来源需要你填写自己的密钥')
        payload.update(mode=data.mode, tier=spec['tier'], updated_at=now())
        with self.repo.connection() as conn:
            conn.execute('INSERT OR REPLACE INTO signal_sources VALUES (?,?)', (source_id, json.dumps(payload, ensure_ascii=False)))
        return self.public()[source_id]

    def client(self, source_id):
        """付费来源只在用户填了密钥并启用后才给出可调用对象；免费来源是页面驱动，不给 HTTP 客户端。"""
        if SIGNAL_SOURCES[source_id]['tier'] != 'paid' or not self.enabled(source_id):
            return None
        return PaidClient(source_id, self.stored(source_id))


class PaidClient:
    def __init__(self, source_id, config):
        self.source_id = source_id
        self.url = config.get('url', '')
        self.key = config.get('key', '')

    def __repr__(self):
        # 日志与异常文本里都可能带出对象，密钥不能跟着出去。
        return f'PaidClient({self.source_id}, key_set={bool(self.key)})'

    def fetch(self, company_name):
        if not self.url:
            raise ValueError('付费来源未填写接口地址')
        import httpx
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
            response = client.get(self.url, params=dict(company=company_name), headers={'Authorization': 'Bearer ' + self.key})
            response.raise_for_status()
            if len(response.content) > 2000000:
                raise ValueError('来源响应超过上限')
            return response.json()
