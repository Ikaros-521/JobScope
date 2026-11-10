"""Call an opted-in paid data API through a *declared* field mapping. No schema guessing.

设计前提：本项目没有付费账号、没有凭据，也就无法核对任何一家服务商的真实响应结构。所以这里
刻意不写死厂商 schema，也刻意不做「从任意 JSON 里找一个像参保人数的数字」那种聪明事——那等于
把猜测包装成情报，是这个功能最坏的失败模式。规则只有两条：

1. 字段名一律来自 `sources.SIGNAL_SOURCES[source]['fields']` 那份声明表（规则名 → 口径 → 槽位
   → 候选字段名）。**那份表是按通用写法预置的假设，等用户按自己采购的接口文档逐行校正**；
   诚实状态写在这里、写在 `sources.py` 的模块文档里、也必须写在每次采集的 message 里。
2. 只在「键名与候选名精确对上（忽略大小写与 `_ - / 空格`）」时取值，值只接受数字或能解析成日期
   的字符串。对不上就这条规则什么都不产出，并把试过的字段名逐个报回 message；
   绝不用默认值、绝不补 0、绝不把「缺字段」当成负面事实。

取数结果的形状与 `collectors.py` / `local.py` 完全同构（`status`/`message`/`signals`，每条信号带
`rule`/`dimension`/`unit`/`source_class`/`url`/`observed_at`/`collected_at`），端点才能共用同一段
「先落可溯源证据、再落信号」的写法。维度与单位一律取自 `rubric.RULES`，本模块不自己写。
日期换算年数复用 `collectors.years_since`，全项目只有一把尺子。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx

from ..repository import now
from .collectors import years_since
from .rubric import RULES
from .sources import SIGNAL_SOURCES

# 槽位词表：这份表之外的槽位名一律算映射写错（`test_reliability_sources` 会红，不会静默不取数）。
SLOTS = frozenset({'founded_date', 'insured_latest', 'insured_previous', 'ratio', 'cases', 'insured',
                   'company_median', 'city_median', 'company_jobs', 'city_jobs',
                   'current', 'previous'})
DATE_SLOTS = frozenset({'founded_date'})
# 付费取数落成证据时的 kind。既不能是公告类（否则 `local.disclosure_available` 会把接口记录
# 数成一份公告，计数随点击无限膨胀），也不该进抽取器的材料（否则厂商字段会第二轮变成模型输入）。
PAID_KIND = 'paid_api_record'
# 件/万人：件数 ÷ 人数 × 10000。口径写进 message，用户才复核得动。
PER_WAN = 10000
# message 里每个槽位最多报几个候选字段名：全列会让一句话长到没法读。
NAMES_SHOWN = 5
MAPPING_NOTE = ('字段名来自 sources.SIGNAL_SOURCES 里那份按通用写法预置的映射，'
                '尚未对照任何服务商文档核实，请按你采购的接口文档校正')
DATE_FORMATS = ('%Y-%m-%d', '%Y/%m/%d', '%Y年%m月%d日', '%Y.%m.%d',
                '%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S')
NUMERIC = re.compile(r'-?\d+(?:\.\d+)?')


def _outcome(status, message, signals=()):
    """返回键与 `collectors.collect()` / `local.collect()` 同构，前端 Badge 不用加分支。"""
    return {'status': status, 'message': message, 'signals': list(signals)}


def candidate_names(source_id, rule, slot):
    """把声明表里某个槽位的候选字段名读出来：代码里没有第二份名单。"""
    recipes = (SIGNAL_SOURCES.get(source_id) or {}).get('fields') or {}
    found = []
    for recipe in recipes.get(rule) or ():
        for name in recipe.get(slot) or ():
            if name not in found:
                found.append(str(name))
    return found


def _norm(name):
    return re.sub(r'[\s_\-/]', '', str(name)).casefold()


def _pairs(payload):
    """广度优先展开 JSON 树，逐个给出 (键, 原始值)：取值不依赖服务商把字段嵌在第几层。"""
    queue = [payload]
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str):
                    yield key, value
                queue.append(value)
        elif isinstance(node, (list, tuple)):
            queue.extend(node)


def _num(value):
    return int(value) if float(value).is_integer() else value


def _as_number(value):
    """只接受数字，或纯数字串（`1,234` 这种千分位写法照样是数字）。不接受 bool。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _num(float(value))
    if isinstance(value, str):
        text = value.strip().replace(',', '').replace('，', '')
        return _num(float(text)) if NUMERIC.fullmatch(text) else None
    return None


