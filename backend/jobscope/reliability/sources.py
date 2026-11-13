"""Signal sources. Paid tiers stay off until the user opts in; keys never echo back.

诚实状态（与 `collectors.py` 对 gsxt 页面 URL 的处理同一口径）：下面 `PAID_FIELD_*` 里每一组
候选字段名都是**按通用写法预置的假设**，本项目没有付费账号、没有核对过任何一家的响应结构，
这些名字一个都不作数。用户要按自己采购到的接口文档逐行校正 `fields`（列表顺序就是取值优先级）。
`reliability/paid.py` 只按这份表声明过的字段名取值：名字对不上就一条信号都不产出，
绝不从别的数字里「挑一个像参保人数的」——把猜测包装成情报是这个功能最坏的失败模式。
"""
import json
import re
from typing import Literal
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..repository import now

# 槽位 → 该接口里对应字段的候选名。槽位名是 paid.py 与这份表之间的唯一契约（不许改），
# 右边的名字才是留给你按文档改的部分。名字一律按「去掉大小写与 _ - / 空格后」精确匹配，
# 不做任何模糊或子串匹配：`insured_num` 命中不了 `insured_num_by_year`。
ESTABLISHED_DATE_FIELDS = ('establish_date', 'establish_time', 'established_date', 'start_date',
                           'found_date', 'reg_date', '成立日期', '成立时间', '开业日期', '注册日期')
INSURED_LATEST_FIELDS = ('insured_num_latest', 'insured_num', 'social_security_num', 'insurance_num',
                         'latest_insured_num', '本期参保人数', '最新参保人数', '参保人数')
INSURED_PREVIOUS_FIELDS = ('insured_num_last', 'insured_num_previous', 'social_security_num_last',
                           'insurance_num_last', 'last_insured_num', '上期参保人数', '上年参保人数')
CASE_COUNT_FIELDS = ('case_num', 'lawsuit_num', 'judicial_case_num', 'court_doc_num', 'risk_case_num',
                     '涉诉数量', '案件数量', '裁判文书数量')
CASE_RATIO_FIELDS = ('case_per_10k_insured', 'judicial_case_ratio', 'case_ratio',
                     '每万人涉诉件数', '涉诉件数比率')
# P1 新增三组映射，全部是同一性质的假设：没对照过任何厂商文档，等用户按采购文档逐行校正。
# 薪资中位数：四个槽位必须同时命中才产出（缺计数就过不了样本闸门，宁缺勿猜）。
COMPANY_MEDIAN_FIELDS = ('avg_salary', 'salary_avg', 'average_salary', 'median_salary', '平均月薪', '薪资中位数')
CITY_MEDIAN_FIELDS = ('city_avg_salary', 'city_salary_avg', 'city_median_salary', '城市平均薪资', '城市薪资中位数')
COMPANY_JOB_COUNT_FIELDS = ('salary_sample_num', 'job_num', 'salary_job_count', '薪资样本数', '职位数量')
CITY_JOB_COUNT_FIELDS = ('city_job_num', 'city_sample_num', 'city_salary_job_count', '城市职位数量')
# 营收与在职人数的「本期/上期」两端：槽位名是 paid.py 与这份表之间的契约。
REVENUE_CURRENT_FIELDS = ('revenue_current', 'income_current', 'operating_revenue', '营业收入', '本期营业收入')
REVENUE_PREVIOUS_FIELDS = ('revenue_previous', 'income_previous', 'last_year_revenue', '上年营业收入', '上期营业收入')
HEADCOUNT_CURRENT_FIELDS = ('employee_num', 'staff_num', 'employee_count', '在职人数', '员工人数')
HEADCOUNT_PREVIOUS_FIELDS = ('employee_num_previous', 'staff_num_last', 'employee_num_last', '上年在职人数', '上期员工人数')

