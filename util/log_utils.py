"""Centralized logging for Porto Agent (copied from Task Bot's
util/log_utils.py). All logs live in _log/ at the project root, one file per
day (UTC) per type:

    _log/app_<YYYYMMDD>.log           general application process (main/app.py)
    _log/conversation_<YYYYMMDD>.log  one JSON line per user message sent
    _log/agent_<YYYYMMDD>.log         every Ark Managed Agent API call's input
                                       and output (one JSON object per line)
    _log/guard_<YYYYMMDD>.log         one JSON line per reply review (metrics,
                                       never the reply text)
"""

import json
from datetime import datetime, timezone
from logging import Formatter, INFO, Logger, getLogger
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from threading import Lock

LOG_DIR = Path(__file__).resolve().parent.parent / "_log"
LOG_DIR.mkdir(exist_ok=True)

_agent_log_lock = Lock()
_conversation_log_lock = Lock()
_guard_log_lock = Lock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


class _DailyFileHandler(TimedRotatingFileHandler):
    """The active file is always "<prefix>_<today>.log" - at UTC midnight it
    opens tomorrow's filename instead of renaming the current one."""

    def __init__(self, prefix: str):
        self._prefix = prefix
        super().__init__(str(LOG_DIR / f"{prefix}_{_today()}.log"), when="midnight",
                         interval=1, backupCount=0, encoding="utf-8", utc=True)

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        self.baseFilename = str(LOG_DIR / f"{self._prefix}_{_today()}.log")
        self.stream = self._open()
        self.rolloverAt = self.computeRollover(int(datetime.now(timezone.utc).timestamp()))


def get_app_logger(name: str = "app") -> Logger:
    logger = getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(INFO)
    logger.propagate = False
    handler = _DailyFileHandler("app")
    handler.setFormatter(Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    logger.addHandler(handler)
    return logger


def _append_json_line(prefix: str, lock: Lock, entry: dict) -> None:
    path = LOG_DIR / f"{prefix}_{_today()}.log"
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(line)


def log_agent_call(*, endpoint: str, status: str, elapsed_seconds: float, request: dict,
                   response: dict | None = None, error: str | None = None) -> None:
    _append_json_line("agent", _agent_log_lock, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "endpoint": endpoint, "status": status, "elapsed_seconds": elapsed_seconds,
        "request": request, "response": response, "error": error,
    })


def log_conversation_turn(*, session_id: str, role: str, text: str, attachments: list | None = None) -> None:
    _append_json_line("conversation", _conversation_log_lock, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id, "role": role, "text": text, "attachments": attachments or [],
    })


def log_guard_review(*, session_id: str, event_id: str, status: str, metrics: dict | None = None,
                     error: str | None = None) -> None:
    _append_json_line("guard", _guard_log_lock, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id, "event_id": event_id, "status": status,
        **(metrics or {}), **({"error": error} if error else {}),
    })
