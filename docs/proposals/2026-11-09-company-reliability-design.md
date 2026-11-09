# 公司求职参考分析（Company Reliability）设计

日期：2026-11-09。分支 `codex/company-reliability`，实现基线 `9b98099`（Task 1-11）加本次边界自检与文档（Task 12）。本文照当前代码写，不照计划正文写：计划正文里有几处写法在 Task 1-9 的实施修正记录里已被推翻，凡是与本文不一致的地方以代码与本文为准。

计划原文见 `docs/plans/2026-11-09-company-reliability.md`；实测状态与未验证清单见 `docs/validation/jobscope-company-reliability.md`。

## 这个功能解决什么问题

在岗位档案里，围绕某一家公司把「本机已经存下的材料」整理成一份可读、可回查、可复现的参考：哪些是官方登记数字、哪些是自己算出来的覆盖度、哪些是别人的一句自述。它回答的是「我手上有材料能说明什么」，不回答「这家公司好不好」。

一句话定位：**本地单人自用的情报整理**。因此整个功能的形态被钉死为不出网、不分享、不排名。

## 四层管线

```mermaid
flowchart TD
  A1["collectors.py 官方公开页（项目 Chrome）"] --> S["company_signals（每条带 evidence_id / source_class / collected_at）"]
  A2["local.py 本项目已存岗位与公告"] --> S
  A3["paid.py 付费接口（声明式字段映射）"] --> S
  E1["extract.py 证据正文 → 结构化信号（唯一模型调用点）"] --> S
  S --> R["rubric.py 纯函数算分（唯一的分数来源）"]
  R --> W["wording.py 模板渲染 + 禁用词 lint（唯一的对外文字来源）"]
  W --> V["service.py 编排 + assert_clean + 幂等落库"]
  V --> P["company_reliability（按 input_hash 幂等）"]
  P --> API["五个本地端点"]
  API --> UI["frontend/src/CompanyReference.tsx"]
```

**取数层（三种互不越界的通路，端点按登记表里的 `transport` 分派）**

- `collectors.py`（`transport='browser_page'`）：复用 `jobscope/browser.py` 的项目 Chrome——同一个 `profile` 目录、同一份端口记录、同一把 OS 会话锁。硬上限写死在模块里：单次任务最多 5 页、页间间隔 2.5 秒、单页超时 25 秒；不重试、不换 UA、不绕风控。命中 `VERIFY_MARKERS` 里的字样就退成 `needs_verification`，并把标签页留在前台交给用户（只有干净收尾才关标签页）。会话被占用退成 `backoff`，没有可用端口退成 `needs_login`。目前 `READERS` 只登记了 `gsxt` 一个来源。
- `local.py`（`transport='local'`）：只读本项目已存数据，不联网、不占会话锁。两条规则量的都是「手上材料有多少」，所以落成信号时 `source_class='platform'`、`url=''`、`observed_at=''`，证据 kind 取 `local_computation`——必须落在公告类 kind 之外，否则下一次采集会把自己写的口径记录再数一遍，计数随点击无限膨胀。
- `paid.py`（`transport='http_api'`）：只在用户自备密钥并启用后才发起一次 GET。字段名一律来自 `sources.SIGNAL_SOURCES[id]['fields']` 那份声明表，键名去掉大小写与 `_ - / 空格` 后**精确匹配**，不做子串或模糊匹配；同名字段在响应里给出多个不同值时直接拒绝取值并把值报回 message；日期只接受日期字符串，不接受 epoch 或 `20150401`。对不上就一条信号都不产出，并把试过的候选字段名逐个写回 message。取数结果落成 `paid_api_record` 类证据，同样不进抽取器的材料列表。接口地址里出现密钥直接拒绝（否则密钥会跟着出处 URL 永久留档）。

`collectors.years_since()` 是全项目唯一的一把年数尺子（`round(days / 365.25, 1)`），浏览器与付费两条通路都用它，避免两处漂移。

**信号层**：`models.SignalInput` 是唯一入仓契约（`extra='forbid'`），每条必须带 `evidence_id`、`dimension`、`rule`、数值 `value`、`source_class` 与 `collected_at`。`repository.signals()` 按规则取最新一条，并用 `rubric.RULES[*].ttl_days` 标记过期（`stale`）——过期不是删掉，是「仍然展示文字，但不进加权」，并在维度 `missing` 里写明「已过期，超出 N 天」。

