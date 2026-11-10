# 公司求职参考 P1：让六个维度真的能出分

**给接手的 agent：** 这是一个已有功能的第二阶段，不是新起一个项目。动手前必须按顺序读完这三样，否则会把 P0 修好的错误重新犯一遍：

1. `docs/plans/2026-11-09-company-reliability.md` 的「固定约束」「法律风险红线」「选型说明」，以及**三段「实施修正记录」**（正文里有若干写法已被落地时的修正推翻，修正记录才是事实）。
2. `docs/proposals/2026-11-09-company-reliability-design.md`（照代码写的现状设计）与 `docs/validation/jobscope-company-reliability.md`（真实验证状态与未验证清单）。
3. 代码：`backend/jobscope/reliability/` 全部模块，以及红线测试 `backend/tests/test_reliability_boundaries.py`。

**工作目录**：`E:\Github_Pro\JobScope-reliability`（git worktree，分支 `codex/company-reliability`，起点 HEAD `518f50f`）。主检出 `E:\Github_Pro\JobScope` 上有别的会话在并发提交，不要在主检出动代码。ResumeLoom 的 `jobscope/` 已被 `.gitignore` 排除，改那边等于没改。

**基线**：后端全量 395 passed（约 2.5 分钟），前端 `npx tsc --noEmit` 与 `npm run build` 通过，工作树干净。系统 Python 3.12.9，无 venv。

---

## 一、P1 要解决的唯一问题

P0 交付后，六维里只有 `stability` 有可评分的规则（3 条），而且其中 2 条依赖**至今没实测**的 gsxt 页面。结果是：真实用户打开面板，看到的几乎永远是「依据不足，暂不出分」加一堆说明文字。

P1 的任务是用**已经在库里的数据**补齐 5 条 ASSESSMENT 规则和 1 条 MATERIAL 规则，让 `pay`、`professional`、`people`、`operations` 四个维度在本地就能出分，不需要网络、不需要凭据、不需要用户先登录哪个平台。

一条都不能是「把我们存了几条材料算成对公司的评价」——这是 P0 里已经修过的最大的一类错。

## 二、地基事实（设计前必须知道的）

**能用、且不需要任何外部条件的数据面：**

- `jobs` 表：`title` / `company` / `location` / `salary`（原始字符串）/ `description` / `description_status`（`listing` 或 `complete`）/ `url` / `updated_at` / `source`。
- `analysis.parse_salary(text)`：只有返回 `currency == 'CNY'` 时 `monthly_min` / `monthly_max` 才可信；年薪、时薪、外币、`最高16薪` 这类口径一律不算已知。
- `analysis.SKILLS`：技能词表，配 `analysis.contains()` 的词边界匹配（能区分 Java 与 JavaScript、Go 与 Google）。
- `evidence` 表：`kind` 有 `disclosure`、`disclosure_metadata`、`announcement`、`media_report`、`employee_statement`、`company_statement`、`user_material`、`job_posting`。`documents.extract_announcement()` 能把巨潮公告 PDF 抽成正文存成 `kind='disclosure'`。
- `reliability/local.py`：已经跑通的「从本地已存数据算信号」的范式，返回值形状与 `collectors` / `paid` 同构，新增本地规则照它写。
- `reliability/extract.py`：证据正文 → 结构化数字信号的严格抽取器，契约只收数字，模型不许交分数，raw 留档。规则清单由 `rubric.RULES` 自动生成，**加规则不用改提示词**。
- `repository.signals(company_id, ttl_days=...)`：按 rule 取最新一条并标 `stale`；`resolve_company` / `peek_report` 的读写口径已经分开。

**不能用的东西，P1 不要设计方案依赖它：**

- gsxt、信用中国、裁判文书网的真实页面（无 DrissionPage、无登录会话，至今未实测）。
- 脉脉、看准网、职友集（需要用户登录，且属于第三方 UGC，法律上要单独一轮设计，不在 P1）。
- 任何付费 API 的真实响应（没有账号；`paid.py` 的字段映射本身也是待校正假设）。
- 行业前景的外部数据（没有可信公开源，**P1 明确不给 `industry` 硬造分数**）。

## 三、要新增的规则

命名、维度、单位、`ttl_days`、`kind` 都要按下面写死，不要自己发挥。分数一律由 `rubric.py` 的纯函数产出。

### R1 `pay_vs_city_median` → `pay`（ASSESSMENT，ttl 90 天）

