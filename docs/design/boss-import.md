# 设计定稿：Boss 岗位「捕获篮 + 一键收割」导入

> 状态：已确认（2026-11-15）。P0 = 链接粘贴 + 捕获当前页 + 捕获篮；P1 = 一键收藏导入 + 逛即采集常开；P2 = 油猴脚本。

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

### 通道三：链接批量粘贴（P0，兜底）
- 多行粘贴框：逐行/整段文本，正则抽 `job_detail/{encryptJobId}.html`。
- encryptJobId 缺失字段的占位岗位靠导入后「补全详情」（现有 `/api/browser/details` 支持 ids 反查）自愈。

## 5. 数据与接口

**数据**（不动表结构，payload JSON 内新增）：
- `imported_via: 'favorite' | 'current_page' | 'link'`
- `captured_from: <捕获时页面 URL>`、`captured_at: <ISO 时间>`
- 收藏导入 `starred=true, starred_at`。

**端点**：
```
GET  /api/browser/tabs            # 或并入 import 命名空间：列 zhipin 标签页
POST /api/import/boss/page        # {tab_id} → 抽卡入篮
POST /api/import/boss/favorites   # P1
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

## 9. 分期

| 期 | 内容 | 状态 |
|---|---|---|
| P0 | 链接粘贴 + 捕获当前页（按 URL 选 tab）+ 篮子（累积/去重/一键入库/可选加星）+ `/api/browser/start` 守卫修复 | 已实现（2026-11-15） |
| P1 | 一键收藏导入（个人中心自动发现入口 + 候选探测 + 滚动自动翻页）+ 逛即采集常开（listen 常驻化）+ 一键后处理（补全详情 / 公司研究）+ 公司快查页「在 Boss 打开」入口 | 已实现（2026-11-15） |
| P2 | 油猴脚本（推送任意本地 JobScope）、篮子持久化跨重启 | 已实现（2026-11-15） |

### P2 实现要点

- **篮子持久化**：`data_dir/boss_import_basket.json`，按数据目录分区（内存缓存 + 原子替换写盘），重启不丢、「清空篮子」才显式清除；所有测试路径显式传 tmp_path，绝不触碰生产 `.data` 的篮子文件。
- **油猴脚本**：`scripts/userscript/jobscope-capture.user.js`（安装说明见同目录 README）。任意浏览器的 Boss 页面右下角「→ JobScope」按钮，抽取逻辑与 `CARDS_JS` 同源，经 `GM_xmlhttpRequest` 推送 `POST /api/import/basket/remote`（油猴特权 API，不需要服务端 CORS）；服务端不信任客户端——id/URL 一律重建为 canonical 无参链接、文本过 `plain()` 截断、`page_url` 限 zhipin.com，否则整包 400。

### P1 首跑校准点

- **收藏页入口**：URL 未被公开索引（登录态页面）。实现为「个人中心 `web/user/` 页 JS 发现含「收藏/感兴趣」的链接 → 候选路径探测（`FAVORITE_CANDIDATES`）→ 都失败引导手动打开收藏页 + 捕获当前页」。首跑确认真实入口后，把实测 URL 补进 `FAVORITE_CANDIDATES` 并上移到首位。
- **收藏页翻页方式**：当前按无限滚动处理（滚动到底触发加载）；若实际是按钮翻页，自动翻页会在第 1 页后停下（消息明示），可手动翻页后重跑（篮子幂等合并）。
- **逛即采集**：只接住 `wapi/zpgeek/search/joblist.json`，字段映射直接复用 `connectors.map_boss`；登录失效（code 7/8/9）只更新提示不产垃圾条目。
