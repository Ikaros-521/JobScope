# 开发与测试数据纪律（硬性规则）

真实数据目录是项目根的 `.data/`（`jobscope.sqlite3` 及采集、浏览器工作目录），这是用户的生产数据。
任何开发、测试行为都**不得污染它**。代码里 `create_app()` 的默认 data_dir 是**绝对定位**的项目根
`.data`（见 `backend/jobscope/main.py`），换端口、换 cwd 都不会改变它，必须显式指定才会隔离。

## 规则

1. **pytest 一律隔离数据库**：用 `create_app(tmp_path)` / `Repository(tmp_path / ...)`，禁止缺省
   data_dir、禁止相对路径 data_dir（会把测试产物写进真实 `.data`，历史上出现过 `pytest-*` 残留
   目录）。跑测试用 `.venv`：`cd backend` 后 `..\.venv\Scripts\python.exe -m pytest`。

2. **手动 / 接口 / UI 验收一律打 8766 测试实例，禁止对 8765 写测试数据**。8765 是用户日常使用的
   生产实例。测试实例必须显式设独立数据目录后再启动：

   ```powershell
   $env:JOBSCOPE_DATA_DIR = "$env:TEMP\jobscope-test-data"
   .\start.ps1 -Port 8766        # 或 cd backend 后手动起 uvicorn --port 8766
   ```

   不设 `JOBSCOPE_DATA_DIR` 就起测试实例 = 与生产实例共用同一个库，端口不同也没用。

3. **万一往真实库注入了测试数据，测完必须清干净**：删除注入的岗位/公司/证据/收藏等记录及其派生
   （实体归并结果、向量索引、聊天记录），或测前备份 `.data/`、测后整目录还原。还原时先停实例，
   连同 `jobscope.sqlite3-wal` / `-shm` 一起处理。优先采用第 2 条隔离，清理只是兜底。

4. **测完不留垃圾**：`.data/` 里不许残留 `pytest-*`、`browser-smoke`、`test_*` 之类测试产物目录；
   临时脚本、截图、导出文件放 `output/` 或系统临时目录，不放 `.data/`。

## 已有约定（遵循即可）

- `scripts/smoke_*.py` 默认打 `http://127.0.0.1:8766` 测试实例，新增 smoke 脚本保持此约定。
- 现有 pytest 套件全部 tmp_path 隔离，新增测试照做。
- 对 8765 只做**只读**操作（GET、页面浏览）不在此限。
