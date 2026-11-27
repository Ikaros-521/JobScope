"""前端样式纪律守卫（2026-11-28）：两条规则历史反复回潮，用例钉死。

- 字号 12px 地板：38de68f（2026-11-20）清过一轮 11px，7f7db11（2026-11-23）又引入 11.5px——
  CSS 不走构建期校验，靠这里扫住。
- 幽灵令牌：--accent/--panel/--ink 等 8 个变量曾全站未定义却被引用 100 次，其中 4 处声明
  被浏览器静默丢弃；引用的 CSS 变量必须先有定义。
"""
from pathlib import Path
import re

CSS_FILES = ['styles.css', 'prepPlan.css', 'jobOps.css', 'chat/chatRecovery.css']
ROOT = Path(__file__).resolve().parents[2] / 'frontend' / 'src'


def test_font_size_floor():
    """字号 12px 地板（AGENTS.md 前端交互约定）：四个样式文件不允许出现 <12px 的 font-size。"""
    offenders = []
    for name in CSS_FILES:
        text = (ROOT / name).read_text(encoding='utf-8')
        for match in re.finditer(r'font-size:\s*(\d+(?:\.\d+)?)px', text):
            if float(match.group(1)) < 12:
                line = text[:match.start()].count('\n') + 1
                offenders.append(f'{name}:{line} font-size: {match.group(1)}px')
    assert not offenders, '字号低于 12px 地板：\n' + '\n'.join(offenders)


def test_no_ghost_css_tokens():
    """引用的 CSS 变量必须在任一样式文件里定义过；回退值只能作迁移期的临时护栏。"""
    all_css = '\n'.join((ROOT / name).read_text(encoding='utf-8') for name in CSS_FILES)
    defined = set(re.findall(r'(--[a-zA-Z0-9-]+)\s*:', all_css))
    used = set(re.findall(r'var\(\s*(--[a-zA-Z0-9-]+)', all_css))
    ghost = sorted(used - defined)
    assert not ghost, '引用了未定义的 CSS 令牌（声明会被浏览器静默丢弃）：\n' + '\n'.join(ghost)
