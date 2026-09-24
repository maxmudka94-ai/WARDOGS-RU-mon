"""Панель управления WARDOGS-серверами (встроена в процесс Discord-бота).

Проксирует HTTP RCON API (Bearer-токен = пароль RCON) наружу через
собственный порт с логином по паролю панели. Токен RCON никогда
не попадает в браузер — все запросы идут через этот бэкенд.
"""

import asyncio
import json
import logging
import secrets
import time
from pathlib import Path

from aiohttp import web

import config
from rcon_client import RCONError, WardogsRCON

log = logging.getLogger("webpanel")

BASE_DIR = Path(__file__).resolve().parent
INDEX_PATH = BASE_DIR / "webpanel" / "index.html"

HOST = config.PANEL_HOST
PORT = config.PANEL_PORT
PASSWORD = config.PANEL_PASSWORD
PUBLIC_URL = config.PANEL_PUBLIC_URL
REFRESH = max(2.0, float(config.PANEL_REFRESH))

ALLOWED_ORIGINS = {
    f"http://{HOST}:{PORT}",
    f"http://localhost:{PORT}",
    f"http://127.0.0.1:{PORT}",
}
if PUBLIC_URL:
    ALLOWED_ORIGINS.add(PUBLIC_URL)

TOKEN = secrets.token_urlsafe(24)
_LOGIN_WINDOW = 300.0
_LOGIN_MAX = 8
_login_attempts = {}

CACHE_TTL = 3.0
_cache = {}
_cache_lock = asyncio.Lock()


class Server:
    def __init__(self, cfg):
        self.id = cfg["id"]
        self.name = cfg["name"]
        self.connect_url = cfg.get("connect_url") or ""
        self.api = WardogsRCON(cfg)
        self.routes = set()
        self.config_writable = False
        self.routes_checked = False

    async def ensure_caps(self):
        if self.routes_checked:
            return
        try:
            caps = await self.api.capabilities()
            self.routes = set(caps.get("routes") or [])
            self.config_writable = bool((caps.get("config") or {}).get("writable"))
            self.api_version = caps.get("apiVersion")
            self.build = caps.get("build")
        except RCONError:
            self.routes = set()
            self.config_writable = False
            self.api_version = None
            self.build = None
        finally:
            self.routes_checked = True

    def has(self, method, path):
        return f"{method} {path}" in self.routes


POOL = {cfg["id"]: Server(cfg) for cfg in config.SERVERS}


def _get_server(sid):
    return POOL.get(sid)


async def _cached(sid, name, func, ttl=CACHE_TTL):
    key = (sid, name)
    async with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
    data = await func()
    async with _cache_lock:
        _cache[key] = (time.monotonic() + ttl, data)
    return data


async def _cached_players(srv, attempts=2):
    """/v1/players нестабилен на загруженных серверах — пара попыток."""
    last = None
    for i in range(attempts):
        try:
            return await _cached(srv.id, "players", srv.api.players)
        except RCONError as e:
            last = e
            if i < attempts - 1:
                await asyncio.sleep(1.5)
    raise last


async def _invalidate(sid):
    async with _cache_lock:
        for key in [k for k in _cache if k[0] == sid]:
            del _cache[key]


# ---------- авторизация ----------

def _origin_ok(request):
    origin = request.headers.get("Origin")
    if not origin:
        return True
    if origin in ALLOWED_ORIGINS:
        return True
    try:
        oh = origin.split("://", 1)[1].split("/", 1)[0].rsplit(":", 1)[0].lower()
        host = (request.headers.get("Host") or "").rsplit(":", 1)[0].lower()
    except Exception:
        return False
    return oh == host


def _require_token(request):
    if not _origin_ok(request):
        raise web.HTTPForbidden(text="bad origin")
    if request.headers.get("X-Panel-Token") != TOKEN:
        raise web.HTTPUnauthorized(text="bad token")


def _srv(request):
    sid = request.match_info["sid"]
    srv = _get_server(sid)
    if srv is None:
        raise web.HTTPNotFound(text="unknown server")
    return srv


async def _json(request, required=()):
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    for key in required:
        if not str(data.get(key) or "").strip():
            raise web.HTTPBadRequest(
                text=json.dumps(
                    {"error": {"code": "missing_field", "message": f"требуется поле {key}"}}
                ),
                content_type="application/json",
            )
    return data


