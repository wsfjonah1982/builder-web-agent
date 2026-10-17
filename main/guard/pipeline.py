"""Response guardrail for Task Bot, adapted from the bot-response-rewrite skill
(agent_work/skill/bot-response-rewrite, scripts/rewrite_response.py).

scan.py, ark_service.py, prompt_templates/ and references/ are copied from
the skill unchanged; this module is the skill's main() pipeline turned into
a function that returns the result instead of writing report files:

  1. pattern scan (deterministic candidates: keys, cards, emails, ...)
  2. chat model reviews meaning, judges scanner candidates, writes the rewrite
  3. score the original from the merged findings
  4. re-review the rewrite (up to max_review_passes); still unsafe -> BLOCK
  5. "Go" judgement, 6. result-page judgement,
  7. reply status: Fail > Done > Gate > WFT > Answer > Progress

Keep the judgement helpers below in step with the skill's
rewrite_response.py when the skill changes.
"""

import json
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

from .ark_service import ArkChatService, extract_token_usage, fill_template, parse_json_reply
from .scan import scan, score

GUARD_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = GUARD_DIR / "prompt_templates"
REFERENCES_DIR = GUARD_DIR / "references"

SERIOUS = ("high", "critical")
# Unambiguous explicit asks ("say Go", "reply 'go'") - overrides a model that misses them.
EXPLICIT_GO = re.compile(r"""(?i)\b(?:say|reply|type|send|answer|hit)\s+(?:with\s+)?["'“‘]?go\b""")
STATUS_ORDER = ["Fail", "Done", "Gate", "WFT", "Answer", "Progress"]

DEFAULTS = {
    "chat_model_id": "deepseek-v4-flash-260425",
    "chat_temperature": 0.1,
    "chat_max_tokens": 8192,
    "chat_timeout_seconds": 120,
    "chat_json_mode": True,
    "audience": "external",
    "max_review_passes": 2,
    "severity_deduction": {"critical": 40, "high": 25, "medium": 10, "low": 3},
    "pass_threshold": 85,
    "visible_redaction_markers": False,
    "rewrite_style": "concise",
    "result_link_pattern": r"https?://[\w.-]*\btos-[\w-]+\.(?:bytepluses|volces)\.com/[^\s)<>\"'`\]]+",
    "block_fallback_message": (
        "Thanks for your message. I'm not able to share that here, but I can help "
        "with something else - just let me know."
    ),
}


