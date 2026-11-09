# JobScope 数据源与浏览器实际验证

日期：2026-11-07（Asia/Shanghai）。本文记录当前机器上的有限验证，不承诺长期访问成功率。没有使用个人 Cookie，没有登录招聘平台，没有保存认证响应或签名种子。

## 公开接口

| 来源 | 实际请求 | 实际结果 | 可交付边界 |
| --- | --- | --- | --- |
| Remotive | GET `https://remotive.com/api/remote-jobs` 及前期带 search/limit 的探测 | HTTP 200，JSON `jobs` 列表；带查询参数返回的结果未可靠反映过滤条件 | 已实现公开接口采集、归档和本地关键词/地点过滤。非中国招聘来源；不是国内采集成功的替代证据 |
| 巨潮资讯 | POST `https://www.cninfo.com.cn/new/hisAnnouncement/query`，`searchkey=比亚迪`、`column=szse`、`tabName=fulltext`、`pageNum=1`、`pageSize=3` | HTTP 200，3条真实公告元信息 | 当前连接器读取标题、主体简称、证券代码、发布时间和附件链接；不据此宣称解析了财报正文 |
| BOSS 直聘 | GET `https://www.zhipin.com/wapi/zpgeek/search/joblist.json`，关键词 Python、北京、第一页 | HTTP 200，业务 `code=37` | 转换为 `needs_verification`，未取得岗位，不能标成采集成功；需用户登录后继续验证浏览器响应路径 |
| Arbeitnow | GET `https://www.arbeitnow.com/api/job-board-api` | HTTP 403，验证 HTML | 未列为首版已支持来源 |

巨潮连接器真实返回的例子：

1. `2026年10月产销快报`，披露主体简称“比亚迪”，发布时间 `2026-11-02T00:07:45+00:00`，[附件](https://static.cninfo.com.cn/finalpage/2026-11-02/1225651111.PDF)。
2. `2026年三季度报告`，披露主体简称“比亚迪”，发布时间 `2026-10-29T16:00:00+00:00`，[附件](https://static.cninfo.com.cn/finalpage/2026-10-30/1225641442.PDF)。

首次使用限定2025–2026日期的探测返回总数272，移除日期限定后的探测返回总数2411，说明日期口径影响结果；最终连接器按时间降序、有上限地查询，而非宣称已取得全部历史公告。发布时刻是接口值的 UTC 表示，不代表报告覆盖期间。

Remotive 公告要求避免频繁轮询并说明职位可能延迟24小时。实现采用全页快照6小时缓存，再本地筛选，UI和结果提示保留原始来源。样本中带参数的远端结果未可靠过滤，因此不依赖远端过滤语义。共享代理、缓存或来源行为均可能影响观察，原因尚未独立确定。

## 浏览器本机生命周期

- 环境：Windows、本机 Google Chrome、项目虚拟环境中的 DrissionPage 4.1.1.4。
- 命令：`jobscope/.venv/Scripts/python.exe jobscope/scripts/smoke_browser.py`。
- 配置目录：`jobscope/.data/browser-smoke/browser/profile`，与日常浏览器资料分离。
- 验证内容：启动无界面 Chrome、打开 `data:` 本地页面、检查标题、通过仅本机 CDP 读取运行状态、退出后状态为未运行。
- 结果：沙箱内出现 `PageDisconnectedError` 或 `BrowserConnectError`；按宿主要求在沙箱外执行后返回 `{"chrome_lifecycle":"passed","profile":"project-only","external_navigation":false}`，进程退出码0。
- 本次没有进行外部浏览器导航、没有测试登录恢复、没有验证 BOSS 页面响应采集成功率。生产 `start()` 为用户可见的专属浏览器；只有用户启动时才打开平台页面。

浏览器采集目前只监听正常搜索产生的岗位列表响应，单次有上限，不循环重试、不读取主配置目录、不持久化 Cookie 到证据库。登录和验证由用户操作。完整岗位职责详情尚未接入，列表记录明确标记摘要；不得以列表摘要宣称完成完整 JD 的适配分析。

## 自动化验证

`test_connectors.py` 先经历未实现模块导致的8项预期失败，再实现连接器；扩展测试分别验证锁竞争与异常结构导致的失败，然后修复。

覆盖：BOSS验证状态、HTTP 200 HTML异常、Remotive本地过滤和缓存、巨潮POST与分页去重、主体简称保留、外部附件URL排除、未知来源拒绝、DrissionPage缺失状态、429不重试、浏览器进程锁、异常JSON结构及跨域重定向拒绝。

测试不会访问真实平台，不会在报告中展示种子、令牌或完整认证响应。

## 报告正文与本地应用集成

主代理通过运行中的 FastAPI 服务，以独立验收数据库执行：

1. `POST /api/collect` 查询比亚迪，返回3条公告，任务状态 `completed`。
2. 从已保存的三季度公告发起 `POST /api/evidence/{id}/extract`，读取上述官方 PDF，返回 `pages=10`、`total_pages=10`、`truncated=false`。
3. `POST /api/chat` 以公司“比亚迪”为范围，提问营业收入和经营现金流，返回 `mode=evidence_search`，其中报告正文摘录定位到第2页，包含主要财务数据原文。

这次验收证明公开公司资料检索、报告读取、保存及问答召回链路可运行。没有配置外部模型，因此不宣称真实模型推理已验收；模型请求、引用及语义评分通过受控响应测试验证。

报告提取默认最多60页、12万字符或20MB下载，超出时明确显示截断。图像型扫描件未包含OCR，空白页不会仅因加入页码标签而作为正文成功。表格提取为文本可能损失布局，涉及列对应关系、币种与报告期间时必须打开原文核对。

本地 UI 使用独立验收资料，经过 Chrome/Playwright 操作验证：手动岗位导入、简历保存、规则分析、带引用摘录、公司资料补充、全库查询、来源与任务页，以及390px宽度无横向溢出。材料为显式虚构验收内容，不进入默认工作库。截图保存在版本控制忽略的 `jobscope/output/`。

## 最终检查结果

- `python -m pytest jobscope/backend/tests -q --basetemp=jobscope/output/pytest-release --tb=short`：70 passed，54.56秒。5条警告来自当前 PyMuPDF 的SWIG绑定弃用信息，无测试失败。
- `python -m compileall -q jobscope/backend/jobscope`：退出码0。
- `npm --prefix jobscope/frontend run build`：TypeScript和Vite通过，30个模块，退出码0。
- `python jobscope/scripts/smoke_ui.py`：`ui_smoke=passed`、无pageerror、`mobile_overflow=false`；包括岗位和独立公司范围追问。
- `git diff --check`：退出码0，仅Git换行转换提示。
- 干净工作区服务：`http://127.0.0.1:8765` 首页200、health正常、默认岗位数0、浏览器依赖已安装且尚未启动。

本次未验证外部模型的真实效果，未验证BOSS登录后采集、完整JD详情、登录恢复或浏览器翻页。员工薪资及评价平台尚未接入；实际薪资和团队情况没有证据时保持未知。模型语义映射存在漏项风险，代码会保底保留未映射JD语句为未知并关闭总分，但不能把形式校验等同于语义正确。
