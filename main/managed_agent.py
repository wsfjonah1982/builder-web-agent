"""BytePlus Ark Managed Agent client for Porto Agent, copied from Task Bot's
main/managed_agent.py. This app never creates or deletes sessions - it talks
to a pre-existing session: the one the project maps to in
project_setting.json, or one passed in by the caller.

Every call is logged via util.log_utils.log_agent_call, one JSON line per
call in _log/agent_<date>.log.
"""

import base64
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from util.log_utils import log_agent_call  # noqa: E402

CREDENTIAL_FILE = BASE_DIR / "credential.json"
CONFIG_FILE = BASE_DIR / "config.json"

# Safety cap on REST history paging (the API returns ~100 events a page).
MAX_HISTORY_PAGES = 200


class ManagedAgentError(Exception):
    pass


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def load_agent_config() -> dict:
    cfg = _load_json(CONFIG_FILE).get("agent", {})
    cfg.setdefault("base_url", "https://ark.ap-southeast.bytepluses.com/api/v3")
    cfg.setdefault("session_id", "")
    cfg.setdefault("max_upload_mb", 8)
    cfg.setdefault("max_document_chars", 20000)
    return cfg


def api_key() -> str:
    return _load_json(CREDENTIAL_FILE).get("agent_ark_key", "")


def _url(path: str) -> str:
    return f"{load_agent_config()['base_url'].rstrip('/')}{path}"


def _request(method: str, path: str, *, log_request: dict | None = None, log_response=None, **kwargs):
    """Authenticated call to the Ark API. `log_response` optionally maps the
    parsed JSON to what gets logged (e.g. to drop presigned URLs or bulky
    event pages)."""
    key = api_key()
    if not key:
        raise ManagedAgentError("Missing agent_ark_key in credential.json")
    url = _url(path)
    headers = {"Authorization": f"Bearer {key}", **kwargs.pop("headers", {})}
    start = time.time()
    resp = None
    try:
        resp = requests.request(method, url, headers=headers, timeout=kwargs.pop("timeout", 30), **kwargs)
        resp.raise_for_status()
        # A request with nothing to return has been observed sending a bare
        # `null` body - normalize so callers can always .get().
        data = resp.json() if resp.content else {}
        data = data or {}
        log_agent_call(endpoint=url, status="success", elapsed_seconds=time.time() - start,
                       request=log_request or {}, response=log_response(data) if log_response else data)
        return data
    except Exception as e:
        detail = resp.text if resp is not None else str(e)
        log_agent_call(endpoint=url, status="error", elapsed_seconds=time.time() - start,
                       request=log_request or {}, error=detail)
        raise ManagedAgentError(detail) from e


def session_id(sid: str | None = None) -> str:
    sid = sid or load_agent_config()["session_id"]
    if not sid:
        raise ManagedAgentError('Missing session_id in config.json["agent"]')
    return sid


def get_session(sid: str | None = None) -> dict:
    """GET /sessions/{id} - notably `status` (idle/running/terminated) and
    `agent` (name, description, model)."""
    return _request("GET", f"/sessions/{session_id(sid)}",
                    log_response=lambda d: {k: d.get(k) for k in ("id", "status")})


def _page_cursor(since: str) -> str:
    """The API's opaque `next_page` cursor is base64("<ISO timestamp>")
    prefixed with "page_", so an arbitrary starting timestamp can be used as
    a cursor. It only accepts UTC, millisecond precision, "Z" suffix -
    not the raw `processed_at` (non-UTC offset, nanoseconds)."""
    normalized = parse_ts(since)
    cursor_ts = normalized.strftime("%Y-%m-%dT%H:%M:%S.") + f"{normalized.microsecond // 1000:03d}Z"
    return "page_" + base64.b64encode(cursor_ts.encode()).decode()


def parse_ts(value: str) -> datetime:
    """Parse an event's `processed_at` (e.g. "...20:28:12.410590177+08:00",
    nanosecond precision, which datetime.fromisoformat rejects on older
    Pythons) into an aware UTC datetime."""
    head, sep, frac = value.partition(".")
    if sep:
        digits = "".join(ch for ch in frac if ch.isdigit())
        tz = frac[len(digits):]
        value = f"{head}.{digits[:6].ljust(6, '0')}{tz}"
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def get_events(since: str | None = None, sid: str | None = None) -> list:
    """GET /sessions/{id}/events (paginated REST history), oldest first -
    the whole history when `since` is None, otherwise from that timestamp."""
    path = f"/sessions/{session_id(sid)}/events"
    page = _page_cursor(since) if since else None
    events = []
    for _ in range(MAX_HISTORY_PAGES):
        data = _request("GET", path, params={"page": page} if page else {},
                        log_request={"page": page},
                        log_response=lambda d: {"count": len(d.get("data") or [])})
        events.extend(data.get("data") or [])
        page = data.get("next_page")
        if not page:
            break
    return events


def send_message(content: list, sid: str | None = None) -> dict:
    """POST a user.message event (a list of content blocks). Returns the
    created event record(s), notably the server-assigned event `id`. The
    agent's turn then runs server-side; its events are picked up via
    get_events(since=...)."""
    payload = {"events": [{"type": "user.message", "content": content}]}
    return _request("POST", f"/sessions/{session_id(sid)}/events", json=payload,
                    log_request=payload, timeout=60)


def upload_file(file_bytes: bytes, filename: str, mime_type: str) -> dict:
    """POST /files (multipart). `purpose` is required - "user_data" is the
    one accepted for user-provided input that can be mounted."""
    return _request("POST", "/files", files={"file": (filename, file_bytes, mime_type)},
                    data={"purpose": "user_data"}, timeout=60,
                    log_request={"filename": filename, "mime_type": mime_type, "bytes": len(file_bytes)})


def add_session_resource(file_id: str, mount_path: str, sid: str | None = None) -> dict:
    """POST /sessions/{id}/resources - mount an uploaded file into the
    session's sandbox at /mnt/session/uploads<mount_path>."""
    payload = {"type": "file", "file_id": file_id, "mount_path": mount_path}
    return _request("POST", f"/sessions/{session_id(sid)}/resources", json=payload, log_request=payload)


def get_file(file_id: str) -> dict:
    """GET /files/{id} - metadata plus a presigned download_url."""
    return _request("GET", f"/files/{file_id}",
                    log_response=lambda d: {k: v for k, v in d.items() if k != "download_url"})
