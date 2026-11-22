"""空窗期叙事生成器（2026-11-22）：资料库「空窗经历」条目 → 简历段落 + 面试应答 + 电梯陈述。

长期求职者初筛最常见的一刀是空窗解释，面试追问也集中在这一段——但它恰恰是
现有工具链（简历优化/模拟面试/评估）都不直接回答的问题。本模块把资料库里的
第一手空窗事实翻译成三份可直接使用的产物；模型走「分析」功能绑定（叙事是
用户-facing 长文本，同 resume_workshop 口径），模型未配置或调用失败退规则
骨架兜底——空窗叙事不能因为模型抖动就整体不可用。

铁律与简历工坊同款：只用给定事实，不编造；规则兜底只做事实复述与骨架，
不替用户编故事。每次生成都落一条版本（gap_narratives），保留最近 8 版供回看。
"""
import json
import logging

RESUME_WINDOW = 2500
JD_WINDOW = 1200
ENTRIES_WINDOW = 5000

MARKERS = ('<<<PITCH>>>', '<<<RESUME>>>', '<<<INTERVIEW>>>')

_SYSTEM = (
    '你是求职教练，专治简历空窗期。根据用户提供的空窗经历事实（第一手）、基础简历摘录'
    '（可选）与目标岗位（可选），产出三份可直接使用的内容。'
    '要求：1.只用给定事实，绝不编造经历、成果或数字；事实不足就写得保守，不脑补；'
    '2.语气克制、职业化，不悲情不卖惨不过度解释；3.把非在职经历翻译成职场语言'
    '（学习→能力更新，接单/自由职业→项目交付，家庭责任→时间管理与抗压），但不得拔高成没有的成果；'
    '4.面试应答用第一人称口语，每段 150–250 字，直接可背。'
    '只按以下协议输出三个小节，不要任何额外解释：\n'
    '<<<PITCH>>>一句话电梯陈述：向面试官解释这段空窗（40 字内，先事实后姿态）\n'
    '<<<RESUME>>>简历空窗期段落：2–3 条 bullet（每条以时间段开头，动词开头，能带数字就带）\n'
    '<<<INTERVIEW>>>高频追问应答：4 个问题各一段——「空窗期在做什么」「为什么出现空窗」'
    '「技能会不会脱节」「为什么现在出来求职」；每个问题单独一行「Q：」，应答紧随其后'
)


def entries_block(repo):
    """启用的空窗经历条目 → 模型素材块（模拟面试空窗专题共用）。没有条目返回空串。"""
    entries = [item for item in repo.profile_entries(enabled_only=True) if item['category'] == 'gap']
    parts = []
    for item in entries:
        header = '｜'.join(filter(None, (item['title'], item['org'], item['period'])))
        parts.append(f'【空窗经历】{header}\n{item["content"]}')
    return '\n\n'.join(parts)[:ENTRIES_WINDOW]


def _resume_text(repo, job_id):
    """目标岗位 pin 的定制简历优先（评估适配同一口径），没有再退主简历。"""
    if job_id:
        pinned = repo.pinned_resume_version(job_id)
        if pinned and pinned.get('markdown'):
            return pinned['markdown']
    try:
        return repo.resume()['text']
    except (TypeError, KeyError):
        return ''


def _split_narrative(content):
    """按协议切三段；resume/interview 两段必须非空，pitch 缺省给空串。"""
    sections = {}
    for marker in MARKERS:
        sections[marker] = ''
    remaining = content.strip()
    for marker in MARKERS:
        if marker not in remaining:
            continue
        before, _, remaining = remaining.partition(marker)
        sections[marker] = remaining
    pitch = sections[MARKERS[0]].split(MARKERS[1])[0].strip()
    resume_block = sections[MARKERS[1]].split(MARKERS[2])[0].strip()
    interview_block = sections[MARKERS[2]].split(MARKERS[0])[0].strip()
    if not resume_block or not interview_block:
        raise ValueError('空窗叙事输出缺少 RESUME/INTERVIEW 段')
    return pitch[:300], resume_block[:4000], interview_block[:6000]


