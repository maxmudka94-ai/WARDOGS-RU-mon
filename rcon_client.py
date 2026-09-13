import asyncio
import json
import logging

import aiohttp

log = logging.getLogger("rcon")


class RCONError(Exception):
    def __init__(self, status, code, message):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"[{status}] {code}: {message}")


class WardogsRCON:
    """Клиент HTTP RCON-API WARDOGS (Bearer-токен = пароль RCON).

    Документация: https://wardogs.tech/rcon-reference
    Роуты: /v1/status, /v1/players, /v1/capabilities, /v1/health и др.
    """

    def __init__(self, cfg):
        self.scheme = cfg.get("scheme", "http")
        self.host = cfg.get("host", "127.0.0.1")
        self.port = cfg.get("port", 7779)
        self.token = cfg.get("token", "")
        self.timeout = aiohttp.ClientTimeout(total=cfg.get("timeout", 30))
        self._session = None

    @property
    def base_url(self):
        return f"{self.scheme}://{self.host}:{self.port}"

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def get(self, api_path, params=None):
        url = f"{self.base_url}{api_path}"
        session = await self._get_session()
        status = 0
        try:
            async with session.get(url, headers=self._headers(), params=params) as resp:
                status = resp.status
                text = await resp.text()
        except asyncio.TimeoutError as e:
            raise RCONError(0, "timeout", f"сервер не ответил за {self.timeout.total}s") from e
        except (aiohttp.ClientError, OSError) as e:
            raise RCONError(0, "network", str(e)) from e
        try:
            data = json.loads(text) if text.strip() else {}
        except Exception:
            data = None
        if status >= 400:
            err = (data or {}).get("error") or {}
            raise RCONError(
                status,
                err.get("code", "http_error"),
                err.get("message", f"HTTP {status}"),
            )
        return data or {}

    async def request(self, method, api_path, json_body=None, raw_body=None,
                      content_type="application/json", params=None, extra_headers=None):
        """Generic RCON-запрос (для панели управления)."""
        url = f"{self.base_url}{api_path}"
        session = await self._get_session()
        headers = self._headers()
        if extra_headers:
            headers.update(extra_headers)
        data = None
        if raw_body is not None:
            data = raw_body
            headers["Content-Type"] = content_type
        status = 0
        try:
            async with session.request(method, url, headers=headers, json=json_body,
                                       data=data, params=params) as resp:
                status = resp.status
                text = await resp.text()
        except asyncio.TimeoutError as e:
            raise RCONError(0, "timeout", f"сервер не ответил за {self.timeout.total}s") from e
        except (aiohttp.ClientError, OSError) as e:
            raise RCONError(0, "network", str(e)) from e
        try:
            data = json.loads(text) if text.strip() else {}
        except Exception:
            data = text
        if status >= 400:
            err = (data or {}).get("error") or {} if isinstance(data, dict) else {}
            raise RCONError(
                status,
                err.get("code", "http_error"),
                err.get("message", f"HTTP {status}"),
            )
        return data

    async def status(self):
        return await self.get("/v1/status")

    async def players(self):
        return await self.get("/v1/players")

    async def capabilities(self):
        return await self.get("/v1/capabilities")

    async def health(self):
        return await self.get("/v1/health")

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None