def _read_template(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8").strip()


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*`_\"'“”‘’]", "", t or "")).strip().lower()


def quoted_in(evidence: str, text: str) -> bool:
    """True if the evidence really quotes `text`. Models sometimes quote their own rewrite."""
    if not evidence:
        return True
    ev, tx = _norm(evidence), _norm(text)
    if ev in tx:
        return True
    m = SequenceMatcher(None, ev, tx, autojunk=False).find_longest_match(0, len(ev), 0, len(tx))
    return m.size >= 0.6 * len(ev)


def judge_go(raw, text: str, key: str = "expects_go") -> dict:
    raw = raw if isinstance(raw, dict) else {}
    m = EXPLICIT_GO.search(text or "")
    evidence = (raw.get("evidence") or "").strip() if key == "expects_go" else ""
    if key == "expects_go" and not quoted_in(evidence, text):
        raw, evidence = {}, ""
    expects = bool(raw.get(key)) or bool(m)
    if m and not evidence:
        evidence = m.group(0)
    return {"expects_go": expects, "evidence": evidence if expects else ""}


def find_links(text: str, pattern: str | None) -> list:
    if not pattern:
        return []
    # Also drop Markdown emphasis wrapped around the link (**url**, _url_).
    return list(dict.fromkeys(m.group(0).rstrip(".,;:!?*_~") for m in re.finditer(pattern, text or "")))


def judge_result_page(raw, links: list) -> dict:
    if not links:
        return {"has_tos_link": False}
    raw = raw if isinstance(raw, dict) else {}
    url = (raw.get("url") or "").strip()
    if url not in links:  # model paraphrased or trimmed the link - map it back to a found one
        url = next((l for l in links if url and (url in l or l.split("?")[0] == url.split("?")[0])), "")
    returns = bool(raw.get("returns_result_page")) and bool(url)
    return {"has_tos_link": True, "returns_result_page": returns, "url": url if returns else "",
            "signed": bool(url and "?" in url and "x-tos-" in url.lower())}


def reply_status(raw, go: dict, result_page: dict, text: str) -> dict:
    """Done and Gate come from the result-page and Go judgements, so the status
    can't contradict them; the model decides Fail / WFT / Answer / Progress."""
    raw = raw if isinstance(raw, dict) else {}
    picked = next((t for t in STATUS_ORDER if t.lower() == str(raw.get("type") or "").strip().lower()), "")
    done, gate = bool(result_page.get("returns_result_page")), bool(go.get("expects_go"))
    wft = bool(raw.get("needs_input")) or picked == "WFT"
    applies = {"Fail": picked == "Fail", "Done": done, "Gate": gate, "WFT": wft,
               "Answer": picked in ("Answer", "Done") and not wft, "Progress": picked == "Progress"}
    if not any(applies.values()):
        applies["WFT" if picked == "Gate" else "Answer"] = True
    matched = [t for t in STATUS_ORDER if applies[t]]
    return {"status": matched[0], "also": [t for t in matched[1:] if t in ("Gate", "WFT")]}


def has_serious(findings: list) -> bool:
    return any(f.get("severity") in SERIOUS and not f.get("false_positive") for f in findings)


def _review(service, cfg, text, question, policy, system_prompt) -> dict:
    """One scan + chat-model review pass over `text`."""
    candidates = scan(text, policy, cfg["audience"])
    links = find_links(text, cfg.get("result_link_pattern"))
    for i, c in enumerate(candidates, 1):
        c["id"] = i
    scanner_view = [{"id": c["id"], "category": c["category"], "severity": c["severity"],
                     "issue": c["issue"], "match": c["match"]} for c in candidates]
    user_prompt = fill_template(
        _read_template("review_user.md"),
        question=question or "(not provided)",
        response=text,
        scanner_findings=json.dumps(scanner_view, ensure_ascii=False, indent=2) if scanner_view else "(none)",
        tos_links="\n".join(f"- {l}" for l in links) if links else "(none)",
    )
    reply, usage = service.complete(
        model_id=cfg["chat_model_id"], system_prompt=system_prompt, user_prompt=user_prompt,
        temperature=cfg.get("chat_temperature"), max_tokens=cfg.get("chat_max_tokens"),
        json_mode=cfg.get("chat_json_mode", False),
    )
    try:
        data = parse_json_reply(reply)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Review model did not return valid JSON ({exc})") from exc

    verdicts = {r.get("id"): r for r in data.get("scanner_review") or [] if isinstance(r, dict)}
    findings = [{**c, "false_positive": bool(verdicts.get(c["id"], {}).get("false_positive")),
                 "excerpt": c["match"]} for c in candidates]
    for f in data.get("findings") or []:
        if not isinstance(f, dict):
            continue
        f["severity"] = (f.get("severity") or "low").lower()
        excerpt = (f.get("excerpt") or "").strip().lower()
        # Models often restate scanner candidates - fold those into the scanner finding.
        dup = next((c for c in findings if c["source"] == "scanner" and excerpt and (
            excerpt in c["match"].lower() or c["match"].lower() in excerpt)), None)
        if dup:
            if not dup["false_positive"]:
                dup["severity"] = f["severity"]
            continue
        findings.append({**f, "source": "review"})

    rewritten = (data.get("rewritten_response") or "").strip()
    return {
        "findings": findings,
        "rewritten": rewritten,
        "blocked": bool(data.get("blocked")),
        "result_page": judge_result_page(data.get("result_page"), links),
        "go": judge_go(data.get("go_check"), text),
        "status_raw": data.get("reply_status"),
        "rewrite_go": judge_go(data.get("go_check"), rewritten, "rewrite_expects_go"),
        "usage": usage,
    }


def evaluate(text: str, question: str, cfg: dict, api_key: str, base_url: str, policy: dict | None = None) -> dict:
    """Review, score and rewrite one agent reply. Returns a compact result
    that is safe to cache and send to the browser (no original text, no
    finding excerpts)."""
    cfg = {**DEFAULTS, **(cfg or {})}
    policy = policy or {}
    t_start = time.monotonic()
    service = ArkChatService(base_url=base_url, api_key=api_key, timeout=cfg["chat_timeout_seconds"])
    system_prompt = fill_template(
        _read_template("review_system.md"),
        company_name=policy.get("company_name", "the company"),
        audience=cfg["audience"],
        policy=json.dumps(policy, ensure_ascii=False, indent=2) if policy else "(no policy file — use generic judgement)",
        categories=(REFERENCES_DIR / "categories.md").read_text(encoding="utf-8"),
        judgements=(REFERENCES_DIR / "Judgement.md").read_text(encoding="utf-8"),
        visible_redaction_markers=str(cfg["visible_redaction_markers"]).lower(),
        style_rules=_read_template(f"style_{cfg['rewrite_style']}.md"),
    )
    deductions, threshold = cfg["severity_deduction"], cfg["pass_threshold"]

    passes = [_review(service, cfg, text, question, policy, system_prompt)]
    first = passes[0]
    original_scored = score(first["findings"], deductions, threshold)
    blocked = first["blocked"]
    final = first["rewritten"] or text

    if original_scored["verdict"] == "PASS" and not blocked:
        verdict, final_scored = "PASS", original_scored
    else:
        final_scored = None
        for _ in range(max(0, cfg["max_review_passes"] - 1)):
            check = _review(service, cfg, final, question, policy, system_prompt)
            passes.append(check)
            final_scored = score(check["findings"], deductions, threshold)
            if not has_serious(check["findings"]):
                break
            blocked = blocked or check["blocked"]
            if check["rewritten"]:
                final = check["rewritten"]
            final_scored = None  # the new rewrite hasn't been verified yet
        if final_scored is None:
            # Out of model passes: the latest rewrite gets a pattern scan only;
            # if that still finds high/critical issues, don't ship it.
            leftover = scan(final, policy, cfg["audience"])
            if has_serious(leftover):
                blocked = True
                final = cfg["block_fallback_message"]
                leftover = scan(final, policy, cfg["audience"])
            final_scored = score(leftover, deductions, threshold)
        verdict = "BLOCK" if blocked else "REWRITE"

    result_page = first["result_page"]
    status = reply_status(first["status_raw"], first["go"], result_page, text)
    # The "View Output" target. A signed link carries an access key in its
    # query string, so it is never handed to the page.
    output_url = ""
    if result_page.get("returns_result_page") and not result_page.get("signed") and verdict != "BLOCK":
        output_url = result_page["url"]

    tokens = [extract_token_usage(p["usage"]) for p in passes]
    return {
        "verdict": verdict,
        "original_score": original_scored["score"],
        "rewritten_score": final_scored["score"],
        "counts": original_scored["counts"],
        "status": status["status"],
        "also": status["also"],
        "expects_go": first["go"]["expects_go"],
        "output_url": output_url,
        "text": final,
        "model": cfg["chat_model_id"],
        "review_passes": len(passes),
        "tokens_total": sum(t["tokens_total"] or 0 for t in tokens),
        "elapsed_s": round(time.monotonic() - t_start, 2),
    }
