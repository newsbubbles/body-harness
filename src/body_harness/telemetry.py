"""Telemetry: counts from a seat to a server its operator runs (body-telemetry), off unless `telemetry` is set.

    "telemetry": {"url": "http://127.0.0.1:8899", "key_env": "BODY_TELEMETRY_KEY", "detail": "counts",
                  "machine": "", "run": ""}

Every event goes to the seat's spool file first (`telemetry.jsonl`) and is posted in batches in the background, so
a server that is down or slow never slows a turn; the spool keeps what was not taken and is posted later.

What leaves the machine is set by `detail`:
- counts (the default): numbers, the names of tools, verbs and models, whether a call was refused and the world's
  refusal code. No speech, notes, reasons or player words.
- lines: adds short cuts (120 characters) of the wake reason, the note and a refusal's reason.
- full: the wake reason, note and refusal reasons in full. Players' words are other people's words: never the
  default.
Never sent at any level: tokens, door files, invites, keys, the brief, the model's messages."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from . import __version__

DETAIL = ("counts", "lines", "full")
SWITCHES = ("menu", "menu_rank", "menu_preopen", "menu_lists", "reflexes", "imagine", "past", "think_tool")
BATCH, SPOOL_MAX, EVERY = 500, 50_000, 20.0
_OK_FALSE = re.compile(r'"ok":\s*false')
_CODE = re.compile(r'"code":\s*"([A-Za-z0-9_.-]{1,40})"')
_REASON = re.compile(r'"(?:reason|refused)":\s*"((?:[^"\\]|\\.){0,400})')


def outcome(out: str) -> tuple[bool, str | None, str | None]:
    """A tool's answer as telemetry reads it: ok or refused, the world's refusal code, and its reason (sent only
    at lines or full)."""
    head = out[:2000]
    if not _OK_FALSE.search(head) and '"refused":' not in head:
        return True, None, None
    code, why = _CODE.search(head), _REASON.search(head)
    return False, code.group(1) if code else None, why.group(1) if why else None


SETUP_MODELS = ("model", "judge", "escalate")
SETUP_NEVER = ("providers", "env_files", "extra_prompt", "extra", "telemetry", "session", "joined", "prices", "crew")
_SECRETISH = re.compile(r"key|token|secret|pass|auth|cookie|door|invite", re.I)
_KIND = re.compile(r"[a-z_]{2,24}")


def safe_setup(cfg: dict[str, Any]) -> dict[str, Any]:
    """The seat's settings that are safe to send: bools, numbers and model names. No prompts, providers, files or
    anything named like a secret (the same rule as body-telemetry's importer)."""
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        if k in SETUP_NEVER or _SECRETISH.search(k) or not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", k):
            continue
        if isinstance(v, bool) or (isinstance(v, (int, float)) and abs(v) < 1e9):
            out[k] = v
        elif k in SETUP_MODELS and isinstance(v, str) and len(v) <= 120:
            out[k] = v
        elif k in SETUP_MODELS and isinstance(v, list) and all(isinstance(x, str) and len(x) <= 120 for x in v):
            out[k] = v[:8]
    return out


def kinds(nudges: list[str]) -> list[str]:
    """Each nudge's kind, the word before its colon: the rest names other characters, so it is never sent."""
    out = []
    for n in nudges:
        head = str(n).split(":", 1)[0].strip()
        out.append(head if _KIND.fullmatch(head) else "other")
    return out


def context(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, int]:
    """What one model call's context is made of, in characters: the system prompt, the tool schemas, the wake, the
    model's own turns and the tool results."""
    out = {"system": 0, "tools": len(json.dumps(tools or [])), "wake": 0, "history": 0, "tool_results": 0}
    for i, m in enumerate(messages):
        c = m.get("content")
        n = len(c) if isinstance(c, str) else len(json.dumps(c or "", default=str))
        role = m.get("role")
        if role == "system":
            out["system"] += n
        elif role == "tool":
            out["tool_results"] += n
        elif role == "assistant":
            out["history"] += n + len(json.dumps(m.get("tool_calls") or [], default=str))
        else:
            out["wake"] += n
    return out


def usage(u: Any) -> dict[str, Any]:
    """A Usage as numbers only."""
    return {k: getattr(u, k, 0) for k in ("calls", "input_tokens", "cached_tokens", "cache_write_tokens",
                                          "output_tokens", "reasoning_tokens", "cost", "seconds")}


def settings_hash(cfg: dict[str, Any]) -> str:
    """The seat's settings, hashed, so runs can be compared by settings without sending them."""
    keep = {k: v for k, v in cfg.items() if k not in ("telemetry", "session", "env_files", "extra_prompt")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()[:12]


class Sender:
    def __init__(self, spool: Path, cfg: dict[str, Any], labels: dict[str, Any] | None = None):
        t = cfg.get("telemetry") if isinstance(cfg.get("telemetry"), dict) else {}
        self.url = str(t.get("url") or "").rstrip("/")
        self.key_env = str(t.get("key_env") or "BODY_TELEMETRY_KEY")
        self.detail = t.get("detail") if t.get("detail") in DETAIL else "counts"
        self.spool = spool
        self.labels: dict[str, Any] = {
            "harness": "body-harness", "version": __version__, "settings": settings_hash(cfg),
            "switches": {k: bool(cfg.get(k)) for k in SWITCHES} | {"escalate": bool(cfg.get("escalate"))},
            "machine": str(t.get("machine") or ""), "run": str(t.get("run") or ""),
            **(labels or {})}
        self._task: asyncio.Task | None = None

    @property
    def on(self) -> bool:
        return bool(self.url)

    def text(self, s: Any) -> str | None:
        """A piece of words at the seat's detail level: none at counts."""
        if self.detail == "counts" or s is None:
            return None
        s = str(s)
        return s if self.detail == "full" else s[:120]

    def emit(self, kind: str, data: dict[str, Any], words: dict[str, Any] | None = None) -> None:
        """One event into the spool. data is counts and names; words go out only at lines or full."""
        if not self.on:
            return
        # uid: a batch posted twice (a server that took it but did not answer) is stored once
        row = {"uid": uuid.uuid4().hex, "ts": time.time(), "kind": kind, **self.labels, **data}
        for k, v in (words or {}).items():
            cut = self.text(v)
            if cut is not None:
                row[k] = cut
        try:
            with self.spool.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except OSError:
            pass  # telemetry never stops a seat

    async def flush(self, client: httpx.AsyncClient) -> int:
        """Post what the spool holds, in batches; what the server did not take stays. Returns events sent."""
        if not self.on or not self.spool.exists():
            return 0
        seen = self.spool.read_text(encoding="utf-8").splitlines()
        lines = [x for x in seen[-SPOOL_MAX:] if x.strip()]  # past the cap the oldest go: a server long down
        sent = 0
        key = os.environ.get(self.key_env, "")
        while sent < len(lines):
            try:
                batch = [json.loads(x) for x in lines[sent:sent + BATCH]]
                r = await client.post(self.url + "/v1/events", json={"events": batch}, timeout=10,
                                      headers={"Authorization": f"Bearer {key}"} if key else {})
            except (httpx.HTTPError, ValueError):
                break
            if r.status_code // 100 == 4 and r.status_code not in (401, 403, 408, 429):
                # a batch the server will never take: post its events one at a time, and drop the ones refused
                for ev in batch:
                    try:
                        one = await client.post(self.url + "/v1/events", json={"events": [ev]}, timeout=10,
                                                headers={"Authorization": f"Bearer {key}"} if key else {})
                    except httpx.HTTPError:
                        break
                    if one.status_code // 100 not in (2, 4) or one.status_code in (401, 403, 408, 429):
                        break
                else:
                    sent += BATCH
                    continue
                break
            if r.status_code // 100 != 2:
                break
            sent += BATCH
        # the spool may have grown while posting: keep what was not taken and what came after the read
        keep = lines[sent:] + self.spool.read_text(encoding="utf-8").splitlines()[len(seen):]
        self.spool.write_text("".join(x + "\n" for x in keep), encoding="utf-8")
        return min(sent, len(lines))

    def start(self, client: httpx.AsyncClient) -> None:
        if self.on and self._task is None:
            self._task = asyncio.create_task(self._loop(client))

    async def _loop(self, client: httpx.AsyncClient) -> None:
        while True:
            await asyncio.sleep(EVERY)
            try:
                await self.flush(client)
            except Exception:
                pass  # a broken server or spool is the operator's to see in the spool, never the seat's problem


OFF = Sender(Path("telemetry.jsonl"), {})  # sends nothing, spools nothing
