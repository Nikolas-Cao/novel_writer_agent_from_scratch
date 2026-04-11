"""
应用级 logging 初始化：将各模块 `logging.getLogger(__name__)` 输出持久化到本地文件。

思路：
1) 使用 TimedRotatingFileHandler(when='midnight')，自然日切换，每天对应一个历史文件；
2) 当前日写入基名 `app.log`，轮转后生成 `app.log.YYYY-MM-DD`（与标准库命名一致）；
3) 挂到 root logger，子 logger 默认 propagate，无需改各文件里的 getLogger；
4) `configure_app_logging` 可重复调用（幂等），避免热重载或重复 import 时重复追加 Handler。

输入前提：
- 目录 `APP_LOG_DIR` 可写；若不可用则仅打 stderr 并跳过文件 Handler。
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

from config import (
    APP_LOG_DIR,
    LOG_BACKUP_DAYS,
    LOG_FILE_ENABLED,
    LOG_LEVEL_NAME,
    LOG_TO_CONSOLE,
)

_CONFIGURED = False

# 用于识别本模块添加到 root 的 Handler，便于 force 时移除
_MARKER_ATTR = "_novel_writer_app_file_handler"


def _resolve_level(name: str) -> int:
    level = getattr(logging, name.upper(), None)
    if isinstance(level, int):
        return level
    return logging.INFO


def _remove_our_handlers(root: logging.Logger) -> None:
    to_remove = [h for h in root.handlers if getattr(h, _MARKER_ATTR, False)]
    for h in to_remove:
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass


def configure_app_logging(
    *,
    log_dir: Optional[Path] = None,
    backup_days: Optional[int] = None,
    level_name: Optional[str] = None,
    log_to_console: Optional[bool] = None,
    log_file_enabled: Optional[bool] = None,
    force: bool = False,
) -> None:
    """
    配置根 logger：可选控制台 + 按日轮转文件。

    测试或子进程可传入 log_dir / force 等覆盖默认行为。
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    root = logging.getLogger()
    if force:
        _remove_our_handlers(root)

    level_str = level_name or LOG_LEVEL_NAME
    root.setLevel(_resolve_level(level_str))

    use_console = LOG_TO_CONSOLE if log_to_console is None else log_to_console
    if use_console:
        has_stream = any(isinstance(h, logging.StreamHandler) and h.stream in (sys.stdout, sys.stderr) for h in root.handlers)
        if not has_stream:
            sh = logging.StreamHandler(sys.stderr)
            sh.setLevel(root.level)
            sh.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
            )
            setattr(sh, _MARKER_ATTR, True)
            root.addHandler(sh)

    file_ok = LOG_FILE_ENABLED if log_file_enabled is None else log_file_enabled
    base_dir = Path(log_dir or APP_LOG_DIR)
    days = int(backup_days if backup_days is not None else LOG_BACKUP_DAYS)

    if file_ok:
        try:
            base_dir.mkdir(parents=True, exist_ok=True)
            log_path = base_dir / "app.log"
            fh = logging.handlers.TimedRotatingFileHandler(
                filename=str(log_path),
                when="midnight",
                interval=1,
                backupCount=max(1, days),
                encoding="utf-8",
                utc=False,
            )
            fh.setLevel(root.level)
            fh.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
            )
            setattr(fh, _MARKER_ATTR, True)
            root.addHandler(fh)
        except OSError as e:
            # 落盘失败时不阻断进程，仅告警（例如只读目录）
            root.warning("logging_setup: 无法创建日志文件于 %s: %s", base_dir, e)

    _CONFIGURED = True


def reset_logging_configuration_for_tests() -> None:
    """仅测试用：清除幂等标记并移除本模块注册的 root Handler。"""
    global _CONFIGURED
    _CONFIGURED = False
    _remove_our_handlers(logging.getLogger())
