"""Console + rotating-file logging."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False


def setup_logging(log_dir: Path, level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    fileh = RotatingFileHandler(
        log_dir / "social_peace.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    fileh.setFormatter(fmt)
    root.addHandler(fileh)

    # third-party noise
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    _CONFIGURED = True