**模型层只出现在 `extract.py`**：输入是证据正文（单条正文截到 12000 字符、最多 40 条、`temperature=0`），输出经 `ExtractInput`/`RawSignal` 校验。这一层是 fail-closed 的：结构解析不出来就整批作废并留档原始响应（`status='degraded'`）；模型多塞 `score`/`grade`/`rating`/`verdict` 一类字段会让整批作废；`value` 里只保留数字，字符串与布尔一律剔除，剔空则丢弃该条；`evidence_id` 不在传入证据里就丢弃；网络异常归 `failed`；未配置模型归 `skipped`，并在 `tasks` 里如实记下真实状态。`KIND_TO_SOURCE_CLASS` 决定证据 kind 映射到哪个 `source_class`，员工自述与媒体报道映射成 `employee`/`media`，因此天然不能单独结算一个维度。

## 六维权重

`rubric.DIMENSIONS`（权重合计 1.00）：

| 维度 | 标签 | 权重 | 现在能出分吗 |
| --- | --- | --- | --- |
| `stability` | 稳定性 | 0.22 | 能（3 条 assessment 规则） |
| `pay` | 薪资待遇 | 0.20 | 不能，只有 1 条 material 规则 |
| `operations` | 实际经营 | 0.18 | 不能，只有 1 条 material 规则 |
| `people` | 人员情况 | 0.16 | 不能，只有 1 条 material 规则 |
| `professional` | 专业性 | 0.12 | 不能，一条规则都没有 |
| `industry` | 行业前景 | 0.12 | 不能，一条规则都没有 |

## 规则清单

`rubric.RULES`，`RUBRIC_VERSION = 'rubric-2'`。所有规则的 `weight` 目前都是 1.0（维度内按观察到的规则数归一），差别全在 `kind`。

| 规则 | 维度 | 单位 | `ttl_days` | `kind` | 模板 | 取值口径 |
| --- | --- | --- | --- | --- | --- | --- |
| `company_age` | stability | 年 | 365 | assessment | `company_age` | `min(1, years / 10)` |
| `insured_headcount_trend` | stability | 人 | 400 | assessment | `headcount_trend` | `0.5 + (after - before) / max(before, 1) / 2`，截到 0-1 |
| `judicial_case_ratio` | stability | 件/万人 | 180 | assessment | `judicial_cases` | 分档：≥25→0.3、≥10→0.55、≥3→0.75、其余 0.9；`ratio` 为 0 或缺失时**返回 None** |
| `salary_quote_coverage` | pay | % | 120 | **material** | `salary_quote_coverage` | `known / total` |
| `employee_review_sample` | people | 条 | 180 | **material** | `employee_review_sample` | `min(1, samples / 10)` |
| `disclosure_available` | operations | 份 | 365 | **material** | `disclosure_available` | `min(1, count / 4)` |

两条分类纪律：

- **assessment 才有资格产出维度分；material 只产出说明文字。** 那三条 material 规则衡量的是本项目手上有多少材料——把「9/10 条岗位能解析出月薪」算成薪资待遇 90 分，用户读到的是「这家薪资给得好」，属于静默谎言。
- **`ReportFinding.kind == 'material'` 时 `score` 一律为 `None`**（`service._report_finding()` 强制）。`RuleFinding.score` 仍在 rubric 内部参与加权，但出到 API 就是 null；前端 `CompanyReference.tsx` 渲染 material 行时只画文字与出处，不画数字。

## 算分与置信度

- 单维度分 = 该维度**可独立成立**（`can_stand_alone`：`official`/`paid_api`/`platform`/`user_material`）的 assessment 依据按 `weight` 加权平均，`round(…, 1)`。
- 单维度 `confidence` = `min(1, 权重合计 / 该维度 assessment 规则条数)`，也就是「这个维度的可评价规则覆盖了几条」。
- 综合分 = 有分的维度按其权重再归一；综合 `confidence` 是这些权重之和，前端与措辞里都称「维度覆盖 {n}%（按权重）」，它和单维度 `confidence` 不是一回事。
- 没有任何 assessment 规则可算时 `score=None`、`confidence=0`，并且**每个维度都必须自己解释为什么没分**：有 assessment 规则但缺数据列「（尚无来源）」，一条 assessment 规则都没有列「该维度还没有可评价的规则，下面只是已收集到的材料」，只有自述材料列「仅有未经独立核实的自述材料，不计入分数」。空维度不留空白。

