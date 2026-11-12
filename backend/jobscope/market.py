"""Extension market: authored curated catalog plus an opt-in online layer.

精选目录为本项目原创内容，不打包第三方 Skill；在线目录仅按用户手动刷新拉取公开
API，带缓存与显式失败回退，全部请求不带任何凭证。
"""
import re
import time

import httpx

from .extensions import parse_skill_markdown, skill_entry

MARKET_TTL = 900  # 在线目录缓存 15 分钟，与 orbit-tools 的做法一致。
SEARCH_TTL = 300
ONLINE_SKILL_LIMIT = 24
ONLINE_NPM_LIMIT = 20
SEARCH_RESULT_LIMIT = 12
SEARCH_SKILL_LIMIT = 8
FEATURED_REPOS = ('anthropics/skills', 'addyosmani/skills', 'mattpocock/skills', 'vercel-labs/skills')
_SEARCH_CACHE: dict[str, tuple[float, dict]] = {}

CURATED_MCP = [
    dict(id='cur-fetch', name='Fetch 网页抓取', icon='🌐', category='网页', official=True, transport='stdio',
         command='uvx', args=['mcp-server-fetch'], desc='抓取公开网页并转为 Markdown 供 Agent 阅读，适合查岗位说明与公司公告。启用并设为自动授权后，Agent 会在需要时主动读取网页核实外部信息。', repo='https://github.com/modelcontextprotocol/servers'),
    dict(id='cur-time', name='Time 时间时区', icon='🕐', category='工具', official=True, transport='stdio',
         command='uvx', args=['mcp-server-time'], desc='查询当前时间与时区换算，安排面试和截止日期时使用。', repo='https://github.com/modelcontextprotocol/servers'),
    dict(id='cur-memory', name='Memory 知识记忆', icon='🧠', category='工具', official=True, transport='stdio',
         command='npx', args=['-y', '@modelcontextprotocol/server-memory'], desc='以知识图谱保存长期记忆，跨对话记住求职进展。', repo='https://github.com/modelcontextprotocol/servers'),
    dict(id='cur-seq', name='Sequential Thinking 分步推理', icon='🧩', category='工具', official=True, transport='stdio',
         command='npx', args=['-y', '@modelcontextprotocol/server-sequential-thinking'], desc='引导模型按步骤推理复杂问题，适合准备案例面试。', repo='https://github.com/modelcontextprotocol/servers'),
    dict(id='cur-fs', name='Filesystem 文件访问', icon='📁', category='工具', official=True, transport='stdio',
         command='npx', args=['-y', '@modelcontextprotocol/server-filesystem', '在此填入允许访问的目录'], desc='读写指定目录内文件。安装后必须把最后一个参数改成你信任的目录，范围越窄越安全。', repo='https://github.com/modelcontextprotocol/servers', needs_config=True),
    dict(id='cur-brave', name='Brave Search 网页搜索', icon='🔎', category='检索', official=True, transport='stdio',
         command='npx', args=['-y', '@modelcontextprotocol/server-brave-search'], desc='联网搜索公开信息，需在环境变量中填入 BRAVE_API_KEY。启用并设为自动授权后，Agent 会在项目资料不足时主动联网核实公司与行业信息。', repo='https://github.com/modelcontextprotocol/servers', needs_key='BRAVE_API_KEY'),
    dict(id='cur-tavily', name='Tavily Search 深度搜索', icon='📡', category='检索', official=False, transport='stdio',
         command='npx', args=['-y', 'tavily-mcp'], desc='面向研究与摘要的搜索服务，需在环境变量中填入 TAVILY_API_KEY。启用并设为自动授权后，Agent 会在项目资料不足时主动联网检索并汇总来源。', repo='https://github.com/tavily-ai/tavily-mcp', needs_key='TAVILY_API_KEY'),
    dict(id='cur-context7', name='Context7 文档检索', icon='📚', category='检索', official=False, transport='http',
         url='https://mcp.context7.com/mcp', desc='查询各类框架与库的最新官方文档，无需密钥。', repo='https://github.com/upstash/context7'),
    dict(id='cur-deepwiki', name='DeepWiki 仓库问答', icon='📖', category='检索', official=False, transport='http',
         url='https://mcp.deepwiki.com/mcp', desc='对公开 GitHub 仓库提问，研究目标团队的技术栈时有用。', repo='https://deepwiki.com'),
    dict(id='cur-amap', name='高德地图', icon='🗺️', category='工具', official=True, transport='http',
         url='https://mcp.amap.com/mcp?key=在此填入你的Key', desc='查询通勤路线与周边配套。安装后把地址中的 Key 替换为高德开放平台申请的密钥。', repo='https://lbs.amap.com/api/mcp-server/summary', needs_config=True),
    dict(id='cur-12306', name='12306 车票查询', icon='🚄', category='工具', official=False, transport='stdio',
         command='npx', args=['-y', '12306-mcp'], desc='查询火车票余票与时刻，跨城面试安排行程时使用。', repo='https://github.com/Joooook/12306-mcp'),
    dict(id='cur-everything', name='Everything 演示服务', icon='🧪', category='演示', official=True, transport='stdio',
         command='uvx', args=['mcp-server-everything'], desc='官方演示服务，用于验证本机 MCP 链路是否可用，不含真实业务数据。', repo='https://github.com/modelcontextprotocol/servers'),
]

