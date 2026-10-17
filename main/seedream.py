"""Seedream image-to-image for the Design tab's editor.

Adapted from jonah-image-editor (app.py generate_image / compute_image_size)
and builder-brand-design's scripts/ark_service.py (plain REST call to Ark's
/images/generations, no SDK). The edited canvas is the first reference
image; an optional second one can be added. Results are saved under
_generated/<project_id>/ and served from this site, so the editor canvas
can load them without CORS.
"""

import base64
import json
import math
import re
import time
import uuid
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config.json"
CREDENTIAL_FILE = BASE_DIR / "credential.json"
GENERATED_DIR = BASE_DIR / "_generated"

IMAGE_RATIOS = ["1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "21:9"]
_RESOLUTION_TARGET_PIXELS = {"1K": 1024 * 1024, "2K": 2048 * 2048}
GENERATED_NAME_RE = re.compile(r"^gen-[0-9a-f]{32}\.(?:jpg|png|webp)$")
_EXT_BY_MIME = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


class SeedreamError(Exception):
    pass


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def image_config() -> dict:
    root = _load_json(CONFIG_FILE)
    cfg = dict(root.get("image", {}))
    cfg.setdefault("model_id", "dola-seedream-5-0-pro-260628")
    cfg.setdefault("resolution", "2K")
    cfg.setdefault("watermark", False)
    cfg.setdefault("max_reference_mb", 10)
    cfg["base_url"] = root.get("agent", {}).get("base_url") or "https://ark.ap-southeast.bytepluses.com/api/v3"
    creds = _load_json(CREDENTIAL_FILE)
    cfg["api_key"] = creds.get("model_ark_key") or creds.get("agent_ark_key") or ""
    return cfg


def closest_ratio(width: int, height: int) -> str:
    """Compared on a log scale so 2:1 and 1:2 are equally far from 1:1."""
    actual = math.log(width / height)
    return min(IMAGE_RATIOS, key=lambda r: abs(actual - math.log(int(r.split(":")[0]) / int(r.split(":")[1]))))


def compute_image_size(ratio: str, resolution: str) -> str:
    """WxH for an aspect ratio at a resolution tier's pixel budget, floored
    to a multiple of 64 so the area never exceeds the API's cap."""
    target = _RESOLUTION_TARGET_PIXELS.get(resolution, _RESOLUTION_TARGET_PIXELS["2K"])
    w_ratio, h_ratio = (float(p) for p in ratio.split(":", 1))
    width = (target * w_ratio / h_ratio) ** 0.5
    height = width * h_ratio / w_ratio
    return f"{max(64, int(width // 64) * 64)}x{max(64, int(height // 64) * 64)}"


def to_data_url(blob: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"


def generate(project_id: str, prompt: str, images: list[tuple[bytes, str]], width: int, height: int) -> dict:
    """Seedream with `images` ((bytes, mime) pairs, the edited canvas first)
    as references, at the canvas's closest ratio. Returns
    {"images": [{"name", "url"}], "size", "elapsed_s", "usage"}."""
    cfg = image_config()
    if not cfg["api_key"]:
        raise SeedreamError("Missing model_ark_key in credential.json")
    size = compute_image_size(closest_ratio(width, height), cfg["resolution"])
    payload = {
        "model": cfg["model_id"],
        "prompt": prompt,
        "image": [to_data_url(b, m) for b, m in images] if len(images) > 1 else to_data_url(*images[0]),
        "size": size,
        "watermark": bool(cfg["watermark"]),
        "response_format": "url",
    }
    t0 = time.monotonic()
    try:
        resp = requests.post(cfg["base_url"].rstrip("/") + "/images/generations", json=payload, timeout=300,
                             headers={"Authorization": f"Bearer {cfg['api_key']}"})
    except requests.RequestException as e:
        raise SeedreamError(f"Seedream request failed: {e}") from e
    if resp.status_code != 200:
        try:
            message = resp.json().get("error", {}).get("message") or resp.text
        except ValueError:
            message = resp.text
        raise SeedreamError(f"Seedream error {resp.status_code}: {message[:500]}")
    data = resp.json()

    out_dir = GENERATED_DIR / project_id
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for item in data.get("data") or []:
        try:
            if item.get("url"):
                r = requests.get(item["url"], timeout=120)
                r.raise_for_status()
                blob, mime = r.content, r.headers.get("Content-Type", "image/jpeg").split(";")[0]
            elif item.get("b64_json"):
                blob, mime = base64.b64decode(item["b64_json"]), "image/png"
            else:
                continue
        except (requests.RequestException, ValueError) as e:
            raise SeedreamError(f"Could not download the generated image: {e}") from e
        name = f"gen-{uuid.uuid4().hex}.{_EXT_BY_MIME.get(mime, 'jpg')}"
        (out_dir / name).write_bytes(blob)
        results.append({"name": name, "url": f"/generated/{project_id}/{name}"})
    if not results:
        raise SeedreamError("Seedream returned no image.")
    return {"images": results, "size": size, "elapsed_s": round(time.monotonic() - t0, 1),
            "usage": data.get("usage") or {}}


def generated_path(project_id: str, name: str) -> Path | None:
    if not GENERATED_NAME_RE.match(name):
        return None
    path = GENERATED_DIR / project_id / name
    return path if path.is_file() else None
