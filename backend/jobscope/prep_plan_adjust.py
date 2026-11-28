"""备战计划的对话式调整层（2026-11-24 第四轮）：用户对计划不满意可以直接说，教练改。

职责切分与生成管线同款：模型只负责「听懂意图 + 回复 + 产出结构化修改指令」，
指令落库、护栏校验、重排确认全部在规则层（prep_plan.apply_adjustments / run 层）。
回复文本与 ===CHANGES=== 标记后的 JSON 指令共用一次流式调用：标记前的文本边
生成边推给前端，指令段对用户隐藏，解析失败不阻断回复显示（只是不改计划）。

多轮上下文挂在计划自己的 conversation 上（prep_plans.conversation_id），
「再轻一点」这类递进说法由此可用。整轮重排（rebuild）不直接执行：run 层推
confirm_required，用户在确认框里点头后才走强制重新生成。
"""
import json
import logging
import re

logger = logging.getLogger(__name__)

MARKER = '===CHANGES==='
REPLY_MAX = 3000
MESSAGE_MAX = 2000
HISTORY_TURNS = 8  # 最近 8 条消息（4 轮）进上下文
TASK_LIST_MAX = 6000
HISTORY_MAX = 3000

_SYSTEM = '''你是求职备战计划教练。用户已有一份每日备战计划，现在想调整它。你会收到计划的任务清单和用户的要求。

要求：
1. 先用 1–3 句中文回应用户（确认改了什么、或解释为什么这样改最合适），语气友好直接，不要客套堆砌。
2. 然后另起一行输出 ===CHANGES=== 标记，标记后输出一个 JSON 数组，每项一条修改指令：
   {"action":"set_prefs","minutes_per_day":10,"goal":"...","job_id":"J-id"}  改每日预算/备战重点/目标岗位（job_id 只能原样引用清单里给的岗位，没有就留空）
   {"action":"edit_task","task_id":"T-xxx","title":"...","detail":"...","minutes":15,"kind":"drill","required":0}  改任务（字段可选）
   {"action":"remove_task","task_id":"T-xxx"}  删任务
   {"action":"add_task","day_index":3,"kind":"drill","title":"...","detail":"...","minutes":15,"required":0}  加任务（day_index 是计划内第几天）
   {"action":"rebuild","reason":"..."}  整轮按最新资料重新生成（必须单独使用，不要和其他指令混在同一条消息）
3. 只有 status 为 pending 的任务才能编辑/删除；task_id 必须原样取自任务清单，绝不编造。
4. 用户没有表达整体不满意就不要 rebuild；小调整直接改任务。每天仍要保留一场模拟面试任务（mock）。
5. 用户只是抱怨而没有明确要求时，给出建议并输出空数组 []。
6. 标记后的 JSON 之外不要输出任何代码块、解释或多余文本。'''


def material(repo, plan, message, history=None):
    """给模型的紧凑上下文：计划头 + 全部任务清单（含 id/status）+ 最近对话轮。"""
    from .repository import business_today, now
    prefs = plan.get('prefs') or {}
    lines = []
    for task in repo.prep_tasks(plan['id']):
        flag = '必做' if task['required'] else '选做'
        lines.append(f"{task['id']} 第{task['day_index']}天({task['date']}) {task['kind']} "
                     f"「{task['title']}」约{task['minutes']}分钟 {flag} [{task['status']}]")
    turns = []
    for item in (history or [])[-HISTORY_TURNS:]:
        role = '用户' if item.get('role') == 'user' else '教练'
        turns.append(f"{role}: {str(item.get('content') or '')[:400]}")
    return dict(
        today=business_today(),
        plan=f"{plan['days']} 天（{plan['start_date']} 至 {plan['end_date']}），每日预算 "
             f"{prefs.get('minutes_per_day', 20)} 分钟，备战重点：{prefs.get('goal') or '未填'}",
        tasks='\n'.join(lines)[:TASK_LIST_MAX],
        history='\n'.join(turns)[:HISTORY_MAX],
        message=str(message or '')[:MESSAGE_MAX],
    )


def parse_changes(content):
    """按标记拆回复与指令；指令段解析失败时返回空数组，不阻断回复显示。"""
    content = str(content or '')
    if MARKER not in content:
        return content.strip()[:REPLY_MAX], []
    reply, _, tail = content.partition(MARKER)
    changes = []
    match = re.search(r'\[.*\]', tail, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, list):
                changes = [row for row in data if isinstance(row, dict)][:10]
        except (json.JSONDecodeError, TypeError):
            logger.warning('备战计划调整指令解析失败：%s', tail[:200])
    return reply.strip()[:REPLY_MAX], changes


def adjust(repo, plan, message, history=None, on_delta=None):
    """一次流式调用拿回复与指令；模型未配置/请求失败抛 ValueError（对话需要把错误告诉用户）。"""
    from .model_transport import output_budget, stream_complete
    from .settings import get_purpose_config, purpose_configured

    if not purpose_configured('analysis'):
        raise ValueError('调整对话需要先在设置中心绑定分析模型')
    config = get_purpose_config('analysis')
    user = json.dumps(material(repo, plan, message, history), ensure_ascii=False)
    reply, _ = stream_complete(config, _SYSTEM, user, on_delta=on_delta,
                               temperature=.4, max_tokens=output_budget(config, 4000), timeout=180)
    return parse_changes(reply)