CURATED_SKILLS = [
    dict(id='cur-sk-interview', name='interview-coach', icon='🎯', category='面试', title='面试演练教练',
         desc='一次一问、按 STAR 结构给出反馈的模拟面试流程。',
         content='''---
name: interview-coach
description: 逐题模拟面试，按 STAR 结构反馈回答质量
category: 面试
icon: 🎯
keywords: [面试, STAR, 演练]
---
# 面试演练教练

流程：每轮只问一个问题，等用户回答后再继续，一次至少完成 5 轮。

1. 开场请用户给出目标岗位的 JD 或岗位要点，从中提炼 5–8 个高频考察点。
2. 按考察点逐题提问；行为类问题要求用户用情境作答。
3. 每次回答后按 STAR 反馈：情境是否具体、任务是否明确、动作是否有"我"的决策、结果是否有量化或验证。
4. 指出 1 个最大风险（如泛泛而谈、无结果）和 1 个追问方向，模拟面试官深挖。
5. 结束时输出总表：每题的考察点、回答评级（强/中/弱）、需补的事实或数字。

边界：只基于用户给出的材料评价，不虚构其经历；提醒用户面试中不得编造数据。'''),
    dict(id='cur-sk-jd', name='jd-decode', icon='🧭', category='求职', title='JD 逐条拆解',
         desc='把岗位描述拆成硬性条件、优先项与隐藏期待，并给出自查清单。',
         content='''---
name: jd-decode
description: 拆解岗位描述为硬性条件、优先项与隐藏期待
category: 求职
icon: 🧭
keywords: [JD, 岗位分析]
---
# JD 逐条拆解

输入：一段岗位描述（可含公司信息）。输出三张清单。

1. 硬性条件：学历、年限、语言、证书、必须技术栈。标注哪些是"申请人自述即可满足"。
2. 优先项：加分技能、行业背景。区分"明说优先"与" JD 未写但常识上加分"。
3. 隐藏期待：从措辞推断团队状态，如"快节奏"暗示加班、"从0到1"暗示体系不全。只作假设并说明依据。

最后给自查表：每条硬性条件逐项问用户是否有可验证的事实支撑；缺失项列入面试准备或投递风险评估。不要给综合匹配分，用户手里的工具才有评分规则。'''),
    dict(id='cur-sk-company', name='company-brief', icon='🏢', category='求职', title='公司研究简报',
         desc='围绕面试准备生成公司研究提纲与信息缺口清单。',
         content='''---
name: company-brief
description: 生成公司研究简报框架，标出已确认事实与信息缺口
category: 求职
icon: 🏢
keywords: [公司研究, 面试准备]
---
# 公司研究简报

输出固定五节：主营业务与收入结构、近一年公开事件、组织与产品线、行业位置、待核实问题。

规则：
- 只使用用户提供的材料或本轮实际检索到的内容；每条事实注明来源。
- 检索不到的条目写"未核实"，不猜测营收、融资或人数。
- 待核实问题一节列出 3–5 个值得在面试中向对方求证的问题。
- 平台上的企业自述（官网、公众号）标注为"企业自述"，与独立报道区分。'''),
    dict(id='cur-sk-salary', name='salary-talk', icon='💬', category='面试', title='谈薪准备',
         desc='整理薪酬谈判的目标区间、话术与让步底线。',
         content='''---
name: salary-talk
description: 准备薪酬谈话的目标区间、依据与应答话术
category: 面试
icon: 💬
keywords: [薪资, 谈判]
---
# 谈薪准备

输入：用户给出的当前/期望薪酬、招聘报价口径、城市与岗位。

1. 区分三种数字：招聘页报价（广告口径）、用户自述期望、实际发放（需证据）。三者不得混用。
2. 帮用户定三档：理想、可接受、底线；每档写一句依据（市场数据需注明来源与时效）。
3. 常见问句各配一段应答：期望薪资、当前薪资、其他 offer、为何值这个价。
4. 提醒：不主动虚报已验证可证伪的信息；总包（股权/加班/五险一金基数）比单月数字更真实。
5. 输出一页谈话要点，供面试前复习。'''),
    dict(id='cur-sk-learning', name='learning-plan', icon='📈', category='成长', title='技能补课计划',
         desc='按 JD 缺口生成周级学习计划与验收标准。',
         content='''---
name: learning-plan
description: 从岗位缺口出发制定周级学习计划，每项带验收标准
category: 成长
icon: 📈
keywords: [学习计划, 技能]
---
# 技能补课计划

输入：JD 要求清单与用户自评。输出周计划。

1. 缺口按"面试会被直接问到 / 上手才需要 / 短期补不了"分三类；只对前两类排计划。
2. 每周不超过 3 个主题，每个主题给出：目标、材料（文档或官方教程优先）、练习任务、验收标准（能独立完成的具体产物）。
3. 总长默认 4 周，可按用户可用时间缩放；写明每周预计小时数。
4. 结束时提醒：计划是自述意图，简历中仍只写可验证的事实。'''),
    dict(id='cur-sk-network', name='outreach-draft', icon='✉️', category='求职', title='内推破冰信',
         desc='起草给内推人或猎头的简短沟通信息，控制篇幅与事实。',
         content='''---
name: outreach-draft
description: 起草内推/猎头沟通的简短信息，只使用已确认事实
category: 求职
icon: ✉️
keywords: [内推, 沟通]
---
# 内推破冰信

输入：目标岗位、对方身份（内推人/猎头/HR）、用户亮点材料。

1. 信息控制在 120 字内（招聘平台私信）或 3 句话（邮件开头）。
2. 结构：一句说明来意 → 一句最强匹配事实（带数字）→ 一句具体请求。
3. 只使用用户确认过的事实；没有量化结果就不写数字，不编造。
4. 生成 2 版：正式版与更口语版，说明各自适用场景。
5. 提醒：群发痕迹明显的模板会降低回复率，发送前替换公司专名等占位符。'''),
    dict(id='cur-sk-resume-check', name='resume-fact-check', icon='✅', category='简历', title='简历事实自查',
         desc='对照材料逐条检查简历表述是否可支撑，标记风险句。',
         content='''---
name: resume-fact-check
description: 逐句检查简历表述是否有材料支撑并标记夸大风险
category: 简历
icon: ✅
keywords: [简历, 事实核查]
---
# 简历事实自查

输入：简历文本 + 用户可提供的佐证材料说明。

1. 逐条找出含数字、比例、排名、负责范围的句子，要求用户指出佐证；无佐证的标"风险"。
2. 检查程度词：精通/主导/从0到1 需要更强证据，否则建议降级为"熟悉/参与/搭建"。
3. 检查时间线冲突与角色重合。
4. 输出三栏表：原文、风险类型（无佐证/夸大/冲突）、建议改法。
5. 只改表述不改事实；提醒最终版本由用户确认后使用。'''),
    dict(id='cur-sk-retro', name='apply-retro', icon='🔁', category='成长', title='投递复盘',
         desc='按阶段复盘投递转化率，找出卡点并给出下一步动作。',
         content='''---
name: apply-retro
description: 按投递漏斗复盘各环节转化，定位卡点并给出改进动作
category: 成长
icon: 🔁
keywords: [复盘, 投递]
---
# 投递复盘

输入：用户报出的各阶段数字（投递、回复、初筛、面试、终面、offer）。

1. 画漏斗：每步转化率与行业常识区间对比（常识区间标注为经验值，非统计数据）。
2. 找最大卡点：简历通过率低看定位与关键词；面试通过率低看表达与事实储备。
3. 每个卡点给 1–2 个可执行动作与观察指标（如"下周改写 5 份定向简历，对比回复率"）。
4. 数字全部来自用户自述，结论不构成对结果的承诺。'''),
]