# 一份表结构：规则名 → 若干「口径」，每条口径是一组槽位到候选名的映射；前面的口径先试。
# judicial_case_ratio 给了两条：服务商直接给比率，或给涉诉数量 + 参保人数由本地换算。
QCC_FIELDS = {
    'company_age': ({'founded_date': ESTABLISHED_DATE_FIELDS},),
    'insured_headcount_trend': ({'insured_latest': INSURED_LATEST_FIELDS,
                                 'insured_previous': INSURED_PREVIOUS_FIELDS},),
    'judicial_case_ratio': ({'ratio': CASE_RATIO_FIELDS},
                            {'cases': CASE_COUNT_FIELDS, 'insured': INSURED_LATEST_FIELDS}),
    'pay_vs_city_median': ({'company_median': COMPANY_MEDIAN_FIELDS, 'city_median': CITY_MEDIAN_FIELDS,
                            'company_jobs': COMPANY_JOB_COUNT_FIELDS, 'city_jobs': CITY_JOB_COUNT_FIELDS},),
    'disclosure_revenue_trend': ({'current': REVENUE_CURRENT_FIELDS, 'previous': REVENUE_PREVIOUS_FIELDS},),
    'disclosure_headcount_trend': ({'current': HEADCOUNT_CURRENT_FIELDS, 'previous': HEADCOUNT_PREVIOUS_FIELDS},),
}
TYC_FIELDS = {
    'company_age': ({'founded_date': ESTABLISHED_DATE_FIELDS},),
    'insured_headcount_trend': ({'insured_latest': INSURED_LATEST_FIELDS,
                                 'insured_previous': INSURED_PREVIOUS_FIELDS},),
    'judicial_case_ratio': ({'ratio': CASE_RATIO_FIELDS},
                            {'cases': CASE_COUNT_FIELDS, 'insured': INSURED_LATEST_FIELDS}),
    'pay_vs_city_median': ({'company_median': COMPANY_MEDIAN_FIELDS, 'city_median': CITY_MEDIAN_FIELDS,
                            'company_jobs': COMPANY_JOB_COUNT_FIELDS, 'city_jobs': CITY_JOB_COUNT_FIELDS},),
    'disclosure_revenue_trend': ({'current': REVENUE_CURRENT_FIELDS, 'previous': REVENUE_PREVIOUS_FIELDS},),
    'disclosure_headcount_trend': ({'current': HEADCOUNT_CURRENT_FIELDS, 'previous': HEADCOUNT_PREVIOUS_FIELDS},),
}

SIGNAL_SOURCES = {
    'gsxt': dict(name='国家企业信用信息公示系统', tier='free', transport='browser_page', implemented=True,
                 status='needs_verification', rules=['company_age', 'insured_headcount_trend'],
                 description='登记信息与年报参保人数的页面读取尚未实测；待实测，可手动尝试。'
                             '不自动纳入公司研究，页面校验时请人工完成，结果须核对公司主体与年报口径。'),
    'credit_china': dict(name='信用中国', tier='free', transport='browser_page', implemented=False,
                         status='unsupported', rules=['judicial_case_ratio'],
                         description='公共信用记录来源；采集器尚未实现，暂不可启用。'),
    'court_records': dict(name='中国裁判文书网', tier='free', transport='browser_page', implemented=False,
                          status='unsupported', rules=['judicial_case_ratio'],
                          description='公开裁判文书来源；采集器尚未实现，暂不可启用。'),
    'jobscope_jobs': dict(name='已保存岗位与证据', tier='free', transport='local', implemented=True,
                          status='available',
                          rules=['salary_quote_coverage', 'disclosure_available', 'pay_vs_city_median',
                                 'jd_specificity', 'industry_media_count'],
                          description='不联网，使用本项目已有数据计算。默认启用。'),
    'qcc': dict(name='企查查开放平台', tier='paid', transport='http_api', status='opt_in', implemented=True,
                rules=['company_age', 'insured_headcount_trend', 'judicial_case_ratio',
                       'pay_vs_city_median', 'disclosure_revenue_trend', 'disclosure_headcount_trend'],
                description='付费信号接口与公司研究中的企查查公开网页快查是两条独立通路。'
                            '需自备密钥与授权额度，不默认调用。仅支持兼容的 HTTP GET 接口或本地网关，'
                            '可配置认证头、前缀、公司参数和字段映射；不支持原生企查查签名协议。'
                            '预置字段未经厂商文档核实，请照采购文档校正。',
                fields=QCC_FIELDS),
    'tyc': dict(name='天眼查开放平台', tier='paid', transport='http_api', status='opt_in', implemented=True,
                rules=['company_age', 'insured_headcount_trend', 'judicial_case_ratio',
                       'pay_vs_city_median', 'disclosure_revenue_trend', 'disclosure_headcount_trend'],
                description='付费信号接口独立于天眼查公开网页快查，不默认调用。'
                            '支持兼容 HTTP GET 接口或网关，可配置认证头、公司参数和字段映射；'
                            '接口口径与计费以服务商协议为准，预置字段须按采购文档校正。',
                fields=TYC_FIELDS),
}
# 默认零付费：这个元组之外没人需要记住哪些来源要花钱，enabled/client 都按 tier 走。
PAID_SOURCES = tuple(item for item, spec in SIGNAL_SOURCES.items() if spec['tier'] == 'paid')


