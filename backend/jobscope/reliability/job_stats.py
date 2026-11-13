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
from .limits import MIN_CITY_JOBS, MIN_COMPANY_JOBS

# 量化结果的识别词表：百分比、日活/月活、QPS/TPS、GMV、亿级/万级一类「写得出数字的结果」。
# 刻意保守：只认材料里明确写出的这些形态，不做语义判断。
QUANTIFIED = re.compile(r'\d+\s*%|日活|月活|DAU|MAU|QPS|TPS|GMV|亿级|千万级|百万级|万级|\d+\s*万(?:级|\b|人|\+)?|\d+\s*亿', re.IGNORECASE)
LOCAL_NOTE = '由本项目已保存数据计算，不访问网络'
# 同层薪资闸门常量（MIN_COMPANY_JOBS / MIN_CITY_JOBS）见 limits.py：零依赖模块，rubric 同引用。


def normalise_city(location):
    """命中已知城市词典就归一到词典写法；否则按规范化原文精确分组。没有第三个分支。"""
    text = Repository.normalise_name(location)
    if not text:
        return ''
    for city in CITY_CODES:
        if city in text:
            return city
    return text


# ── 分层对比（R1 公平性）───────────────────────────────────────────────
# 薪资与职类、经验要求、学历要求强相关：把审核员（大专/经验不限）和 AI 开发（本科/3-5年）
# 混进同一个中位数里比，是在拿低层级岗位对标同城的高层级基线，比值天然偏低且无法解读。
# 三张分类表只认材料里明确写出的标注；缺任何一层标注的岗位不参与评分，只进缺口说明。
ROLE_FAMILIES = (
    ('审核认证', ('审核', '认证', '体系', 'ccaa', '外审', '验厂')),
    ('技术研发', ('前端', '后端', 'java', 'python', '算法', '开发', '工程师', '测试', '运维', '架构', 'golang', 'php', 'ai')),
    ('产品与设计', ('设计', '视觉', '交互', '产品经理', 'ui', 'ux')),
    ('销售与市场', ('销售', '市场', '商务', '客户经理', '渠道', '电销', '电商')),
    ('人事与财务', ('人事', '招聘', '行政', '财务', '会计', '人力')),
    ('运营与客服', ('运营', '客服', '文员', '助理', '支持')),
)
# 两个来源的标注写法都收：boss 是「5-10年、本科」或「杭州 · 5-10年 · 本科」，智联是
# 「经验：经验不限\n学历：大专」。匹配只看写出的标注，不从职位名称里的「高级」猜级别——
# 「高级招聘专员」不是高级技术岗。
EXP_TEXT = re.compile(r'经验[：:]\s*([^\n]{1,12})|(\d+\s*[-–~至]\s*\d+年|\d+年以上|\d+年以下|\d+年以内|经验不限|无经验|应届生?|在校生)')
EDU_TEXT = re.compile(r'学历[：:]\s*([^\n]{1,8})|(学历不限|博士|硕士|研究生|本科|大专|中专|中技|高中|初中)')


def experience_bucket(text):
    """把标注文本归一到经验档位；档位名保留原文数字，不同的档位就是不同的层，不归并不猜。"""
    if not text:
        return None
    if re.search(r'不限|无经验', text):
        return '不限'
    if re.search(r'应届|在校', text):
        return '应届'
    match = re.search(r'(\d+)\s*[-–~至]\s*(\d+)年', text)
    if match:
        return f'{match.group(1)}-{match.group(2)}年'
    for pattern, label in ((r'(\d+)\s*年以上', '{}年以上'), (r'(\d+)\s*年以下', '{}年以下'), (r'(\d+)\s*年以内', '{}年以内')):
        match = re.search(pattern, text)
        if match:
            return label.format(match.group(1))
    return None


def education_bucket(text):
    """把标注文本归一到学历档位；「研究生」并入「硕士」，中专/中技/高中/初中并成一层。"""
    if not text:
        return None
    if '不限' in text:
        return '不限'
    for token, bucket in (('博士', '博士'), ('硕士', '硕士'), ('研究生', '硕士'), ('本科', '本科'),
                          ('大专', '大专'), ('中专', '高中及以下'), ('中技', '高中及以下'),
                          ('高中', '高中及以下'), ('初中', '高中及以下')):
        if token in text:
            return bucket
    return None