def _write_result(status, data):
    """Нормализует ответ RCON-записи: на 200 просто отдаём тело сервера."""
    if status == 200:
        return web.json_response(data if isinstance(data, dict) else {"ok": True, "data": data})
    if status in (204, 202, 201):
        return web.json_response({"ok": True, "data": data}, status=status)
    if isinstance(data, dict) and "error" in data:
        return web.json_response(data, status=status)
    return web.json_response(
        {"ok": False, "error": {"code": "http_error", "message": f"HTTP {status}"}},
        status=status,
    )


# ---------- API ----------

async def h_login(request):
    ip = request.remote or "?"
    now = time.monotonic()
    cur = _login_attempts.get(ip)
    if cur and cur[0] + _LOGIN_WINDOW < now:
        cur = None
    cnt = cur[0] if cur else 0
    if cnt >= _LOGIN_MAX:
        return web.json_response(
            {"error": {"code": "rate_limited", "message": "слишком много попыток, подождите"}},
            status=429,
        )
    try:
        data = await request.json()
    except Exception:
        data = {}
    pw = str(data.get("password") or "")
    if PASSWORD and secrets.compare_digest(pw, PASSWORD):
        _login_attempts.pop(ip, None)
        return web.json_response({"ok": True, "token": TOKEN})
    _login_attempts[ip] = (cnt + 1, now)
    return web.json_response(
        {"error": {"code": "bad_password", "message": "неверный пароль"}}, status=401
    )


async def h_session(request):
    authed = _origin_ok(request) and request.headers.get("X-Panel-Token") == TOKEN
    return web.json_response(
        {
            "ok": True,
            "authed": authed,
            "needs_password": bool(PASSWORD),
            "refresh": REFRESH,
            "servers": [{"id": s.id, "name": s.name} for s in POOL.values()],
        }
    )


async def h_overview(request):
    _require_token(request)
    srv = _srv(request)
    st = players = rotation = bans = health = None
    bans_count = None
    err = None
    status = 200
    try:
        st = await _cached(srv.id, "status", srv.api.status)
    except RCONError as e:
        err = {"code": e.code, "message": e.message}
        status = e.status or 502
    if err is None:
        try:
            players = await _cached_players(srv)
        except RCONError:
            players = None
        for name, f in (
            ("rotation", srv.api.rotation),
            ("bans", srv.api.bans),
            ("health", srv.api.health),
        ):
            try:
                data = await _cached(srv.id, name, f)
                if name == "rotation":
                    rotation = data
                elif name == "bans":
                    bans = data
                    bans_count = data.get("count") if isinstance(data, dict) else None
                else:
                    health = data
            except RCONError:
                pass
    await srv.ensure_caps()
    return web.json_response(
        {
            "ok": err is None,
            "server": {
                "id": srv.id,
                "name": srv.name,
                "connect_url": srv.connect_url,
                "routes": sorted(srv.routes),
                "config_writable": srv.config_writable,
                "api_version": getattr(srv, "api_version", None),
                "build": getattr(srv, "build", None),
            },
            "error": err,
            "status": st,
            "players": players,
            "rotation": rotation,
            "bans": bans,
            "bans_count": bans_count,
            "health": health,
            "ts": time.time(),
        },
        status=status,
    )


async def h_players(request):
    _require_token(request)
    srv = _srv(request)
    try:
        data = await _cached_players(srv)
    except RCONError as e:
        return web.json_response({"error": {"code": e.code, "message": e.message}}, status=e.status or 502)
    count = data.get("count", len(data.get("players") or []))
    return web.json_response({"ok": True, "count": count, "players": data.get("players") or []})


async def h_rotation(request):
    _require_token(request)
    srv = _srv(request)
    try:
        data = await _cached(srv.id, "rotation", srv.api.rotation)
    except RCONError as e:
        return web.json_response({"error": {"code": e.code, "message": e.message}}, status=e.status or 502)
    return web.json_response({"ok": True, **data})


async def h_bans(request):
    _require_token(request)
    srv = _srv(request)
    try:
        data = await _cached(srv.id, "bans", srv.api.bans)
    except RCONError as e:
        return web.json_response({"error": {"code": e.code, "message": e.message}}, status=e.status or 502)
    return web.json_response({"ok": True, **data})


