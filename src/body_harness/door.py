"""The door into a hosted embody world: every op is POST <base>/op/<name> with the grant's bearer token.

Plain HTTPS, not an MCP session, so a redeploy of the server costs one retry instead of a lost seat. The token stays
in this object and in the seat's door file; it never reaches the model, the logs or the sandbox."""

from __future__ import annotations

import asyncio
import json
import random
import re
from pathlib import Path
from typing import Any

import httpx


class DoorError(RuntimeError):
    """The door itself refused or went away (not a refusal inside the world)."""

    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


_BEARER = re.compile(r"Bearer ([A-Za-z0-9_\-.~+/=]+)")
_URL = re.compile(r"(https?://[^\s\"'<>]+?)/mcp\b")


def parse_invite(text: str) -> tuple[str, str]:
    """Pull (base, token) out of an invite page or paste. base is the server address without /mcp."""
    tok = _BEARER.search(text)
    url = _URL.search(text)
    if not tok or not url:
        raise DoorError("no server address and bearer token in that invite: pass the link, the page or a file with it")
    return url.group(1), tok.group(1)


async def open_invite(client: httpx.AsyncClient, link_or_text: str) -> tuple[str, str, str]:
    """Follow an invite link (it opens once) or read a pasted page. Returns (base, token, page with the token cut)."""
    text = link_or_text
    p = Path(link_or_text)
    if len(link_or_text) < 400 and p.exists():
        text = p.read_text(encoding="utf-8")
    m = re.search(r"https?://\S+/i/[A-Za-z0-9_\-]+", text)
    if m and "Bearer" not in text:
        r = await client.get(m.group(0), headers={"accept": "text/plain", "user-agent": "body-harness"}, timeout=30)
        if r.status_code != 200:
            raise DoorError(f"the invite link answered {r.status_code}: links open once and last a day; ask for a "
                            f"new one", r.status_code)
        text = r.text
    base, token = parse_invite(text)
    return base, token, text.replace(token, "<token>")


class Door:
    def __init__(self, base: str, token: str, client: httpx.AsyncClient, wait_cap: float = 90.0):
        self.base = base.rstrip("/")
        self._token = token
        self.client = client
        self.wait_cap = wait_cap  # the server holds a wait at most 120 s, proxies often less

    @classmethod
    def load(cls, path: Path, client: httpx.AsyncClient) -> "Door":
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(d["base"], d["token"], client, float(d.get("wait_cap", 90)))

    def save(self, path: Path) -> None:
        path.write_text(json.dumps({"base": self.base, "token": self._token, "wait_cap": self.wait_cap}),
                        encoding="utf-8")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "content-type": "application/json",
                "user-agent": "body-harness"}

    async def op(self, _op: str, /, _http: float = 60.0, _retries: int = 6, **args: Any) -> Any:
        """Call one op. The op is positional-only so an op's own `name` argument passes through (find(name=)).
        A refusal inside the world comes back as {ok: false, reason, hint, alternatives}; only the door failing raises. Network trouble and 5xx retry with backoff and jitter, so many seats never hammer a server
        that is coming back up."""
        args = {k: v for k, v in args.items() if v is not None}
        delay = 2.0
        last = ""
        for _ in range(_retries):
            try:
                r = await self.client.post(f"{self.base}/op/{_op}", json=args, headers=self._headers(),
                                           timeout=_http)
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}"
            else:
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (401, 403):
                    raise DoorError(f"the door refused this grant ({r.status_code}): revoked, expired or relinked",
                                    r.status_code)
                if r.status_code in (400, 404, 422):
                    try:
                        return r.json()
                    except ValueError:
                        return {"ok": False, "reason": f"{_op}: {r.status_code} {r.text[:300]}"}
                last = f"{r.status_code}"
            await asyncio.sleep(delay + random.uniform(0, delay))
            delay = min(delay * 2, 60)
        raise DoorError(f"{_op}: the server did not answer ({last})", 0)

    async def get(self, path: str) -> Any:
        r = await self.client.get(f"{self.base}{path}", headers=self._headers(), timeout=30)
        if r.status_code != 200:
            raise DoorError(f"GET {path}: {r.status_code}", r.status_code)
        return r.json()

    async def wait(self, until: str | None, timeout: float, **kw: Any) -> Any:
        t = max(1.0, min(timeout, self.wait_cap))
        return await self.op("wait", _http=t + 30, until=until or None, timeout=t, **kw)
