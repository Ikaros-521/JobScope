"""Isolate main.py's module-level app before pytest imports any test modules.

Individual tests still use tmp_path for their apps/repositories. This directory only
contains initialization from `jobscope.main.app = create_app()` during import.
"""
import os
from pathlib import Path
from tempfile import TemporaryDirectory


_previous_data_dir = os.environ.get('JOBSCOPE_DATA_DIR')
_import_data = TemporaryDirectory(prefix='jobscope-pytest-app-')
os.environ['JOBSCOPE_DATA_DIR'] = str(Path(_import_data.name).resolve())


def pytest_sessionfinish(session, exitstatus):
    _import_data.cleanup()
    if _previous_data_dir is None:
        os.environ.pop('JOBSCOPE_DATA_DIR', None)
    else:
        os.environ['JOBSCOPE_DATA_DIR'] = _previous_data_dir
