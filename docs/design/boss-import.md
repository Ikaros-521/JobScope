# 设计定稿：Boss 岗位「捕获篮 + 一键收割」导入

> 状态：已确认（2026-11-15）。P0 = 链接粘贴 + 捕获当前页 + 捕获篮；P1 = 一键收藏导入 + 逛即采集常开；P2 = 油猴脚本。
> 2026-11-23 新增第四通道「投递记录同步」（读 Boss「已投递」回填管线状态，带真实投递时刻），见 §4 通道四与 §10。

## 1. 问题定义

两条不同的需求线：

- **A. 收藏导入**：账号私有数据，搜索入口拿不到，必须走用户登录态读取。
- **B. 按公司提取岗位**：公开数据，但 Boss 默认"最近"排序 = GPS + 个性化聚合，JobScope 只按城市码搜索，两个结果空间天然不一致。

**结论**：不模拟定位、不仿造请求。项目浏览器（DrissionPage 持久 profile，`.data/browser/profile`）已保持用户登录态，把方向反过来——**用户在项目浏览器里看到的 Boss 页面就是导入源，所见即所得**。

## 2. 设计原则

1. **只读不仿**：导入器只读已渲染 DOM / 接住页面自己发的请求（listen），不代替用户发搜索请求。
2. **导入 ≠ 采集完成**：导入建立 listing 级占位，完整 JD 与公司尽调复用现有管线（`job_details.run_batch`、`/api/company-research`）。
3. **幂等可重放**：`source='boss' + encryptJobId` 生成岗位主键（repository 现有逻辑），重复导入、翻页累积零脏数据；收藏与搜索采集自动归并同一条岗位。
4. **校准文化**：Boss DOM 选择器首跑校准（快照到 `output/`），解析失败降级为链接粘贴兜底，不硬编码赌选择器。

## 3. 交互形态：「Boss 导入中心」面板

```
┌─ Boss 导入中心 ────────────────┐
│ ● 项目浏览器已连接 · Boss 已登录 │
│ [导入收藏(一键)]  [捕获当前页 ▾] │  ← ▾ 列出当前 zhipin 标签页
│ 🧺 捕获篮: 47 个 (12 已在库)     │
│ [查看篮子] [一键入库] [清空]     │
│ ☑ 逛即采集  ☑ 收藏自动加星      │
│ [粘贴链接…]                     │
└────────────────────────────────┘
```

- 三通道（收藏 / 当前页 / 链接粘贴）产出同一种 basket item，确认后走同一套落库与后处理。
- 会话状态直读：浏览器未开 → 「启动项目浏览器」；Boss 未登录 → needs_login 引导（沿用现有文案模式）。

## 4. 通道设计

### 通道一：捕获当前页（P0）
- `get_tabs()` 按 URL 过滤 zhipin.com 标签页，用户选 tab → `run_js` 抽岗位卡（`a[href*="job_detail/"]` 上溯卡片容器）。
- 捕获累积式：翻页再捕获，按 encryptJobId 合并去重。
- 公司研究页联动入口「在 Boss 打开该公司」（P1）。

### 通道二：收藏夹导入（P1）
- 一键：接管项目浏览器开收藏页 → 自动翻页（5s±随机、上限 10 页、验证码走 gsxt 同款人工等待）→ 自动加星入库。
- 优先探测 in-page fetch 收藏列表接口（带用户 cookie），DOM 抽卡兜底；收藏页 URL/选择器首跑校准。
- 自动加星为默认（Boss 收藏 = 表达兴趣；save_job 已保护 starred 幸存）。

### 通道四：投递记录同步（2026-11-23）
- 目标：Boss 自己记着「简历已发送」，据此把库里的岗位标成「已投递」。后续的面试 / Offer 不由平台判定——
  有的面试是电话约的，没走平台；平台数据只到「发了简历」这一步，状态合并必须只升不降。
