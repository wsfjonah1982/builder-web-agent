"""Design tab: a project's selected brand design idea on TOS.

The brainstorm page (brainstorm.html, made by the builder-brand-design skill)
lists the design ideas; each idea's gallery holds its designs (logo, brand
character, brand story, business card) under img/. The page keeps the
user's pick in its localStorage; the Brainstorm tab serves the page from
this site's origin, reads the pick, and sends it here: the chosen idea's
images are copied into a new timestamp folder,
brand/<project_id>/<design_folder>/<YYYYMMDD-HHMMSS>/ (UTC), together with a
selection.json describing the pick, so earlier picks are never
overwritten. The originals stay in img/, since brainstorm.html still shows
them. Images left directly in the design folder by the older one-pick
layout are listed as their own group.
"""

import json
import posixpath
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

import tos_client

BASE_DIR = Path(__file__).resolve().parent.parent
CREDENTIAL_FILE = BASE_DIR / "credential.json"
CONFIG_FILE = BASE_DIR / "config.json"

SELECTION_FILE = "selection.json"
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif"}
# Design files are replaced in place when edited, so browsers must revalidate.
CACHE_CONTROL = "no-cache"
# One folder per Send for Analysis: UTC time, plus -2, -3... within the same second.
PICK_FOLDER_RE = re.compile(r"^\d{8}-\d{6}(?:-\d{1,3})?$")


class DesignError(Exception):
    pass


class InvalidDesignName(DesignError):
    """A file name the routes reject outright (400, not a TOS failure)."""


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def tos_config() -> dict:
    root = _load_json(CONFIG_FILE)
    cfg = dict(root.get("tos", {}))
    cfg.setdefault("endpoint", "tos-ap-southeast-1.bytepluses.com")
    cfg.setdefault("region", "ap-southeast-1")
    cfg.setdefault("key_prefix_template", "brand/{project_id}")
    creds = _load_json(CREDENTIAL_FILE)
    cfg["access_key"] = creds.get("tos_access_key_id", "")
    cfg["secret_key"] = creds.get("tos_secret_access_key", "")
    cfg["bucket"] = creds.get("tos_bucket", "")
    cfg["design_folder"] = (root.get("project", {}).get("design_folder") or "design").strip("/")
    return cfg


def _require_credentials(cfg: dict) -> None:
    missing = [k for k, v in (("tos_access_key_id", cfg["access_key"]), ("tos_secret_access_key", cfg["secret_key"]),
                              ("tos_bucket", cfg["bucket"])) if not v]
    if missing:
        raise DesignError(f"Missing {', '.join(missing)} in credential.json")


def _host(cfg: dict) -> str:
    return f"{cfg['bucket']}.{cfg['endpoint']}"


def design_prefix(project_id: str, cfg: dict) -> str:
    return f"{cfg['key_prefix_template'].format(project_id=project_id).strip('/')}/{cfg['design_folder']}/"


def public_url(key: str, cfg: dict) -> str:
    return f"https://{_host(cfg)}/{key}"


# ---------- Brainstorm page ----------

class _BrainstormParser(HTMLParser):
    """Collects the idea rows (<article class=row data-id data-title>, with
    a button data-gal pointing at the gallery) and the gallery templates
    (<template id=galN> of <a class=frame href data-zoom>)."""

    def __init__(self):
        super().__init__()
        self.ideas, self.galleries = [], {}
        self._row = self._gallery = None
        self._depth = 0  # <article> nesting, to know when the row ends

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "article" and "row" in classes and a.get("data-id"):
            self._row = {"id": a["data-id"], "title": a.get("data-title") or f"Idea {a['data-id']}",
                         "gallery": None}
            self.ideas.append(self._row)
            self._depth = 1
        elif tag == "article" and self._row:
            self._depth += 1
        elif self._row and a.get("data-gal"):
            self._row["gallery"] = a["data-gal"]
        if tag == "template" and a.get("id"):
            self._gallery = self.galleries.setdefault(a["id"], [])
        elif self._gallery is not None and tag == "a" and "frame" in classes and a.get("href"):
            self._gallery.append({"href": a["href"], "label": a.get("data-zoom") or ""})

    def handle_endtag(self, tag):
        if tag == "article" and self._row:
            self._depth -= 1
            if self._depth == 0:
                self._row = None
        elif tag == "template":
            self._gallery = None


