"""
Centralized logging setup for the enrichment pipeline.

Writes to logs/enrichment_YYYY-MM-DD.log with a console mirror.
Format is parseable and greppable: timestamp [LEVEL] logger: message
"""

import logging
from datetime import date
from pathlib import Path

_LOG_DIR = Path(__file__).parent.parent / "logs"
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(name: str = "enrichment", level: int = logging.INFO) -> logging.Logger:
    """Configure root logger for file + console output. Idempotent."""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _LOG_DIR / f"{name}_{date.today().isoformat()}.log"

    root = logging.getLogger()
    root.setLevel(level)

    if any(getattr(h, "_ua_configured", False) for h in root.handlers):
        return logging.getLogger(name)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler._ua_configured = True
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler._ua_configured = True
    root.addHandler(console_handler)

    return logging.getLogger(name)


def summary_path(name: str = "enrichment") -> Path:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    return _LOG_DIR / f"{name}_summary_{date.today().isoformat()}.txt"
