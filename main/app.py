"""Porto Agent.

A single-page, no-login Flask site for one PortoBuild project, adapted from
Task Bot (web_app/task-bot). The page takes a project id (`?project=<id>`,
or config.json["project"]["default_project_id"]) and has three tabs:

- Chat: talks to the BytePlus Ark Managed Agent session the project maps to
  in project_setting.json (`?session=<id>` overrides it). As in Task Bot, the
  history is read straight from the session's REST event history (nothing is
  stored locally), a message is posted as a user.message event, and the page
  polls for the agent's events until the session goes back to idle.
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
  back over itself.
"""

import base64
import html
import json
import re
import sys
import time
import uuid
from pathlib import Path

import markdown
import requests
from flask import Flask, Response, jsonify, render_template, request, send_file
from markupsafe import Markup
from werkzeug.utils import secure_filename

import design
import managed_agent
import seedream

MAIN_DIR = Path(__file__).resolve().parent
BASE_DIR = MAIN_DIR.parent
sys.path.insert(0, str(BASE_DIR))
from util.log_utils import get_app_logger, log_conversation_turn  # noqa: E402

CONFIG_FILE = BASE_DIR / "config.json"

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
    return cfg


def project_settings() -> dict:
    """project_setting.json["projects"]: project id -> {"session_id", and
    optionally "brainstorm_url"}. Re-read on every call, so edits apply
    without a restart."""
    path = BASE_DIR / project_config()["setting_path"]
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("projects", {})
    except (OSError, ValueError) as e:
        logger.warning("failed to read %s: %s", path, e)
        return {}


def resolve_project(project_id: str) -> dict:
    """The project's session id and brainstorm page URL. Raises ValueError
    for a malformed id; an id missing from project_setting.json resolves to
    no session (the brainstorm page can still be shown)."""
    if not PROJECT_ID_RE.match(project_id):
        raise ValueError(f'Invalid project id "{project_id}".')
    entry = project_settings().get(project_id) or {}
    brainstorm_url = entry.get("brainstorm_url") or \
        project_config()["brainstorm_url_template"].format(project_id=project_id)
    return {"project_id": project_id, "session_id": entry.get("session_id") or "",
            "brainstorm_url": brainstorm_url, "known": bool(entry)}


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


def event_to_item(e: dict) -> dict | None:
    """Convert one raw Managed Agent event into a render-ready item, or None
    for events with nothing to show (span.*, agent.thinking, and tool calls
    and their results, which are deliberately kept off the page)."""
    t = e.get("type")
    base = {"id": e.get("id"), "ts": _ts_ms(e), "processed_at": e.get("processed_at")}

    if t in STATUS_EVENTS:
        return {**base, "kind": "status", "status": STATUS_EVENTS[t]}

    if t == "user.message":
        texts, images, attachments = [], [], []
        for b in e.get("content") or []:
            if b.get("type") == "text":
                text = b.get("text", "")
                if m := UPLOAD_NOTE_RE.match(text.strip()):
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
        return {**base, "kind": "user", "text": text, "images": images, "attachments": attachments}

    if t == "agent.message":
        text = "".join(b.get("text", "") for b in e.get("content") or [] if b.get("type") == "text")
        if not text:
            return None
        return {**base, "kind": "assistant", "html": str(markdown_filter(text))}

    if t == "session.error":
        err = e.get("error") or {}
        message = err.get("message") or f"The agent session reported an error ({err.get('type', 'unknown')})."
        return {**base, "kind": "error", "text": message}

    return None


def _events_payload(events: list, sid: str) -> dict:
    items = [i for i in (event_to_item(e) for e in events) if i]
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
    error, project = "", {"project_id": project_id, "session_id": "", "brainstorm_url": "", "known": False}
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
MAX_BRAINSTORM_BYTES = 5 * 1024 * 1024


@app.route("/brainstorm/<project_id>")
def brainstorm_page(project_id: str):
    """The project's brainstorm page, served from this site's origin so the
    Brainstorm tab can read the idea selected in it (its DOM and its
    localStorage are then this site's). A <base href> at the page's real
    URL keeps its relative images and links pointing at TOS."""
    try:
        project = resolve_project(project_id)
    except ValueError as e:
        return Response(html.escape(str(e)), status=400, mimetype="text/plain")
    try:
        r = requests.get(project["brainstorm_url"], timeout=20, headers={"Cache-Control": "no-cache"})
        r.raise_for_status()
        if len(r.content) > MAX_BRAINSTORM_BYTES:
            raise ValueError("The brainstorm page is too large.")
    except (requests.RequestException, ValueError) as e:
        logger.warning("brainstorm page fetch failed for %s: %s", project_id, e)
        status = getattr(getattr(e, "response", None), "status_code", None)
        message = ("This project has no brainstorm page yet." if status == 404
                   else "The brainstorm page couldn't be loaded.")
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

    try:
        data = managed_agent.send_message(content, sid)
    except managed_agent.ManagedAgentError as e:
        logger.warning("send message failed: %s", e)
        return jsonify({"error": f"Agent call failed: {e}"}), 502

    log_conversation_turn(session_id=sid, role="user", text=text, attachments=attachment_names)
    created = (data.get("data") or [{}])[0]
    return jsonify({"event_id": created.get("id"), "sent_at": int(time.time() * 1000)})


if __name__ == "__main__":
    # threaded=True so a slow history fetch doesn't block other requests.
    # debug is off since the site binds to all interfaces.
    cfg = site_config()
    app.run(host=cfg["host"], port=int(cfg["port"]), debug=False, threaded=True)
