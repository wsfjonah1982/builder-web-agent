"""Porto Agent.

A single-page, no-login Flask site for one PortoBuild project, adapted from
Task Bot (web_app/task-bot). The page takes a project id (`?project=<id>`,
or config.json["project"]["default_project_id"]) and has four tabs:

- Chat: talks to the BytePlus Ark Managed Agent session the project maps to
  in project_setting.json (`?session=<id>` overrides it). As in Task Bot, the
  history is read straight from the session's REST event history (nothing is
  stored locally), a message is posted as a user.message event, and the page
  polls for the agent's events until the session goes back to idle. Every
  agent reply passes through Task Bot's response guardrail (guard/) first:
  it is reviewed for sensitive content, rewritten if needed, and labelled
  with a reply category (Done / Gate / WFT / Answer / Progress / Fail). The
  original text is never sent to the page; results are cached per event
  under _evaluations/.
- Brainstorm: shows the project's brainstorm page on object storage
  (config.json["project"]["brainstorm_url_template"]) in an iframe. The
  page is fetched server-side and served from this site (/brainstorm/<id>,
  with a <base> pointing back at TOS), so the parent page can read the idea
  picked in it; "Send for Analysis" copies just that idea to a new timestamp
  folder under the design folder.
- Design: shows the project's design picks on TOS, grouped by timestamp
  folder, newest first, filled by the Brainstorm tab's "Send for Analysis"
  (design.py). Each image can be edited (canvas tools, crop and Seedream,
  adapted from jonah-image-editor; seedream.py, static/editor.js) and saved
  back over itself, and each group can be sent to the agent in the chat for
  the analysis report ("Request Analysis Report").
- Analysis: shows the project's analysis page
  (config.json["project"]["analysis_url_template"]) the same way as the
  Brainstorm tab (/analysis/<id>).
"""

import base64
import html
import json
import os
import re
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import markdown
import requests
from flask import Flask, Response, jsonify, render_template, request, send_file
from markupsafe import Markup
from werkzeug.utils import secure_filename

import design
import guard
import managed_agent
import seedream

MAIN_DIR = Path(__file__).resolve().parent
BASE_DIR = MAIN_DIR.parent
sys.path.insert(0, str(BASE_DIR))
from util.log_utils import get_app_logger, log_conversation_turn, log_guard_review  # noqa: E402

CONFIG_FILE = BASE_DIR / "config.json"
EVALUATIONS_DIR = BASE_DIR / "_evaluations"
UI_TEXT_FILE = BASE_DIR / "ui_text.md"

logger = get_app_logger()
app = Flask(__name__)

IMAGE_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
# Only images and PDF are accepted by the Files API for mounting into the
# sandbox; plain-text documents are inlined into the message instead.
MOUNTABLE_MIME = {**IMAGE_MIME, "pdf": "application/pdf"}
INLINE_TEXT_EXTENSIONS = {"txt", "md", "csv", "json"}
ALLOWED_EXTENSIONS = set(MOUNTABLE_MIME) | INLINE_TEXT_EXTENSIONS

# Prefixes of the content blocks this app appends to a user.message for
# attachments - hidden from the rendered bubble and shown as chips instead.
UPLOAD_NOTE_RE = re.compile(r"^\[Uploaded file available at (?P<path>[^\]]+)\]$")
ATTACHED_NOTE_RE = re.compile(r"^\[Attached file: (?P<name>[^\]]+)\]\n")
# Leads the next message until the session has been told the project id.
PROJECT_NOTE = "[Project id: {project_id}]"
PROJECT_NOTE_RE = re.compile(r"^\[Project id: (?P<id>[^\]]+)\]$")
SESSION_ID_RE = re.compile(r"^sesn-[A-Za-z0-9-]{1,64}$")
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

STATUS_EVENTS = {
    "session.status_idle": "idle", "session.thread_status_idle": "idle",
    "session.status_running": "running", "session.thread_status_running": "running",
    "session.status_terminated": "terminated", "session.thread_status_terminated": "terminated",
}

