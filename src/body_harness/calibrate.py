"""bh calibrate: the same wake, through several models, scored side by side.

One wake the seat really had (kept in cache/wakes.json) is replayed for each model on a copy of the seat under
BH_HOME/calibrate/. Nothing reaches the world: perceive and join answer with the bundle the seat saw, reads that
leave no trace (handbook, guide, inspect, find, recall, history) go to the world, and every deed is answered
a plain ok. The copy's own ledger, calls and usage then say how each model played the same moment: tool names it
made up, arguments it got wrong, nudges the harness had to give, whether it answered who spoke to it, whether it
slept on its own, what it cost. The same seat, the same harness, the same world: what differs is the model.
"""

from __future__ import annotations

import asyncio
import copy
import re
import shutil
import time
from pathlib import Path
from typing import Any

import httpx

from .door import Door
from .seat import Seat, read_json, read_jsonl, write_json

PASS = {"handbook", "guide", "inspect", "find", "recall", "history", "timers"}


class DryDoor:
    """The seat's door with the world's writes cut off. Holds the real door for reads; never hands out its token."""

    def __init__(self, real: Door, bundle: dict[str, Any]):
        self._real = real
        self.bundle = bundle
        self.wait_cap = real.wait_cap
        self.sent: list[str] = []

    def _verbs(self) -> set[str]:
        b = self.bundle
        out = {str(a.get("verb")) for a in (b.get("affordances") or []) if isinstance(a, dict) and a.get("verb")}
        return out | {str(v).split(":")[0].strip() for v in (b.get("verbs") or [])}

    async def op(self, _op: str, /, _http: float = 60.0, _retries: int = 6, **args: Any) -> Any:
        if _op in ("perceive", "join"):
            return copy.deepcopy(self.bundle)
        if _op in PASS:
            self.sent.append(_op)
            return await self._real.op(_op, _http=_http, _retries=2, **args)
        if _op == "wait":
            return {"ok": True}
        verbs = self._verbs()
        if _op == "act" and verbs and args.get("verb") not in verbs:
            return {"ok": False, "reason": f"no verb {args.get('verb')!r} here",
                    "alternatives": sorted(verbs)[:20]}
        # a plain ok, as the world gives: an answer that says "not sent" changed how the models played (they spent
        # their steps on it), and the replay is only worth having if the moment reads the same as the real one
        return {"ok": True}

    async def get(self, path: str) -> Any:
        return await self._real.get(path)

    async def wait(self, until: str | None, timeout: float, **kw: Any) -> Any:
        return {"ok": True}


def wakes(seat: Seat) -> list[dict[str, Any]]:
    ring = read_json(seat.p("cache", "wakes.json"), [])
    return ring if isinstance(ring, list) else []


def model_sets(path: Path) -> dict[str, list[str]]:
    """{set name: [provider:model, ...]} from a .toml or .json file: either {"sets": {...}} or the sets at the top."""
    if path.suffix == ".toml":
        import tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    else:
        data = read_json(path, {})
    data = data.get("sets", data) if isinstance(data, dict) else {}
    return {str(k): [str(m) for m in v] for k, v in data.items() if isinstance(v, list)}


def _copy_seat(src: Seat, root: Path, model: str, wake: dict[str, Any],
               cfg: dict[str, Any] | None = None) -> Seat:
    """A seat under root with src's notes and settings, the wake's state, the model given and no token."""
    root.mkdir(parents=True, exist_ok=True)
    for name in ("config.json",):
        if (src.home / name).exists():
            shutil.copy2(src.home / name, root / name)
    for d in ("craft", "skills"):
        if (src.home / d).is_dir():
            shutil.copytree(src.home / d, root / d, dirs_exist_ok=True)
    s = Seat(root, src.name).create()
    for d in ("memory", "skills", "apps"):
        if src.p(d).is_dir():
            shutil.copytree(src.p(d), s.p(d), dirs_exist_ok=True)
    for f in ("inbox.jsonl", "schedule.jsonl"):
        if src.p(f).exists():
            shutil.copy2(src.p(f), s.p(f))
    door = read_json(src.p("door.json"), {})
    write_json(s.p("door.json"), {"base": door.get("base", ""), "token": "", "wait_cap": door.get("wait_cap", 90)})
    write_json(s.p("config.json"), {**read_json(src.p("config.json"), {}), **(cfg or {}), "model": model})
    write_json(s.p("state.json"), wake.get("state") or {})
    return s