def _as_date(value):
    """日期只认日期字符串。数字一律不取：epoch 秒、epoch 毫秒与 `20150401` 谁都可能，猜错一次就是一条假信号。"""
    if not isinstance(value, str):
        return None
    text = value.strip()[:32]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _lookup(payload, names, kind):
    """按候选名顺序在响应里取值：命中第一个用到的字段名；同名字段给出多个不同值时不猜。

    返回 (字段名, 取值, 该字段名出现过的不同值)。字段名为 None 表示这个槽位没对上。
    """
    wanted = {}
    for index, name in enumerate(names):
        wanted.setdefault(_norm(name), index)
    found = {}
    for key, value in _pairs(payload):
        index = wanted.get(_norm(key))
        if index is None:
            continue
        for item in (value if isinstance(value, (list, tuple)) else [value]):
            parsed = _as_date(item) if kind == 'date' else _as_number(item)
            if parsed is None:
                continue
            found.setdefault(index, []).append(parsed)
    if not found:
        return None, None, ()
    index = min(found)
    values = tuple(dict.fromkeys(found[index]))
    return names[index], values[0], values


def _miss_reason(fields, slots):
    parts = []
    for slot in slots:
        names = [str(item) for item in fields.get(slot) or ()]
        shown = '、'.join(names[:NAMES_SHOWN])
        if len(names) > NAMES_SHOWN:
            shown += f'…（共 {len(names)} 个候选名）'
        hint = '（本模块只接受日期字符串，数字型日期不取）' if slot in DATE_SLOTS else ''
        parts.append(f'{slot} 的映射字段 {shown} 在该响应里不存在{hint}')
    return '；'.join(parts)


def _ambiguous_reason(slot, name, values):
    shown = '、'.join(str(item) for item in values[:3])
    return (f'{slot} 映射到的字段 {name} 在响应里对应多个不同值（{shown}），'
            '无法确定该取哪一个，未取用')


def _from_slots(rule, resolved):
    """把对上了的槽位拼成一条信号；返回 (构造结果, 原因)。返回 None 时原因必须说清楚为什么。"""
    if rule == 'company_age':
        name, founded = resolved['founded_date']
        years = years_since(founded)
        observed = founded.date().isoformat()
        return dict(value={'years': years}, observed_at=observed,
                    note=f'company_age 口径：成立/开业日期取自字段 {name}（{observed}），'
                         f'按与浏览器采集同一把算法换算为 {years} 年'), ''
    if rule == 'insured_headcount_trend':
        latest_name, latest = resolved['insured_latest']
        previous_name, previous = resolved['insured_previous']
        return dict(value={'before': previous, 'after': latest}, observed_at='',
                    note=f'insured_headcount_trend 口径：本期参保人数取字段 {latest_name}={latest} 人、'
                         f'上一期取字段 {previous_name}={previous} 人，单位 {RULES[rule].unit}；'
                         '哪一期算「本期」完全取决于你把哪个字段填进 insured_latest，请照接口文档核对——'
                         '两期写反会把扩员读成缩编'), ''
    if rule == 'judicial_case_ratio':
        if 'ratio' in resolved:
            name, value = resolved['ratio']
            return dict(value={'ratio': value}, observed_at='',
                        note=f'judicial_case_ratio 口径：直接取比率字段 {name}={value}，'
                             f'本模块按这份映射声明它的口径已经是 {RULES[rule].unit}，不做任何换算；'
                             '若该字段其实是百分比或每千人口径，请改映射'), ''
        cases_name, cases = resolved['cases']
        insured_name, insured = resolved['insured']
        if insured <= 0:
            return None, (f'参保人数字段 {insured_name}={insured}，分母为 0 时比率没有定义，'
                          '不产出该条（0 人参保不等于没有涉诉）')
        ratio = round(cases * PER_WAN / insured, 2)
        return dict(value={'ratio': ratio}, observed_at='',
                    note=f'judicial_case_ratio 口径：分子=涉诉数量字段 {cases_name}（{cases} 件），'
                         f'分母=参保人数字段 {insured_name}（{insured} 人），'
                         f'按 件 ÷ 人 × {PER_WAN} 换算为 {ratio} {RULES[rule].unit}'), ''
    if rule == 'pay_vs_city_median':
        # 四个槽位缺一不可：只有中位数没有样本数，样本闸门就无从成立，映射不算配置成功。
        median_name, company_median = resolved['company_median']
        city_name, city_median = resolved['city_median']
        jobs_name, company_jobs = resolved['company_jobs']
        city_jobs_name, city_jobs = resolved['city_jobs']
        ratio = round(company_median / city_median, 3) if city_median > 0 else 0
        return dict(value={'company_median': company_median, 'city_median': city_median, 'ratio': ratio,
                           'company_jobs': company_jobs, 'city_jobs': city_jobs}, observed_at='',
                    note=f'pay_vs_city_median 口径：本公司中位数取字段 {median_name}={company_median}、'
                         f'城市中位数取字段 {city_name}={city_median}，样本数取 {jobs_name}={company_jobs}、'
                         f'{city_jobs_name}={city_jobs}；这是该接口声明的薪资统计口径，'
                         '样本闸门仍由固定规则判断，样本不够就不出分'), ''
    if rule in ('disclosure_revenue_trend', 'disclosure_headcount_trend'):
        # 两个时期都取自同一次接口响应；「同一份材料」约束由映射表声明，接口口径是否真的同源请照文档核对。
        current_name, current = resolved['current']
        previous_name, previous = resolved['previous']
        return dict(value={'current': current, 'previous': previous}, observed_at='',
                    note=f'{rule} 口径：本期取字段 {current_name}={current}、上期取字段 {previous_name}={previous}，'
                         '两个数字来自同一次接口响应；数字自带公告/接口的口径（万元、亿元、人数），'
                         '本模块不换算、不补单位'), ''
    return None, f'本模块没有 {rule} 的取数写法（映射表里出现这条规则等于多写了配置）'