# file_id -> filename for images attached via the Files API. Those files
# (purpose "user_data") have no download_url and no content endpoint, so
# they can't be displayed - they're shown as a filename chip instead.
_file_names: dict = {}


def _root_config() -> dict:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.is_file() else {}


def guard_config() -> dict:
    cfg = _root_config().get("guard", {})
    cfg.setdefault("enabled", True)
    cfg.setdefault("max_parallel_reviews", 4)
    return cfg


# Page wording, editable in ui_text.md; these are the fallbacks for any key
# missing there.
DEFAULT_UI_TEXT = {
    "view_output_button": "View Output",
    "view_brainstorm_button": "View Brainstorm",
    "view_analysis_button": "View Analysis",
    "analysis_report_button": "Request Analysis Report",
    "analysis_request": "Please write the analysis report for the design selected below and publish it as "
                        "analysis.html: look at its images in the design folder (including the ones I "
                        "edited), rank it against the other ideas, and give the reasons for the ranking.",
    "go_button": "Go",
    "go_reply": "Go",
    "review_failed_notice": "This reply couldn't be checked right now, so it's hidden. "
                            "Reload the page in a moment to try again.",
    "hint_done": "Result delivered",
    "hint_gate": "Waiting for your go-ahead",
    "hint_wft": "Waiting for your input",
    "hint_answer": "Complete answer",
    "hint_progress": "Still working",
    "hint_fail": "Couldn't complete the request",
    "hint_unchecked": "This reply couldn't be checked",
}
_ui_text_cache: dict = {"mtime": None, "text": DEFAULT_UI_TEXT}


def ui_text() -> dict:
    """DEFAULT_UI_TEXT overlaid with ui_text.md, where each `## key` heading
    starts an item and the text under it is the value. Re-read whenever the
    file changes."""
    try:
        mtime = UI_TEXT_FILE.stat().st_mtime
    except OSError:
        return DEFAULT_UI_TEXT
    if _ui_text_cache["mtime"] != mtime:
        text, key, lines = dict(DEFAULT_UI_TEXT), None, []
        for line in UI_TEXT_FILE.read_text(encoding="utf-8").splitlines() + ["## "]:
            if line.startswith("## "):
                if key in DEFAULT_UI_TEXT and "\n".join(lines).strip():
                    text[key] = "\n".join(lines).strip()
                key, lines = line[3:].strip(), []
            elif key is not None:
                lines.append(line)
        _ui_text_cache.update(mtime=mtime, text=text)
    return _ui_text_cache["text"]


def site_config() -> dict:
    cfg = _root_config().get("site", {})
    cfg.setdefault("title", "Porto Agent")
    cfg.setdefault("host", "0.0.0.0")
    cfg.setdefault("port", 7012)
    return cfg


def project_config() -> dict:
    cfg = _root_config().get("project", {})
    cfg.setdefault("setting_path", "project_setting.json")
    cfg.setdefault("default_project_id", "")
    cfg.setdefault("brainstorm_url_template",
                   "https://portobuild.tos-ap-southeast-1.bytepluses.com/brand/{project_id}/brainstorm.html")
    cfg.setdefault("analysis_url_template",
                   "https://portobuild.tos-ap-southeast-1.bytepluses.com/brand/{project_id}/analysis.html")
    return cfg


def project_settings() -> dict:
    """project_setting.json["projects"]: project id -> {"session_id", and
    optionally "brainstorm_url" / "analysis_url"}. Re-read on every call, so
    edits apply without a restart."""
    path = BASE_DIR / project_config()["setting_path"]
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("projects", {})
    except (OSError, ValueError) as e:
        logger.warning("failed to read %s: %s", path, e)
        return {}


