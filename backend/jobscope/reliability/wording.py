"""All outward-facing sentences come from here. Numbers and sources, never verdicts."""

WORDING_VERSION = 'wording-2'

# 这些词把「可核实的数字」升级成了对他人商誉的断言，任何模板产出命中即视为缺陷。
BANNED = ('欠薪', '拖欠工资', '黑心', '血汗', '骗子', '诈骗', '皮包公司', '跑路', '老赖', '倒闭',
          '裁员', '裁撤', '垃圾', '坑', '无良', '高危', '高风险', '危险', '不良', '劣迹', '信誉差', '不靠谱')
# 区间文案必须跟着分数语义走：55 分是「材料方向不一致」，不是「出现了问题」。
# 45-60 段以前写成「出现了值得你重点核实的信号」，一家成立 2 年却在扩员的公司会被这样描述，属于文案说谎。
_BANDS = ((75, '公开材料里的稳定信号偏多'),
          (60, '已有材料偏向中性，仍有维度缺少依据'),
          (45, '手上的材料方向不一致，请逐条对照下面的依据'),
          (0, '公开材料里出现了需要你重点核实的信号'))
# 总分那个百分比是「六个维度里按权重有据的部分」，不是单维度的依据充分度，别混称。
OVERALL = '综合参考分 {score}/100，维度覆盖 {confidence}%（按权重）。{band}。JobScope 只整理你能查到的公开材料，这个分数不是对公司的评价或结论。'
UNKNOWN_OVERALL = '可纳入计算的公开依据不足，暂不给出综合参考分。补齐下方缺口后重新计算。'
UNVERIFIED_SUFFIX = '（来自个人自述，未见独立核实）'

TEMPLATES = {
    'company_age': '登记信息显示成立约 {years} 年（来源见出处）。',
    'headcount_trend': '公开年报的参保人数由 {before} 人变为 {after} 人；原因可能有多种，本工具不推断经营好坏。',
    'judicial_cases': '公开裁判信息中相关案件约 {ratio} 件/万人参保规模；涉诉数量与用工质量没有既定关系，请结合案由自行判断。',
    'salary_quote_coverage': '该公司已保存岗位中 {known}/{total} 条能解析出人民币月薪区间，其余报价口径不支持。',
    # 条数多不代表可信，也不代表少；样本量大小的评价需要口径，模板里不许凭空断言。
    'employee_review_sample': '收集到 {samples} 条员工自述材料，属个人经历陈述。',
    'disclosure_available': '已保存 {count} 份公开公告元信息，正文是否已解析以出处标记为准。',
}


def lint(text):
    return [word for word in BANNED if word in text]


def band(score):
    return next(label for minimum, label in _BANDS if score >= minimum)


def render(finding):
    template = TEMPLATES[finding.template]
    text = template.format(**finding.data)
    if finding.source_class in ('employee', 'media'):
        text = text.rstrip('。') + UNVERIFIED_SUFFIX
    return text


def overall_text(score_value, confidence):
    if score_value is None:
        return UNKNOWN_OVERALL
    return OVERALL.format(score=score_value, confidence=round(confidence * 100), band=band(score_value))


def assert_clean(result):
    """Guard used by the service: one assertive sentence or one un-sourced finding fails the build."""
    texts = [render(item) for item in result.findings] + [overall_text(result.score, result.confidence)]
    offenders = {text: lint(text) for text in texts if lint(text)}
    if offenders:
        raise ValueError(f'措辞命中禁用词，已停止输出：{offenders}')
    unlinked = [item.rule for item in result.findings if not item.evidence_id]
    if unlinked:
        raise ValueError(f'依据缺少证据编号：{unlinked}')
