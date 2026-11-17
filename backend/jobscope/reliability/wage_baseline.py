"""人社局工资指导价位的手动导入：CSV 解析与校验，无网络、无模型。

产品口径（2026-11-17 免费通道路线图第 2 条）：官方指导价位按 职业×学历 分层发布，作为
`pay_vs_city_median` 同城同层对照样本不足时的兜底基线。只做手动导入，不抓取官网。

导入格式（表头必须完全一致，逗号或制表符分隔，容忍 UTF-8 BOM 与行尾空白）：

    城市,职类,学历,月薪中位数,年份,备注
    杭州,技术研发,本科,18000,2025,2025年人力资源市场工资价位

约束映射到本项目已有的词表，不另造口径：
- 城市：命中 connectors.CITY_CODES（canonical 城市名）；
- 职类：必须是 job_stats.ROLE_FAMILIES 六类之一（官方职业细类到职类的映射由导入者完成，
  程序不做关键词猜测——映射错层比留空更误导）；
- 学历：必须是 job_stats.education_bucket 的输出档位之一；
- 月薪中位数：正数（元/月）；年份：4 位数字。

parse() 返回 (rows, errors)：rows 是可直接入库的字典；errors 是「第 N 行：原因」的列表。
调用方把两者如实回给用户——部分成功照常入库，坏行不静默丢弃。
"""
from __future__ import annotations

import csv
import io
import math
import re

from ..connectors import CITY_CODES
from .job_stats import ROLE_FAMILIES

HEADER = ['城市', '职类', '学历', '月薪中位数', '年份', '备注']
ROLES = tuple(family for family, _ in ROLE_FAMILIES)
# education_bucket 的输出档位（含「不限」）；与 job_states 的分层键一致，基线才能配上层。
EDUCATIONS = ('不限', '博士', '硕士', '本科', '大专', '高中及以下')
YEAR_TEXT = re.compile(r'^(19|20)\d{2}$')
MAX_ROWS = 2000


def _cells(line):
    """一行拆成至多 6 列：备注里允许再出现逗号，超出部分并回备注。"""
    parts = line.split('\t') if '\t' in line else next(csv.reader([line]))
    if len(parts) > len(HEADER):
        parts = parts[:len(HEADER) - 1] + [','.join(parts[len(HEADER) - 1:])]
    return [part.strip() for part in parts]


def parse(text):
    rows, errors = [], []
    cleaned = (text or '').lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n').strip()
    if not cleaned:
        return [], ['内容为空：请粘贴表头和数据行，或直接上传 CSV 文件。']
    lines = [line for line in cleaned.split('\n') if line.strip()]
    header = _cells(lines[0])
    if header[:len(HEADER)] != HEADER:
        return [], [f'表头不符：需要「{"，".join(HEADER)}」，收到「{"，".join(header[:len(HEADER)])}」。']
    for index, line in enumerate(lines[1:], start=2):
        cells = _cells(line)
        if len(cells) < len(HEADER):
            errors.append(f'第 {index} 行：只有 {len(cells)} 列，需要 {len(HEADER)} 列。')
            continue
        city, role, education, median_text, year, note = cells
        # 城市按「字典名出现在输入里」归一（兼容「杭州市」写法）；单元格限长，防止长备注里
        # 顺带出现别的城市名被误配。
        normalised_city = (next((name for name in CITY_CODES if name and name in city), '')
                           if len(city) <= 12 else '')
        if not normalised_city:
            errors.append(f'第 {index} 行：城市「{city[:20]}」不在城市字典里，请用城市名（如 杭州 或 杭州市）。')
            continue
        if role not in ROLES:
            errors.append(f'第 {index} 行：职类「{role}」不在 {"、".join(ROLES)} 之内。')
            continue
        if education not in EDUCATIONS:
            errors.append(f'第 {index} 行：学历「{education}」不在 {"、".join(EDUCATIONS)} 之内。')
            continue
        try:
            monthly_median = float(median_text)
        except ValueError:
            errors.append(f'第 {index} 行：月薪中位数「{median_text}」不是数字。')
            continue
        # float('inf')/'Infinity' 能通过 >0 校验：入库后 JSON 序列化输出裸 Infinity，
        # 前端 JSON.parse 直接失败，所以有限性要在这里闸住（NaN 已被 >0 挡住，inf 不会）。
        if not math.isfinite(monthly_median) or not monthly_median > 0:
            errors.append(f'第 {index} 行：月薪中位数「{median_text}」需要是大于 0 的有限数字。')
            continue
        if not YEAR_TEXT.match(year):
            errors.append(f'第 {index} 行：年份「{year}」需要是 4 位数字（如 2025）。')
            continue
        rows.append(dict(city=normalised_city, role=role, education=education,
                         monthly_median=round(monthly_median, 2), year=year, note=note[:200]))
        if len(rows) >= MAX_ROWS:
            errors.append(f'超过单次导入上限 {MAX_ROWS} 行，其余行未处理。')
            break
    return rows, errors