async def h_audit(request):
    _require_token(request)
    srv = _srv(request)
    limit = int(request.query.get("limit") or 50)
    limit = max(1, min(limit, 500))
    try:
        data = await srv.api.get("/v1/audit", params={"limit": limit})
    except RCONError as e:
        return web.json_response({"error": {"code": e.code, "message": e.message}}, status=e.status or 502)
    return web.json_response({"ok": True, "entries": data.get("entries") or []})


async def h_health(request):
    _require_token(request)
    srv = _srv(request)
    try:
        data = await _cached(srv.id, "health", srv.api.health)
    except RCONError as e:
        return web.json_response({"error": {"code": e.code, "message": e.message}}, status=e.status or 502)
    return web.json_response({"ok": True, **data})


async def h_catalog(request):
    _require_token(request)
    srv = _srv(request)

    async def _maps():
        return await srv.api.get("/v1/catalog/maps")

    async def _lightings():
        return await srv.api.get("/v1/catalog/lightings")

    try:
        maps = await _cached(srv.id, "catalog_maps", _maps)
        lightings = await _cached(srv.id, "catalog_lightings", _lightings)
    except RCONError as e:
        return web.json_response({"error": {"code": e.code, "message": e.message}}, status=e.status or 502)
    by_map = {}
    for m in maps.get("maps") or []:
        mid = m["id"]
        ex = al = {}
        try:
            ex = await _cached(
                srv.id, f"catalog_ex_{mid}",
                lambda mid=mid: srv.api.get(f"/v1/catalog/maps/{mid}/experiences"),
            )
        except RCONError:
            pass
        try:
            al = await _cached(
                srv.id, f"catalog_al_{mid}",
                lambda mid=mid: srv.api.get(f"/v1/catalog/maps/{mid}/alternators"),
            )
        except RCONError:
            pass
        by_map[mid] = {
            "display": m.get("displayName", mid),
            "experiences": list(ex.get("experiences") or []) if isinstance(ex, dict) else [],
            "alternators": [
                {"tag": a.get("tag"), "display": a.get("displayName", a.get("tag"))}
                for a in (al.get("alternators") or [])
                if isinstance(al, dict)
            ],
        }
    return web.json_response(
        {
            "ok": True,
            "maps": by_map,
            "lightings": [l.get("id") for l in lightings.get("lightings") or []],
        }
    )


async def h_config_get(request):
    _require_token(request)
    srv = _srv(request)
    try:
        status, data = await srv.api.request_raw("GET", "/v1/config")
    except RCONError as e:
        return web.json_response({"error": {"code": e.code, "message": e.message}}, status=e.status or 502)
    if status >= 400:
        return web.json_response(data if isinstance(data, dict) else {"error": {"code": "http", "message": str(data)}}, status=status)
    return web.json_response({"ok": True, "revision": data.get("revision"), "writable": data.get("writable"),
                              "text": data.get("text"), "warnings": data.get("warnings") or []})


async def _config_write(srv, method, body_text, revision, params=None):
    extra = {}
    if revision:
        extra["If-Match"] = f'"{revision}"'
    return await srv.api.request_raw(
        method, "/v1/config", raw_body=body_text,
        content_type="text/plain", params=params, extra_headers=extra,
    )


async def h_config_validate(request):
    _require_token(request)
    srv = _srv(request)
    try:
        body = await request.text()
    except Exception:
        body = ""
    if not body.strip():
        return web.json_response({"error": {"code": "missing_field", "message": "пустой конфиг"}}, status=400)
    try:
        status, data = await _config_write(srv, "POST", body, "")
    except RCONError as e:
        return web.json_response({"error": {"code": e.code, "message": e.message}}, status=e.status or 502)
    resp = {"ok": status == 200, "status": status, "data": data}
    if isinstance(data, dict):
        resp.update(data)
    return web.json_response(resp, status=status if status >= 400 else 200)


async def h_config_put(request):
    _require_token(request)
    srv = _srv(request)
    query = dict(request.query)
    params = None
    if query.get("force") == "true":
        params = {"force": "true"}
    if query.get("fullApply") == "true":
        params = dict(params or {})
        params["fullApply"] = "true"
    try:
        body = await request.text()
    except Exception:
        body = ""
    if not body.strip():
        return web.json_response({"error": {"code": "missing_field", "message": "пустой конфиг"}}, status=400)
    revision = (request.headers.get("If-Match") or "").strip().strip('"')
    try:
        status, data = await _config_write(srv, "PUT", body, revision, params=params)
    except RCONError as e:
        return web.json_response({"error": {"code": e.code, "message": e.message}}, status=e.status or 502)
    if status == 200:
        await _invalidate(srv.id)
    return _write_result(status, data)