def _fallback(entries):
    """规则骨架兜底：只复述条目事实与追问骨架，不替用户编故事（origin='rules' 标注）。"""
    pitch_parts, bullets = [], []
    for item in entries:
        header = '｜'.join(filter(None, (item['period'], item['title'])))
        lead = ' '.join(str(item['content']).split())[:120]
        bullets.append(f'- {header}：{lead}')
        pitch_parts.append(f'{item["period"]}{item["title"]}'.strip())
    resume_block = '\n'.join(bullets)
    facts = '\n'.join(f'· {item["title"]}：{" ".join(str(item["content"]).split())[:80]}' for item in entries)
    interview_block = (
        '以下为应答骨架（规则生成，把括号处换成你的真实细节后即可使用）：\n\n'
        'Q：这段时间你在做什么？\n'
        f'这段时间我主要做了几件事：\n{facts}\n'
        '（补一句这些事与目标岗位的关联。）\n\n'
        'Q：为什么出现空窗？\n'
        '（一句话客观原因，不抱怨不渲染；随后立刻接「这段时间我做了上面的安排」。）\n\n'
        'Q：技能会不会脱节？\n'
        f'我一直在保持手感：\n{facts}\n（补一个最近完成的具体产出作为证据。）\n\n'
        'Q：为什么现在出来求职？\n'
        '（一句话说明节奏已拉回：原计划的事项已收尾/目标已达成，现在可以全身心投入。）'
    )
    pitch = f'空窗期间：{"；".join(pitch_parts)}——现在已回到求职节奏。'[:120]
    return pitch, resume_block, interview_block


def generate(repo, job_id='', jd_text='', job_title=''):
    """生成空窗期叙事并落版本。没有启用的空窗条目直接拒绝；模型不可用退规则骨架。"""
    entries = [item for item in repo.profile_entries(enabled_only=True) if item['category'] == 'gap']
    if not entries:
        raise ValueError('资料库还没有启用状态的「空窗经历」条目——先补 1–2 条事实（时间段+做了什么+产出），再生成叙事')
    entries_text = entries_block(repo)
    origin = 'rules'
    pitch, resume_block, interview_block = '', '', ''
    from .settings import get_purpose_config, purpose_configured
    if purpose_configured('analysis'):
        job = repo.job(job_id) if job_id else None
        jd = ''
        if job:
            jd = str(job.get('description') or '')[:JD_WINDOW]
            job_title = job_title or job.get('title', '')
        jd_text = (jd_text or '').strip()[:JD_WINDOW]
        payload = {
            'gap_entries': entries_text,
            'resume': _resume_text(repo, job_id)[:RESUME_WINDOW],
            'target_jd': jd or jd_text,
            'job_title': (job_title or '').strip(),
        }
        from .model_transport import complete
        try:
            content, _usage = complete(get_purpose_config('analysis'), _SYSTEM,
                                       json.dumps(payload, ensure_ascii=False),
                                       temperature=0.4, max_tokens=4000, timeout=180)
            pitch, resume_block, interview_block = _split_narrative(content)
            origin = 'model'
        except Exception as exc:  # 模型抖动不阻断：退规则骨架并记录。
            logging.getLogger(__name__).warning('空窗叙事模型调用失败，退规则骨架：%s', exc)
    if not resume_block or not interview_block:
        pitch, resume_block, interview_block = _fallback(entries)
    entry_titles = [item['title'] for item in entries]
    return repo.save_gap_narrative(dict(
        origin=origin, job_id=job_id or '', job_title=(job_title or '').strip(),
        payload=dict(pitch=pitch, resume_block=resume_block, interview_block=interview_block,
                     entry_ids=[item['id'] for item in entries], entry_titles=entry_titles,
                     job_id=job_id or '', jd_text=(jd_text or '').strip()[:JD_WINDOW])))
