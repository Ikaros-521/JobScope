"""Signal sources. Paid tiers stay off until the user opts in; keys never echo back.

诚实状态（与 `collectors.py` 对 gsxt 页面 URL 的处理同一口径）：下面 `PAID_FIELD_*` 里每一组
候选字段名都是**按通用写法预置的假设**，本项目没有付费账号、没有核对过任何一家的响应结构，
这些名字一个都不作数。用户要按自己采购到的接口文档逐行校正 `fields`（列表顺序就是取值优先级）。
`reliability/paid.py` 只按这份表声明过的字段名取值：名字对不上就一条信号都不产出，
绝不从别的数字里「挑一个像参保人数的」——把猜测包装成情报是这个功能最坏的失败模式。
"""
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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
                          status='available',
                          rules=['salary_quote_coverage', 'disclosure_available', 'pay_vs_city_median',
                                 'jd_specificity', 'industry_media_count'],
                          description='不联网，使用本项目已有数据计算。默认启用。'),
    'qcc': dict(name='企查查开放平台', tier='paid', transport='http_api', status='opt_in',
                rules=['company_age', 'insured_headcount_trend', 'judicial_case_ratio',
                       'pay_vs_city_median', 'disclosure_revenue_trend', 'disclosure_headcount_trend'],
                description='需你自备企业、密钥与授权额度；JobScope 不代持密钥、不默认调用。'
                            '字段映射是按通用写法预置的假设，请照你采购的接口文档校正。',
                fields=QCC_FIELDS),
    'tyc': dict(name='天眼查开放平台', tier='paid', transport='http_api', status='opt_in',
                rules=['company_age', 'insured_headcount_trend', 'judicial_case_ratio',
                       'pay_vs_city_median', 'disclosure_revenue_trend', 'disclosure_headcount_trend'],
                description='同上，接口口径与计费以服务商协议为准。字段映射同样待你按文档校正。',
                fields=TYC_FIELDS),
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
        """一次 GET，密钥只进请求头。

        诚实状态：能靠设置校正的只有 `fields` 里的字段名和这个接口地址；把公司名放在名为
        `company` 的查询参数上、用 `Authorization: Bearer` 认证，同样是按通用写法预置的假设。
        服务商要求 GET/POST 有别、或要签名（appId + timestamp + sign 一类）时，这里会 4xx，
        `paid.collect()` 退成 failed 并原样带回原因——不会伪造任何信号。
        """
        if not self.url:
            raise ValueError('付费来源未填写接口地址')
        import httpx
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
            response = client.get(self.url, params=dict(company=company_name), headers={'Authorization': 'Bearer ' + self.key})
            response.raise_for_status()
            if len(response.content) > 2000000:
                raise ValueError('来源响应超过上限')
            return response.json()
