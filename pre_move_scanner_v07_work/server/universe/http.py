"""Minimal async JSON-over-HTTPS client.

Uses httpx when installed, otherwise the standard library (urllib in a worker
thread), so tools like selftest can run even without third-party packages.
"""
from __future__ import annotations

import asyncio
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional


class HttpError(Exception):
    def __init__(self, status: int, message: str = "", retry_after: Optional[float] = None):
        super().__init__(f"HTTP {status}: {message[:200]}")
        self.status = status
        self.retry_after = retry_after


def _retry_after(headers) -> Optional[float]:
    try:
        v = headers.get("Retry-After") if headers is not None else None
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class HttpClient:
    def __init__(self, timeout: float = 20.0, user_agent: str = "pre-move-scanner/0.7"):
        self.timeout = timeout
        self.headers = {"accept": "application/json", "user-agent": user_agent}
        self._httpx = None
        try:
            import httpx  # type: ignore
            self._httpx = httpx.AsyncClient(timeout=timeout, headers=self.headers, follow_redirects=True)
        except Exception:
            self._httpx = None

    async def get_json(self, url: str, params: Optional[Dict[str, Any]] = None,
                       headers: Optional[Dict[str, str]] = None) -> Any:
        if self._httpx is not None:
            r = await self._httpx.get(url, params=params, headers=headers)
            if r.status_code >= 400:
                raise HttpError(r.status_code, r.text, _retry_after(r.headers))
            return r.json()
        return await asyncio.to_thread(self._get_sync, url, params, headers)

    def _get_sync(self, url, params, headers):
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={**self.headers, **(headers or {})})
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
            raise HttpError(exc.code, body, _retry_after(exc.headers)) from None

    async def close(self) -> None:
        if self._httpx is not None:
            await self._httpx.aclose()