**口径**：该公司可解析的人民币月薪报价中位数，对比**同一城市**全部已存岗位的可解析报价中位数。单条报价取 `(monthly_min + monthly_max) / 2`。

`value`：`{company_median, city_median, ratio, company_jobs, city_jobs}`（全部数字）。

**样本闸门**（闸门不满足时 scorer 返回 `None`，靠 `gate_note` 说清原因，绝不硬算）：`company_jobs >= 3`、`city_jobs >= 20`、且 `city_median > 0`。城市取该公司岗位里出现次数最多的可归一城市；城市归一只做「命中已知大城市词典就归一，否则按 `normalise_name` 后的原文精确分组」，**不做模糊匹配、不做「周边城市并入」**。

映射：`ratio >= 1.2 → 0.95`、`>= 1.0 → 0.8`、`>= 0.85 → 0.6`、`>= 0.7 → 0.45`、其余 `0.3`。

**必须写进模板的口径声明**：这是招聘平台报价的对比，不是实际到手薪资，也不含奖金与股票；同城样本不是同职级同技能样本。措辞禁用词表照旧。

### R2 `jd_specificity` → `professional`（ASSESSMENT，ttl 180 天）

**口径**：该公司 `description_status == 'complete'` 的岗位职责有多具体。四个子指标归一后加权：描述平均字符数、职责/要求的条目数、`SKILLS` 词表命中的技术栈条目数、含量化结果（`\d+%`、日活、QPS、亿级等）的条目占比。

`value`：`{jobs, chars_mean, items_mean, skills_mean, quantified_share}`。

**样本闸门**：`jobs >= 3`（只数完整 JD）。只有列表摘要的岗位**不参与**，并在 `gate_note` 里提示「完整职责未采集」——这是 P0 在 `/api/analyze` 里就立过的规矩：半截 JD 不能用来评估。

这条衡量的是「这家公司把要做什么讲清楚了没有」，不是「这家公司技术强不强」，模板必须这么写，不许滑成后者。

### R3 `team_size_statement` → `people`（ASSESSMENT，ttl 240 天）

**口径**：由 `extract.py` 从岗位描述与公司自述材料里抽出的「团队 N 人 / 部门 N 人」类数字，取中位数与条数。

`value`：`{median_size, statements}`。

**闸门**：`statements >= 2`。

**来源约束**：`source_class` 走 `extract.KIND_TO_SOURCE_CLASS` 的既有映射（`company_statement → user_material`），属公司口径。模板必须写「岗位描述自述的团队规模，属公司口径，未见独立核实」，而且这一条**只说明规模，不说明氛围**。

### R4 `disclosure_revenue_trend` → `operations`（ASSESSMENT，ttl 365 天）

**口径**：同一份公告正文里本期与上一期的营业收入或营收数字。

`value`：`{current, previous}`。**闸门**：`previous > 0`。映射：同比 `>= 0.2 → 0.95`、`>= 0.05 → 0.8`、`>= -0.05 → 0.65`、`>= -0.2 → 0.45`、其余 `0.25`。

必须拆成两条规则而不是用 `metric` 字符串参数区分，因为 `_for_signal()` 只保留数字字段——这是 P0 的实现事实，别去改它来塞字符串。

### R5 `disclosure_headcount_trend` → `operations`（ASSESSMENT，ttl 365 天）

同 R4，字段是公告口径的在职人数/员工总数两端。**模板严禁出现「裁员」「裁撤」**（都在禁用词表里），写法固定为「公告口径在职员工数由 {previous} 变为 {current}；人数变动有多种成因，本工具不推断经营好坏，也不据此判断招聘冻结」。

### R6 `industry_media_count` → `industry`（MATERIAL，ttl 365 天）

`industry` 维度 P1 **不出分**，只补一条 material 规则：`value = {count}`，统计该公司 `kind in ('media_report', 'announcement')` 的证据条数。作用是让这个维度在面板上从「什么都没有」变成「已有几条行业与媒体材料，但本工具没有可评价依据」，并引导用户去补。

## 四、框架侧要改的地方