RAW_HOST = 'https://raw.githubusercontent.com'


def _client(timeout=10):
    return httpx.Client(timeout=timeout, trust_env=False, follow_redirects=True)


def refresh_online():
    """Fetch the online layer on explicit user action. Per-repo failures are
    skipped; only a total failure raises so the old cache survives."""
    online_skills, online_mcp, stars = [], [], {}
    with _client() as client:
        for repo in FEATURED_REPOS:
            try:
                tree = client.get(f'https://api.github.com/repos/{repo}/git/trees/HEAD', params={'recursive': '1'}).json()
                paths = [item['path'] for item in tree.get('tree', []) if item.get('type') == 'blob' and item['path'].endswith('/SKILL.md')]
                stars[repo] = client.get(f'https://api.github.com/repos/{repo}').json().get('stargazers_count')
            except Exception:
                continue
            for path in sorted(paths)[:ONLINE_SKILL_LIMIT]:
                try:
                    raw = client.get(f'{RAW_HOST}/{repo}/HEAD/{path}')
                    if raw.status_code != 200:
                        continue
                    content = raw.text[:60000]
                    name, description, _, _ = parse_skill_markdown(content)
                except Exception:
                    continue
                online_skills.append(dict(id='gh:' + repo + ':' + path, name=name[:80], desc=description[:200],
                                          icon='≡', category='在线', repo=repo, path=path, official=repo == 'anthropics/skills',
                                          stars=stars.get(repo)))
        try:
            search = client.get('https://registry.npmjs.org/-/v1/search', params={'text': 'mcp server', 'size': 25}).json()
            for package in search.get('objects', []):
                info = package.get('package', {})
                name = info.get('name', '')
                if 'mcp' not in name or len(online_mcp) >= ONLINE_NPM_LIMIT:
                    continue
                online_mcp.append(dict(id='npm:' + name, name=name[:80], icon='⬢', category='在线',
                                       desc=(info.get('description') or 'npm 上的 MCP 服务（描述缺失）')[:200],
                                       transport='stdio', command='npx', args=['-y', name],
                                       repo=(info.get('links') or {}).get('repository', '')))
        except Exception:
            pass
    if not online_skills and not online_mcp:
        raise ValueError('在线目录刷新失败：推荐技能仓库与 npm 检索都不可达。请稍后重试，精选目录不受影响。')
    return dict(skills=online_skills, mcp=online_mcp, fetched_at=time.time())


