"""Isolate main.py's module-level app before pytest imports any test modules.

Individual tests still use tmp_path for their apps/repositories. This directory only
contains initialization from `jobscope.main.app = create_app()` during import.
Also keeps the Boss applied-sync scheduler thread off for the whole session: no test may
end up launching a real browser from a timer, even one that flips the setting on.
"""
import os
from pathlib import Path
from tempfile import TemporaryDirectory


_previous_data_dir = os.environ.get('JOBSCOPE_DATA_DIR')
_import_data = TemporaryDirectory(prefix='jobscope-pytest-app-')
os.environ['JOBSCOPE_DATA_DIR'] = str(Path(_import_data.name).resolve())
# 定时同步调度线程的总闸：用例里打开「自动同步」也只验证设置读写，绝不起线程去拉浏览器。
_previous_auto_sync = os.environ.get('JOBSCOPE_DISABLE_AUTO_SYNC')
os.environ['JOBSCOPE_DISABLE_AUTO_SYNC'] = '1'


def pytest_sessionfinish(session, exitstatus):
    _import_data.cleanup()
    if _previous_data_dir is None:
        os.environ.pop('JOBSCOPE_DATA_DIR', None)
    else:
        os.environ['JOBSCOPE_DATA_DIR'] = _previous_data_dir
    if _previous_auto_sync is None:
        os.environ.pop('JOBSCOPE_DISABLE_AUTO_SYNC', None)
    else:
        os.environ['JOBSCOPE_DISABLE_AUTO_SYNC'] = _previous_auto_sync
