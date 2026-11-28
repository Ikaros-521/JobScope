"""生存跑道推导（2026-11-22 P2）：月支出/存款/社保停缴/失业金状态 → 决策参考值。

长期求职者比较 Offer 的真实约束是现金流——「这个 offer 能不能接」往往是
「跑道还剩几个月」。本模块做纯本地推导，无 LLM：

隐私三条硬底线（与用户约定，改任何逻辑都不能破）：
1. 只存本机 runway_profile 单行表，绝无同步/上报路径；
2. **绝不进模型素材池**——不进 profile_entries、不进工坊取材、不进评估事实池、
   不出现在任何 prompt 组装路径里（新增素材入口时必须绕开本表）；
3. 跨页展示只给推导值（跑道月数/断缴月数），金额原值只在本页可见。

政策类提示全部措辞从宽（「一般」「以当地人社部门为准」），不做保证、不做催办。
"""
import re

from .repository import business_today, now

_MONTH_RE = re.compile(r'^(\d{4})-(\d{2})$')

CLAIM_HINTS = {
    'not_claimed': '尚未申领：失业金一般要求失业前缴满约 1 年、且非因本人意愿中断就业，有申领时限，越早办越稳——以当地人社部门口径为准。',
    'claimed': '已在领取：留意按当地要求做月度资格认证，避免停发。',
}

MEDICAL_HINT = '断缴期间医保待遇可能与在缴时不同；可了解灵活就业自行参保的口径——以当地政策为准。'


def _paused_months(last_month):
    """社保最后缴纳月（YYYY-MM）到今天的整月数；同月/未来月算 0，无数据 None。"""
    match = _MONTH_RE.match(str(last_month or ''))
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    from datetime import date
    today = date.fromisoformat(business_today())  # 东八区业务日，与全站「今天」同口径
    months = (today.year - year) * 12 + (today.month - month)
    return max(0, months)


def derive(profile):
    """单行档案 → 推导值。缺什么少什么，绝不猜数。"""
    profile = profile or {}
    expense = profile.get('monthly_expense')
    savings = profile.get('savings')
    runway = round(savings / expense, 1) if expense and expense > 0 and savings is not None else None
    paused = _paused_months(profile.get('social_security_last_month'))
    claim = profile.get('unemployment_claim') or ''
    return {
        'runway_months': runway,
        'paused_months': paused,
        'claim_hint': CLAIM_HINTS.get(claim, ''),
        'medical_hint': MEDICAL_HINT if paused and paused >= 1 else '',
    }


def clean_profile(data):
    """PUT 入参 → 存储形态：白名单字段 + 归一化（空串/None 归 None，负数拒绝在模型层）。"""
    return {
        'monthly_expense': data.get('monthly_expense') if data.get('monthly_expense') is not None else None,
        'savings': data.get('savings') if data.get('savings') is not None else None,
        'social_security_last_month': str(data.get('social_security_last_month') or '').strip(),
        'unemployment_claim': data.get('unemployment_claim') or '',
        'notes': str(data.get('notes') or '').strip()[:500],
    }
