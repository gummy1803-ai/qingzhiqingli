"""日志配置:统一管理全应用日志输出。

- 控制台输出 INFO 及以上级别
- 格式含时间 / 模块 / 级别 / 消息
- 通过 setup_logging() 在 app 入口调用一次即可
- 各模块只需 `import logging; logger = logging.getLogger(__name__)`
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"
_configured = False


def setup_logging(level: int = logging.INFO, log_file: str | None = None) -> None:
    """初始化全局日志配置。仅在应用入口调用一次。

    Args:
        level: 控制台输出级别,默认 INFO
        log_file: 可选,同时写入文件路径。None 则只输出到 stderr
    """
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(level)

    # 控制台 handler(Streamlit 环境下走 stderr 才能在终端看到)
    if not any(isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
               for h in root.handlers):
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(level)
        sh.setFormatter(logging.Formatter(_DEFAULT_FORMAT, _DEFAULT_DATEFMT))
        root.addHandler(sh)

    # 文件 handler(可选,用于审计/排查历史)
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(str(log_path), encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter(_DEFAULT_FORMAT, _DEFAULT_DATEFMT))
        root.addHandler(fh)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """便捷获取 logger,顺便确保已 setup。"""
    if not _configured:
        setup_logging()
    return logging.getLogger(name)