def score(s: Seat, wake: dict[str, Any], seconds: float) -> dict[str, Any]:
    """What the copy's own logs say about the one turn it played."""
    led = (read_jsonl(s.p("ledger.jsonl")) or [{}])[-1]
    calls = read_jsonl(s.p("calls.jsonl"))
    outs = [str(c.get("out", "")) for c in calls]
    me = (wake.get("state") or {}).get("me") or ((wake.get("bundle") or {}).get("you") or {}).get("id")
    owed = bool((wake.get("bundle") or {}).get("owed")) or any(
        e.get("kind") in ("say", "talked") and e.get("actor") != me and
        me in ((e.get("payload") or {}).get("to"), e.get("target")) for e in wake.get("events") or [])
    note = str(led.get("note") or "")
    u = led.get("usage") or {}
    says = [str((c.get("args") or {}).get("text", "")) for c in calls if c.get("tool") == "say" or
            (c.get("tool") == "act" and (c.get("args") or {}).get("verb") in ("talk", "ask", "tell"))]
    return {
        "calls": len(calls),
        "tools": [c.get("tool") for c in calls],
        "invented": len(read_jsonl(s.p("invented.jsonl"))),
        "bad_args": sum("bad arguments" in o for o in outs),
        "refused": sum(bool(re.search(r'"ok":\s*false', o)) for o in outs),
        "nudged": sum(("Not asleep yet" in o) or ("(harness:" in o) for o in outs),
        "slept": not note.startswith(("(the turn ran out", "(the model failed", "(no note")),
        "owed": owed,
        "answered": bool(says) if owed else None,
        "said": says[:3],
        "note": note[:300],
        "watch": led.get("watch"),
        "escalated": led.get("escalated") or [],
        "model_calls": int(u.get("calls", 0)),
        "in": int(u.get("input_tokens", 0)), "cached": int(u.get("cached_tokens", 0)),
        "out": int(u.get("output_tokens", 0)), "cost": float(u.get("cost", 0) or 0),
        "seconds": round(seconds, 1),
    }


async def _one(src: Seat, model: str, wake: dict[str, Any], root: Path, client: httpx.AsyncClient,
               gate: asyncio.Semaphore, cfg: dict[str, Any] | None = None, label: str = "") -> dict[str, Any]:
    from .loop import Mind
    async with gate:
        s = _copy_seat(src, root, model, wake, cfg)
        row: dict[str, Any] = {"model": model, "dir": str(s.dir), "variant": label}
        t0 = time.time()
        try:
            m = Mind(s, client)
            m.door = DryDoor(Door.load(src.p("door.json"), client), wake.get("bundle") or {})  # type: ignore
            await m.start()
            await m.turn(str(wake.get("reason") or "time is up"), wake.get("events") or [])
            row.update(score(s, wake, time.time() - t0))
        except Exception as e:  # one model failing is a result, not a stop
            row.update({"error": f"{type(e).__name__}: {str(e)[:300]}", "seconds": round(time.time() - t0, 1)})
        return row


async def calibrate(src: Seat, models: list[str], wake_index: int = -1, runs: int = 1, parallel: int = 4,
                    variants: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """variants: {label: config overrides}, each run for every model: the model held still, the harness changed."""
    ring = wakes(src)
    if not ring:
        raise ValueError(f"{src.name} has no saved wakes yet: it keeps the last few as it plays (keep_wakes)")
    wake = ring[wake_index]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    gate = asyncio.Semaphore(max(1, parallel))
    async with httpx.AsyncClient() as client:
        combos = [(m, label, cfg) for m in models for label, cfg in (variants or {"": {}}).items()
                  for _ in range(runs)]
        jobs = [_one(src, m, wake, src.home / "calibrate" / f"{src.name}-{stamp}" / f"{i:02d}", client, gate, cfg,
                     label) for i, (m, label, cfg) in enumerate(combos)]
        rows = await asyncio.gather(*jobs)
    for r in rows:
        r["wake"] = {"turn": wake.get("turn"), "reason": str(wake.get("reason") or "")[:120]}
    return list(rows)


def table(rows: list[dict[str, Any]]) -> str:
    lines = [f"{'model':44} calls inv bad ref nudge slept answered   cost     s",]
    for r in rows:
        if r.get("error"):
            lines.append(f"{r['model'][-44:]:44} error: {r['error']}")
            continue
        ans = "-" if r["answered"] is None else ("yes" if r["answered"] else "NO")
        name = f"{r['model']} [{r['variant']}]" if r.get("variant") else r["model"]
        lines.append(f"{name[-44:]:44} {r['calls']:5} {r['invented']:3} {r['bad_args']:3} {r['refused']:3} "
                     f"{r['nudged']:5} {'yes' if r['slept'] else 'NO':5} {ans:8} ${r['cost']:.4f} {r['seconds']:5}")
        if r.get("escalated"):
            lines.append(f"{'':44}   escalated to: {', '.join(r['escalated'])}")
        if r["said"]:
            lines.append(f"{'':44}   said: {r['said'][0][:110]!r}")
        lines.append(f"{'':44}   note: {r['note'][:110]}")
    return "\n".join(lines)