- **`rubric.py` → `rubric-3`**：`RuleSpec` 增加 `gate_note: str`，scorer 返回 `None` 时的缺口文案用 `f'{rule}（{gate_note}）'` 而不是现在的通用「现有数字不足以判断」；`ASSESSING_BY_DIMENSION`、维度 `confidence` 分母、`overall` 加权都会自动跟随，不用手改。`DIMENSIONS` 的权重**保持不动**（0.22 / 0.20 / 0.18 / 0.16 / 0.12 / 0.12）。
- **`wording.py` → `wording-3`**：六条新模板 + 现有的「极端取值 × 全规则」参数化测试继续覆盖新模板；`TEMPLATES` 与 `RULES[*].template` 的一一对应测试必须仍然绿。
- **新文件 `reliability/job_stats.py`**：R1 与 R2 的本地计算层，纯函数 + 只读仓储查询，**不导入浏览器模块、不调模型、不碰 `research_jobs` 索引**（语义索引可能压根没建，不能依赖）。返回形状与 `local.py` 一致。
- **`local.py`**：把 `jobscope_jobs` 来源的规则表扩到包含 R1、R2、R6；派生证据的 `kind` 继续用 `local_computation` 并被 `company_materials()` 排除。
- **`extract.py`**：R3、R4、R5 的 value 字段名进规则清单即可（清单是自动生成的），但提示词里要补一句「同一规则的两个时期数字必须来自同一份材料」，避免模型跨年拼接。
- **`sources.py`**：`SIGNAL_SOURCES['jobscope_jobs']['rules']` 扩充；`paid.py` 的 `fields` 映射表为 R4/R5/R1 增加候选字段名，全部照现有风格标注为待校正假设。
- **历史与单公司趋势**：新增 `GET /api/companies/{company}/reliability/history` 与 service 层的 diff（对比**同一家公司**的两次快照，把分数变化归因到具体是哪几条规则变了）。跨公司对比、排名、榜单**一律不做**，并且要把这条约束继续钉在红线测试里。
- **前端**：维度可展开看每条依据的文字、出处、口径、计算时间与是否过期；缺口清单每项带行动入口（补资料 / 本地重算 / 从材料抽取）；报告底部加单公司历史趋势；material 行永远不画数字；配色保持中性，不加警示红。

## 五、验收标准（逐条可执行）

1. 造 12 条已存岗位（其中 ≥3 条完整 JD、≥8 条同城可解析报价），只跑本地采集与重算，**不联网**，`pay`、`professional` 两个维度必须出分，`report['confidence']` 显著高于 P0 的 0.22。
2. 样本闸门有测试覆盖：`city_jobs < 20`、`company_jobs < 3`、`statements < 2`、`previous <= 0` 四种情况各自**不产出分数**，且 `missing` 里能看到 `gate_note` 的具体原因。
3. `industry` 维度 `score is None` 且有 R6 的 material 文字，material 的 `score` 序列化后仍是 `null`。
4. 全部新模板在 `0 / 1 / 7 / 10**6` 四个极端探针下 `lint()` 均为空，且 `TEMPLATES` 与 `RULES` 一一对应。
5. 红线测试继续全绿，并新增：小样本必须不出分、趋势端点只接受单个公司、不存在跨公司排名路由。
6. 同一输入重复 `recompute` 得到同一个 `input_hash`（P0 修过的幂等性不能退化）。
7. 后端全量回归通过，前端 `tsc --noEmit` + `npm run build` 通过。

## 六、明确不做

- 不给 `industry` 造分数，不用「关键词 + 大模型感觉」推断行业前景。
- 不做跨公司对比、排名、红黑榜、公开分享页、对外 API。
- 不加任何静默默认值：缺字段就是不产出信号，并在 message 里点名缺了什么。
- 不让员工自述或媒体报道单独结算某个维度（`can_stand_alone()` 已有这道闸）。
- 不为迁就新规则去改 `rubric` 的 assessment/material 分层，或让 material 出数字。
- 不猜厂商 schema，不引入第二套浏览器栈。

## 七、P0 踩过的坑，别再来一遍