def payload(store):
    """Merged market view; skills carry no content here (install refetches)."""
    cached, fetched_at = store.market_cache()
    fresh = bool(cached and fetched_at and time.time() - fetched_at < MARKET_TTL)
    installed_skills = {item['name'] for item in store._all('agent_skills')}
    installed_mcp = {(item['name'], item.get('transport'), item.get('url') or item.get('command')) for item in store._all('agent_mcp')}
    curated_mcp = [dict(entry, installed=(entry['name'], entry['transport'], entry.get('url') or entry.get('command')) in installed_mcp) for entry in CURATED_MCP]
    curated_skills = [dict(entry, installed=entry['name'] in installed_skills) for entry in CURATED_SKILLS]
    return dict(curated_mcp=curated_mcp, curated_skills=curated_skills, online=cached if fresh else None, fetched_at=fetched_at)


def install_online_skill(store, repo, path):
    """Install one online skill by refetching its SKILL.md; content never sits in the cache."""
    # 仓库与路径都做严格格式校验；内容仍经 parse_skill_markdown 解析与限额。
    if not isinstance(repo, str) or not re.fullmatch(r'[\w.\-]+/[\w.\-]+', repo):
        raise ValueError('仓库标识无效')
    clean = str(path or '').strip()
    if not re.fullmatch(r'[\w./\-]{1,200}', clean) or '..' in clean or not (clean == 'SKILL.md' or clean.endswith('/SKILL.md')):
        raise ValueError('技能路径无效')
    with _client() as client:
        response = client.get(f'{RAW_HOST}/{repo}/HEAD/{clean}')
    if response.status_code != 200:
        raise ValueError('在线技能下载失败，请稍后重试')
    name, description, body, optional = parse_skill_markdown(response.text[:60000])
    entry = skill_entry(name, description, body, optional)
    entry['source'] = f'github:{repo}'
    return store._store_skill(entry)


