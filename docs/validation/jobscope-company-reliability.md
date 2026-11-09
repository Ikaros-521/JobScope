# JobScope 公司求职参考分析验证记录

日期：2026-11-09（Asia/Shanghai）。本文只记录**当前机器上实际发生过**的运行与观察，没做过的逐项写在「未验证」里，不做任何承诺。功能设计见 `docs/proposals/2026-11-09-company-reliability-design.md`，计划与实施修正记录见 `docs/plans/2026-11-09-company-reliability.md`。

## 环境

| 项 | 实测值 | 取法 |
| --- | --- | --- |
| OS | Microsoft Windows 10 专业版 | `Get-CimInstance Win32_OperatingSystem` |
| Python | 3.12.9（系统解释器，无独立 venv） | `python --version` |
| Node / npm | v22.23.3 / 11.16.0（`frontend/node_modules` 已安装） | `node --version`、`npm --version` |
| 浏览器采集依赖 | **未安装**：`import DrissionPage` → `ModuleNotFoundError`；`browser.status()` 返回 `installed=False`，提示需安装 Google Chrome 与可选依赖 | 本次运行 |
| Chrome | 标准安装路径下未找到 `chrome.exe`，因此版本号未取到 | 本次运行 |

最后一行直接决定了本次的性质：**官方公示页通路在这台机器上无法实测**，不是实测失败，是根本没有可跑的浏览器会话。

## 已验证

### 后端测试（本次 Task 12 运行）

```text
python -m pytest backend/tests -q -k "reliability"   # 171 passed, 224 deselected in 28.58s
python -m pytest backend/tests/test_reliability_boundaries.py backend/tests/test_reliability_api.py -q
                                                    # 39 passed in 13.78s（新增 14 + 原 API 25）
```

全量后端 `381 passed`（本分支起点基线 224 passed）由 HEAD `9b98099` 那一轮全量运行得到；Task 12 按分工**没有**重跑全量，只跑了 reliability 子集，全量由本次会话最后一次统一运行确认。本次 `-k reliability` 那一轮的收集数是 171 + 224 = 395，比 381 多的正好是新增的 14 条边界用例，而 224 条非 reliability 用例数与本功能开始前一致，说明这次改动没有触碰其他模块的用例（这只是收集数核对，不是全量通过结论）。

新增的 `backend/tests/test_reliability_boundaries.py` 把红线钉成 14 条断言组，本次全部为绿；红线被人为破坏时确认会变红（下表后两条是变异实验，不是产品行为）：

| 红线 | 由哪条测试钉住 |
| --- | --- |
| 路由表里没有 `share`/`rank`/`public`/榜单类路径，本功能对外只有那五个路径与 GET/POST/PUT 动词 | `test_app_exposes_no_sharing_ranking_or_public_route`、`test_reliability_surface_is_exactly_the_five_local_paths` |
| 每一份报告（含库里所有历史行）都带 `nature='subjective_reference'`、非空 `disclaimer`、`disclaimer_version` 与非空 `limitations`，并带三个版本号 | `test_every_report_is_marked_subjective_reference` |
| 设置过的一次性密钥原文不出现在那五个路径上的 9 次响应（两个付费来源各一次 PUT、来源列表 GET、三种 transport 各一次 collect、extract、重算 POST、读取 GET），也不出现在 `/api/evidence`、`/api/tasks`、`/api/companies` 的响应里，还不落进 `evidence`/`company_signals`/`company_reliability`/`tasks` 四张表的 payload | `test_no_reliability_response_echoes_the_configured_secret` |
| 所有付费来源默认 `disabled`、所有免费来源默认 `enabled`，登记表里不许写死 `mode`，付费来源必须走 `http_api` | `test_paid_sources_start_disabled_and_free_ones_enabled` |
| 每条 finding 都有非空 `evidence_id`；`kind=='material'` 的 finding `score` 必须是 `None`（序列化后是 `"score":null`），单条 material 规则独算时该维度不出分 | `test_every_finding_cites_evidence_and_material_never_shows_a_number` |
| 分数只能来自 rubric：`reliability/` 内除 `extract.py` 外任何模块不得导入模型层；`rubric`/`wording` 不导入外部世界；`RuleFinding` 只能由 rubric 构造 | `test_only_the_extractor_may_reach_the_model_layer`、`test_rubric_and_wording_cannot_reach_the_outside_world`、`test_only_the_rubric_can_mint_a_scored_finding` |
| 抽取契约禁止 `score`/`grade`/`rating`/`verdict` 字段，提示词里写明分数不归模型；把模型层全部改成「一调用就抛」之后重算，数字仍与 `rubric.score()` 逐维度一致 | `test_the_extractor_contract_has_no_room_for_a_verdict`、`test_scores_are_reproducible_with_the_model_layer_refusing_to_answer` |
| 禁用词表对每条在册规则都触不到：6 条模板 × 19 组极端取值（0、负数、1e9、小数）× 6 类 `source_class` 追加后缀后 lint 全空；真实报告的每一句对外文字（summary、disclaimer、limitations、findings、missing）lint 全空；lint 本身不是空转 | `test_banned_words_are_unreachable_from_every_registered_template`、`test_banned_list_covers_the_words_the_plan_named_and_lint_is_not_vacuous`、`test_every_outward_sentence_of_a_real_report_lints_clean` |