def resolve_project(project_id: str) -> dict:
    """The project's session id and brainstorm / analysis page URLs. Raises
    ValueError for a malformed id; an id missing from project_setting.json
    resolves to no session (the pages can still be shown)."""
    if not PROJECT_ID_RE.match(project_id):
        raise ValueError(f'Invalid project id "{project_id}".')
    entry = project_settings().get(project_id) or {}
    cfg = project_config()
    return {"project_id": project_id, "session_id": entry.get("session_id") or "",
            "brainstorm_url": entry.get("brainstorm_url") or
            cfg["brainstorm_url_template"].format(project_id=project_id),
            "analysis_url": entry.get("analysis_url") or
            cfg["analysis_url_template"].format(project_id=project_id),
            "known": bool(entry)}


def markdown_filter(text: str) -> Markup:
    """Render model output as Markdown. The source is escaped first so any raw
    HTML the model emits (e.g. via prompt injection) renders as literal text
    instead of passing through - python-markdown doesn't sanitize raw HTML on
    its own, and this text ends up inserted via innerHTML client-side."""
    escaped = html.escape(text or "")
    return Markup(markdown.markdown(escaped, extensions=["extra", "sane_lists"]))


def static_url(filename: str) -> str:
    """Static URL with a `?v=<mtime>` cache-buster."""
    path = MAIN_DIR / "static" / filename
    try:
        version = int(path.stat().st_mtime)
    except OSError:
        version = 0
    return f"/static/{filename}?v={version}"


app.jinja_env.globals["static_url"] = static_url


def resolve_session_id() -> str:
    """The session to talk to: `session` from the query string or form (the
    page passes it through on every API call). Required and must be
    well-formed."""
    sid = (request.values.get("session") or "").strip()
    if not sid:
        raise ValueError("No session id given.")
    if not SESSION_ID_RE.match(sid):
        raise ValueError(f'Invalid session id "{sid}".')
    return sid


def _ts_ms(e: dict) -> int | None:
    try:
        return int(managed_agent.parse_ts(e["processed_at"]).timestamp() * 1000)
    except (KeyError, ValueError, TypeError):
        return None


def _file_name(file_id: str) -> str:
    if file_id not in _file_names:
        try:
            _file_names[file_id] = managed_agent.get_file(file_id).get("filename") or file_id
        except managed_agent.ManagedAgentError:
            return file_id  # not cached, so a transient failure is retried next time
    return _file_names[file_id]


def _image_src(block: dict) -> str | None:
    source = block.get("source") or {}
    if source.get("type") == "url" and str(source.get("url", "")).startswith("https://"):
        return source["url"]
    if source.get("type") == "base64" and source.get("media_type") in IMAGE_MIME.values():
        return f"data:{source['media_type']};base64,{source.get('data', '')}"
    return None


def _project_note_of(e: dict) -> str | None:
    """The project id a user.message event was led with, if any."""
    if e.get("type") != "user.message":
        return None
    for b in e.get("content") or []:
        if b.get("type") == "text" and (m := PROJECT_NOTE_RE.match(b.get("text", "").strip())):
            return m.group("id")
    return None


def event_to_item(e: dict) -> dict | None:
    """Convert one raw Managed Agent event into a render-ready item, or None
    for events with nothing to show (span.*, agent.thinking, and tool calls
    and their results, which are deliberately kept off the page)."""
    t = e.get("type")
    base = {"id": e.get("id"), "ts": _ts_ms(e), "processed_at": e.get("processed_at")}

    if t in STATUS_EVENTS:
        return {**base, "kind": "status", "status": STATUS_EVENTS[t]}

    if t == "user.message":
        texts, images, attachments, project = [], [], [], None
        for b in e.get("content") or []:
            if b.get("type") == "text":
                text = b.get("text", "")
                if m := PROJECT_NOTE_RE.match(text.strip()):
                    project = m.group("id")
                elif m := UPLOAD_NOTE_RE.match(text.strip()):
                    attachments.append(m.group("path").rsplit("/", 1)[-1])
                elif m := ATTACHED_NOTE_RE.match(text):
                    attachments.append(m.group("name"))
                else:
                    texts.append(text)
            elif b.get("type") == "image":
                source = b.get("source") or {}
                if source.get("type") == "file" and source.get("file_id"):
                    attachments.append(_file_name(source["file_id"]))
                elif src := _image_src(b):
                    images.append(src)
        text = "\n\n".join(t for t in texts if t)
        if not (text or images or attachments):
            return None
        return {**base, "kind": "user", "text": text, "images": images, "attachments": attachments,
                "project": project}

    if t == "agent.message":
        text = "".join(b.get("text", "") for b in e.get("content") or [] if b.get("type") == "text")
        if not text:
            return None
        # "_text" is consumed by _apply_guard() and never sent to the page.
        return {**base, "kind": "assistant", "_text": text}

    if t == "session.error":
        err = e.get("error") or {}
        message = err.get("message") or f"The agent session reported an error ({err.get('type', 'unknown')})."
        return {**base, "kind": "error", "text": message}

    return None