def _read_rule(rule, recipes, payload):
    """逐条口径尝试；全部没对上就把试过的字段名原样报回去。"""
    if not recipes:
        return None, f'这份映射没有 {rule} 的口径'
    reasons, ambiguous = [], []
    for recipe in recipes:
        resolved, failed = {}, []
        for slot, names in recipe.items():
            name, value, distinct = _lookup(payload, names, 'date' if slot in DATE_SLOTS else 'number')
            if name is None:
                failed.append(slot)
                continue
            if len(distinct) > 1:
                ambiguous.append(_ambiguous_reason(slot, name, distinct))
                failed.append(slot)
                continue
            resolved[slot] = (name, value)
        if not failed:
            return _from_slots(rule, resolved)
        reasons.append(_miss_reason(recipe, failed))
    return None, '；'.join(ambiguous + reasons)


def _signal(source_id, rule, built, url, stamp):
    """维度与单位一律取自 rubric；`text` 是给证据正文用的口径说明，不是信号契约的字段。"""
    spec = RULES[rule]
    note = f'{built["note"]}。{MAPPING_NOTE}；出处即本条 url 指向的接口地址，密钥不写进地址也不写进正文。'
    return dict(rule=rule, dimension=spec.dimension, value=built['value'], unit=spec.unit,
                source_class='paid_api', observed_at=built['observed_at'], url=url,
                collected_at=stamp, source=source_id, text=f'{rule} 由付费接口 {source_id} 的声明字段取得。{note}')


def collect(source_id, company, *, client=None):
    """One request, one pass. Fields that are not declared are not looked for, and never invented."""
    spec = SIGNAL_SOURCES.get(source_id) or {}
    fields = spec.get('fields')
    if spec.get('tier') != 'paid' or spec.get('transport') != 'http_api' or not fields:
        return _outcome('unsupported', '该来源不是登记了字段映射的付费 HTTP 接口，本模块不取数。')
    if client is None:
        return _outcome('unsupported', '该付费来源尚未启用或缺少你自己的密钥；JobScope 不代持密钥、不发起请求。')
    url = str(getattr(client, 'url', '') or '')
    key = str(getattr(client, 'key', '') or '')
    if key and key in url:
        # 出处地址会原样落进证据：密钥一旦写进地址就等于永久留档，这里直接拒绝而不是替它擦掉。
        return _outcome('failed', '接口地址里出现了密钥：密钥只能放在请求头里。请在设置里改填不含密钥的接口地址。')
    keyword = str(company or '').strip()[:120]
    if not keyword:
        return _outcome('failed', '请先确定要查询的公司名称。')
    try:
        # 付费接口按次计费：一次调用，不重试、不换参数、不改口径。
        payload = client.fetch(keyword)
    except (ValueError, httpx.HTTPError) as exc:
        detail = str(exc) or '请求失败'
        return _outcome('failed', f'付费接口 {source_id} 没有取回数据：{detail}。本轮未采集任何信号。')
    if not isinstance(payload, dict):
        # 响应根本不是对象：字段映射无从按名取值，这跟「对象里没有这些字段」是两件事。
        return _outcome('unsupported', f'付费接口 {source_id} 返回的不是 JSON 对象（实际是 {type(payload).__name__}），'
                                       f'声明式字段映射无从取值，本轮未采集任何信号。')
    stamp = now()
    signals, notes, missing = [], [], []
    for rule in spec['rules']:
        built, reason = _read_rule(rule, fields.get(rule) or (), payload)
        if built is None:
            missing.append(f'未取到 {rule}：{reason}')
            continue
        notes.append(built['note'])
        signals.append(_signal(source_id, rule, built, url, stamp))
    if not signals:
        # 有响应、有内容，只是字段对不上：说 partial，并把每条规则试过的字段名逐条列出。
        return _outcome('partial', f'{source_id} 的付费接口返回了对象，但没有一条规则的字段能对上'
                                   f'（{"；".join(missing)}）。{MAPPING_NOTE}。')
    message = f'从 {source_id} 的付费接口采集到 {len(signals)} 类信号。' + '；'.join(notes) + '。'
    if missing:
        message += f'（{"；".join(missing)}。{MAPPING_NOTE}。）'
    return _outcome('completed' if not missing else 'partial', message, signals)