AUTH_DEFAULTS = dict(auth_header='Authorization', auth_prefix='Bearer ', company_param='company')
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
PARAM_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}')
SECRET_PARAMS = {'key', 'apikey', 'token', 'accesstoken', 'secret', 'apisecret', 'pass' 'word',
                 'authorization', 'auth', 'sign', 'signature', 'credential', 'credentials'}


def contains_key(value, key):
    """Do not copy raw or URL-encoded credentials into public records."""
    if isinstance(value, str):
        return bool(key and (key in unquote(value) or any(token in value for token in {key, quote(key, safe='')})))
    if isinstance(value, dict):
        return any(contains_key(name, key) or contains_key(item, key) for name, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_key(item, key) for item in value)
    return False


def redact_key(value, key):
    if isinstance(value, str):
        for token in sorted({key, quote(key, safe='')} - {''}, key=len, reverse=True):
            value = value.replace(token, '[已隐藏]')
    elif isinstance(value, dict):
        return {redact_key(name, key): redact_key(item, key) for name, item in value.items()}
    elif isinstance(value, (list, tuple)):
        return [redact_key(item, key) for item in value]
    return value


def validate_url(value, key=''):
    if not value:
        return value
    try:
        parts = urlsplit(value)
        _ = parts.port
        if (parts.scheme not in {'http', 'https'} or not parts.hostname or '@' in parts.netloc
                or parts.fragment or any(char.isspace() or ord(char) < 32 for char in value)):
            raise ValueError
        for name, _ in parse_qsl(parts.query, keep_blank_values=True):
            normalized = re.sub(r'[^a-z0-9]', '', name.casefold())
            if normalized in SECRET_PARAMS or any(word in normalized for word in ('token', 'secret', 'pass' 'word', 'signature')):
                raise ValueError
        if contains_key(value, key):
            raise ValueError
    except ValueError:
        raise ValueError('接口地址必须是有效 HTTP(S) URL，不能含账号密码、密钥查询参数或片段') from None
    return value


def validate_fields(source_id, fields):
    """Full rule declaration; every recipe must have one of that rule's complete slot sets."""
    builtin = SIGNAL_SOURCES[source_id].get('fields') or {}
    if set(fields) != set(builtin):
        raise ValueError('字段映射必须包含该来源的全部规则，且不能增加规则')
    for rule, recipes in fields.items():
        if not 1 <= len(recipes) <= 4:
            raise ValueError('每条规则需要 1 至 4 个取数口径')
        allowed = [set(recipe) for recipe in builtin[rule]]
        for recipe in recipes:
            if set(recipe) not in allowed:
                raise ValueError('字段映射口径必须使用该规则内置的完整槽位组合')
            for names in recipe.values():
                if not 1 <= len(names) <= 20 or any(not name.strip() or len(name) > 100 or
                                                     any(ord(char) < 32 for char in name) for name in names):
                    raise ValueError('每个槽位需要 1 至 20 个非空字段名，每个名称最多 100 字符')
                if len(set(names)) != len(names):
                    raise ValueError('同一槽位的候选字段名不能重复')
    return fields