- 数据源：被动接住页面自己请求的 `wapi/zprelation/resume/geekDeliverList?page=N`（`zpData.cardList` + `hasMore`），
  与逛即采集同一纪律（不代发请求）。选接口而不是 DOM：接口带平台记录的投递时刻 `happenTime`（毫秒，
  实测与页面 `actionDateDesc` 逐条吻合），卡片 DOM 里没有时间；而漏斗的推进天数 / 投递批次 / 周几投递
  都按事件时间算。接不到响应时退回 DOM 抽卡（时刻未知，按同步时间记）。
- 落库分两路：库内岗位直接走 `repository.sync_applied`（不经捕获篮——既成事实不需要再确认）；
  不在库的进捕获篮（`imported_via='applied'`，带 `applied_at`），确认入库时落库并回填「已投递」。
- 防污染闸：个人中心默认停在上次看的页签，深链一旦失效页面上可能是收藏 / 沟通过列表。所以每次导航后
  按 `ka` 把页面钉到「已投递」页签；DOM 兜底只在确认当前页签是「已投递」时才采信，否则同步失败并明说。
- 截断如实报告：接口说还有下一页而本轮没读完（页数上限 / 翻页不生效 / URL 不能翻页）时，结果里明示
  「仅读取了前 N 页，Boss 上还有更多投递记录未同步」。

### 通道三：链接批量粘贴（P0，兜底）
- 多行粘贴框：逐行/整段文本，正则抽 `job_detail/{encryptJobId}.html`。
- encryptJobId 缺失字段的占位岗位靠导入后「补全详情」（现有 `/api/browser/details` 支持 ids 反查）自愈。

## 5. 数据与接口

**数据**（不动表结构，payload JSON 内新增）：
- `imported_via: 'favorite' | 'current_page' | 'link' | 'browse' | 'script' | 'applied'`
- `captured_from: <捕获时页面 URL>`、`captured_at: <ISO 时间>`
- 收藏导入 `starred=true, starred_at`。
- 投递记录通道：篮子条目与岗位 payload 带 `applied_at`（Boss 记录的投递时刻，ISO/UTC）与
  `applied_source='boss'`；两者与 `pipeline_track`、收藏、管线状态一起在重新采集 / 合并重复项时幸存。
  事件流按 `sync_applied` 的规则回溯（见 §10）。

**端点**：
```
GET  /api/browser/tabs            # 或并入 import 命名空间：列 zhipin 标签页
POST /api/import/boss/page        # {tab_id} → 抽卡入篮
POST /api/import/boss/favorites   # P1
POST /api/import/boss/applied     # 投递记录同步（后台任务，轮询 /tasks/{id}）
POST /api/import/links            # {text} → 解析入篮
GET  /api/import/basket           # 篮子内容（含 in_library 标记）
POST /api/import/basket/clear
POST /api/import/confirm          # {auto_star, ids?} → 批量落库
```

**后处理**：完成页/面板提供「补全详情」（复用 `/api/browser/details`）与「对新增公司跑研究」（复用 `/api/company-research`）。

## 6. 复用件与挂点（代码事实）

- DrissionPage 附加会话：`browser.py` `_connection()` / `session_lock()` / `start()`；`run_js` 读 DOM 先例：`company_lookup.py` CARDS_JS、`job_details.py` DETAIL_SNAPSHOT。
- 字段映射：`connectors.map_boss`（wapi JSON 源）；DOM 卡片需独立 mapper，encryptJobId 从 href 正则提取。
- 落库：`repository.save_job`（`J-` + sha256(source:key)[:24]，ON CONFLICT 幂等；starred/用户补录 JD 幸存）。
- 任务化：`/api/browser/collect` 的后台线程 + tasks 轮询模式；全程遵守单浏览器任务锁，占用返回 backoff。
- 前端挂载：App.tsx 岗位页操作区 `job-entry-actions`；JobCard 来源表无需改（source 仍为 'boss'）。

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| 风控/封号 | 只读 DOM / 接页面自身请求；自动翻页慢节奏且限页；验证码暂停等人工 |
| 登录态失效 | needs_login 文案引导在项目浏览器登录（现有模式） |
| DOM 变更 | 首跑快照校准 `output/`；0 结果明确报"页面结构可能变化"并指引用链接粘贴 |
| 链接失效 | 篮子标 ⚠️ 疑似失效；补全详情失败落 needs-attention，不阻塞批次 |
| 浏览器任务冲突 | 全程走 `session_lock`，占用即 backoff |

