"""Ark REST API chat client + shared helpers for bot-response-rewrite.

Same design as the video-workflow skill's scripts/ark_service.py: plain HTTP calls via
`requests` against the BytePlus Ark OpenAI-compatible endpoint, settings from config.json,
secret from the `model_ark_key` env var or credential.json.
"""
import json
import os
import re
import sys
from pathlib import Path

import requests

BASE_DIR        = Path(__file__).resolve().parent.parent  # skill root — credential.json/config.json/policy.json live here
CREDENTIAL_PATH = BASE_DIR / "credential.json"
CONFIG_PATH     = BASE_DIR / "config.json"
LOG_DIR         = BASE_DIR / "_task" / "log"
OUTPUT_DIR      = BASE_DIR / "_task" / "output"
TEMPLATES_DIR   = Path(__file__).resolve().parent / "prompt_templates"  # LLM system/user templates
REFERENCES_DIR  = BASE_DIR / "references"


def configure_console() -> None:
    """Allow Unicode output on Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def read_json(path: Path) -> dict:
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config file not found: {CONFIG_PATH}")
    return read_json(CONFIG_PATH)


def load_api_key() -> str:
    env_key = os.environ.get("model_ark_key")
    if env_key:
        return env_key
    if not CREDENTIAL_PATH.exists():
        raise FileNotFoundError(
            f"model_ark_key env var not set, and credential file not found: {CREDENTIAL_PATH}"
        )
    api_key = read_json(CREDENTIAL_PATH).get("model_ark_key")
    if not api_key or api_key == "...":
        raise KeyError("model_ark_key env var not set and `model_ark_key` missing in credential.json")
    return api_key


def resolve_policy_path(cli_value: str | None, config: dict) -> Path | None:
    """--policy wins; otherwise config's policy_path (relative to the skill root). Returns None
    when no policy file exists — the pipeline then runs generic detection only."""
    if cli_value:
        path = Path(cli_value)
        if not path.exists():
            raise FileNotFoundError(f"Policy file not found: {path}")
        return path
    configured = config.get("policy_path")
    if configured:
        path = Path(configured)
        path = path if path.is_absolute() else BASE_DIR / path
        if path.exists():
            return path
    return None


def write_log(log_path: Path, data: dict) -> None:
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def read_text_file(path: Path, label: str) -> str:
    """Reads a user-supplied text file (response/question), raising if it's empty."""
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"{label} file is empty: {path}")
    return text


def read_template(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8").strip()


def fill_template(text: str, /, **values) -> str:
    """Substitutes `{key}` placeholders. Unlike str.format, braces inside the values (or
    elsewhere in the template — e.g. JSON examples) are left alone instead of raising."""
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def extract_token_usage(usage: dict | None) -> dict:
    """Normalizes an Ark `usage` dict into {tokens_in, tokens_out, tokens_total}. Missing
    values are None, not 0, so a genuine zero is never confused with "not reported"."""
    usage = usage or {}
    tokens_in = usage.get("prompt_tokens")
    if tokens_in is None:
        tokens_in = usage.get("input_tokens")
    tokens_out = usage.get("completion_tokens")
    if tokens_out is None:
        tokens_out = usage.get("output_tokens")
    return {"tokens_in": tokens_in, "tokens_out": tokens_out, "tokens_total": usage.get("total_tokens")}


def parse_json_reply(text: str) -> dict:
    """Parses a model reply that should be a JSON object, tolerating ```json fences or prose
    around it (models don't always honour json mode)."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


class ArkChatService:
    """Text chat-completions client (Deepseek models) for reviewing and rewriting responses."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 120):
        self._url = base_url.rstrip('/') + '/chat/completions'
        self._timeout = timeout
        self._headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        }

    def complete(self, model_id: str, system_prompt: str, user_prompt: str,
                 temperature: float | None = None, max_tokens: int | None = None,
                 json_mode: bool = False) -> tuple[str, dict]:
        """Returns (text, usage) — usage is the Ark API's token-usage dict, or {} if absent."""
        print(f"Chat completion: model={model_id} system_len={len(system_prompt)} "
              f"user_len={len(user_prompt)} json_mode={json_mode}", file=sys.stderr)

        payload = {
            'model': model_id,
            'messages': [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            'stream': False,
        }
        if temperature is not None:
            payload['temperature'] = temperature
        if max_tokens is not None:
            payload['max_tokens'] = max_tokens
        if json_mode:
            payload['response_format'] = {'type': 'json_object'}

        resp = requests.post(self._url, headers=self._headers, json=payload, timeout=self._timeout)
        if resp.status_code == 400 and json_mode and 'response_format' in resp.text:
            # Not every Ark model accepts json mode — the prompt already demands JSON, so retry without it.
            print("Model rejected response_format; retrying without json mode", file=sys.stderr)
            payload.pop('response_format')
            resp = requests.post(self._url, headers=self._headers, json=payload, timeout=self._timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"Ark API error {resp.status_code}: {resp.text}")
        data = resp.json()

        choices = data.get('choices') or []
        if not choices:
            raise RuntimeError(f"Chat completion returned no choices: {data}")
        text = ((choices[0].get('message') or {}).get('content') or '').strip()
        usage = data.get('usage') or {}
        print(f"Chat completion done: result_len={len(text)} "
              f"tokens_in={usage.get('prompt_tokens')} tokens_out={usage.get('completion_tokens')}", file=sys.stderr)
        return text, usage
