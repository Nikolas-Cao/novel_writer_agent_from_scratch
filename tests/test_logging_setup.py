"""logging_setup：按日轮转文件与幂等初始化。"""
from __future__ import annotations

import logging
import shutil
import tempfile
import unittest
from pathlib import Path

from logging_setup import configure_app_logging, reset_logging_configuration_for_tests


class TestLoggingSetup(unittest.TestCase):
    def test_configure_writes_to_custom_dir(self) -> None:
        td = tempfile.mkdtemp()
        try:
            tmp_path = Path(td)
            configure_app_logging(log_dir=tmp_path, log_to_console=False, force=True)
            log = logging.getLogger("tests.logging_probe")
            log.info("probe_message_123")
            matches = list(tmp_path.glob("app.log*"))
            self.assertTrue(matches, "应在 log_dir 下创建 app.log")
            text = (tmp_path / "app.log").read_text(encoding="utf-8")
            self.assertIn("probe_message_123", text)
        finally:
            reset_logging_configuration_for_tests()
            shutil.rmtree(td, ignore_errors=True)

    def test_configure_is_idempotent(self) -> None:
        td = tempfile.mkdtemp()
        try:
            tmp_path = Path(td)
            configure_app_logging(log_dir=tmp_path, log_to_console=False, force=True)
            n1 = len(logging.getLogger().handlers)
            configure_app_logging(log_dir=tmp_path, log_to_console=False, force=False)
            n2 = len(logging.getLogger().handlers)
            self.assertEqual(n1, n2)
        finally:
            reset_logging_configuration_for_tests()
            shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
