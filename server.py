"""HTTP JSON bridge over esimplus.py for the Astro apps (admin + client).

Endpoints:
  GET  /countries                     -> [{slug, name}]
  GET  /numbers/{slug}                -> [{number, friendly, age, is_new, enabled}]
  GET  /sms/{number}?perPage=&page=   -> {items, total, page, per_page, status}
  GET  /admin/enabled                 -> {number: {slug, on}}  (persisted state)
  POST /admin/toggle-number/{number}?slug=&enabled=1|0        -> {enabled}
  POST /admin/toggle-country/{slug}?enabled=1|0               -> {count, enabled}
  GET  /client/numbers                -> enabled numbers with country info
  GET  /health                        -> ok

State persists in state.json next to this file. Only enabled numbers appear
on the client panel.
"""
from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote

import esimplus

PORT = int(os.environ.get("BRIDGE_PORT", "8788"))
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
_state_lock = threading.Lock()


def _load_state() -> dict:
    with _state_lock:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"enabled": {}}


def _save_state(st: dict) -> None:
    with _state_lock:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)


def _enabled_map() -> dict:
    return _load_state().get("enabled", {})


def _is_enabled(number: str) -> bool:
    return bool(_enabled_map().get(number, {}).get("on"))


def _country_names() -> dict:
    return {c["slug"]: c["name"] for c in esimplus.get_countries()}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json_error(self, status: int, message: str) -> None:
        self._send(status, {"status": status, "message": message, "data": None})

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _query(self) -> dict:
        q = self.path.split("?", 1)
        return parse_qs(q[1]) if len(q) > 1 else {}

    def do_GET(self) -> None:
        path = unquote(self.path.split("?", 1)[0])
        try:
            if path == "/health":
                self._send(200, {"status": 200, "message": "ok"})
            elif path == "/countries":
                countries = esimplus.get_countries()
                enabled = _enabled_map()
                enabled_slugs = {info.get("slug", "") for num, info in enabled.items() if info.get("on")}
                for c in countries:
                    c["anyEnabled"] = c["slug"] in enabled_slugs
                self._send(200, {"status": 200, "data": countries})
            elif path == "/admin/enabled":
                self._send(200, {"status": 200, "data": _enabled_map()})
            elif path == "/client/numbers":
                self._client_numbers()
            elif path.startswith("/numbers/"):
                slug = path[len("/numbers/"):]
                if not re.fullmatch(r"[a-z0-9-]+", slug):
                    self._json_error(400, "bad slug")
                    return
                items = esimplus.get_numbers(slug)
                for it in items:
                    it["enabled"] = _is_enabled(it["number"])
                self._send(200, {"status": 200, "data": items})
            elif path.startswith("/sms/"):
                number = path[len("/sms/"):]
                if not re.fullmatch(r"\d{10,12}", number):
                    self._json_error(400, "bad number")
                    return
                q = self._query()
                per_page = int((q.get("perPage") or ["30"])[0])
                page = int((q.get("page") or ["1"])[0])
                self._send(200, esimplus.get_sms(number, per_page=per_page, page=page))
            else:
                self._json_error(404, f"not found: {path}")
        except esimplus.EsimPlusError as e:
            self._json_error(502, str(e))
        except Exception as e:  # noqa: BLE001
            self._json_error(500, f"{type(e).__name__}: {e}")

    def _client_numbers(self) -> None:
        enabled = _enabled_map()
        names = _country_names()
        by_slug: dict[str, list[dict]] = {}
        for number, info in enabled.items():
            if not info.get("on"):
                continue
            by_slug.setdefault(info.get("slug", ""), []).append(number)
        out = []
        for slug, numbers in by_slug.items():
            try:
                items = {n["number"]: n for n in esimplus.get_numbers(slug)}
            except esimplus.EsimPlusError:
                continue
            for number in numbers:
                it = items.get(number)
                if not it:  # number rotated out of the public list
                    continue
                out.append({
                    "number": number,
                    "friendly": it["friendly"],
                    "age": it["age"],
                    "is_new": it["is_new"],
                    "slug": slug,
                    "country": names.get(slug, slug),
                })
        out.sort(key=lambda x: (x["country"], x["number"]))
        self._send(200, {"status": 200, "data": out})

    def do_POST(self) -> None:
        path = unquote(self.path.split("?", 1)[0])
        q = self._query()
        enabled = (q.get("enabled") or ["0"])[0] in ("1", "true", "True", "yes")
        try:
            if path.startswith("/admin/toggle-number/"):
                number = path[len("/admin/toggle-number/"):]
                if not re.fullmatch(r"\d{10,12}", number):
                    self._json_error(400, "bad number")
                    return
                slug = (q.get("slug") or [""])[0]
                st = _load_state()
                st.setdefault("enabled", {})[number] = {"slug": slug, "on": enabled}
                _save_state(st)
                self._send(200, {"status": 200, "enabled": enabled})
            elif path.startswith("/admin/toggle-country/"):
                slug = path[len("/admin/toggle-country/"):]
                if not re.fullmatch(r"[a-z0-9-]+", slug):
                    self._json_error(400, "bad slug")
                    return
                items = esimplus.get_numbers(slug)
                st = _load_state()
                st.setdefault("enabled", {})
                for it in items:
                    st["enabled"][it["number"]] = {"slug": slug, "on": enabled}
                _save_state(st)
                self._send(200, {"status": 200, "count": len(items), "enabled": enabled})
            else:
                self._json_error(404, f"not found: {path}")
        except esimplus.EsimPlusError as e:
            self._json_error(502, str(e))
        except Exception as e:  # noqa: BLE001
            self._json_error(500, f"{type(e).__name__}: {e}")

    def log_message(self, fmt, *args) -> None:  # quiet
        pass


def main() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"bridge listening on http://127.0.0.1:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