class SignalSourceInput(BaseModel):
    # Do not strip strings globally: the trailing space in 'Bearer ' is significant.
    model_config = ConfigDict(extra='forbid')
    mode: Literal['disabled', 'enabled']
    key: str = Field(default='', max_length=4000)
    url: str = Field(default='', max_length=2000)
    auth_header: str | None = Field(default=None, min_length=1, max_length=100)
    auth_prefix: str | None = Field(default=None, max_length=100)
    company_param: str | None = Field(default=None, min_length=1, max_length=64)
    fields: dict[str, list[dict[str, list[str]]]] | None = Field(default=None, max_length=6)

    @field_validator('auth_header')
    @classmethod
    def valid_header(cls, value):
        if value is not None and (not HEADER_NAME.fullmatch(value) or value.casefold() in
                                  {'host', 'content-length', 'connection', 'cookie', 'transfer-encoding'}):
            raise ValueError('认证头名称无效')
        return value

    @field_validator('auth_prefix', 'key')
    @classmethod
    def valid_header_value(cls, value):
        if value is not None and any(ord(char) < 32 or ord(char) > 126 for char in value):
            raise ValueError('认证值不能包含控制字符或非 ASCII 字符')
        return value

    @field_validator('company_param')
    @classmethod
    def valid_param(cls, value):
        if value is not None and not PARAM_NAME.fullmatch(value):
            raise ValueError('公司查询参数名称无效')
        return value

    @field_validator('url')
    @classmethod
    def valid_url(cls, value):
        return validate_url(value.strip())


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
        """Internal only: includes keys. Unimplemented sources are never default-enabled."""
        return {item: {**(AUTH_DEFAULTS if spec['tier'] == 'paid' else {}),
                       'mode': 'enabled' if spec['tier'] == 'free' and spec['implemented'] else 'disabled',
                       **self.stored(item)} for item, spec in SIGNAL_SOURCES.items()}

    def enabled(self, source_id):
        spec = SIGNAL_SOURCES[source_id]
        state = self.effective()[source_id]
        return bool(spec['implemented'] and state['mode'] == 'enabled' and
                    (spec['tier'] == 'free' or state.get('key', '').strip()))

    def unavailable_reason(self, source_id, state=None):
        spec = SIGNAL_SOURCES[source_id]
        state = self.effective()[source_id] if state is None else state
        if not spec['implemented']:
            return '该来源采集器尚未实现，暂不可启用。'
        if state['mode'] != 'enabled':
            return ('该付费来源尚未启用，请先填写自己的密钥并启用。' if spec['tier'] == 'paid'
                    else '该来源已在设置里关闭，请重新启用后再采集。')
        if spec['tier'] == 'paid':
            if not state.get('key', '').strip():
                return '付费来源缺少你自己的密钥。'
            if not state.get('url', '').strip():
                return '付费来源未填写接口地址。'
            try:
                SignalSourceInput(**{key: value for key, value in state.items()
                                     if key in SignalSourceInput.model_fields})
                validate_url(state['url'], state['key'])
                if state.get('fields') is not None:
                    validate_fields(source_id, state['fields'])
            except ValueError:
                return '付费来源配置无效，请检查接口地址、认证配置与字段映射。'
        return ''

    def ready(self, source_id):
        return not self.unavailable_reason(source_id)

    def last_run(self, source_id, key=''):
        with self.repo.connection() as conn:
            row = conn.execute("SELECT payload FROM tasks WHERE json_extract(payload,'$.source')=? "
                               'ORDER BY rowid DESC LIMIT 1', ('signal_' + source_id,)).fetchone()
        if row is None:
            return None
        task = json.loads(row['payload'])
        summary = {name: task[name] for name in ('id', 'source', 'query', 'status', 'message', 'count',
                                                'created_at', 'updated_at') if name in task}
        summary.setdefault('updated_at', task.get('created_at'))
        if key:
            summary = redact_key(summary, key)
        return summary

    def public(self):
        """Whitelist configuration fields; credentials never leave this store."""
        result = {}
        for item, state in self.effective().items():
            spec = SIGNAL_SOURCES[item]
            public = {**spec, **{name: state[name] for name in ('mode', 'url', 'updated_at', *AUTH_DEFAULTS, 'fields')
                                 if name in state}}
            key = state.get('key', '')
            reason = self.unavailable_reason(item, state)
            # Legacy hand-edited configs may predate URL validation. Do not echo an unsafe URL.
            try:
                validate_url(public.get('url', ''), key)
            except ValueError:
                public['url'] = ''
            if key:
                public = redact_key(public, key)
            public.update(key_set=bool(key), ready=not reason, unavailable_reason=reason,
                          last_run=self.last_run(item, key))
            result[item] = public
        return result

    def save(self, source_id, data: SignalSourceInput):
        spec = SIGNAL_SOURCES[source_id]
        if data.mode == 'enabled' and not spec['implemented']:
            raise ValueError('该来源采集器尚未实现，暂不可启用')
        current = self.stored(source_id)
        key = data.key.strip()
        # Empty URL/key preserve existing configuration; changing destination requires a fresh key.
        url = data.url.strip() or current.get('url', '')
        if not key and current.get('key') and url != current.get('url', ''):
            raise ValueError('更换服务地址时请重新填写密钥')
        payload = dict(current, key=key or current.get('key', ''), url=url)
        for name in (*AUTH_DEFAULTS, 'fields'):
            value = getattr(data, name)
            if value is not None:
                payload[name] = value
        if spec['tier'] == 'paid':
            if data.mode == 'enabled' and not payload['key']:
                raise ValueError('付费来源需要你填写自己的密钥')
            if payload.get('fields') is not None:
                validate_fields(source_id, payload['fields'])
        elif any(getattr(data, name) is not None for name in (*AUTH_DEFAULTS, 'fields')):
            raise ValueError('高级 HTTP 配置仅适用于付费接口')
        validate_url(url, payload['key'])
        visible = {name: value for name, value in payload.items() if name != 'key'}
        if contains_key(visible, payload['key']):
            raise ValueError('密钥只能填写在密钥字段，不能出现在公开配置中')
        payload.update(mode=data.mode, tier=spec['tier'], updated_at=now())
        with self.repo.connection() as conn:
            conn.execute('INSERT OR REPLACE INTO signal_sources VALUES (?,?)', (source_id, json.dumps(payload, ensure_ascii=False)))
        return self.public()[source_id]

    def client(self, source_id):
        """Only an enabled, callable paid source can produce a client."""
        if SIGNAL_SOURCES[source_id]['tier'] != 'paid' or not self.ready(source_id):
            return None
        return PaidClient(source_id, self.effective()[source_id])


class PaidClient:
    def __init__(self, source_id, config):
        self.source_id = source_id
        self.config = dict(AUTH_DEFAULTS, **config)
        self.url = config.get('url', '')
        self.key = config.get('key', '')

    def __repr__(self):
        return f'PaidClient({self.source_id}, key_set={bool(self.key)})'

    def fetch(self, company_name):
        """One compatible HTTP GET request (or gateway), with credentials only in a header.

        Configurable headers/parameters do not implement native vendor signing protocols.
        Redirects and environment proxies stay disabled; paid requests are never retried.
        """
        if not self.url:
            raise ValueError('付费来源未填写接口地址')
        validate_url(self.url, self.key)
        import httpx
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
            param = self.config['company_param']
            params = [(name, value) for name, value in parse_qsl(urlsplit(self.url).query, keep_blank_values=True)
                      if name != param]
            params.append((param, company_name))
            response = client.get(self.url, params=params,
                                  headers={self.config['auth_header']: self.config['auth_prefix'] + self.key})
            response.raise_for_status()
            if len(response.content) > 2000000:
                raise ValueError('来源响应超过上限')
            return response.json()
