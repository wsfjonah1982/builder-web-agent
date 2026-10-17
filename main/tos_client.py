"""Minimal BytePlus TOS client — plain HTTP via `requests`, signed with TOS's
own TOS4-HMAC-SHA256 scheme. No vendor SDK. Copied unchanged (apart from
this line) from builder-brand-design/scripts/tos_client.py; used by
design.py for the Design tab.

TOS is *not* literally AWS-S3-signature-compatible despite an S3-like REST
surface (confirmed live: a real AWS SigV4 request gets rejected outright with
"Unsupported Authorization Type"). The scheme here is transcribed from the
installed `tos` SDK's own auth.py (AuthBase.sign_request / _make_signature /
_signature) and clientv2.py (list_objects_type2's JSON response shape) —
BytePlus's own docs pages didn't render the exact header-based signing spec
for a web fetch, so the SDK source was the authoritative reference.

Two things confirmed only by hitting the real API, not documented anywhere
found:
  - The server verifies the real body hash via an explicit
    x-tos-content-sha256 header — the SDK's own client-side "UNSIGNED-PAYLOAD"
    fallback for header-less requests is not accepted server-side once a body
    is present (a real 403 SignatureDoesNotMatch echoed back SHA256("") in
    its recomputed canonical request when this header was omitted).
  - list_objects_type2's response body is JSON (`Contents: [{"Key": ...}]`,
    `NextContinuationToken`), not S3's XML.
"""
import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import quote

import requests

TOS_ALGORITHM = "TOS4-HMAC-SHA256"
TOS_SERVICE = "tos"
TOS_DATE_FORMAT = "%Y%m%dT%H%M%SZ"


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, date8: str, region: str) -> bytes:
    # No "TOS4"/"AWS4" prefix on the secret — unlike AWS SigV4, TOS signs
    # straight off the raw secret key bytes (per tos/auth.py: AuthBase._signature).
    k_date = _hmac(secret_key.encode("utf-8"), date8)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, TOS_SERVICE)
    return _hmac(k_service, "request")


def _canonical_query_string(params: dict) -> str:
    parts = [f"{quote(str(k), safe='-_.~')}={quote(str(v), safe='-_.~')}" for k, v in sorted(params.items())]
    return "&".join(parts)


def _authorization(method: str, path: str, query_params: dict, headers_to_sign: dict,
                    content_sha256: str, access_key: str, secret_key: str, region: str, date: str) -> str:
    date8 = date[:8]
    signed_header_names = ";".join(sorted(headers_to_sign))
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted(headers_to_sign.items()))
    canonical_request = "\n".join([
        method, quote(path, safe="/~"), _canonical_query_string(query_params),
        canonical_headers, signed_header_names, content_sha256,
    ])
    credential_scope = f"{date8}/{region}/{TOS_SERVICE}/request"
    string_to_sign = "\n".join([
        TOS_ALGORITHM, date, credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(_signing_key(secret_key, date8, region),
                          string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return (f"{TOS_ALGORITHM} Credential={access_key}/{credential_scope}, "
            f"SignedHeaders={signed_header_names}, Signature={signature}")


def put_object(endpoint: str, region: str, bucket: str, key: str,
                access_key: str, secret_key: str, content: bytes, content_type: str,
                cache_control: str | None = None) -> None:
    """PUT one object, public-read. `cache_control` is stored as the object's
    Cache-Control metadata and served back on every GET."""
    host = f"{bucket}.{endpoint}"
    date = datetime.now(timezone.utc).strftime(TOS_DATE_FORMAT)
    content_sha256 = hashlib.sha256(content).hexdigest()

    headers_to_sign = {
        "content-type": content_type,
        "host": host,
        "x-tos-acl": "public-read",
        "x-tos-content-sha256": content_sha256,
        "x-tos-date": date,
    }
    if cache_control:
        headers_to_sign["cache-control"] = cache_control
    authorization = _authorization("PUT", f"/{key}", {}, headers_to_sign, content_sha256,
                                    access_key, secret_key, region, date)

    headers = {
        "Host": host, "Date": date, "x-tos-date": date, "x-tos-acl": "public-read",
        "x-tos-content-sha256": content_sha256, "Content-Type": content_type,
        "Authorization": authorization,
    }
    if cache_control:
        headers["Cache-Control"] = cache_control
    resp = requests.put(
        f"https://{host}/{key}",
        headers=headers,
        data=content,
        timeout=60,
    )
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"TOS PUT failed for {key}: {resp.status_code} {resp.text}")


def delete_object(endpoint: str, region: str, bucket: str, key: str,
                   access_key: str, secret_key: str) -> None:
    """DELETE one object. TOS returns 204 whether or not the key existed."""
    host = f"{bucket}.{endpoint}"
    date = datetime.now(timezone.utc).strftime(TOS_DATE_FORMAT)
    content_sha256 = hashlib.sha256(b"").hexdigest()

    headers_to_sign = {
        "host": host,
        "x-tos-content-sha256": content_sha256,
        "x-tos-date": date,
    }
    authorization = _authorization("DELETE", f"/{key}", {}, headers_to_sign, content_sha256,
                                    access_key, secret_key, region, date)

    resp = requests.delete(
        f"https://{host}/{key}",
        headers={
            "Host": host, "Date": date, "x-tos-date": date,
            "x-tos-content-sha256": content_sha256, "Authorization": authorization,
        },
        timeout=60,
    )
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"TOS DELETE failed for {key}: {resp.status_code} {resp.text}")


def list_objects(endpoint: str, region: str, bucket: str,
                  access_key: str, secret_key: str, prefix: str) -> list[str]:
    """List every object key under `prefix`, following pagination."""
    return [e["Key"] for e in list_object_entries(endpoint, region, bucket, access_key, secret_key, prefix)]


def list_object_entries(endpoint: str, region: str, bucket: str,
                        access_key: str, secret_key: str, prefix: str) -> list[dict]:
    """Like list_objects, but returns each object's listing entry
    ({"Key", "Size", "LastModified", ...}), following pagination."""
    host = f"{bucket}.{endpoint}"
    entries: list[dict] = []
    continuation_token = None

    while True:
        query_params = {"list-type": "2", "fetch-owner": "true", "prefix": prefix}
        if continuation_token:
            query_params["continuation-token"] = continuation_token

        date = datetime.now(timezone.utc).strftime(TOS_DATE_FORMAT)
        content_sha256 = hashlib.sha256(b"").hexdigest()
        headers_to_sign = {
            "host": host,
            "x-tos-content-sha256": content_sha256,
            "x-tos-date": date,
        }
        authorization = _authorization("GET", "/", query_params, headers_to_sign, content_sha256,
                                        access_key, secret_key, region, date)

        resp = requests.get(
            f"https://{host}/",
            params=query_params,
            headers={
                "Host": host, "Date": date, "x-tos-date": date,
                "x-tos-content-sha256": content_sha256, "Authorization": authorization,
            },
            timeout=60,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"TOS list failed for prefix {prefix!r}: {resp.status_code} {resp.text}")

        data = resp.json()
        entries.extend(data.get("Contents") or [])

        if not data.get("IsTruncated"):
            break
        continuation_token = data.get("NextContinuationToken")
        if not continuation_token:
            break

    return entries
