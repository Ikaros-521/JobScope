"""Signals computed from the data this project already stores. No socket, no Chrome, no score.

`sources.SIGNAL_SOURCES` 把 `jobscope_jobs` 登记成 `transport='local'`：它的两条规则量的都是
「本项目手上有多少材料」，所以取数完全走本地仓储，不导入浏览器模块、不占会话锁、不限速。

诚实标记写死在这里，避免被读成外部来源：

- `source_class='platform'`、`url=''`、`observed_at=''`：没有外部页面可指，就没有观察时间可写；
  出处那一栏留空，配合 `source='jobscope_jobs'` 与说明文字，才看得出来这条不是抓来的。
- 落库时证据的 `kind` 取 `DERIVED_KIND`（`job_posting`）。这三个公告类 kind 之外是关键：
  把计算记录本身写成 `disclosure`，下一次采集就会把它数成一份公告，计数会跟着点击次数无限膨胀；
  而 `job_posting` 同样被抽取路径的 `include_job_postings=False` 排除，算出来的口径不会反过来
  喂给抽取器变成第二批「材料」。
- 这两条规则在 `rubric` 里都是 `MATERIAL`：本模块只报条数与覆盖率，绝不产出分数。
"""
from __future__ import annotations

from ..analysis import parse_salary
from ..repository import Repository, now
from .rubric import RULES

# 与 `sources.SIGNAL_SOURCES` 里 transport='local' 的来源键对齐；端点按这张表分派本地路径。
SOURCES = ('jobscope_jobs',)
# 公告类证据的 kind 沿用 `extract.KIND_TO_SOURCE_CLASS` 里已有的三个，不新造词。
DISCLOSURE_KINDS = ('disclosure', 'disclosure_metadata', 'announcement')
# 见模块文档：计算记录必须落在这三个 kind 之外，否则自己会把自己数进去。
DERIVED_KIND = 'job_posting'
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


def _coverage_note(value, stamp):
    return (f'salary_quote_coverage 口径：本项目已保存该公司岗位 {value["total"]} 条，其中 {value["known"]} 条'
            '的报价能被 analysis.parse_salary() 解析出人民币月薪区间；年薪、时薪与外币报价按口径不计入已知。'
            f'{LOCAL_NOTE}。计算时间 {stamp}。')


def _disclosure_note(value, stamp):
    return (f'disclosure_available 口径：本项目已保存该公司公告类证据 {value["count"]} 份'
            f'（kind 取 {"、".join(DISCLOSURE_KINDS)}），只统计条数，正文是否已解析以出处标记为准。'
            f'{LOCAL_NOTE}。计算时间 {stamp}。')


def collect(source_id, company, *, repo, identity=None, browser_module=None, data_dir=None):
    """Compute the local source's rules from stored rows. `browser_module` is never read."""
    if source_id not in SOURCES:
        return _outcome('unsupported', '该来源不由本项目已保存数据计算。')
    names = _company_names(identity, company)
    if not names:
        return _outcome('failed', '请先确定要查询的公司名称。')
    jobs = [item for item in repo.jobs() if Repository.normalise_name(item['company']) in names]
    # 只有 currency == 'CNY' 才算「报价口径已知」：parse_salary 对年薪/时薪/外币把 currency 留成 None。
    known = sum(1 for item in jobs if parse_salary(item.get('salary', '')).get('currency') == 'CNY')
    materials = [item for item in repo.evidence(include_job_postings=False)
                 if Repository.normalise_name(item['company']) in names and item['kind'] in DISCLOSURE_KINDS]
    stamp = now()
    signals = [
        _signal(source_id, 'salary_quote_coverage', {'known': known, 'total': len(jobs)}, stamp,
                _coverage_note({'known': known, 'total': len(jobs)}, stamp)),
        _signal(source_id, 'disclosure_available', {'count': len(materials)}, stamp,
                _disclosure_note({'count': len(materials)}, stamp)),
    ]
    message = (f'{LOCAL_NOTE}：{len(jobs)} 条已存岗位里 {known} 条能解析出人民币月薪区间，'
               f'已存公告类证据 {len(materials)} 份。')
    return _outcome('completed', message, signals)