## 8. 验收标准

- 「打开项目浏览器中的 Boss」→「10 条收藏岗位入库加星」≤ 2 分钟（首次登录除外）。
- 当前页导入结果与用户所见逐条一致（排序、筛选不丢失）。
- 同一批岗位重复导入 3 次，库中 0 重复；starred 与用户补录 JD 不丢失。
- 投递记录同步：连续同步 3 次，库内状态与事件数不变（幂等）；已是面试中 / Offer / 归档的岗位状态不动；
  页签无法确认时同步失败且一条数据都不落（宁可失败，不误标）。

## 9. 分期

| 期 | 内容 | 状态 |
|---|---|---|
| P0 | 链接粘贴 + 捕获当前页（按 URL 选 tab）+ 篮子（累积/去重/一键入库/可选加星）+ `/api/browser/start` 守卫修复 | 已实现（2026-11-15） |
| P1 | 一键收藏导入（个人中心自动发现入口 + 候选探测 + 滚动自动翻页）+ 逛即采集常开（listen 常驻化）+ 一键后处理（补全详情 / 公司研究）+ 公司快查页「在 Boss 打开」入口 | 已实现（2026-11-15） |
| P2 | 油猴脚本（推送任意本地 JobScope）、篮子持久化跨重启 | 已实现（2026-11-15） |
| — | 投递记录同步：「已投递」接口响应 → 回填管线状态与真实投递时刻 + 防污染闸 + 截断提示；收藏导入兜底改「个人中心点页签」 | 已实现（2026-11-23/24） |

### P2 实现要点

- **篮子持久化**：`data_dir/boss_import_basket.json`，按数据目录分区（内存缓存 + 原子替换写盘），重启不丢、「清空篮子」才显式清除；所有测试路径显式传 tmp_path，绝不触碰生产 `.data` 的篮子文件。
- **油猴脚本**：`scripts/userscript/jobscope-capture.user.js`（安装说明见同目录 README）。任意浏览器的 Boss 页面右下角「→ JobScope」按钮，抽取逻辑与 `CARDS_JS` 同源，经 `GM_xmlhttpRequest` 推送 `POST /api/import/basket/remote`（油猴特权 API，不需要服务端 CORS）；服务端不信任客户端——id/URL 一律重建为 canonical 无参链接、文本过 `plain()` 截断、`page_url` 限 zhipin.com，否则整包 400。

### P1 首跑校准点