def load_ideas(brainstorm_url: str) -> list:
    """The design ideas on the brainstorm page, in page order, each with
    absolute image URLs for its designs."""
    try:
        r = requests.get(brainstorm_url, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        raise DesignError(f"Could not load the brainstorm page: {e}") from e
    r.encoding = r.encoding or "utf-8"
    parser = _BrainstormParser()
    parser.feed(r.text)
    ideas = []
    for idea in parser.ideas:
        designs = [{"label": d["label"], "url": urljoin(brainstorm_url, d["href"])}
                   for d in parser.galleries.get(idea["gallery"] or "", [])]
        ideas.append({"id": idea["id"], "title": idea["title"], "designs": designs})
    if not ideas:
        raise DesignError("No design ideas found on the brainstorm page.")
    return ideas


# ---------- Design folder ----------

def _read_selection(key: str, cfg: dict) -> dict | None:
    try:
        r = requests.get(public_url(key, cfg), timeout=20, headers={"Cache-Control": "no-cache"})
        return r.json() if r.ok else None
    except (requests.RequestException, ValueError):
        return None


def list_design(project_id: str) -> dict:
    """The project's design picks, newest first: one group per timestamp
    folder (its images and selection.json), then any images left directly
    in the design folder by the older layout. Image "name"s are relative to
    the design folder ("<folder>/<file>"), as the file routes take them."""
    cfg = tos_config()
    _require_credentials(cfg)
    prefix = design_prefix(project_id, cfg)
    try:
        entries = tos_client.list_object_entries(cfg["endpoint"], cfg["region"], cfg["bucket"],
                                                 cfg["access_key"], cfg["secret_key"], prefix)
    except (RuntimeError, requests.RequestException) as e:
        raise DesignError(f"Could not list the design folder: {e}") from e

    groups = {}  # folder ("" = top level) -> {"images": [...], "sel_key": key}
    for e in sorted(entries, key=lambda e: e["Key"]):
        parts = e["Key"][len(prefix):].split("/")
        if len(parts) == 1:
            folder, name = "", parts[0]
        elif len(parts) == 2 and PICK_FOLDER_RE.match(parts[0]):
            folder, name = parts
        else:
            continue
        if not name:
            continue
        g = groups.setdefault(folder, {"images": [], "sel_key": None})
        if name == SELECTION_FILE:
            g["sel_key"] = e["Key"]
        elif posixpath.splitext(name)[1].lower() in IMAGE_TYPES:
            version = re.sub(r"\D", "", str(e.get("LastModified") or ""))
            g["images"].append({"name": f"{folder}/{name}" if folder else name, "file": name,
                                "size": e.get("Size"), "last_modified": e.get("LastModified"),
                                "url": public_url(e["Key"], cfg) + (f"?v={version}" if version else "")})

    sel_keys = [g["sel_key"] for g in groups.values() if g["sel_key"]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        selections = dict(zip(sel_keys, pool.map(lambda k: _read_selection(k, cfg), sel_keys)))

    result = []
    # Timestamp folders newest first; the older top-level layout last.
    for folder in sorted(groups, key=lambda f: (f != "", f), reverse=True):
        g = groups[folder]
        images, selection = g["images"], selections.get(g["sel_key"])
        if not images:
            continue
        # Show the images in the order of the pick (logo, character, story, card).
        if selection:
            designs = selection.get("designs") or []
            order = {d.get("file"): i for i, d in enumerate(designs)}
            images.sort(key=lambda img: order.get(img["file"], len(order)))
            by_file = {d.get("file"): d for d in designs}
            for img in images:
                img["label"] = by_file.get(img["file"], {}).get("label") or ""
                img["edited_at"] = by_file.get(img["file"], {}).get("edited_at")
        result.append({"folder": folder, "url": public_url(prefix + (folder + "/" if folder else ""), cfg),
                       "images": images, "selection": selection})
    return {"folder": public_url(prefix, cfg), "groups": result}


def select_idea(project_id: str, brainstorm_url: str, idea_id: str, notes: dict | None = None) -> dict:
    """Copy one idea's designs into a new timestamp folder under the design
    folder and write its selection.json (with `notes` - the user's "note" on
    the idea and "general" notes from the brainstorm page - if given).
    Nothing already there is changed. Returns the new folder listing, with
    "created" naming the new folder."""
    cfg = tos_config()
    _require_credentials(cfg)
    idea = next((i for i in load_ideas(brainstorm_url) if i["id"] == str(idea_id)), None)
    if not idea:
        raise DesignError(f'Idea "{idea_id}" is not on the brainstorm page.')
    if not idea["designs"]:
        raise DesignError(f'Idea "{idea["title"]}" has no designs.')

    prefix = design_prefix(project_id, cfg)
    project_prefix = prefix[:-len(cfg["design_folder"]) - 1]
    copies = []
    for d in idea["designs"]:
        u = urlparse(d["url"])
        key = u.path.lstrip("/")
        ext = posixpath.splitext(key)[1].lower()
        # Only images of this project in this bucket - never anything else the page links to.
        if u.netloc != _host(cfg) or not key.startswith(project_prefix) or ext not in IMAGE_TYPES:
            raise DesignError(f"Unexpected design image link: {d['url']}")
        copies.append((d, key, posixpath.basename(key), IMAGE_TYPES[ext]))

    try:
        blobs = []
        for d, key, name, ctype in copies:
            r = requests.get(public_url(key, cfg), timeout=60)
            r.raise_for_status()
            blobs.append(r.content)

        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%d-%H%M%S")
        folder = stamp
        for n in range(2, 100):  # never reuse a folder, even within the same second
            if not tos_client.list_objects(cfg["endpoint"], cfg["region"], cfg["bucket"],
                                           cfg["access_key"], cfg["secret_key"], prefix + folder + "/"):
                break
            folder = f"{stamp}-{n}"
        else:
            raise DesignError("Could not find a free folder name; try again.")
        pick_prefix = prefix + folder + "/"

        for (d, key, name, ctype), blob in zip(copies, blobs):
            tos_client.put_object(cfg["endpoint"], cfg["region"], cfg["bucket"], pick_prefix + name,
                                  cfg["access_key"], cfg["secret_key"], blob, ctype, CACHE_CONTROL)

        # Written last, so a folder with a selection.json is complete.
        selection = {
            "type": "brand-design-selection", "version": 1, "project_id": project_id,
            "folder": folder,
            "idea": {"id": int(idea["id"]) if idea["id"].isdigit() else idea["id"], "title": idea["title"]},
            "selected_at": now.isoformat(),
            "source": brainstorm_url,
            "designs": [{"label": d["label"], "file": name, "source_key": key} for d, key, name, _ in copies],
            **(notes or {}),
        }
        tos_client.put_object(cfg["endpoint"], cfg["region"], cfg["bucket"], pick_prefix + SELECTION_FILE,
                              cfg["access_key"], cfg["secret_key"],
                              json.dumps(selection, ensure_ascii=False, indent=2).encode("utf-8"),
                              "application/json; charset=utf-8", CACHE_CONTROL)
    except (RuntimeError, requests.RequestException) as e:
        raise DesignError(f"Copy to the design folder failed: {e}") from e
    return {**list_design(project_id), "created": folder}


def mark_analysis_requested(project_id: str, folder: str) -> dict:
    """Record in a pick's selection.json that it was sent to the agent for
    the analysis report ("analysis_requested_at", the latest request).
    Returns the new folder listing."""
    if not PICK_FOLDER_RE.match(folder or ""):
        raise InvalidDesignName(f'Invalid design folder "{folder}".')
    cfg = tos_config()
    _require_credentials(cfg)
    sel_key = design_prefix(project_id, cfg) + folder + "/" + SELECTION_FILE
    selection = _read_selection(sel_key, cfg)
    if not selection:
        raise DesignError(f'design/{folder}/ has no {SELECTION_FILE}.')
    selection["analysis_requested_at"] = datetime.now(timezone.utc).isoformat()
    try:
        tos_client.put_object(cfg["endpoint"], cfg["region"], cfg["bucket"], sel_key,
                              cfg["access_key"], cfg["secret_key"],
                              json.dumps(selection, ensure_ascii=False, indent=2).encode("utf-8"),
                              "application/json; charset=utf-8", CACHE_CONTROL)
    except (RuntimeError, requests.RequestException) as e:
        raise DesignError(f"Updating {SELECTION_FILE} failed: {e}") from e
    return list_design(project_id)


# ---------- Editing a design image ----------

DESIGN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _design_key(project_id: str, name: str, cfg: dict) -> str:
    """`name` is "<timestamp folder>/<file>", or just "<file>" for the older
    top-level layout."""
    folder, _, file = name.rpartition("/")
    if ((folder and not PICK_FOLDER_RE.match(folder)) or not DESIGN_NAME_RE.match(file)
            or posixpath.splitext(file)[1].lower() not in IMAGE_TYPES):
        raise InvalidDesignName(f'Invalid design file name "{name}".')
    return design_prefix(project_id, cfg) + name


def fetch_design_image(project_id: str, name: str) -> tuple[bytes, str]:
    """One image from the design folder, fetched server-side so the editor
    can load it into a canvas (TOS sends no CORS headers, so a canvas that
    drew it straight from TOS couldn't be exported)."""
    cfg = tos_config()
    key = _design_key(project_id, name, cfg)
    try:
        r = requests.get(public_url(key, cfg), timeout=60, headers={"Cache-Control": "no-cache"})
        r.raise_for_status()
    except requests.RequestException as e:
        raise DesignError(f'Could not load "{name}": {e}') from e
    return r.content, IMAGE_TYPES[posixpath.splitext(name)[1].lower()]


def save_design_image(project_id: str, name: str, blob: bytes) -> dict:
    """Overwrite one image in the design folder with an edited version and
    note it in selection.json (the original stays in img/). Returns the new
    folder listing."""
    cfg = tos_config()
    _require_credentials(cfg)
    key = _design_key(project_id, name, cfg)
    existing = tos_client.list_objects(cfg["endpoint"], cfg["region"], cfg["bucket"],
                                       cfg["access_key"], cfg["secret_key"], key)
    if key not in existing:
        raise DesignError(f'"{name}" is not in the design folder.')
    try:
        tos_client.put_object(cfg["endpoint"], cfg["region"], cfg["bucket"], key, cfg["access_key"],
                              cfg["secret_key"], blob, IMAGE_TYPES[posixpath.splitext(name)[1].lower()],
                              CACHE_CONTROL)
        sel_key = posixpath.dirname(key) + "/" + SELECTION_FILE  # the pick's own selection.json
        selection = _read_selection(sel_key, cfg)
        if selection:
            now = datetime.now(timezone.utc).isoformat()
            for d in selection.get("designs") or []:
                if d.get("file") == posixpath.basename(key):
                    d["edited_at"] = now
            tos_client.put_object(cfg["endpoint"], cfg["region"], cfg["bucket"], sel_key,
                                  cfg["access_key"], cfg["secret_key"],
                                  json.dumps(selection, ensure_ascii=False, indent=2).encode("utf-8"),
                                  "application/json; charset=utf-8", CACHE_CONTROL)
    except (RuntimeError, requests.RequestException) as e:
        raise DesignError(f'Saving "{name}" failed: {e}') from e
    return list_design(project_id)
