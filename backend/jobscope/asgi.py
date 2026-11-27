"""ASGI 入口：模块级 app 只在这里建（2026-11-27 审计 P0）。

此前 `app = create_app()` 写在 main.py 末尾，任何 `import jobscope.main`（临时脚本、
测试收集、审计工具）都会先用默认 data_dir——项目根 `.data/`（生产库）——建一次应用并跑
建表/迁移/启动清扫。挪到本文件后，import main 不再有副作用；启动用：

    python -m uvicorn jobscope.asgi:app --host 127.0.0.1 --port 8765
"""
from .main import create_app

app = create_app()