## 措辞模板与禁用词

`wording.py`（`WORDING_VERSION = 'wording-2'`）是唯一产生对外文字的地方，模板键与 `RULES[*].template` 纯靠字符串对齐，由测试抓拼写漂移：

| 模板 | 文案 |
| --- | --- |
| `company_age` | 登记信息显示成立约 {years} 年（来源见出处）。 |
| `headcount_trend` | 公开年报的参保人数由 {before} 人变为 {after} 人；原因可能有多种，本工具不推断经营好坏。 |
| `judicial_cases` | 公开裁判信息中相关案件约 {ratio} 件/万人参保规模；涉诉数量与用工质量没有既定关系，请结合案由自行判断。 |
| `salary_quote_coverage` | 该公司已保存岗位中 {known}/{total} 条能解析出人民币月薪区间，其余报价口径不支持。 |
| `employee_review_sample` | 收集到 {samples} 条员工自述材料，属个人经历陈述。 |
| `disclosure_available` | 已保存 {count} 份公开公告元信息，正文是否已解析以出处标记为准。 |

- 分数区间只描述「材料说明了什么」：≥75 稳定信号偏多；≥60 偏向中性、仍有维度缺少依据；≥45 手上的材料方向不一致、请逐条对照依据；<45 才要求重点核实。45-60 那段以前写成「出现了值得你重点核实的信号」，一家成立 2 年却在扩员的公司拿 55.6 分会被这样描述，属于文案说谎。
- 无综合分时用 `UNKNOWN_OVERALL`（「可纳入计算的公开依据不足，暂不给出综合参考分」），不画空环、不摆占位数字。
- `employee`/`media` 类依据的句子结尾固定追加 `（来自个人自述，未见独立核实）`，这是措辞上限，UI 不展示作者、ID 或截图粒度。
- `BANNED` 禁用词表（22 项）：欠薪、拖欠工资、黑心、血汗、骗子、诈骗、皮包公司、跑路、老赖、倒闭、裁员、裁撤、垃圾、坑、无良、高危、高风险、危险、不良、劣迹、信誉差、不靠谱。这些词把可核实的数字升级成对他人商誉的断言。
- `assert_clean()` 在 service 里每次都跑：任何一句命中禁用词、任何一条依据缺 `evidence_id`，直接抛 `ValueError`，端点转成 422，宁可不出报告也不出违规报告。

## 来源登记表

`sources.py` 的 `SIGNAL_SOURCES`（默认值只由 `tier` 推导，登记表里没有一条写死的 `mode`）：

| 来源 | 名称 | 层级 | transport | 默认 | `status` | 覆盖规则 |
| --- | --- | --- | --- | --- | --- | --- |
| `gsxt` | 国家企业信用信息公示系统 | free | browser_page | 启用 | `needs_verification` | company_age、insured_headcount_trend |
| `credit_china` | 信用中国 | free | browser_page | 启用 | `needs_verification` | judicial_case_ratio（尚无 reader） |
| `court_records` | 中国裁判文书网 | free | browser_page | 启用 | `needs_verification` | judicial_case_ratio（尚无 reader） |
| `jobscope_jobs` | 已保存岗位与证据 | free | local | 启用 | `available` | salary_quote_coverage、disclosure_available |
| `qcc` | 企查查开放平台 | paid | http_api | **关闭** | `opt_in` | 三条 stability 规则 |
| `tyc` | 天眼查开放平台 | paid | http_api | **关闭** | `opt_in` | 三条 stability 规则 |

密钥做法与 `ModelSettings` 一致：存本机 SQLite 的 `signal_sources` 表、`public()` 的返回结构里根本没有 `key` 字段（只有 `key_set` 布尔）、换服务地址必须重填密钥、付费来源没有密钥即使 `mode='enabled'` 也不算启用。闸门语义统一走 `sources.enabled(id)`：免费来源被手动关闭同样返回 False。

`status` 的词汇沿用 `connectors.result()` 那套（`completed`/`partial`/`needs_login`/`needs_verification`/`backoff`/`unsupported`），前端 Badge 不用加分支。

## 法律边界的四条约束