def search_online(term: str):
    """Live market search: npm registry for MCP servers, GitHub repos for Skills."""
    term = (term or '').strip()
    if not term or len(term) > 50:
        raise ValueError('搜索词为空或超过 50 字符')
    key = term.lower()
    now_ts = time.time()
    cached = _SEARCH_CACHE.get(key)
    if cached and now_ts - cached[0] < SEARCH_TTL:
        return cached[1]
    results = []
    with _client() as client:
        try:
            search = client.get('https://registry.npmjs.org/-/v1/search', params={'text': f'{term} mcp', 'size': 20}).json()
        except Exception:
            raise ValueError('在线搜索失败：npm 检索不可达，请稍后重试。') from None
        for package in search.get('objects', []):
            info = package.get('package', {})
            name = info.get('name', '')
            desc = info.get('description') or ''
            if 'mcp' not in name.lower() and 'mcp' not in desc.lower() and 'model context protocol' not in desc.lower():
                continue
            if len(results) >= SEARCH_RESULT_LIMIT:
                break
            results.append(dict(id='npm:' + name, name=name[:80], icon='⬢', category='在线',
                                desc=desc[:200] or 'npm 上的 MCP 服务（描述缺失）',
                                transport='stdio', command='npx', args=['-y', name],
                                repo=(info.get('links') or {}).get('repository', '')))
        try:
            skills = _search_skills(client, term)
            skills_error = None
        except Exception:
            skills, skills_error = [], 'Skills 在线检索失败（GitHub 不可达或超出限额），请稍后重试。'
    payload = dict(term=term, mcp=results, skills=skills, skills_error=skills_error, fetched_at=now_ts)
    _SEARCH_CACHE[key] = (now_ts, payload)
    return payload


def _search_skills(client, term):
    """GitHub repo search (unauthenticated) → locate SKILL.md → fetch and parse."""
    found = client.get('https://api.github.com/search/repositories', params={'q': f'{term} skill in:name,description', 'sort': 'stars', 'per_page': 6}).json()
    needle = term.lower()
    entries: list[dict] = []
    for repo in found.get('items', [])[:4]:
        full = repo.get('full_name', '')
        if not full:
            continue
        try:
            tree = client.get(f'https://api.github.com/repos/{full}/git/trees/HEAD', params={'recursive': '1'}).json()
            paths = [item['path'] for item in tree.get('tree', []) if item.get('type') == 'blob' and item['path'].endswith('SKILL.md')]
        except Exception:
            continue
        # 根目录单技能仓库优先，其次路径中包含搜索词的条目。
        paths.sort(key=lambda path: (path != 'SKILL.md', needle not in path.lower()))
        for path in paths[:3]:
            if len(entries) >= SEARCH_SKILL_LIMIT:
                break
            try:
                raw = client.get(f'{RAW_HOST}/{full}/HEAD/{path}')
                if raw.status_code != 200:
                    continue
                name, description, _, _ = parse_skill_markdown(raw.text[:60000])
            except Exception:
                continue
            if any(item['name'] == name for item in entries):
                continue
            entries.append(dict(id='gh:' + full + ':' + path, name=name[:80], desc=description[:200],
                                icon='≡', category='在线搜索', repo=full, path=path, official=False,
                                stars=repo.get('stargazers_count')))
    return entries


def manifest():
    """Small catalog descriptor for tests and the frontend capability banner."""
    return dict(curated_mcp=len(CURATED_MCP), curated_skills=len(CURATED_SKILLS), featured_repos=list(FEATURED_REPOS), ttl=MARKET_TTL)
