"""The door into a hosted embody world: every op is POST <base>/op/<name> with the grant's bearer token.

Plain HTTPS, not an MCP session, so a redeploy of the server costs a short outage instead of a lost seat. The token stays
in this object and in the seat's door file; it never reaches the model, the logs or the sandbox."""

from __future__ import annotations

import asyncio
import json
import random
import re
from pathlib import Path
from typing import Any

import httpx

from .seat import read_text


class DoorError(RuntimeError):
    """The door itself refused or went away (not a refusal inside the world)."""

    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


class DoorAway(DoorError):
    """The server is not answering at all (restarting, redeploying, the network down). Not the call's fault and not
    the model's to fix: the seat stops calling and rides it out (Mind.ride_out), then joins again."""


def backoff(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """Exponential backoff with jitter: base * 2^attempt, capped, then a random point in its upper half, so many
    seats that lost the same server do not come back in step and hammer it as it starts."""
    d = min(cap, base * 2 ** attempt)
    return random.uniform(d / 2, d)


# what counts as the server being away, as opposed to the server answering badly
AWAY_STATUS = {502, 503, 504}


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
        text = read_text(p)
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
        self.principal = ""  # a sub-principal of the grant (one body of a crew), sent as x-embody-principal

    @classmethod
    def load(cls, path: Path, client: httpx.AsyncClient) -> "Door":
        d = json.loads(read_text(path))
        return cls(d["base"], d["token"], client, float(d.get("wait_cap", 90)))

    def save(self, path: Path) -> None:
        path.write_text(json.dumps({"base": self.base, "token": self._token, "wait_cap": self.wait_cap}),
                        encoding="utf-8")

    def sub(self, principal: str) -> "Door":
        """The same grant speaking as one of its sub-principals: a crew mind's grant names several bodies, and the
        world gives each its own seat and cursor under the one token."""
        d = Door(self.base, self._token, self.client, self.wait_cap)
        d.principal = principal
        return d

    def _headers(self) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._token}", "content-type": "application/json", "user-agent": "body-harness"}
        if self.principal:
            h["x-embody-principal"] = self.principal
        return h

    async def op(self, _op: str, /, _http: float = 60.0, _retries: int = 4, **args: Any) -> Any:
        """Call one op. The op is positional-only so an op's own `name` argument passes through (find(name=)).
        A refusal inside the world comes back as {ok: false, reason, hint, alternatives}; only the door failing raises.

        Failures come in tiers:
        - the grant refused (401, 403): DoorError at once; nothing to retry.
        - a bad call (400, 404, 422): the world's own refusal, returned.
        - the server away (no connection, a timeout, 502, 503, 504, 429): a few quick retries with backoff and
          jitter (about 15 s in all; 429 waits as long as Retry-After says), then DoorAway, so a turn stops calling a
          server that is down instead of spending a model call on each failure.
        - the server failing on this call (500 and other 5xx): one quick retry, then a refusal the model can read and
          route around. A 500 is usually this call's shape meeting a server bug, which waiting does not fix (a
          pilot's params sent as a string gave 24 of them, 25 Nov)."""
        args = {k: v for k, v in args.items() if v is not None}
        last, fails = "", 0
        for attempt in range(_retries):
            wait = backoff(attempt, 1.0, 8.0)
            try:
                r = await self.client.post(f"{self.base}/op/{_op}", json=args, headers=self._headers(),
                                           timeout=_http)
            except httpx.HTTPError as e:
                last = type(e).__name__
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
                last = str(r.status_code)
                if r.status_code == 429:
                    try:
                        wait = max(wait, min(60.0, float(r.headers.get("retry-after") or 0)))
                    except ValueError:
                        pass
                elif r.status_code >= 500 and r.status_code not in AWAY_STATUS:
                    fails += 1
                    if fails >= 2:
                        return {"ok": False, "code": "server_error",
                                "reason": f"{_op}: the server failed on this call ({r.status_code})",
                                "hint": "the server's fault, not a refusal: check the arguments' shape (an object, "
                                        "not a string of JSON), try another way, or go on without it"}
            if attempt < _retries - 1:
                await asyncio.sleep(wait)
        raise DoorAway(f"{_op}: the server did not answer ({last})", 0)

    async def alive(self) -> bool:
        """Whether the server itself answers, for riding out an outage. A proxy's 502/503/504 while the app behind it
        restarts is not the server."""
        try:
            r = await self.client.get(f"{self.base}/op", headers=self._headers(), timeout=15)
            return r.status_code not in AWAY_STATUS
        except httpx.HTTPError:
            return False

    async def get(self, path: str) -> Any:
        try:
            r = await self.client.get(f"{self.base}{path}", headers=self._headers(), timeout=30)
        except httpx.HTTPError as e:
            raise DoorAway(f"GET {path}: the server did not answer ({type(e).__name__})") from e
        if r.status_code in AWAY_STATUS:
            raise DoorAway(f"GET {path}: the server did not answer ({r.status_code})", r.status_code)
        if r.status_code != 200:
            raise DoorError(f"GET {path}: {r.status_code}", r.status_code)
        return r.json()

    async def wait(self, until: str | None, timeout: float, **kw: Any) -> Any:
        t = max(1.0, min(timeout, self.wait_cap))
        return await self.op("wait", _http=t + 30, until=until or None, timeout=t, **kw)