1. **对外文字只能来自 `wording.py` 的模板**，`rubric_version` / `wording_version` / `disclaimer_version` 随分数一起落库，事后能复现当初那句话是怎么写的。
2. **数字代替形容词、来源代替结论**：涉诉、参保下降这类事实只陈述「数字 + 来源 + 抓取时间」，不生成定性断言，禁用词表由测试 lint。
3. **`employee` 类信号（脉脉、看准网等自述）不得单独产生负面维度分**：必须与 `official`/`platform` 类共同出现才参与加权，措辞上限锁死为「未见独立核实」，UI 不展示作者、ID、截图粒度。
4. **不做对外分享页、不做公开 API、不做公司红黑榜或跨公司排名**。

外加一条实体纪律：**不做模糊合并**。公司归一只做全角/空白/标点规范化（删除所有空白）与显式别名表，强键是统一社会信用代码；宁可漏合并，也不把 A 公司的诉讼挂到 B 公司头上。

## 明确不做

- 分享页、公开只读端点、榜单/排名/评级接口（Task 12 的 `backend/tests/test_reliability_boundaries.py` 直接扫应用路由表，路径里出现 `share`/`rank`/`public`/`leaderboard`/`blacklist`/`rating`/`scoreboard`/`compare` 任一子串即变红，并断言本功能的对外面就只有那五个路径）。
- 自动重试、换 UA、绕验证码或风控；命中校验就把页面交还用户。
- 代持付费密钥、默认调用付费接口、从任意 JSON 里「挑一个像参保人数的数字」。
- 让模型算分：分数只在 `rubric.py` 产生，`rubric`/`wording` 除 `.models` 与 `dataclasses` 外不导入任何东西，`RuleFinding` 只由 rubric 构造，`extract.py` 是唯一被允许调用模型层的模块。
- 把「没查到负面」写成「好」。

## 当前 P0 的真实能力边界（重要）

- **只有 `stability` 能被打分。** `pay`/`operations`/`people` 三个维度目前各只有一条 material 规则，永远只出说明文字不出分；`professional`/`industry` 一条规则都没有，只出「该维度还没有可评价的规则」。**P1 必须给 `pay`/`operations`/`people` 补 assessment 规则**，否则这三个维度形同文字板。
- `stability` 的三条 assessment 规则里，两条依赖还没实测的 gsxt 页面，一条依赖至今没有 reader 的裁判网/信用网；因此默认配置下真实数据基本只会得到「无综合分 + 缺口清单 + 若干说明文字」。**这是设计意图，不是缺陷。**
- 唯一现在就可能算出分的通路是付费接口（`qcc`/`tyc`），而它的字段映射与认证方式同样未经核实，需要用户照自己采购到的接口文档校正。
- **缺数据一律不出分**：`ratio` 为 0（检索不到案件可能是覆盖面不够）、数值不完整、只有自述材料、规则已过期——四种情况都退回 `missing` 说明而不是分数。

## 失败模式一览

| 情况 | 表现 |
| --- | --- |
| 未配置模型 | `extract` 退 `skipped`，报告照常可算 |
| 模型返回不可解析结构 | `degraded`，整批作废并留档原始响应 |
| 模型/网络异常 | `failed`，`message` 原样带回，不伪造信号 |
| 页面被校验或登录墙挡住 | `needs_verification` / `needs_login`，标签页留在前台 |
| 浏览器会话被占用 | `backoff`，不排队不抢占 |
| 付费字段对不上 | `partial`，逐条列出试过的候选字段名 |
| 措辞命中禁用词 / 依据缺出处 | 抛 `ValueError`，端点 422，不出报告 |
| 同一批输入重复计算 | `company_reliability` 按 `(company_id, input_hash)` `INSERT OR IGNORE`，不落第二份 |

## 实现落点

后端 `backend/jobscope/reliability/`（`models`/`rubric`/`wording`/`service`/`sources`/`collectors`/`local`/`paid`/`extract`，包的 `__init__` 保持零导入，端点里函数级 import）；`main.py` 在 `/api/companies` 之后挂五个路径：`POST|GET /api/companies/{company}/reliability`、`POST /api/companies/{company}/signals/collect`、`POST /api/companies/{company}/signals/extract`、`GET /api/reliability/sources`、`PUT /api/reliability/sources/{source_id}`。写路径用 `resolve_company` 确保实体存在，读路径用 `peek`/`find_company`，一次拼错的查询不留一行。前端 `frontend/src/CompanyReference.tsx` 挂在岗位详情「公司情报」区块内，来源设置在 `App.tsx` 的 `SignalSourceSettings`（数据与采集页），密钥字段任何位置都不回显原文。免责与限制条件是常驻区块，不折叠、不 hover。
