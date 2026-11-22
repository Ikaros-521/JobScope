"""触达生成器（2026-11-22）：打招呼/跟进话术生成与版本记录。

漏斗的最后一公里：评估帮用户把岗位研究到位，触达消息才是真正发出去的那句话——
回复率是长期求职者最痛且最可控的杠杆。模型走「抽取」功能绑定（同 company_merge
口径：短文本一次调用，不需要流式）；模型未配置或调用失败时退回规则模板兜底——
触达不能因为模型抖动就整体不可用。

每次生成都落一条版本（outreach_versions），A/B 对比靠版本列表 + used_at 使用标记：
用户手动把话术发到平台（BOSS 浏览器通道保持人工把关），用哪条回这里标一下，
漏斗侧的回复数据对比才有归因依据。
"""
import json

GREETING_MAX_CHARS = 120
FOLLOWUP_MAX_CHARS = 150
# 简历给模型的窗口：打招呼只需要 2 个匹配点，全文塞进去反而稀释。
RESUME_WINDOW = 2500
JD_WINDOW = 1200

_SYSTEM = (
    '你是求职触达话术助手。根据岗位与候选人简历写一句直接发给招聘者的消息。'
    '要求：1.口语化、像真人第一句话，不寒暄不套话不自夸；'
    '2.只挑简历与 JD 最匹配的 1–2 个点，每个点尽量带一个具体证据（年限/成果/技术名）；'
    '3.不要复述 JD，不要编造简历里没有的事实；4.只输出消息正文，不要任何解释、引号或前缀。'
)

_PROMPTS = {
    'greeting': ('给「{company}」的「{title}」岗位写打招呼开场白（BOSS 首条消息）。',
                 '硬性要求：不超过 {} 字。'),
    'followup': ('给「{company}」的「{title}」岗位写跟进消息：已投递/沟通后一段时间没有回复，'
                 '礼貌催进展并补充一个新价值点（从简历里挑一个还没提过的）。',
                 '硬性要求：不超过 {} 字，不要抱怨对方不回复。'),
}


def _resume_text(repo, job_id):
    """优先取该岗位 pin 的定制简历（评估适配同一口径），没有再退主简历。"""
    pinned = repo.pinned_resume_version(job_id)
    if pinned and pinned.get('markdown'):
        return pinned['markdown']
    try:
        return repo.resume()['text']
    except (TypeError, KeyError):
        return ''


def _evidence_tags(job):
    tags = [tag for tag in (job.get('tags') or []) if str(tag).strip()]
    return tags[:2]


def _fallback(job, kind, days):
    """规则模板兜底：不编造事实，只复述岗位标题与可见标签。"""
    tags = '、'.join(_evidence_tags(job))
    if kind == 'greeting':
        body = '您好，看到贵司「{}」岗位，与我的背景比较匹配{}。方便的话想进一步沟通，谢谢！'.format(
            job.get('title', ''), f'（{tags}）' if tags else '')
    else:
        waited = f'前一段时间' if not days else f'{days} 天前'
        body = '您好，{}投递了贵司「{}」岗位，想跟进一下进展。如需补充材料或安排沟通，我随时方便，谢谢！'.format(
            waited, job.get('title', ''))
    return body


def _days_since_applied(job):
    """跟进消息的等待天数：优先当前状态变化时间，没有就用收藏时间。"""
    from datetime import datetime, timezone
    stamp = job.get('pipeline_status_changed_at') or job.get('starred_at') or ''
    if not stamp:
        return None
    try:
        seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(str(stamp))).total_seconds()
    except (TypeError, ValueError):
        return None
    return max(0, int(seconds // 86400))


def generate(repo, job_id, kind):
    """生成一条话术并落版本。返回版本行；模型不可用走规则兜底（origin 标注来源）。"""
    if kind not in _PROMPTS:
        raise ValueError('话术类型只支持 greeting / followup')
    job = repo.job(job_id)
    if not job:
        raise ValueError('岗位不存在')
    days = _days_since_applied(job) if kind == 'followup' else None
    max_chars = GREETING_MAX_CHARS if kind == 'greeting' else FOLLOWUP_MAX_CHARS
    origin = 'rules'
    text = ''
    from .settings import get_purpose_config, purpose_configured
    if purpose_configured('extraction'):
        opening, constraint = _PROMPTS[kind]
        payload = {
            'instruction': (opening + constraint).format(company=job.get('company', ''), title=job.get('title', ''),
                                                         max_chars=max_chars),
            'job': {key: job.get(key, '') for key in ('title', 'company', 'salary', 'location')},
            'tags': _evidence_tags(job),
            'jd': str(job.get('description') or '')[:JD_WINDOW],
            'resume': _resume_text(repo, job_id)[:RESUME_WINDOW],
        }
        if kind == 'followup' and days:
            payload['days_since_applied'] = days
        from .model_transport import complete
        try:
            content, _usage = complete(get_purpose_config('extraction'), _SYSTEM,
                                       json.dumps(payload, ensure_ascii=False),
                                       temperature=0.4, max_tokens=800, timeout=60)
            cleaned = content.strip().strip('“”"\'\'「」')
            if cleaned and len(cleaned) <= max_chars * 2:
                text = cleaned
                origin = 'model'
        except Exception as exc:  # 模型抖动不阻断触达：落兜底并记录。
            import logging
            logging.getLogger(__name__).warning('触达话术模型调用失败，退规则模板：%s', exc)
    if not text:
        text = _fallback(job, kind, days)
    return repo.save_outreach_version(job_id, kind, text, origin)
