"""test_logging.py — 日志写入、格式、目录不可写时的安全降级、重复初始化。"""

import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from logging_setup import APP_NAME, get_logger, setup_logging


class LoggingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        logger = logging.getLogger(APP_NAME)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def test_log_written_with_format(self):
        log_path = self.root / "logs" / "app.log"
        setup_logging(log_path)
        get_logger("test.module").info("hello world")
        for handler in logging.getLogger(APP_NAME).handlers:
            handler.flush()
        content = log_path.read_text(encoding="utf-8")
        self.assertIn("hello world", content)
        self.assertIn("INFO", content)
        self.assertIn("MangaUpscaler.test.module", content)

    def test_unwritable_log_dir_degrades_to_stderr(self):
        blocker = self.root / "blocker"
        blocker.write_text("x")          # 用文件挡住目录，使 mkdir 失败
        log_path = blocker / "app.log"
        logger = setup_logging(log_path)  # 不应抛异常
        self.assertTrue(any(isinstance(h, logging.StreamHandler)
                            for h in logger.handlers))

    def test_unwritable_and_no_stderr_uses_nullhandler(self):
        blocker = self.root / "blocker"
        blocker.write_text("x")
        log_path = blocker / "app.log"
        with mock.patch.object(sys, "stderr", None):
            logger = setup_logging(log_path)  # 不应抛异常
        self.assertTrue(any(isinstance(h, logging.NullHandler)
                            for h in logger.handlers))

    def test_repeated_setup_no_duplicate_handlers(self):
        log_path = self.root / "logs" / "app.log"
        setup_logging(log_path)
        setup_logging(log_path)
        self.assertEqual(len(logging.getLogger(APP_NAME).handlers), 1)

    def test_repeated_setup_single_log_line(self):
        log_path = self.root / "logs" / "app.log"
        setup_logging(log_path)
        setup_logging(log_path)
        get_logger("test.module").info("once")
        for handler in logging.getLogger(APP_NAME).handlers:
            handler.flush()
        content = log_path.read_text(encoding="utf-8")
        self.assertEqual(content.count("once"), 1)  # 不重复输出同一条消息

    def test_logger_error_does_not_raise(self):
        blocker = self.root / "blocker"
        blocker.write_text("x")
        log_path = blocker / "app.log"
        with mock.patch.object(sys, "stderr", None):
            setup_logging(log_path)
        get_logger("test.module").error("boom")  # 不应抛异常


if __name__ == "__main__":
    unittest.main()
