import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import logger


class log_redirect:
    def __enter__(self):
        self._orig_dir = logger.LOG_DIR
        self._orig_handles = dict(logger._handles)
        self._tmp = tempfile.TemporaryDirectory()
        logger.LOG_DIR = Path(self._tmp.name) / "logs"
        logger._handles = {}
        return self

    def __exit__(self, *exc):
        for handle in logger._handles.values():
            try:
                handle.close()
            except OSError:
                pass
        logger._handles = self._orig_handles
        logger.LOG_DIR = self._orig_dir
        self._tmp.cleanup()
        return False
