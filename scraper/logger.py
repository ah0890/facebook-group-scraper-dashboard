"""Run logger: writes ``ScraperLog`` rows (shown in the live log panel) and mirrors
them to Python logging / an optional console echo for the CLI."""

from __future__ import annotations

import logging
from typing import Callable

from django.db import DatabaseError
from django.utils import timezone

LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "SUCCESS": 25, "WARNING": 30, "ERROR": 40}
_PY_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "SUCCESS": logging.INFO,
              "WARNING": logging.WARNING, "ERROR": logging.ERROR}

RULE = "-" * 40

pylog = logging.getLogger("scraper")

EchoFn = Callable[[str, str], None]


class RunLogger:
    """Structured logger bound to one ``ScraperRun``.

    Logging must never crash a run, so database errors while writing a log row are
    reported to the Python logger and otherwise swallowed.
    """

    def __init__(self, run, min_level: str = "INFO", echo: EchoFn | None = None):
        self.run = run
        self.min_level = LEVEL_ORDER.get(min_level.upper(), 20)
        self.echo = echo
        self.phase = run.current_phase if run is not None else "IDLE"
        self.group = None

    # -- context --------------------------------------------------------------

    def set_phase(self, phase: str) -> None:
        if phase == self.phase:
            return
        self.phase = phase
        if self.run is None:
            return
        from dashboard.models import ScraperRun

        try:
            ScraperRun.objects.filter(pk=self.run.pk).update(current_phase=phase, heartbeat_at=timezone.now())
            self.run.current_phase = phase
        except DatabaseError:
            pylog.exception("Could not update run phase")

    def set_group(self, group) -> None:
        self.group = group

    # -- writing ----------------------------------------------------------------

    def log(self, level: str, message: str) -> None:
        level = level.upper()
        pylog.log(_PY_LEVELS.get(level, logging.INFO), "[run %s] %s", getattr(self.run, "pk", "-"), message)
        if LEVEL_ORDER.get(level, 20) < self.min_level:
            return
        if self.echo:
            self.echo(level, message)
        from dashboard.models import ScraperLog

        try:
            ScraperLog.objects.create(
                run=self.run, level=level, phase=self.phase, group=self.group, message=message
            )
        except DatabaseError:
            pylog.exception("Could not write scraper log row")

    def debug(self, message: str) -> None:
        self.log("DEBUG", message)

    def info(self, message: str) -> None:
        self.log("INFO", message)

    def success(self, message: str) -> None:
        self.log("SUCCESS", message)

    def warning(self, message: str) -> None:
        self.log("WARNING", message)

    def error(self, message: str) -> None:
        self.log("ERROR", message)

    def rule(self) -> None:
        self.info(RULE)