def _steam_id(request):
    sid = (request.match_info["steam_id"] or "").strip()
    return sid


async def h_broadcast(request):
    _require_token(request)
    srv = _srv(request)
    data = await _json(request, ("message",))
    try:
        await srv.api.request("POST", "/v1/broadcast", json_body=data)
    except RCONError as e:
        return web.json_response({"error": {"code": e.code, "message": e.message}}, status=e.status or 502)
    return web.json_response({"ok": True})


async def h_ban_add(request):
    _require_token(request)
    srv = _srv(request)
    data = await _json(request, ("steamId",))
    payload = {"steamId": str(data["steamId"]).strip()}
    reason = str(data.get("reason") or "").strip()
    if reason:
        payload["reason"] = reason
    try:
        await srv.api.request("POST", "/v1/bans", json_body=payload)
    except RCONError as e:
        return web.json_response({"error": {"code": e.code, "message": e.message}}, status=e.status or 502)
    await _invalidate(srv.id)
    return web.json_response({"ok": True})


async def h_ban_del(request):
    _require_token(request)
    srv = _srv(request)
    sid = _steam_id(request)
    try:
        await srv.api.request("DELETE", f"/v1/bans/{sid}")
    except RCONError as e:
        return web.json_response({"error": {"code": e.code, "message": e.message}}, status=e.status or 502)
    await _invalidate(srv.id)
    return web.json_response({"ok": True})


async def h_kick(request):
    _require_token(request)
    srv = _srv(request)
    sid = _steam_id(request)
    data = await _json(request)
    payload = {}
    reason = str(data.get("reason") or "").strip()
    if reason:
        payload["reason"] = reason
    try:
        await srv.api.request("POST", f"/v1/players/{sid}/kick", json_body=payload)
    except RCONError as e:
        return web.json_response({"error": {"code": e.code, "message": e.message}}, status=e.status or 502)
    await _invalidate(srv.id)
    return web.json_response({"ok": True})


async def h_kill(request):
    _require_token(request)
    srv = _srv(request)
    sid = _steam_id(request)
    try:
        await srv.api.request("POST", f"/v1/players/{sid}/kill")
    except RCONError as e:
        return web.json_response({"error": {"code": e.code, "message": e.message}}, status=e.status or 502)
    await _invalidate(srv.id)
    return web.json_response({"ok": True})


async def h_message(request):
    _require_token(request)
    srv = _srv(request)
    sid = _steam_id(request)
    data = await _json(request, ("message",))
    try:
        await srv.api.request("POST", f"/v1/players/{sid}/message", json_body={"message": data["message"]})
    except RCONError as e:
        return web.json_response({"error": {"code": e.code, "message": e.message}}, status=e.status or 502)
    return web.json_response({"ok": True})


async def h_player_patch(request):
    _require_token(request)
    srv = _srv(request)
    sid = _steam_id(request)
    data = await _json(request, ("faction",))
    try:
        await srv.api.request("PATCH", f"/v1/players/{sid}", json_body={"faction": data["faction"]})
    except RCONError as e:
        return web.json_response({"error": {"code": e.code, "message": e.message}}, status=e.status or 502)
    await _invalidate(srv.id)
    return web.json_response({"ok": True})


async def h_match_map(request):
    _require_token(request)
    srv = _srv(request)
    data = await _json(request, ("map",))
    payload = {"map": str(data["map"]).strip()}
    for key in ("experiences", "lighting", "zoneAlternator"):
        val = data.get(key)
        if isinstance(val, list):
            val = [str(v) for v in val if str(v).strip()]
        elif key == "experiences":
            val = [str(data[key]).strip()] if str(data.get(key) or "").strip() else []
        else:
            val = str(val).strip() if val else ""
        if val:
            payload[key] = val
    try:
        await srv.api.request("POST", "/v1/match/map", json_body=payload)
    except RCONError as e:
        return web.json_response({"error": {"code": e.code, "message": e.message}}, status=e.status or 502)
    await _invalidate(srv.id)
    return web.json_response({"ok": True})


