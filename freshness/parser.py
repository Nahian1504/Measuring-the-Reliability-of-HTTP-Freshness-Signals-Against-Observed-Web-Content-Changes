from __future__ import annotations

import re
from email.utils import parsedate_to_datetime

DELTA_DIRECTIVES = {"max-age", "s-maxage", "stale-while-revalidate", "stale-if-error", "min-fresh", "max-stale"}


def _split_commas(value: str) -> list[str]:
    parts, buf, in_quotes = [], [], False
    for ch in value:
        if ch == '"':
            in_quotes = not in_quotes
        if ch == "," and not in_quotes:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def parse_cache_control(value: str | None) -> dict:
    result = {"directives": {}, "invalid": [], "duplicates": []}
    if not value:
        return result
    for part in _split_commas(value):
        if "=" in part:
            name, _, raw = part.partition("=")
            name, raw = name.strip().lower(), raw.strip()
            if len(raw) >= 2 and raw[0] == raw[-1] == '"':
                raw = raw[1:-1]
        else:
            name, raw = part.strip().lower(), None
        if not name:
            continue
        if name in result["directives"]:
            result["duplicates"].append(name)
            continue
        if name in DELTA_DIRECTIVES:
            if raw is None or not re.fullmatch(r"\d+", raw):
                result["invalid"].append(name)
                continue
            result["directives"][name] = int(raw)
        else:
            result["directives"][name] = True if raw is None else raw
    return result


def parse_etag(value: str | None) -> dict:
    if value is None:
        return {"etag": None, "opaque": None, "weak": None, "valid": False}
    v = value.strip()
    weak = v[:2] in ("W/", "w/")
    body = v[2:] if weak else v
    valid = bool(re.fullmatch(r'"[^"]*"', body)) and (not weak or v.startswith("W/"))
    opaque = body[1:-1] if len(body) >= 2 and body[0] == body[-1] == '"' else body
    return {"etag": v, "opaque": opaque, "weak": weak, "valid": valid}


def parse_http_date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:  
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def freshness_lifetime(headers: dict, shared: bool = True) -> tuple[float | None, str]:
    cc = parse_cache_control(headers.get("cache-control"))["directives"]
    if "no-store" in cc:
        return 0.0, "no-store"
    if "no-cache" in cc and cc["no-cache"] is True:
        return 0.0, "no-cache"
    if shared and "s-maxage" in cc:
        return float(cc["s-maxage"]), "s-maxage"
    if "max-age" in cc:
        return float(cc["max-age"]), "max-age"
    if "expires" in headers:
        exp = parse_http_date(headers.get("expires"))
        if exp is None:
            return 0.0, "expires-invalid"
        date = parse_http_date(headers.get("date"))
        if date is None:
            return None, "expires-no-date"
        return max(0.0, exp - date), "expires"
    return None, "none"


def lm_uninformative(last_modified: str | None, date: str | None, window_s: float = 60) -> bool | None:
    lm, d = parse_http_date(last_modified), parse_http_date(date)
    if lm is None or d is None:
        return None
    return abs(d - lm) <= window_s