变异实验（临时、不入库）确认检测器有效：给路由表加 `/api/share/companies` 与 `/api/companies/ranking` 会被子串表抓到；把 `headcount_trend` 模板改成含「裁员」的写法，极端取值扫描报出 `['裁员', '裁员']`。

计划正文里「除 `rubric.py` 外没有任何模块 import `model_transport`/`get_model_config`」这句按字面实现时是红的：`extract.py` 的本职就是调用模型把证据正文抽成信号。红线按真实意图写成「除 `extract.py` 外不得接触模型层，且 `extract.py` 交不出分数」，其余 13 条首跑即绿。

### 前端（本次 Task 12 运行）

```text
cd frontend
npx tsc --noEmit    # 退出码 0，无输出
npm run build       # ✓ built in 4.35s，dist/assets/index-*.js 719.67 kB（gzip 221.56 kB）
```

`npm run build` 只有一条 chunk 体积超过 500 kB 的 Vite 提示，与本次功能无关，未处理。

### 面板行为（Task 11 的实施代理实测，非本次）

以下六条由 Task 11 的实施代理在 2026-11-09 用 browser-use MCP 对本机 8766 端口上跑起来的应用点击得到，结论由该轮运行负责：

1. 报告不存在时面板不画空环，只出「依据不足，暂不出分」与说明文字。
2. `material` 行只出文字与出处，页面上没有数字。
3. 未配置模型时「从材料里抽取信号」显示 `skipped`。
4. 切换岗位后参考面板清空，不留上一家的报告。
5. 付费来源默认关闭，设置页任何位置都不回显密钥原文（只有「已设置」状态）。
6. 连点三次「用已有材料算覆盖度」，公告与岗位计数不涨（本地计算记录不把自己数进去）。

**这六条本轮（Task 12）没有复验**：那一次点击跑完之后，后续那轮 UI 点击被权限层拦下，未再执行。因此上面 1-6 的证据止于 Task 11 那一轮运行，Task 12 只复核了能在命令行复现的部分（测试、类型检查、构建）。

## 未验证（逐条，全部保持 `needs_verification`）

下面每一条都是**代码里的假设**，不是实测结果。`sources.py` 里 `gsxt`、`credit_china`、`court_records` 的 `status` 必须继续保持 `needs_verification`，在人工实测通过之前不得改成 `available`。