async def h_match_end(request):
    _require_token(request)
    srv = _srv(request)
    try:
        await srv.api.request("POST", "/v1/match/end")
    except RCONError as e:
        return web.json_response({"error": {"code": e.code, "message": e.message}}, status=e.status or 502)
    await _invalidate(srv.id)
    return web.json_response({"ok": True})


async def h_match_restart(request):
    _require_token(request)
    srv = _srv(request)
    try:
        await srv.api.request("POST", "/v1/match/restart")
    except RCONError as e:
        return web.json_response({"error": {"code": e.code, "message": e.message}}, status=e.status or 502)
    await _invalidate(srv.id)
    return web.json_response({"ok": True})


async def h_lighting(request):
    _require_token(request)
    srv = _srv(request)
    data = await _json(request, ("lighting",))
    try:
        await srv.api.request("PUT", "/v1/world/lighting", json_body={"lighting": data["lighting"]})
    except RCONError as e:
        return web.json_response({"error": {"code": e.code, "message": e.message}}, status=e.status or 502)
    await _invalidate(srv.id)
    return web.json_response({"ok": True})


# ---------- фронтенд ----------

async def h_index(request):
    if not INDEX_PATH.exists():
        return web.Response(text="webpanel/index.html отсутствует", content_type="text/plain", status=500)
    html = INDEX_PATH.read_text(encoding="utf-8")
    html = html.replace("__PANEL_LOGIN__", "true" if PASSWORD else "false")
    html = html.replace("__PANEL_TOKEN__", "" if PASSWORD else TOKEN)
    html = html.replace("__PANEL_REFRESH__", str(REFRESH))
    return web.Response(text=html, content_type="text/html")


async def h_static(request):
    name = request.match_info["name"]
    if name not in ("style.css", "app.js"):
        raise web.HTTPNotFound()
    path = BASE_DIR / "webpanel" / name
    if not path.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def start(bot=None):
    app = web.Application(client_max_size=1024 * 1024)
    app["panel_token"] = TOKEN

    app.router.add_get("/", h_index)
    app.router.add_get("/{name}", h_static)
    app.router.add_post("/api/login", h_login)
    app.router.add_get("/api/session", h_session)

    app.router.add_get("/api/server/{sid}/overview", h_overview)
    app.router.add_get("/api/server/{sid}/players", h_players)
    app.router.add_get("/api/server/{sid}/rotation", h_rotation)
    app.router.add_get("/api/server/{sid}/bans", h_bans)
    app.router.add_get("/api/server/{sid}/audit", h_audit)
    app.router.add_get("/api/server/{sid}/health", h_health)
    app.router.add_get("/api/server/{sid}/config", h_config_get)
    app.router.add_get("/api/server/{sid}/catalog", h_catalog)

    app.router.add_post("/api/server/{sid}/config/validate", h_config_validate)
    app.router.add_put("/api/server/{sid}/config", h_config_put)
    app.router.add_post("/api/server/{sid}/broadcast", h_broadcast)
    app.router.add_post("/api/server/{sid}/bans", h_ban_add)
    app.router.add_delete("/api/server/{sid}/bans/{steam_id}", h_ban_del)
    app.router.add_post("/api/server/{sid}/players/{steam_id}/kick", h_kick)
    app.router.add_post("/api/server/{sid}/players/{steam_id}/kill", h_kill)
    app.router.add_post("/api/server/{sid}/players/{steam_id}/message", h_message)
    app.router.add_patch("/api/server/{sid}/players/{steam_id}", h_player_patch)
    app.router.add_post("/api/server/{sid}/match/map", h_match_map)
    app.router.add_post("/api/server/{sid}/match/end", h_match_end)
    app.router.add_post("/api/server/{sid}/match/restart", h_match_restart)
    app.router.add_put("/api/server/{sid}/world/lighting", h_lighting)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    log.info("Панель управления запущена: http://%s:%s/", HOST, PORT)
    print(f"[WEBPANEL] http://{HOST}:{PORT}/", flush=True)
    if not PASSWORD:
        log.warning("PANEL_PASSWORD не задан — вход только по токену %s (см. конфиг panel.password)", TOKEN[:8])
    if bot is not None:
        bot._webpanel_runner = runner
    return runner