- **收藏页入口**（已校准 2026-11-20）：实测为推荐中心第 4 个页签 `https://www.zhipin.com/web/geek/recommend?tab=4&sub=1&page=1&tag=4`，已固化为 `FAVORITE_PRIMARY` 直进；打不开仍回退「个人中心 `web/user/` 页 JS 发现含「收藏/感兴趣」的链接 → 历史候选探测（`FAVORITE_CANDIDATES`）→ 都失败引导手动打开收藏页 + 捕获当前页」。入口再改版时把实测 URL 换进 `FAVORITE_PRIMARY` 即可。
- **收藏页翻页方式**（已校准 2026-11-20）：URL `page` 参数翻页。实现为 `_page_url` 逐页打开（2-4s 随机间隔，上限 10 页）；URL 不带 page 参数的列表页退回滚动加载。翻到空页（无新岗位）自动收停，消息明示页数。
- **收藏页结构与 SPA 渲染**（已校准 2026-11-20）：收藏卡是 `li.item-boss`，干净字段在 `.job-name-text`（岗位名）、`.company-info b`（公司名）、`.location`（地点）；薪资在 `p.gray`（与公司信息行同名，类名抓不到），用文本 K 型正则兜底（「1000-9999人」不带 K 不误中）。右侧 `sider-recommend` 平台推送栏必须整栏排除——它的卡片是异形 `<a>` 包 `<li>`，旧上溯会把整栏串成一张卡、把平台推荐岗混进收藏（数据污染）。Boss 是 SPA，导航完成后卡片约 2-3s 才异步渲染，探针与翻页统一走 `_snapshot_cards` 轮询（~8s 上限），一进页面就读 DOM 只会拿到空。`closest('li')` 拿到容器后绝不能再上溯，否则第一张卡的字段错配给所有卡。
- **详情顺带读取与去重**（2026-11-20）：导入在列表捕获后新开专 tab 逐条走 `job_details.read_detail_on_tab`（快照/校验/登录墙/下架识别与「补全详情」同一通路），完整 JD 随捕获进篮；确认入库时公司自述经 `persist_company_statement` 落证据与规模信号（与补全详情同源）。去重两道闸：库里已有完整详情（browser_dom）的岗位整条跳过不入篮；篮子里已带完整详情的（上次中断遗留）跳过详情读取——收藏越攒越多，重复导入不再重复提取。下架岗位保留列表摘要并计数。
- **逛即采集**：只接住 `wapi/zpgeek/search/joblist.json`，字段映射直接复用 `connectors.map_boss`；登录失效（code 7/8/9）只更新提示不产垃圾条目。
- **「已投递」入口与数据源**（已校准 2026-11-23）：直达 `https://www.zhipin.com/web/geek/recommend?tab=2&page=1&tag=1`。列表由页面自己请求 `wapi/zprelation/resume/geekDeliverList?page=N` 取回，`zpData = {hasMore, cardList}`；每条含 `encryptJobId`、`jobName`、`brandName`、`salaryDesc`、`unifyLocationDetail`、`jobLabels`，以及投递时刻 `happenTime`（毫秒时间戳）与 `actionDateDesc`（如「11月20日 19:15」「今天17:51」）。卡片 DOM 与收藏卡同为 `li.item-boss`，**没有时间文本**，右侧 `sider-recommend` 推送栏照旧要排除。只落岗位字段，不落 HR 姓名。翻页：URL `page` 参数 + `hasMore` 收停（账号不足一页时翻页未被真实数据验证过，失效时按「截断」如实报告）。
- **个人中心页签机制**（受控实验校准 2026-11-24，`output/diag_personal_tab_state.py`）：个人中心 = `recommend?tab=2`，顶部页签是 JS 切换的 `span`（`ka=personal_top_added` 沟通过 / `personal_top_submitted` 已投递 / `personal_top_interview` 面试 / `personal_top_interest` 感兴趣），不是链接，点击页签 URL 未必跟着变。URL 的 `tag` 只对部分页签有效：`tag=1` → 已投递是可靠深链（实测能覆盖「上次停留的页签」）；`tag=2` / `tag=4` 不切页签，落到上次停留的那个——**不能当深链用**。点「感兴趣」页签会跳到 `tab=4&sub=1&tag=4`（即 `FAVORITE_PRIMARY`）。旧个人中心地址 `/web/user/` 现已跳首页（旧兜底因此一直是死路）。结论：凡依赖页签的读取，一律按 `ka` 点选并校验当前页签（`_select_personal_tab`），不赌 URL。

## 10. 投递记录同步的状态合并规则（`repository.sync_applied`）

输入 `[(job_id, applied_at)]`，`applied_at` 是平台记录的投递时刻（空串 = 未知）。**状态只升不降、时间以平台为准**：

| 库内当前状态 | 状态位 | 「已投递」事件 | 说明 |
|---|---|---|---|
| 无状态 / 意向 | → 已投递（自动收藏） | 新落一条，时间 = 投递时刻 | 状态变化时间也记投递时刻：跟进提醒按「投递了几天」算，不按「哪天导入的」算 |
| 已投递 | 不动 | 没有就补；有且平台给了时刻 → 首条对齐平台时刻 | 纠正手工标记的时间；平台没给时刻则不改写已有事件 |
| 面试中 / Offer | 不动 | 补一条回溯事件 | 平台数据只到「发了简历」，用户手工推进的后续阶段是更晚的事实；补事件后漏斗分母与推进周期才算得到这条岗位 |
| 归档 | 不动 | 补一条回溯事件 | 尊重用户的归档决定，只把「投过」记进事件流 |

