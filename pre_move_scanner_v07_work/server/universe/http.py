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

from .. import __version__


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
    def __init__(self, timeout: float = 20.0, user_agent: str = f"pre-move-scanner/{__version__}"):
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
        return await asyncio.to_thread(self._send_sync, url, params, headers, None)

    async def post_json(self, url: str, body: Any, headers: Optional[Dict[str, str]] = None) -> Any:
        """POST a JSON body (JSON-RPC endpoints, Koios) and decode the JSON answer."""
        if self._httpx is not None:
            r = await self._httpx.post(url, json=body, headers=headers)
            if r.status_code >= 400:
                raise HttpError(r.status_code, r.text, _retry_after(r.headers))
            return r.json()
        return await asyncio.to_thread(self._send_sync, url, None, headers, body)

    def _send_sync(self, url, params, headers, body):
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        hdrs = {**self.headers, **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["content-type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if body is not None else "GET")
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
            raise HttpError(exc.code, text, _retry_after(exc.headers)) from None

    # kept for callers / tests written against v0.7
    def _get_sync(self, url, params, headers):
        return self._send_sync(url, params, headers, None)

    async def close(self) -> None:
        if self._httpx is not None:
            await self._httpx.aclose()