def role_family(title):
    """职位名称 → 职类。中文关键词用普通子串匹配——`contains` 的字母词边界会把
    「工程师0」「审核员1」这类数字化标题挡掉；字母词（ai/java/php…）仍走 `contains`，
    'ai' 不会命中别的字母词。认不出就返回 None。"""
    text = str(title or '')
    for family, keywords in ROLE_FAMILIES:
        for keyword in keywords:
            if contains(text, keyword) if keyword.isascii() else keyword in text:
                return family
    return None


def job_stratum(item):
    """可比层键 =（职类，经验档，学历档）。缺任何一层标注返回 None：拿不到层级一致的证据，
    就不做「要求相当」的断言——未标注的岗位和谁都不保证同层。"""
    description = item.get('description') or ''
    exp_match, edu_match = EXP_TEXT.search(description), EDU_TEXT.search(description)
    family = role_family(item.get('title', ''))
    exp = experience_bucket(exp_match.group(0) if exp_match else None)
    edu = education_bucket(edu_match.group(0) if edu_match else None)
    if not family or not exp or not edu:
        return None
    return (family, exp, edu)


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


def pay_analysis(jobs, names):
    """R1 `pay_vs_city_median` 的统计量：同城 + 同职类 + 同经验档 + 同学历档的分层报价中位数对比。

    与旧口径（公司全体岗位 vs 同城全库）的差别是分层：混层中位数会让审核员对标技术岗基线。
    - 城市取该公司岗位里出现次数最多的可归一城市；本公司只取该市的岗位（苏州的审核员不进
      杭州的对比），同城对照也只取同层岗位。
    - 每层闸门：本公司 ≥3 条、同城同层 ≥10 条、两个中位数都大于 0；过闸的层按本公司样本数
      加权出总比值。出不出分仍归 rubric（合计 ≥3/≥20 与分档在 scorer）。
    - 缺城市/职类/要求标注的岗位与样本不足的层不参与评分，逐条写进 note 让缺口可行动。
    返回 None 只在「该公司没有任何已存岗位或归一不出城市」时；分不出可比层时返回全零统计量。
    """
    company = [item for item in jobs if Repository.normalise_name(item['company']) in names]
    if not company:
        return None
    counts = Counter(city for city in map(_city_of, company) if city)
    if not counts:
        return None
    city = counts.most_common(1)[0][0]
    local_company = [item for item in company if _city_of(item) == city]
    pool = {}
    for item in jobs:
        if _city_of(item) == city:
            key = job_stratum(item)
            if key:
                pool.setdefault(key, []).append(item)
    groups = {}
    for item in local_company:
        key = job_stratum(item)
        if key:
            groups.setdefault(key, []).append(item)
    ratios, detail, covered_own, covered_same = [], [], [], []
    for key in sorted(groups):
        own, same = _midpoints(groups[key]), _midpoints(pool.get(key, []))
        label = '·'.join(key)
        if len(own) >= MIN_COMPANY_JOBS and len(same) >= MIN_CITY_JOBS:
            company_median, city_median = round(median(own), 2), round(median(same), 2)
            if company_median > 0 and city_median > 0:
                ratio = round(company_median / city_median, 3)
                ratios.append((len(own), ratio))
                covered_own.extend(own)
                covered_same.extend(same)
                detail.append(f'{label}：本公司 {len(own)} 条中位 {company_median} 元 / 同城同层 {len(same)} 条中位 '
                              f'{city_median} 元，比值 {ratio}')
                continue
        detail.append(f'{label}：本公司 {len(own)} 条、同城同层 {len(same)} 条，样本不够（本公司需 ≥{MIN_COMPANY_JOBS} 条、'
                      f'同层 ≥{MIN_CITY_JOBS} 条），不比')
    excluded_city = len(company) - len(local_company)
    excluded_unstratified = len(local_company) - sum(len(group) for group in groups.values())
    excluded = []
    if excluded_city:
        excluded.append(f'{excluded_city} 条不在{city}')
    if excluded_unstratified:
        excluded.append(f'{excluded_unstratified} 条职类或经验/学历标注缺失')
    if ratios:
        weighted = round(sum(count * ratio for count, ratio in ratios) / sum(count for count, _ in ratios), 3)
        value = dict(ratio=weighted, company_jobs=len(covered_own), city_jobs=len(covered_same))
        # 中位数只在单层覆盖时给出：多层合并的中位数会把不同层混在一起，正是这次分层要除掉的误导。
        # 前端事实小图拿不到这两个键就不画，句子与口径正文照常说明。
        if len(ratios) == 1:
            value.update(company_median=round(median(covered_own), 2), city_median=round(median(covered_same), 2))
    else:
        # 分不出可比层：全零统计量，让闸门缺口说清「分层凑不齐」，而不是无声无息。
        value = dict(ratio=0, company_jobs=0, city_jobs=0)
    lines = [f'pay_vs_city_median 口径：按职类、经验要求、学历要求分层对比已存岗位的月薪报价中位数，'
             f'单条报价取区间中点，年薪、时薪与外币报价不计入。']
    lines.extend(f'第 {index} 层 {line}' for index, line in enumerate(detail, 1))
    if excluded:
        lines.append('另有' + '、'.join(excluded) + '，未参与对比。')
    if ratios:
        lines.append(f'{len(ratios)} 个可比分层按本公司样本数加权得总比值 {value["ratio"]}。')
    lines.append('只对比标注相当（同职类、同经验要求、同学历要求）的样本；这是招聘平台报价对比，'
                 '不是实际到手薪资，不含奖金与股票，要求相当也不等于职级完全一致。' + LOCAL_NOTE + '。')
    return dict(value=value, city=city, note='\n'.join(lines))


