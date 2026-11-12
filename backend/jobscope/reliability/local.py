"""Signals computed from the data this project already stores. No socket, no Chrome, no score.

`sources.SIGNAL_SOURCES` 把 `jobscope_jobs` 登记成 `transport='local'`：它的规则全部取数于
本项目已存数据，所以取数完全走本地仓储，不导入浏览器模块、不占会话锁、不限速。

诚实标记写死在这里，避免被读成外部来源：

- `source_class='platform'`、`url=''`、`observed_at=''`：没有外部页面可指，就没有观察时间可写；
  出处那一栏留空，配合 `source='jobscope_jobs'` 与说明文字，才看得出来这条不是抓来的。
- 落库时证据的 `kind` 取 `DERIVED_KIND`（`local_computation`）。这条必须落在公告类 kind 之外：
  把计算记录写成 `disclosure`，下一次采集就会把它数成一份公告，计数跟着点击次数无限膨胀；
  同时它也不能进抽取路径的 materials，否则算出来的口径会反过来喂给模型变成第二批「材料」。
- 材料计数类规则（salary_quote_coverage / disclosure_available / industry_media_count）在
  `rubric` 里都是 `MATERIAL`：本模块只报条数，绝不产出分数。pay_vs_city_median 与
  jd_specificity 的统计量由 `job_stats.py` 计算，分数仍只由 rubric 的闸门与分档决定。
"""
from __future__ import annotations

from ..analysis import parse_salary
from ..repository import Repository, now
from . import job_stats
from .rubric import RULES

# 与 `sources.SIGNAL_SOURCES` 里 transport='local' 的来源键对齐；端点按 transport 分派，
# 这张表是本模块自己的第二道闸门：被直接调用时也只算这几个来源。
SOURCES = ('jobscope_jobs',)
# 公告类证据的 kind 沿用 `extract.KIND_TO_SOURCE_CLASS` 里已有的三个，不新造词。
DISCLOSURE_KINDS = ('disclosure', 'disclosure_metadata', 'announcement')
# R6 只数这两种行业与媒体材料：disclosure/disclosure_metadata 是公司自己的披露，不算行业媒体面。
MEDIA_KINDS = ('media_report', 'announcement')
# 见模块文档：计算记录必须落在公告类 kind 之外，否则自己会把自己数进去。
DERIVED_KIND = 'local_computation'
LOCAL_NOTE = '由本项目已保存数据计算，不访问网络'


def _outcome(status, message, signals=()):
    """返回键与 `collectors.collect()` 同构，端点才能共用同一段「先证据后信号」的落库代码。"""
    return {'status': status, 'message': message, 'signals': list(signals)}


def _signal(source_id, rule, value, stamp, note):
    """维度与单位仍取自 rubric；`text` 是给证据正文用的口径说明，不是信号契约的字段。"""
    spec = RULES[rule]
    return dict(rule=rule, dimension=spec.dimension, value=value, unit=spec.unit,
                source_class='platform', observed_at='', url='', collected_at=stamp,
                source=source_id, text=note)


def _company_names(identity, company):
    """与端点的 `company_materials()` 同一口径：实体名与别名一起按规范化比对，绝不模糊匹配。"""
    raw = [identity['name'], *(identity.get('aliases') or [])] if identity else [company]
    return {Repository.normalise_name(item) for item in raw if str(item or '').strip()}


def _coverage_note(value):
    # 正文里不写计算时间：证据身份含内容哈希，带上时刻会让每次点击都新增一份口径记录和一条信号。
    return (f'salary_quote_coverage 口径：本项目已保存该公司岗位 {value["total"]} 条，其中 {value["known"]} 条'
            '的报价能解析出人民币月薪区间；年薪、时薪与外币报价按口径不计入已知。' + LOCAL_NOTE + '。')


def _disclosure_note(value):
    return (f'disclosure_available 口径：本项目已保存该公司公告类证据 {value["count"]} 份'
            f'（kind 取 {"、".join(DISCLOSURE_KINDS)}），只统计条数，正文是否已解析以出处标记为准。' + LOCAL_NOTE + '。')


def _media_note(value):
    return (f'industry_media_count 口径：本项目已保存该公司媒体报道与公告类证据 {value["count"]} 条'
            f'（kind 取 {"、".join(MEDIA_KINDS)}）；行业前景没有可评价依据，本条只说明已有材料数量。' + LOCAL_NOTE + '。')


def collect(source_id, company, *, repo, identity=None, browser_module=None, data_dir=None):
    """Compute the local source's rules from stored rows. `browser_module` is never read."""
    if source_id not in SOURCES:
        return _outcome('unsupported', '该来源不由本项目已保存数据计算。')
    names = _company_names(identity, company)
    if not names:
        return _outcome('failed', '请先确定要查询的公司名称。')
    all_jobs = repo.jobs()
    jobs = [item for item in all_jobs if Repository.normalise_name(item['company']) in names]
    # 只有 currency == 'CNY' 才算「报价口径已知」：parse_salary 对年薪/时薪/外币把 currency 留成 None。
    known = sum(1 for item in jobs if parse_salary(item.get('salary', '')).get('currency') == 'CNY')
    materials = [item for item in repo.evidence(include_job_postings=False)
                 if Repository.normalise_name(item['company']) in names and item['kind'] in DISCLOSURE_KINDS]
    media = [item for item in repo.evidence(include_job_postings=False)
             if Repository.normalise_name(item['company']) in names and item['kind'] in MEDIA_KINDS]
    stamp = now()
    signals = [
        _signal(source_id, 'salary_quote_coverage', {'known': known, 'total': len(jobs)}, stamp,
                _coverage_note({'known': known, 'total': len(jobs)})),
        _signal(source_id, 'disclosure_available', {'count': len(materials)}, stamp,
                _disclosure_note({'count': len(materials)})),
    ]
    message = (f'{LOCAL_NOTE}：{len(jobs)} 条已存岗位里 {known} 条能解析出人民币月薪区间，'
               f'已存公告类证据 {len(materials)} 份')
    pay = job_stats.pay_analysis(all_jobs, names) if jobs else None
    if pay is not None:
        value = pay['value']
        signals.append(_signal(source_id, 'pay_vs_city_median', value, stamp, pay['note']))
        message += (f'，{pay["city"]}分层报价对比覆盖本公司 {value["company_jobs"]} 条、'
                    f'同层对照 {value["city_jobs"]} 条')
    jd = job_stats.jd_value(jobs, names) if jobs else None
    if jd is not None:
        signals.append(_signal(source_id, 'jd_specificity', jd, stamp, job_stats.jd_note(jd)))
        message += f'，完整岗位描述 {jd["jobs"]} 条'
    signals.append(_signal(source_id, 'industry_media_count', {'count': len(media)}, stamp,
                           _media_note({'count': len(media)})))
    message += f'，行业与媒体材料 {len(media)} 条。'
    return _outcome('completed', message, signals)
