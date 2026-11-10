# 岗位档案：筛选与批量编辑/删除（2026-11-10）

岗位档案此前只能按关键词+地点筛选（且复用采集表单的值），没有任何编辑和删除能力。本次加了信息源/薪资区间筛选、卡片多选、批量编辑（地点/薪资/标签）与批量删除（含按筛选一键全选）。

## 实现口径

- **薪资数值键**：`jobs.payload` 新增 `salary_monthly_min/max`（单位：元，由 `parse_salary` 从原文算出）和 `salary_parsed` 标记。`save_job` 每次写入都重算；`Repository` 启动时对缺键的老档案回填一次（有标记或薪资为空的不重复扫）。面议/年薪/时薪/外币解析不出每月数值，键为 None。
- **薪资筛选是重叠语义**：岗位月薪区间与给定区间有交集即命中（如 `3-5万` 会命中 20–35K 的筛选）；解析不出数值的岗位任何薪资筛选都不命中。前端输入单位 K/月，提交时 ×1000。
- **信息源筛选**：`payload.source` 精确匹配；`q`、`location` 口径不变，可叠加。
- **`GET /api/jobs/ids`**：同一套筛选返回全部命中 id（上限 20000），供"选筛选全部"。
- **`PATCH /api/jobs`**：body `{ids: 1..1000, location?, salary?, tags?}`，缺省字段不改；显式空串=清空，`tags: []`=清空标签。批量编辑字段只有地点/薪资/标签——批量改 JD、链接、公司名没有真实场景，且模型 `extra=forbid` 挡住。
- **编辑不重排 id、不写新证据**：`update_jobs` 原位改 payload（`save_job` 的 id 由 title/company/location 派生且每次落一条证据快照，批量编辑都不该触发）；改薪资时重算数值键。
- **删除级联**（`delete_jobs`，单事务）：删 `job_evidence` 链接、`reports`、仅被待删岗位引用的 evidence（多岗位共享的公司证据保留）、scope 指向这些岗位的 `conversations`，最后删 jobs 行。没有回收站，确认弹窗兜底。

## 验证（本机 8765 实例，1729 条真实档案）

```powershell
cd E:\Github_Pro\ResumeLoom\jobscope
.venv\Scripts\python.exe -m pytest backend/tests -q   # 464 passed（含新增 backend/tests/test_job_batch.py 9 项）
cd frontend
npm run build                                          # tsc -b + vite build 通过
```

- 老数据回填：重启实例后首条 `1-1.7万` → 10000/17000、`salary_parsed=true`；真实库筛选总数合理（zhaopin 1430，20–35K 1114，叠加 882）。
- API 全链路（自测导入岗，跑完即删）：`面议` 导入后数值键为 None → PATCH 改地点/薪资/标签 → id 不变、证据数不变、数值键变 15000/25000 → `q`+`salary_min` 能筛到 → batch-delete 返回 `{deleted:1}`，再查 404，档案总数恢复 1729。
- 浏览器 UI 实测：筛选栏（关键词/信息源/地点/薪资 K）筛出"岗位档案 882"，与 API 一致；勾选 1 条出现 批量编辑/删除/取消选择；批量编辑弹窗提示"将应用到已选的 1 份档案"且留空不改；删除确认弹窗写明连带删除证据、报告与对话；两弹窗取消均无副作用；清除筛选恢复 1729。

## 边界与未测

- 大批量（几百条）的真实删除只在单测里跑过（HTTP 层 1000 条上限校验），真实档案上的多量大删由用户自行操作，路径与自测相同。
- `parse_salary` 口径不变：`1-1.7万`、`20-30K`、`3-5万` 可解析；`18元/时`、`薪水面议`、美元年薪不参与薪资筛选。
- 筛选"选筛选全部"依赖 `/api/jobs/ids` 与列表同一 where 构建，单测断言两者 total 一致；未测超过 20000 条的截断行为（当前库 1729 条）。