1. **覆盖度不是评价**。`salary_quote_coverage`、`disclosure_available`、`industry_media_count` 这类只量「我们存了几条」的规则必须是 `MATERIAL`，持久化 `score` 必须是 `null`。
2. **缺数据不是好消息也不是坏消息**。没查到涉诉记录不等于干净，参保为 0 不等于没有；scorer 该返回 `None` 就返回 `None`。
3. **派生证据正文里不能写时刻**。证据身份含内容哈希，带时间会让每次点击都新增记录、`input_hash` 每次都变、历史表无限膨胀。时间只留在信号的 `collected_at`。
4. **`company_signals.collected_at` 不是表列**，排序要用 `json_extract(payload,'$.collected_at')`；TTL 判定要用 `timedelta` 比较而不是 `.days`（边界会漏报过期）。
5. **`TestClient` 继承 `httpx.Client`**，替换 `httpx.Client.post` 会把测试客户端自己的请求也当成模型响应，mock 里要放行 `isinstance(self, TestClient)`。
6. **`str.format` 不能用在含 JSON 字面量的提示词上**（`{"signals":...}` 会让 format 抛 `KeyError`），提示词模板用 `replace`。
7. 行尾：后端 LF、前端 CRLF，git 自动转换，**不要手工统一**；`git add` 只用具体路径，不要 `-A`；不要 push、不要 tag、不要建 PR。
8. 全量后端约 2.5 分钟，日常只跑相关测试文件，收尾再全量。

## 八、需要人类参与的收尾（不属于 P1 编码，但要留口）

- 装上 `DrissionPage` 并让用户在项目 Chrome 里登录一次，才能实测 gsxt 的两个页面、校准 `VERIFY_MARKERS` 与年报「由新到旧」的排序假设（读反会把扩员报成萎缩，是方向性错误）。实测前这些来源的 `status` 必须保持 `needs_verification`。
- 付费接口若真要接，需要用户拿到凭据与接口文档后校正 `paid.py` 的字段映射与认证方式（现在写死 `Bearer` + 查询参数 `company`，多数厂商要签名）。

---

## 实施修正记录（P1 已交付，后面按这个现状写代码）

P1 于 2026-11-10 在本分支交付：全量后端 453 passed（P0 基线 395），`npx tsc --noEmit` 与 `npm run build` 通过。落地时相对正文有 6 处修正，都有测试兜住：

1. **R3 的分档映射与聚合层（正文的两处空档，用户拍板）**：正文给了 `value={median_size, statements}` 与闸门，但没给分档映射，也没说中位数在哪层合成。定案为**自述一致性**：分数 = 多处自述之间的相容占比（全部相容 → 0.9、≥2/3 → 0.6、否则 0.3），与规模大小完全无关——用户特别指出「5000-10000 人」这类正常区间宽度不能被读成矛盾，所以相容判定按**区间相容**：一条自述给区间就按原文区间，另一条的点值落在区间内即相容；两条都是点值才看倍数（≤2.5 倍相容）；区间宽度本身永不扣分。聚合在**抽取端代码**完成（`extract.aggregate_team_size`）：模型逐条材料交 `{"size": N}`（区间另加 `"size_max"`），代码算中位数与最大相容子集，`value` 落库为 `{median_size, statements, compatible}`（对正文 value 形状的一处已确认扩展）；锚条取区间中点最接近中位数的那条，其余参与编号写进任务 message 留档。
2. **完整 JD 进抽取材料**：正文 R3 说从「岗位描述与公司自述材料」抽取，但 P0 的 `company_materials()` 把 job_posting 类证据全部排除。P1 起端点把 `description_status == 'complete'` 的岗位证据（save_job 落的真实 job_posting 记录）加进抽取材料；列表摘要仍然不进——半截 JD 不能用来评估这条规矩不变。
3. **验收一的城市样本口径**：正文写「造 12 条已存岗位」同时 R1 闸门要求同城 ≥20 条，字面矛盾。落地为：12 条是**目标公司**的岗位（9 完整 JD、8 条同城可解析报价），同城样本不足 20 的部分由**其他公司的同城岗位**补足（测试里 14 条），两组闸门同时如实成立。
4. **闸门信号照常落库**：R1/R2 在统计量构不成时返回全零 value 而不是不出信号（公司没有任何岗位才不出）——这样缺口清单能显示 gate_note 的具体阈值（「同城 ≥20 条」「完整职责未采集」），而不是一句「尚无来源」。
5. **`ReportFinding`/`RuleFinding` 增加 `collected_at`**：前端展开维度要看「每条依据的计算时间」，这个时刻只能随句子一起持久化；过期判定仍在重算时做，过期依据落 `missing` 不进 `findings`。对外路径从五个变六个（`GET .../reliability/history`），红线测试同步更新。
6. **单公司 diff 的 material 归因**：material 的 score 恒为 null，纯分数对比看不见新材料。快照摘要因此分两份：`rules`（assessment 分数）与 `materials`（登记名单），diff 输出规则分数变化 + material 登记「新增/移除」，不给 material 造任何数字。
