"""esimplus.me client: countries, temp numbers, SMS.

Handles two network hurdles transparently:
  1. ISP DNS hijacks esimplus.me -> resolved to real Cloudflare IPs (via 8.8.8.8)
     and connected with `CurlOpt.RESOLVE` so TLS SNI stays `esimplus.me`.
  2. Cloudflare bot protection -> curl_cffi Chrome TLS impersonation
     (plain requests/curl get 403; chrome150 impersonation passes).
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from bs4 import BeautifulSoup
from curl_cffi import Curl, CurlInfo, CurlOpt

BASE = "https://esimplus.me"

# Real Cloudflare edge IPs for esimplus.me (system DNS is RPZ-hijacked)
IPS = ["104.20.22.124", "172.66.167.95"]
# Fallback ladder for TLS impersonation
IMPS = ["chrome150", "chrome146", "chrome136"]

# alias slug -> canonical slug (landing page lists both)
SLUG_ALIASES = {"ca": "canada", "us": "united-states"}

COUNTRY_TTL = 900  # seconds
NUMBERS_TTL = 120  # seconds

_countries_cache: dict[str, Any] = {"t": 0.0, "data": None}
_numbers_cache: dict[str, tuple[float, list[dict]]] = {}


class EsimPlusError(Exception):
    """Site unreachable / blocked / bad response."""


def _fetch(url: str, timeout: float = 30.0) -> str:
    """GET with Chrome TLS impersonation + DNS bypass. Returns decoded body."""
    last: Any = None
    for imp in IMPS:
        for ip in IPS:
            try:
                c = Curl()
                hdrs: list[bytes] = []
                body: list[bytes] = []
                c.setopt(CurlOpt.URL, url.encode())
                c.setopt(CurlOpt.RESOLVE, [f"esimplus.me:443:{ip}"])
                c.setopt(CurlOpt.IMPERSONATE, imp)
                c.setopt(CurlOpt.ACCEPT_ENCODING, b"")  # gzip/br auto-decode
                c.setopt(CurlOpt.HEADERFUNCTION, lambda b: hdrs.append(b))
                c.setopt(CurlOpt.WRITEFUNCTION, lambda b: body.append(b))
                c.setopt(CurlOpt.TIMEOUT_MS, int(timeout * 1000))
                c.perform()
                code = c.getinfo(CurlInfo.RESPONSE_CODE)
                c.close()
                if code == 200:
                    return b"".join(body).decode("utf-8", "replace")
                last = f"HTTP {code}"
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
    raise EsimPlusError(f"esimplus.me unreachable: {last}")


# ---------------------------------------------------------------- countries

def get_countries(force: bool = False) -> list[dict[str, str]]:
    """All temp-number countries: [{'slug': 'canada', 'name': '🇨🇦 Canada'}]."""
    now = time.time()
    if not force and _countries_cache["data"] and now - _countries_cache["t"] < COUNTRY_TTL:
        return _countries_cache["data"]

    html = _fetch(f"{BASE}/temporary-numbers")
    soup = BeautifulSoup(html, "html.parser")
    seen: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        m = re.fullmatch(r"/temporary-numbers/([a-z0-9-]+)", a["href"])
        if not m:
            continue
        slug = m.group(1)
        if slug.isdigit():  # pagination links ("/2", "/3", ...)
            continue
        name = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        if not name or len(name) > 40:
            continue
        seen[slug] = name

    # ca -> canada, us -> united-states (canonical page)
    merged: dict[str, str] = {}
    for slug, name in seen.items():
        canonical = SLUG_ALIASES.get(slug, slug)
        if canonical not in merged or slug == canonical:
            merged[canonical] = name

    _countries_cache["t"] = now
    _countries_cache["data"] = [
        {"slug": slug, "name": name} for slug, name in merged.items()
    ]
    return _countries_cache["data"]


# ----------------------------------------------------------------- numbers

# Embedded RSC number objects on a country page (authoritative list incl. the
# link-less "featured/latest" card at the top). Fields are escaped JSON.
OBJ_RE = re.compile(
    r'\\"phoneNumber\\":\\"(\d{10,12})\\",\\"countryCode\\":\\"([A-Z]{2})\\",'
    r'\\"slug\\":\\"([a-z-]+)\\",\\"description\\":[^,]{0,20},'
    r'\\"createdAt\\":\\"([^\\]+)\\",\\"friendlyPhoneNumber\\":\\"([^\\]+)'
)
# Rendered number cards: <div class="styles_number__xxx">+1 636-662-0272</div>
CARD_RE = re.compile(r'<div class="styles_number__[^"]+">([+]?[\d][\d\s-]{8,18})</div>')
# Raw number links in the list
LINK_RE = re.compile(r"/temporary-numbers/(?:[a-z0-9-]+)/(\d{10,12})")


def _age_seconds(created: str) -> float:
    """createdAt ISO -> seconds old."""
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return max(0, (datetime.now(timezone.utc) - dt).total_seconds())
    except (ValueError, TypeError):
        return float("inf")


def _age_label(created: str) -> str:
    """createdAt -> 'added 3h ago'."""
    s = _age_seconds(created)
    if s == float("inf"):
        return ""
    if s < 3600:
        return f"added {max(1, s // 60)}m ago"
    if s < 86400:
        return f"added {s // 3600}h ago"
    if s < 604800:
        return f"added {s // 86400}d ago"
    return f"added {s // 604800}w ago"


def get_numbers(slug: str, force: bool = False) -> list[dict]:
    """Temp numbers for a country, newest first (matches the web's ordering,
    including the link-less 'featured/latest' card):

    [{'number': '16366620272', 'friendly': '+1 636-662-0272',
      'age': 'added 3h ago', 'is_new': True}]"""
    now = time.time()
    entry = _numbers_cache.get(slug)
    if not force and entry and now - entry[0] < NUMBERS_TTL:
        return entry[1]

    merged: dict[str, dict] = {}
    for page in range(1, 16):
        url = f"{BASE}/temporary-numbers/{slug}" if page == 1 else f"{BASE}/temporary-numbers/{slug}/{page}"
        try:
            html = _fetch(url)
        except EsimPlusError:
            break
        before = len(merged)
        for m in OBJ_RE.finditer(html):
            num, cc, s, created, friendly = m.groups()
            if s != slug:
                continue
            merged.setdefault(num, {"number": num, "friendly": friendly, "createdAt": created, "_dom": before})
        # fallbacks in case the RSC shape ever changes
        for m in CARD_RE.finditer(html):
            num = re.sub(r"\D", "", m.group(1))
            if num and num not in merged:
                merged[num] = {"number": num, "friendly": m.group(1).strip(), "createdAt": "", "_dom": before}
        for m in LINK_RE.finditer(html):
            num = m.group(1)
            if num and num not in merged:
                merged[num] = {"number": num, "friendly": "", "createdAt": "", "_dom": before}
        if len(merged) == before:
            break  # page empty or no new numbers -> stop

    result = []
    for n in merged.values():
        result.append({
            "number": n["number"],
            "friendly": n["friendly"] or n["number"],
            "age": _age_label(n["createdAt"]),
            "is_new": bool(n["createdAt"]) and _age_seconds(n["createdAt"]) < 86400,
            "_ts": n["createdAt"],
        })
    # newest first (createdAt DESC); entries without createdAt fall to the end
    result.sort(key=lambda x: x["_ts"], reverse=True)
    for it in result:
        it.pop("_ts", None)
    _numbers_cache[slug] = (now, result)
    return result


# --------------------------------------------------------------------- sms

def get_sms(number: str, per_page: int = 8, page: int = 1) -> dict:
    """SMS list for a number:
    {'items': [{provider, from, to, body, receivedAt, friendlyFrom, friendlyTo}],
     'total': int, 'page': int, 'per_page': int, 'status': int, 'message': str}"""
    url = f"{BASE}/api/sms-receiver/{number}/sms?perPage={per_page}&page={page}"
    try:
        j = json.loads(_fetch(url))
    except json.JSONDecodeError as e:
        raise EsimPlusError("Bad response from SMS API") from e
    return {
        "items": j.get("data") or [],
        "total": int((j.get("pagination") or {}).get("total", 0)),
        "page": int((j.get("pagination") or {}).get("currentPage", page)),
        "per_page": per_page,
        "status": j.get("status", 200),
        "message": j.get("message", ""),
    }


# ------------------------------------------------------------------ helpers

def rel_time(iso: Optional[str]) -> str:
    """'2026-09-05T10:33:20.000000Z' -> '3m ago' (UTC)."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        s = int((datetime.now(timezone.utc) - dt).total_seconds())
    except (ValueError, TypeError):
        return ""
    if s < 0:
        return "just now"
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    if s < 604800:
        return f"{s // 86400}d ago"
    return dt.strftime("%d %b %Y")