1. **gsxt 两个页面的 URL**：`https://www.gsxt.gov.cn/index.html?keyword=<公司>` 与 `/nbgs.html?keyword=<公司>` 从未在真实会话里访问过，页面是否存在、是否需要先检索再跳转都不知道。
2. **gsxt 的两处正则**：`成立日期[^0-9]{0,12}(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})` 与 `城镇职工基本养老保险[^0-9]{0,20}(\d+)\s*人` 的标签写法、间隔长度、单位呈现全为按公开页面结构写的猜测。
3. **`VERIFY_MARKERS` 词表**（9 项）：验证码/滑块/登录墙的标志文字没有一项来自实际返回，可能既漏判也误判。
4. **年报「由新到旧」的排序假设（一旦读反就是方向性错误，是这份清单里最需要先实测的一条）**：`_read_annual_report()` 把 `counts[0]` 当最近一期（`after`）、`counts[1]` 当上一期（`before`）。如果公示系统实际是由新到旧的反面，这两个端点必须交换，否则趋势会把扩员读成萎缩，而措辞模板会照抄这两个数字。实测第一步就是核对这一点。
5. **年报信号的 `observed_at` 留空**：目前没有把握把页面上的「XXXX年度报告」与相邻的两个参保人数逐行配对，所以宁可不写观察时间（法律口径是不猜）。
6. **付费接口的字段映射**：`sources.QCC_FIELDS` / `TYC_FIELDS` 里每一组候选字段名（成立日期、本期/上期参保人数、涉诉数量与比率）都是按通用写法预置的假设。本项目没有付费账号、没有核对过任何一家的响应结构，这些名字一个都不作数。
7. **`PaidClient` 的请求形态**：查询参数名写成 `company`、认证写成 `Authorization: Bearer <key>`，同样是通用写法假设。服务商若要求 POST、要求不同参数名，或要求签名（`appId` + `timestamp` + `sign` 一类，本项目**没有**对应的认证配置项），真接上会 4xx，`paid.collect()` 退成 `failed` 并原样带回原因——不会伪造任何信号，但也拿不到数据。
8. **`credit_china` 与 `court_records` 至今没有 reader**：`collectors.READERS` 只登记了 `gsxt`，这两个来源被点击采集时退成 `unsupported`（「该来源不由项目浏览器采集。」），登记表里的 `rules=['judicial_case_ratio']` 目前是空头承诺。
9. **没有任何一次真实外部分数落过库**：本次与历次运行都没有联网采集 gsxt 或调用付费接口，`stability` 的分数只在测试里由直接落库的信号与假响应算出。因此「第一个真实外部分数要等人工实测」这句话现在仍然成立。

## 人工实测步骤模板（留给以后填）

每个来源一份，填完才允许把 `sources.py` 里的 `status` 改成 `available`。请照 `docs/validation/jobscope-domestic-sources.md` 的做法：**只写实际发生的请求与返回，不写推测**。

```text
来源：<gsxt / credit_china / court_records / qcc / tyc>
实测人：            日期与时刻（含时区）：
环境：OS、Python、Chrome 版本、是否已登录该来源、网络出口
浏览器会话：profile 目录、端口来源（browser.py 记录的本机回环端口）

请求序列（逐条，按实际顺序）
1) 动作：<打开 URL / 在搜索框输入 / 点击翻页>   URL 或选择器：
   页间等待：2.5s 实际值：          是否命中校验或登录墙：
2) …（单次任务 ≤5 页，命中风控就停在这里交给人工）

实际返回（原样摘录，不改写）
- 成立日期原文呈现：              正则命中：是/否
- 参保人数列表原样（含年份标签）：              页面排列顺序：由新到旧 / 由旧到新 / 无法判定
- 校验或登录提示原文：            VERIFY_MARKERS 命中项：
- 付费来源另填：HTTP 状态、响应根结构、命中的字段名与值、message 里报回的候选字段名

结论
- 哪个假设被推翻，代码改了哪一行（文件与函数名）：
- 年报两端是否需要交换 before/after：
- 该来源 status 是否可以从 needs_verification 改成 available：
- 若仍不通，走不通的入口原样保留在此以免重复踩：
```

## 回归

```powershell
cd E:\Github_Pro\JobScope-reliability
python -m pytest backend/tests -q                                   # 全量；Task 12 本轮未跑，由统一运行确认
python -m pytest backend/tests -q -k reliability                    # 171 passed
cd frontend
npx tsc --noEmit
npm run build
```
