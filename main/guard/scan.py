#!/usr/bin/env python3
"""Deterministic scanner and scorer for bot-response-rewrite.

Usage:
  python scan.py scan  --input response.txt [--policy policy.json] [--audience external|internal]
  python scan.py score --findings findings.json

`scan` prints {"findings": [...]} as JSON. `score` accepts either that object or a bare
list of findings and prints {"score", "verdict", "counts", "deductions"}.
Standard library only.
"""
import argparse
import ipaddress
import json
import re
import sys
from pathlib import Path

SEVERITY_DEDUCTION = {"critical": 40, "high": 25, "medium": 10, "low": 3}

# (category, severity, label, regex)
PATTERNS = [
    ("CREDENTIALS", "critical", "Private key block",
     r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----"),
    ("CREDENTIALS", "critical", "AWS access key ID", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("CREDENTIALS", "critical", "AWS secret access key",
     r"(?i)aws_?secret_?access_?key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}"),
    ("CREDENTIALS", "critical", "GitHub token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"),
    ("CREDENTIALS", "critical", "Slack token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    ("CREDENTIALS", "critical", "LLM / Stripe-style secret key",
     r"\b(?:sk|rk)[-_](?:live|test|proj|ant(?:-api\d+)?)?[-_]?[A-Za-z0-9_-]{20,}\b"),
    ("CREDENTIALS", "critical", "Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("CREDENTIALS", "high", "JWT", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ("CREDENTIALS", "high", "Bearer token", r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*"),
    ("CREDENTIALS", "critical", "Connection string with password",
     r"\b[a-z][a-z0-9+.-]{1,20}://[^\s:/@]+:[^\s@/]{3,}@[^\s/]+"),
    ("CREDENTIALS", "high", "Assigned secret/password",
     r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret)\b\s*[:=]\s*['\"]?[^\s'\"<>]{6,}"),
    ("PII", "critical", "US SSN", r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
    ("PII", "critical", "IBAN", r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b"),
    ("PII", "high", "Email address", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ("PII", "medium", "Phone number",
     r"(?<![\w-])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?|\d{2,4}[\s.-])\d{3,4}[\s.-]\d{3,4}(?![\w-])"),
]
CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?!\.?\d)")
PLACEHOLDER_RE = re.compile(r"(?i)(x{4,}|\*{4,}|example|placeholder|your[_-]|<[^>]+>|changeme|redacted|\.\.\.)")


def luhn_ok(digits):
    total, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        alt = not alt
    return total % 10 == 0


def load_policy(path):
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def norm(s):
    return re.sub(r"[\s().-]", "", s.lower())


def scan(text, policy, audience):
    findings = []
    allowed = {norm(a) for a in policy.get("allowed_public", [])}
    override = policy.get("audience_overrides", {}).get(audience, {})
    allow_terms = {t.lower() for t in override.get("allow_terms", [])}
    allow_internal_domains = override.get("allow_internal_domains", False)
    taken = []  # spans already claimed by a more specific pattern

    def overlaps(s, e):
        return any(s < te and e > ts for ts, te in taken)

    def add(cat, sev, label, m_text, s, e, claim=True):
        if norm(m_text) in allowed or any(a and a in norm(m_text) for a in allowed if len(a) > 6 and "@" in a):
            return
        if overlaps(s, e):
            return
        placeholder = bool(PLACEHOLDER_RE.search(m_text))
        if placeholder and cat == "CREDENTIALS":
            sev, label = "low", label + " (looks like placeholder)"
        findings.append({"category": cat, "severity": sev, "issue": label, "match": m_text,
                         "start": s, "end": e, "source": "scanner"})
        if claim:
            taken.append((s, e))

    # Cards first so their digit groups aren't claimed as phone numbers.
    for m in CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and luhn_ok(digits) and len(set(digits)) > 1:
            add("PII", "critical", "Payment card number (Luhn valid)", m.group(0).strip(), m.start(), m.end())

    for cat, sev, label, pat in PATTERNS:
        for m in re.finditer(pat, text):
            add(cat, sev, label, m.group(0), m.start(), m.end())

    for m in IPV4_RE.finditer(text):
        try:
            ip = ipaddress.ip_address(m.group(0))
        except ValueError:
            continue
        if ip.is_private and policy.get("internal_ip_ranges_are_confidential", True) and audience != "internal":
            add("COMPANY_CONFIDENTIAL", "medium", "Internal/private IP address", m.group(0), m.start(), m.end())
        elif not ip.is_private and not ip.is_loopback and not ip.is_unspecified:
            add("PII", "low", "Public IP address", m.group(0), m.start(), m.end())

    if not allow_internal_domains:
        for dom in policy.get("internal_domains", []):
            for m in re.finditer(r"(?i)\b[\w.-]*" + re.escape(dom) + r"\b(?:/[^\s)]*[^\s).,;:!?'\"])?", text):
                add("COMPANY_CONFIDENTIAL", "medium", "Internal domain/URL: " + dom,
                    m.group(0), m.start(), m.end(), claim=False)

    for entry in policy.get("confidential_terms", []):
        term = entry["term"] if isinstance(entry, dict) else entry
        if term.lower() in allow_terms:
            continue
        sev = entry.get("severity", "high") if isinstance(entry, dict) else "high"
        note = entry.get("note", "Confidential term") if isinstance(entry, dict) else "Confidential term"
        for m in re.finditer(r"(?i)\b" + re.escape(term) + r"\b", text):
            add("COMPANY_CONFIDENTIAL", sev, f"{note} ('{term}')", m.group(0), m.start(), m.end(), claim=False)

    findings.sort(key=lambda f: f["start"])
    return findings


def load_scoring_config():
    """Severity deductions + pass threshold from the skill's config.json (stdlib only, so this
    script still runs standalone without requests installed)."""
    path = Path(__file__).resolve().parent.parent / "config.json"
    cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return cfg.get("severity_deduction", SEVERITY_DEDUCTION), cfg.get("pass_threshold", 85)


def score(findings, deductions_map=None, pass_threshold=None):
    """Score 0-100 (100 = safe) and a PASS/REWRITE suggestion. BLOCK is decided by the rewrite
    step (nothing useful survives), not by the number alone."""
    if deductions_map is None or pass_threshold is None:
        cfg_map, cfg_threshold = load_scoring_config()
        deductions_map = deductions_map or cfg_map
        pass_threshold = cfg_threshold if pass_threshold is None else pass_threshold
    seen, deductions, counts = set(), [], {k: 0 for k in deductions_map}
    for f in findings:
        if f.get("false_positive"):
            continue
        sev = (f.get("severity") or "low").lower()
        key = (f.get("category"), (f.get("match") or f.get("excerpt") or f.get("issue", "")).strip().lower())
        if key in seen:
            continue
        seen.add(key)
        counts[sev] = counts.get(sev, 0) + 1
        deductions.append({"category": f.get("category"), "severity": sev,
                           "points": deductions_map.get(sev, 0), "issue": f.get("issue")})
    s = max(0, 100 - sum(d["points"] for d in deductions))
    serious = counts.get("high", 0) + counts.get("critical", 0)
    verdict = "PASS" if s >= pass_threshold and serious == 0 else "REWRITE"
    return {"score": s, "verdict": verdict, "counts": counts, "deductions": deductions}


def mask_excerpt(category, text):
    """Masks secrets / sensitive identifiers so reports and logs don't re-leak them. A
    token-like excerpt (a key, an email, a number) is masked whole; a sentence-length excerpt
    keeps its wording and only has the secrets found inside it masked."""
    text = text or ""
    if not re.search(r"\s", text.strip()):
        if category in ("CREDENTIALS", "PII") and len(text) > 8:
            return text[:4] + "…" + text[-4:]
        return text
    for f in sorted(scan(text, {}, "external"), key=lambda f: -f["start"]):
        if f["category"] == "CREDENTIALS" or f["severity"] == "critical":
            m = f["match"]
            text = text[:f["start"]] + (m[:4] + "…" + m[-4:] if len(m) > 8 else "…") + text[f["end"]:]
    return text


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("scan")
    a.add_argument("--input", required=True, help="text file, or - for stdin")
    a.add_argument("--policy")
    a.add_argument("--audience", default="external", choices=["external", "internal"])
    b = sub.add_parser("score")
    b.add_argument("--findings", required=True, help="JSON file, or - for stdin")
    args = ap.parse_args()

    if args.cmd == "scan":
        text = sys.stdin.read() if args.input == "-" else open(args.input, encoding="utf-8").read()
        findings = scan(text, load_policy(args.policy), args.audience)
        print(json.dumps({"findings": findings, "policy_loaded": bool(args.policy)}, indent=2, ensure_ascii=False))
    else:
        raw = sys.stdin.read() if args.findings == "-" else open(args.findings, encoding="utf-8").read()
        data = json.loads(raw)
        findings = data.get("findings", []) if isinstance(data, dict) else data
        print(json.dumps(score(findings), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
