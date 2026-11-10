"""Local statistics over stored job rows for the pay / professional rules. No socket, no Chrome, no model.

P1 给 `pay` 与 `professional` 补的两条 ASSESSMENT 规则全部由本模块从**已存岗位行**算出：

- 只读 `repository.jobs()`，不导入浏览器模块、不调模型、不碰 `research_jobs` 语义索引
  （语义索引可能压根没建，不能依赖）。
- 返回形状与 `local.py` 同构：每个函数给出 value 与一段**不带时刻**的口径说明（证据身份含内容
  哈希，正文带时刻会让每次点击都新增记录，时间只留在信号的 `collected_at`）。
- 统计量只在这里算，分数仍在 `rubric.py`：本模块交原始数字，闸门与分档都由规则表的 scorer 决定。
- 城市归一只做「命中已知城市词典就归一，否则按 normalise_name 后的原文精确分组」，不做模糊匹配、
  不做「周边城市并入」——把不属于该市的岗位算进同城样本，比样本小更糟。
"""
from __future__ import annotations

import re
from collections import Counter
from statistics import median

from ..analysis import SKILLS, contains, parse_salary
from ..connectors import CITY_CODES
from ..repository import Repository

# 量化结果的识别词表：百分比、日活/月活、QPS/TPS、GMV、亿级/万级一类「写得出数字的结果」。
# 刻意保守：只认材料里明确写出的这些形态，不做语义判断。
QUANTIFIED = re.compile(r'\d+\s*%|日活|月活|DAU|MAU|QPS|TPS|GMV|亿级|千万级|百万级|万级|\d+\s*万(?:级|\b|人|\+)?|\d+\s*亿', re.IGNORECASE)
LOCAL_NOTE = '由本项目已保存数据计算，不访问网络'


def normalise_city(location):
    """命中已知城市词典就归一到词典写法；否则按规范化原文精确分组。没有第三个分支。"""
    text = Repository.normalise_name(location)
    if not text:
        return ''
    for city in CITY_CODES:
        if city in text:
            return city
    return text


def _city_of(job):
    return normalise_city(job.get('location', ''))


def _midpoints(jobs):
    """可解析人民币月薪报价的区间中点；年薪、时薪、外币一律不算已知。"""
    mids = []
    for item in jobs:
        parsed = parse_salary(item.get('salary', ''))
        low, high = parsed.get('monthly_min'), parsed.get('monthly_max')
        if parsed.get('currency') == 'CNY' and low is not None and high is not None:
            mids.append((low + high) / 2)
    return mids


def pay_value(jobs, names):
    """R1 `pay_vs_city_median` 的统计量。城市取该公司岗位里出现次数最多的可归一城市。

    返回 None 只出现在「该公司没有任何已存岗位或归一不出城市」时；有岗位但一条可解析报价都
    没有时返回全零统计量，让闸门缺口说明样本不够，而不是留一句无从着手的「尚无来源」。
    样本闸门（公司 ≥3 条、同城 ≥20 条、中位数大于 0）不在这一层判——出不出分归 rubric。
    """
    company = [item for item in jobs if Repository.normalise_name(item['company']) in names]
    if not company:
        return None
    counts = Counter(city for city in map(_city_of, company) if city)
    if not counts:
        return None
    city = counts.most_common(1)[0][0]
    own_mids = _midpoints(company)
    city_mids = _midpoints([item for item in jobs if _city_of(item) == city])
    company_median = round(median(own_mids), 2) if own_mids else 0
    city_median = round(median(city_mids), 2) if city_mids else 0
    ratio = round(company_median / city_median, 3) if company_median > 0 and city_median > 0 else 0
    return dict(company_median=company_median, city_median=city_median, ratio=ratio,
                company_jobs=len(own_mids), city_jobs=len(city_mids)), city


def pay_note(value, city):
    return (f'pay_vs_city_median 口径：本公司 {value["company_jobs"]} 条已存岗位的可解析人民币月薪报价中位数 '
            f'{value["company_median"]} 元，与同城（{city}）{value["city_jobs"]} 条已存岗位报价中位数 '
            f'{value["city_median"]} 元对比；单条报价取月薪区间中点，年薪、时薪与外币报价不计入；'
            '这是招聘平台报价对比，不是实际到手薪资，同城样本也不是同职级同技能样本。' + LOCAL_NOTE + '。')


def _jd_stats(description):
    lines = [line.strip() for line in description.splitlines() if line.strip()]
    items = len(lines)
    skills = len({skill for skill in SKILLS if contains(description, skill)})
    quantified = sum(1 for line in lines if QUANTIFIED.search(line))
    return dict(chars=len(description.strip()), items=items, skills=skills,
                quantified_share=round(quantified / items, 3) if items else 0)


def jd_value(jobs, names):
    """R2 `jd_specificity` 的统计量。只统计 description_status == 'complete' 的岗位：
    列表摘要（半截 JD）不参与评估，这是 P0 在 /api/analyze 里就立过的规矩。

    公司有已存岗位但没有完整 JD 时也返回全零统计量，让闸门缺口在报告里说清「完整职责未采集」，
    而不是留一句无从着手的「尚无来源」。
    """
    company = [item for item in jobs if Repository.normalise_name(item['company']) in names]
    if not company:
        return None
    complete = [item for item in company
                if item.get('description_status') == 'complete' and (item.get('description') or '').strip()]
    stats = [_jd_stats(item['description']) for item in complete]
    count = len(stats)
    if not count:
        return dict(jobs=0, chars_mean=0, items_mean=0, skills_mean=0, quantified_share=0)
    return dict(
        jobs=count,
        chars_mean=round(sum(item['chars'] for item in stats) / count, 1),
        items_mean=round(sum(item['items'] for item in stats) / count, 1),
        skills_mean=round(sum(item['skills'] for item in stats) / count, 1),
        quantified_share=round(sum(item['quantified_share'] for item in stats) / count, 3),
    )


def jd_note(value):
    if value['jobs'] == 0:
        return ('jd_specificity 口径：该公司已存岗位里没有 description_status=complete 的完整描述，'
                '只有列表摘要的岗位不参与评估，因此没有可计算的描述统计。' + LOCAL_NOTE + '。')
    return (f'jd_specificity 口径：只统计 description_status=complete 的 {value["jobs"]} 条完整岗位描述，'
            f'平均 {value["chars_mean"]} 字，职责/要求条目平均 {value["items_mean"]} 条，'
            f'可识别技术栈平均 {value["skills_mean"]} 项，含量化结果的条目占比 {value["quantified_share"]}；'
            '衡量的是岗位描述把要做什么讲得有多具体，不是技术实力。' + LOCAL_NOTE + '。')