- 不走 `save_job`（那是整包覆盖）：状态回填只改状态位，档案正文、跟进信息、求职线与证据快照原样保留。
- 未知 / 非法 / 未来的投递时刻按「现在」记并标为未知：只用于新落的事件，绝不拿「现在」改写已有事件。
- 幂等：同一批记录重复同步，事件数不变。岗位 payload 记 `applied_at` / `applied_source`。
- 已知限制：只按 Boss `encryptJobId` 匹配库内岗位；同一岗位若曾以「粘贴 JD」等方式入档（没有 encryptJobId），
  会被当作「不在库」进捕获篮，确认后是两条档案，需再走重复项合并。
- 后续可扩展（未做）：个人中心还有「面试」页签（`personal_top_interview`），平台内约的面试可同样读取回填「面试中」；
  电话约的面试平台不知道，仍需手工标记。

## 11. 定时同步（2026-11-24，`sync_schedule.py`）

用户提出「省得每次手动同步」。同步动作本身不变——定时入口与收藏页 / 导入面板的按钮共用 `import_applied`
（任务记录、状态合并规则、防污染闸完全一致）；`sync_schedule` 模块只管「什么时候跑」。
入口在 **设置中心 → 系统设置 → 「Boss 投递自动同步」**：开关 + 周期 + 时间点，改动即存，并显示下次 / 上次自动同步。

**调度语义**（全部按本机本地时间）：

| 规则 | 说明 |
|---|---|
| 周期两类 | 按小时：每 6 / 12 小时，距上次尝试满 N 小时即到点。按天：每天 / 每 2 / 3 天 / 每周，落在固定时间点（网格以开启当天为基准） |
| 开启 / 改设置不立即跑 | 保存即把基准记为现在：开启当天的时间点若已过，从下个周期起算，不会一开就拉起浏览器 |
| 错过的补跑一次 | 应用没开 / 睡眠错过的槽位，恢复后的第一次检查只跑一次，之后回到网格（不会连补好几次） |
| 一次尝试占一个周期 | 成功、需要登录、失败都算，下个周期才再试——登录失效的窗口留给用户处理，不反复弹 |
| 浏览器正忙例外 | `backoff` 不算尝试：5 分钟后重试，连续 3 次仍忙则放弃本周期 |
| 只在应用运行时生效 | 线程随进程生死；启动 60 秒后做第一次检查 |

**实现要点**：
- 存储：`sync_schedule` 键值表（`settings` 用户可改，`state` 只由调度器写），随数据库一起进备份。
- 到点判定是纯函数（`upcoming` / `is_due` / `pending_slot`），界面显示的「下次同步」与调度器用的是同一份计算。
- `SyncScheduler`：守护线程每 60 秒检查一次，同一时刻至多一个同步；检查本身出错只记日志不拖垮线程。
- 端点：`GET/PUT /api/settings/applied-sync`；`register(app, repo, data_dir)` 一行挂到 `create_app`，线程随应用启停。
- 测试总闸：`JOBSCOPE_DISABLE_AUTO_SYNC=1` 时不起线程（端点照常可用），`tests/conftest.py` 全局设置——
  任何用例打开开关都不会真去拉浏览器。
- 冒烟：`scripts/smoke_boss_applied_sync_schedule.py`（打 8766 的真实设置端点，结束前还原为关闭；
  `--static <临时构建目录>` 可在未上线时把静态资源指到临时构建，不碰 `frontend/dist`）。

**已知边界**：到点时项目浏览器没开会自动打开一个 Chrome 窗口（与手动按钮同一行为）；应用不在运行时不会同步；
首次补跑可能发生在刚启动 JobScope 之后的一分钟内。