# ---------- Response guardrail (from Task Bot) ----------

def _evaluation_path(sid: str, event_id: str) -> Path:
    return EVALUATIONS_DIR / sid / f"{secure_filename(event_id)}.json"


def _last_question_path(sid: str) -> Path:
    return EVALUATIONS_DIR / sid / "_last_question.json"


def _load_json_file(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_json_atomic(path: Path, data) -> None:
    """Write to a temp file then rename, so a concurrent reader never sees a
    half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _evaluate_reply(sid: str, event_id: str, text: str, question: str) -> dict | None:
    """Run the guard on one agent reply, or return its cached result.
    Returns None if the review could not be completed."""
    cached = _load_json_file(_evaluation_path(sid, event_id))
    if cached:
        return cached
    cfg = guard_config()
    policy = {}
    if cfg.get("policy_path"):
        policy = _load_json_file(BASE_DIR / cfg["policy_path"]) or {}
    try:
        result = guard.evaluate(text, question, cfg, managed_agent.api_key(),
                                managed_agent.load_agent_config()["base_url"], policy)
    except Exception as e:  # noqa: BLE001 - any failure must hide the reply, never leak it
        logger.warning("guard review failed for %s/%s: %s", sid, event_id, e)
        log_guard_review(session_id=sid, event_id=event_id, status="failed", error=str(e))
        return None
    _save_json_atomic(_evaluation_path(sid, event_id), result)
    # Metrics only - never the reply text.
    metrics = {k: result[k] for k in ("verdict", "original_score", "rewritten_score", "counts",
                                      "also", "expects_go", "review_passes", "tokens_total", "elapsed_s", "model")}
    metrics["category"] = result["status"]  # "status" is the log entry's own succeeded/failed field
    metrics["has_output_url"] = bool(result["output_url"])
    log_guard_review(session_id=sid, event_id=event_id, status="succeeded", metrics=metrics)
    return result


def _apply_guard(items: list, sid: str) -> None:
    """In place: replace each assistant item's raw text with the guard's
    rewritten text, reply category and actions. The context given to the
    review is the latest user message before the reply."""
    cfg = guard_config()
    jobs, question = [], None
    for item in items:
        if item["kind"] == "user":
            question = item["text"]
        elif item["kind"] == "assistant":
            if question is None:  # polling window starts mid-turn - use the last message sent
                question = (_load_json_file(_last_question_path(sid)) or {}).get("text", "")
            jobs.append((item, question))

    if not cfg["enabled"]:
        for item, _ in jobs:
            item["html"] = str(markdown_filter(item.pop("_text")))
        return

    def run(job):
        item, q = job
        return _evaluate_reply(sid, item["id"] or uuid.uuid4().hex, item["_text"], q)

    with ThreadPoolExecutor(max_workers=max(1, int(cfg["max_parallel_reviews"]))) as pool:
        results = list(pool.map(run, jobs))

    for (item, _), result in zip(jobs, results):
        item.pop("_text")
        if result is None:
            item.update({"html": str(markdown_filter(ui_text()["review_failed_notice"])),
                         "category": "Unchecked", "also": []})
            continue
        item.update({
            "html": str(markdown_filter(result["text"])),
            "category": result["status"],
            "also": result["also"],
            "verdict": result["verdict"],
            # Reviews cached before find_links() dropped Markdown emphasis can
            # still end in "**".
            "output_url": (result["output_url"] or "").rstrip(".,;:!?*_~"),
        })


def _events_payload(events: list, sid: str) -> dict:
    items = [i for i in (event_to_item(e) for e in events) if i]
    _apply_guard(items, sid)
    cursor = next((e["processed_at"] for e in reversed(events) if e.get("processed_at")), None)
    try:
        status = managed_agent.get_session(sid).get("status", "unknown")
    except managed_agent.ManagedAgentError as e:
        logger.warning("failed to fetch session status: %s", e)
        status = "unknown"
    return {"items": items, "cursor": cursor, "status": status}


@app.route("/")
def index():
    project_id = (request.args.get("project") or project_config()["default_project_id"]).strip()
    error, project = "", {"project_id": project_id, "session_id": "", "brainstorm_url": "",
                          "analysis_url": "", "known": False}
    if project_id:
        try:
            project = resolve_project(project_id)
        except ValueError as e:
            error = str(e)
    session_override = (request.args.get("session") or "").strip()
    if session_override:
        project["session_id"] = session_override
    if not error and project["session_id"] and not SESSION_ID_RE.match(project["session_id"]):
        error = f'Invalid session id "{project["session_id"]}".'

    agent = {}
    if project["session_id"] and not error:
        try:
            agent = managed_agent.get_session(project["session_id"]).get("agent") or {}
        except managed_agent.ManagedAgentError as e:
            logger.warning("failed to fetch session for page header: %s", e)
    return render_template(
        "index.html",
        site=site_config(),
        project=project,
        agent_name=(agent.get("name") or "Agent").replace("_", " "),
        agent_description=agent.get("description") or "",
        error=error,
        ui_text={k: v for k, v in ui_text().items() if k != "review_failed_notice"},
    ), 400 if error else 200


@app.route("/api/project/<project_id>")
def api_project(project_id: str):
    try:
        return jsonify(resolve_project(project_id))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


def _project_or_400(project_id: str):
    try:
        return resolve_project(project_id), None
    except ValueError as e:
        return None, (jsonify({"error": str(e)}), 400)


BASE_RE = re.compile(r"""<base\b[^>]*>""", re.I)
MAX_PROJECT_PAGE_BYTES = 5 * 1024 * 1024


@app.route("/brainstorm/<project_id>")
def brainstorm_page(project_id: str):
    """The project's brainstorm page, served from this site's origin so the
    Brainstorm tab can read the idea selected in it (its DOM and its
    localStorage are then this site's)."""
    return _project_page(project_id, "brainstorm")


@app.route("/analysis/<project_id>")
def analysis_page(project_id: str):
    """The project's analysis page, for the Analysis tab."""
    return _project_page(project_id, "analysis")


def _project_page(project_id: str, kind: str) -> Response:
    """Fetch the project's `<kind>_url` page from TOS and serve it from this
    site. A <base href> at the page's real URL keeps its relative images and
    links pointing at TOS."""
    try:
        project = resolve_project(project_id)
    except ValueError as e:
        return Response(html.escape(str(e)), status=400, mimetype="text/plain")
    try:
        r = requests.get(project[f"{kind}_url"], timeout=20, headers={"Cache-Control": "no-cache"})
        r.raise_for_status()
        if len(r.content) > MAX_PROJECT_PAGE_BYTES:
            raise ValueError(f"The {kind} page is too large.")
    except (requests.RequestException, ValueError) as e:
        logger.warning("%s page fetch failed for %s: %s", kind, project_id, e)
        status = getattr(getattr(e, "response", None), "status_code", None)
        message = (f"This project has no {kind} page yet." if status == 404
                   else f"The {kind} page couldn't be loaded.")
        return Response(f"<!doctype html><meta charset=utf-8><p style='font:15px system-ui;padding:24px'>"
                        f"{html.escape(message)}</p>", status=502, mimetype="text/html")
    r.encoding = r.encoding if "charset" in r.headers.get("Content-Type", "") else "utf-8"
    page = BASE_RE.sub("", r.text)
    head = f'<base href="{html.escape(r.url, quote=True)}">'
    for tag_re in (r"<head\b[^>]*>", r"<html\b[^>]*>", r"<!doctype[^>]*>"):
        m = re.search(tag_re, page, re.I)
        if m:
            page = page[:m.end()] + head + page[m.end():]
            break
    else:
        page = head + page
    resp = Response(page, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------- Design image editor ----------

DATA_URL_RE = re.compile(r"^data:(image/(?:png|jpeg|webp));base64,(.+)$", re.S)


@app.route("/api/design/<project_id>/file/<path:name>")
def api_design_file(project_id: str, name: str):
    """A design-folder image ("<timestamp folder>/<file>") served from this
    site, for the editor canvas."""
    project, err = _project_or_400(project_id)
    if err:
        return err
    try:
        blob, mime = design.fetch_design_image(project_id, name)
    except design.DesignError as e:
        return jsonify({"error": str(e)}), 400 if isinstance(e, design.InvalidDesignName) else 502
    resp = Response(blob, mimetype=mime)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/design/<project_id>/file/<path:name>", methods=["PUT"])
def api_design_save(project_id: str, name: str):
    """Overwrite a design-folder image with the editor's result, sent as
    {"image": "data:image/...;base64,..."} in the file's own format."""
    project, err = _project_or_400(project_id)
    if err:
        return err
    m = DATA_URL_RE.match((request.get_json(silent=True) or {}).get("image") or "")
    if not m:
        return jsonify({"error": "Missing or invalid image data."}), 400
    expected = design.IMAGE_TYPES.get(Path(name).suffix.lower())
    if m.group(1) != expected:
        return jsonify({"error": f"Image must be {expected} to replace {name}."}), 400
    try:
        blob = base64.b64decode(m.group(2), validate=True)
        result = design.save_design_image(project_id, name, blob)
    except (ValueError, design.DesignError) as e:
        logger.warning("design save failed for %s/%s: %s", project_id, name, e)
        return jsonify({"error": str(e)}), 400 if isinstance(e, (ValueError, design.InvalidDesignName)) else 502
    logger.info("design image edited: %s/%s (%d bytes)", project_id, name, len(blob))
    return jsonify(result)


@app.route("/api/design/<project_id>/generate", methods=["POST"])
def api_design_generate(project_id: str):
    """Seedream image-to-image: `prompt`, `image` (the edited canvas, PNG),
    optional `reference` (an extra image), `width`/`height` of the canvas."""
    project, err = _project_or_400(project_id)
    if err:
        return err
    prompt = (request.form.get("prompt") or "").strip()
    canvas = request.files.get("image")
    if not prompt:
        return jsonify({"error": "Enter a prompt first."}), 400
    if not canvas:
        return jsonify({"error": "Missing the image to edit."}), 400
    max_bytes = int(float(seedream.image_config()["max_reference_mb"]) * 1024 * 1024)
    images = []
    for f in [canvas, request.files.get("reference")]:
        if not f or not f.filename:
            continue
        blob = f.read()
        mime = (f.mimetype or "").split(";")[0]
        if mime not in ("image/png", "image/jpeg", "image/webp"):
            return jsonify({"error": "Images must be PNG, JPEG or WebP."}), 400
        if len(blob) > max_bytes:
            return jsonify({"error": f"An image is larger than {seedream.image_config()['max_reference_mb']} MB."}), 400
        images.append((blob, mime))
    try:
        width, height = int(request.form.get("width") or 0), int(request.form.get("height") or 0)
    except ValueError:
        width = height = 0
    if width <= 0 or height <= 0:
        return jsonify({"error": "Missing image size."}), 400
    try:
        result = seedream.generate(project_id, prompt, images, width, height)
    except seedream.SeedreamError as e:
        logger.warning("seedream failed for %s: %s", project_id, e)
        return jsonify({"error": str(e)}), 502
    logger.info("seedream generated for %s: %d image(s), size %s, %ss", project_id,
                len(result["images"]), result["size"], result["elapsed_s"])
    return jsonify(result)


@app.route("/generated/<project_id>/<name>")
def generated_file(project_id: str, name: str):
    if not PROJECT_ID_RE.match(project_id):
        return jsonify({"error": "Not found."}), 404
    path = seedream.generated_path(project_id, name)
    if not path:
        return jsonify({"error": "Not found."}), 404
    return send_file(path)


@app.route("/api/design/<project_id>")
def api_design(project_id: str):
    project, err = _project_or_400(project_id)
    if err:
        return err
    try:
        return jsonify(design.list_design(project_id))
    except design.DesignError as e:
        logger.warning("design listing failed for %s: %s", project_id, e)
        return jsonify({"error": str(e)}), 502


@app.route("/api/design/<project_id>/select", methods=["POST"])
def api_design_select(project_id: str):
    project, err = _project_or_400(project_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    idea_id = str(body.get("idea_id") or "").strip()
    if not idea_id:
        return jsonify({"error": "Missing idea_id."}), 400
    # Optional notes from the brainstorm page, kept in selection.json.
    notes = {k: str(body.get(k) or "").strip()[:4000] for k in ("note", "general")}
    try:
        result = design.select_idea(project_id, project["brainstorm_url"], idea_id,
                                    {k: v for k, v in notes.items() if v})
    except design.DesignError as e:
        logger.warning("design select failed for %s idea %s: %s", project_id, idea_id, e)
        return jsonify({"error": str(e)}), 502
    logger.info("design selected for %s: idea %s -> %s", project_id, idea_id, result.get("created"))
    return jsonify(result)


@app.route("/api/design/<project_id>/analysis-requested", methods=["POST"])
def api_design_analysis_requested(project_id: str):
    """Called by the Design tab after it has sent a pick to the agent for
    the analysis report: notes the time in the pick's selection.json."""
    project, err = _project_or_400(project_id)
    if err:
        return err
    folder = str((request.get_json(silent=True) or {}).get("folder") or "").strip()
    try:
        result = design.mark_analysis_requested(project_id, folder)
    except design.InvalidDesignName as e:
        return jsonify({"error": str(e)}), 400
    except design.DesignError as e:
        logger.warning("analysis request mark failed for %s/%s: %s", project_id, folder, e)
        return jsonify({"error": str(e)}), 502
    logger.info("design %s/%s sent for the analysis report", project_id, folder)
    return jsonify(result)


@app.route("/api/history")
def api_history():
    try:
        sid = resolve_session_id()
        return jsonify(_events_payload(managed_agent.get_events(sid=sid), sid))
    except (managed_agent.ManagedAgentError, ValueError) as e:
        logger.warning("history fetch failed: %s", e)
        return jsonify({"error": f"Could not load the conversation: {e}"}), 502


@app.route("/api/events")
def api_events():
    since = request.args.get("since") or ""
    if not since:
        return jsonify({"error": "Missing `since`."}), 400
    try:
        sid = resolve_session_id()
        return jsonify(_events_payload(managed_agent.get_events(since, sid), sid))
    except (managed_agent.ManagedAgentError, ValueError) as e:
        logger.warning("events fetch failed: %s", e)
        return jsonify({"error": f"Could not fetch updates: {e}"}), 502


def _mount_upload(filename: str, blob: bytes, mime_type: str, sid: str) -> str:
    """Upload to the Files API and mount into the session's sandbox at
    /mnt/session/uploads/<filename>, falling back to a uniquified name if
    that mount path is already taken by an earlier upload."""
    file_obj = managed_agent.upload_file(blob, filename, mime_type)
    mount_path = f"/{filename}"
    try:
        managed_agent.add_session_resource(file_obj["id"], mount_path, sid)
    except managed_agent.ManagedAgentError:
        mount_path = f"/{uuid.uuid4().hex[:8]}-{filename}"
        managed_agent.add_session_resource(file_obj["id"], mount_path, sid)
    return f"/mnt/session/uploads{mount_path}"


@app.route("/api/message", methods=["POST"])
def api_message():
    text = (request.form.get("text") or "").strip()
    uploads = [f for f in request.files.getlist("files") if f and f.filename]
    if not text and not uploads:
        return jsonify({"error": "Message is empty."}), 400

    try:
        sid = resolve_session_id()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    project_id = (request.form.get("project") or "").strip()
    if project_id and not PROJECT_ID_RE.match(project_id):
        return jsonify({"error": f'Invalid project id "{project_id}".'}), 400

    cfg = managed_agent.load_agent_config()
    max_bytes = int(cfg["max_upload_mb"]) * 1024 * 1024
    max_doc_chars = int(cfg["max_document_chars"])

    content = [{"type": "text", "text": text}] if text else []
    attachment_names = []
    for f in uploads:
        filename = secure_filename(f.filename) or "upload"
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in ALLOWED_EXTENSIONS:
            return jsonify({"error": f'File type ".{ext}" is not allowed.'}), 400
        blob = f.read()
        if len(blob) > max_bytes:
            return jsonify({"error": f'"{filename}" exceeds the {cfg["max_upload_mb"]}MB upload limit.'}), 400
        attachment_names.append(filename)
        if ext in MOUNTABLE_MIME:
            try:
                path = _mount_upload(filename, blob, MOUNTABLE_MIME[ext], sid)
            except managed_agent.ManagedAgentError as e:
                logger.warning("failed to attach %s: %s", filename, e)
                return jsonify({"error": f'Upload of "{filename}" failed: {e}'}), 502
            content.append({"type": "text", "text": f"[Uploaded file available at {path}]"})
        else:
            doc_text = blob.decode("utf-8", errors="replace")
            if len(doc_text) > max_doc_chars:
                doc_text = doc_text[:max_doc_chars] + "\n... (truncated)"
            content.append({"type": "text", "text": f"[Attached file: {filename}]\n{doc_text}"})

    # Until the session has been told this project id (including sessions
    # started elsewhere), lead the message with it.
    project_sent = False
    if project_id:
        try:
            told = any(_project_note_of(e) == project_id for e in managed_agent.get_events(sid=sid))
        except managed_agent.ManagedAgentError as e:
            logger.warning("history check before send failed: %s", e)
            return jsonify({"error": f"Could not reach the session: {e}"}), 502
        if not told:
            content.insert(0, {"type": "text", "text": PROJECT_NOTE.format(project_id=project_id)})
            project_sent = True

    try:
        data = managed_agent.send_message(content, sid)
    except managed_agent.ManagedAgentError as e:
        logger.warning("send message failed: %s", e)
        return jsonify({"error": f"Agent call failed: {e}"}), 502

    if project_sent:
        logger.info("project id %s sent to session %s", project_id, sid)
    # Context for reviewing replies whose polling window starts mid-turn.
    if text:
        try:
            _save_json_atomic(_last_question_path(sid), {"text": text})
        except OSError as e:
            logger.warning("failed to save last question: %s", e)
    log_conversation_turn(session_id=sid, role="user", text=text, attachments=attachment_names)
    created = (data.get("data") or [{}])[0]
    return jsonify({"event_id": created.get("id"), "sent_at": int(time.time() * 1000),
                    "project": project_id if project_sent else None})


if __name__ == "__main__":
    # threaded=True so a slow history fetch doesn't block other requests.
    # debug is off since the site binds to all interfaces.
    cfg = site_config()
    app.run(host=cfg["host"], port=int(cfg["port"]), debug=False, threaded=True)
