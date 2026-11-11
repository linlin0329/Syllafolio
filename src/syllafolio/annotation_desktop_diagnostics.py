"""Bounded persistent diagnostics for native UI operations, not observations."""
from __future__ import annotations
from .annotation_english import system_error

from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
from pathlib import Path
import tempfile
import traceback
import uuid


class DesktopDiagnostics:
    """Write JSON-line operation diagnostics without storing audio or labels."""

    def __init__(self, directory=None):
        configured = directory or os.environ.get("SYLLAFOLIO_DIAGNOSTICS_DIR")
        if configured is None:
            configured = Path(tempfile.gettempdir()) / "syllafolio-diagnostics" / ("process-" + str(os.getpid()))
        self.directory = Path(configured).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "runtime.log"
        self.run_id = os.environ.get("SYLLAFOLIO_RUN_ID") or "desktop-" + uuid.uuid4().hex
        self._logger = logging.getLogger("syllafolio.desktop." + uuid.uuid4().hex)
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._handler = RotatingFileHandler(self.path, maxBytes=2*1024*1024, backupCount=3,
                                           encoding="utf-8", delay=False)
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(self._handler)
        self._closed = False
        self._exception_hook = None
        self._previous_exception_hook = None

    def install_exception_hook(self, context_provider=None):
        """Persist exceptions escaping Qt Python callbacks, retaining stderr too."""
        if self._exception_hook is not None:
            return
        self._previous_exception_hook = sys.excepthook
        def hook(error_type, error, tb):
            context = context_provider() if context_provider else {}
            self.record_exception(error.with_traceback(tb), "unhandled Python exception", context)
            self._previous_exception_hook(error_type, error, tb)
        self._exception_hook = hook
        sys.excepthook = hook

    def record_event(self, level, message, operation="desktop", context=None):
        if self._closed:
            return
        event = {"recorded_at": datetime.now(timezone.utc).isoformat(),
                 "run_id": self.run_id, "event_id": uuid.uuid4().hex,
                 "level": str(level).upper(), "operation": str(operation),
                 "message": str(message), "context": dict(context or {})}
        severity = getattr(logging, str(level).upper(), logging.INFO)
        self._logger.log(severity, json.dumps(event, ensure_ascii=False, default=str))

    def record_exception(self, error, operation="desktop", context=None):
        if self._closed:
            return
        event = {"recorded_at": datetime.now(timezone.utc).isoformat(),
                 "run_id": self.run_id, "event_id": uuid.uuid4().hex,
                 "level": "ERROR", "operation": str(operation),
                 "exception_type": type(error).__name__,
                 "error_code": getattr(error, "code", None),
                 "message": system_error(error), "context": dict(context or {}),
                 "traceback": "".join(traceback.format_exception(type(error), error, error.__traceback__))}
        self._logger.error(json.dumps(event, ensure_ascii=False, default=str))

    def close(self):
        if not self._closed:
            if self._exception_hook is not None and sys.excepthook is self._exception_hook:
                sys.excepthook = self._previous_exception_hook
            self._handler.flush()
            self._handler.close()
            self._logger.removeHandler(self._handler)
            self._closed = True
