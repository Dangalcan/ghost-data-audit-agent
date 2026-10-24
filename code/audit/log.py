"""Structured logging for the audit pipeline.

One logger hierarchy (`audit.*`), key=value style records so a grep over the run
log answers "which case, which model, how many tokens" without parsing prose.
"""

from __future__ import annotations

import logging
import sys

FORMAT = "%(asctime)s level=%(levelname)s logger=%(name)s %(message)s"


def configure(level: str = "INFO") -> None:
    """Configure the root `audit` logger once; later calls only adjust the level."""
    logger = logging.getLogger("audit")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(FORMAT, datefmt="%Y-%m-%dT%H:%M:%S"))
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"audit.{name}")


def kv(**fields: object) -> str:
    """Render fields as key=value pairs (values with spaces are quoted)."""
    parts = []
    for k, v in fields.items():
        s = str(v)
        parts.append(f'{k}="{s}"' if " " in s else f"{k}={s}")
    return " ".join(parts)