def pay_value(jobs, names):
    """兼容入口：只要（统计量，城市）。需要分层明细与口径正文时用 `pay_analysis`。"""
    analysis = pay_analysis(jobs, names)
    return None if analysis is None else (analysis['value'], analysis['city'])


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


def stratum_breakdown(jobs, names):
    """横评页的岗位分层概要：给「薪资依赖岗位构成」一个可读的对照面。

    只读已存岗位，与 `pay_analysis` 同一套分层键（职类×经验档×学历档）、同一套闸门
    （本公司 ≥ MIN_COMPANY_JOBS 条且同层同城 ≥ MIN_CITY_JOBS 条才给中位数）——横评页展示的数字必须与报告口径同源，
    不在页面层另立一套「参考性中位数」。与打分路径的差别只有一点：这里逐层全列（含样本
    不足的层，标 gate），缺什么一目了然；分数仍然只由 rubric 产生，本函数不评分。
    mix 按职类统计全部已存岗位（含认不出职类的，记「未分类」），让「两家公司岗位构成
    不同、公司级中位数不可比」这件事直接看得见。
    """
    company = [item for item in jobs if Repository.normalise_name(item['company']) in names]
    if not company:
        return dict(city='', total_jobs=0, parseable=0, mix=[], layers=[])
    counts = Counter(city for city in map(_city_of, company) if city)
    city = counts.most_common(1)[0][0] if counts else ''
    pool, groups = {}, {}
    for item in jobs:
        if not city or _city_of(item) == city:
            key = job_stratum(item)
            if key:
                pool.setdefault(key, []).append(item)
    for item in company:
        if not city or _city_of(item) == city:
            key = job_stratum(item)
            if key:
                groups.setdefault(key, []).append(item)
    mix_counter = Counter((role_family(item.get('title', '')) or '未分类') for item in company)
    layers = []
    for key in sorted(groups):
        own, same = _midpoints(groups[key]), _midpoints(pool.get(key, []))
        layer = dict(role=key[0], experience=key[1], education=key[2],
                     company_jobs=len(own), city_jobs=len(same))
        if len(own) >= MIN_COMPANY_JOBS and len(same) >= MIN_CITY_JOBS:
            company_median, city_median = round(median(own), 2), round(median(same), 2)
            if company_median > 0 and city_median > 0:
                layer.update(company_median=company_median, city_median=city_median,
                             ratio=round(company_median / city_median, 3), gate='pass')
                layers.append(layer)
                continue
        layer['gate'] = 'few_company' if len(own) < MIN_COMPANY_JOBS else 'few_city'
        layers.append(layer)
    return dict(city=city, total_jobs=len(company), parseable=len(_midpoints(company)),
                mix=[dict(role=role, jobs=count) for role, count in
                     sorted(mix_counter.items(), key=lambda item: (-item[1], item[0]))],
                layers=layers)
