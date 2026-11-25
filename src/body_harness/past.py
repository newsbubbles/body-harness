"""past: search what the seat really did and what came of it, from its own call log (calls.jsonl).

Remembering is not enough to learn someone: a seat has to line moments up. "Every time I struck twice running, what
did Oren do next?" is a question about many moments at once, and nothing else answers it: memo search reads notes,
the world's history reads events, relive replays one moment at a time. past() reads every deed the seat did, with
its result, and answers with the matching moments side by side, what came before and after each, and a tally.
Deterministic, bounded, and it costs no model call.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .seat import read_jsonl

DEEDS = ("act", "say")
LIMIT, MAX_LIMIT, MAX_CONTEXT = 12, 40, 5
TAIL = 2_000_000


def calls(p: Path, tail: int | None = TAIL) -> list[dict[str, Any]]:
    """The call log, or its last `tail` bytes."""
    if tail is None or not p.exists() or p.stat().st_size <= tail:
        return read_jsonl(p)
    with open(p, "rb") as f:
        f.seek(-tail, 2)
        lines = f.read().decode("utf-8", "replace").splitlines()[1:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def _answer(out: str) -> dict[str, Any]:
    """The world's JSON answer inside a logged out, after any (harness: ...) lines."""
    i = out.find("{")
    if i < 0:
        return {}
    try:
        r = json.loads(out[i:])
    except ValueError:
        return {}
    return r if isinstance(r, dict) else {}


def deeds(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every act and say the seat did, oldest first, as flat moments: n, turn, verb, target, ok, what came of it."""
    out = []
    for c in rows:
        tool = c.get("tool")
        if tool not in DEEDS:
            continue
        a = c.get("args") or {}
        r = _answer(str(c.get("out") or ""))
        m: dict[str, Any] = {"n": len(out) + 1, "turn": c.get("turn"), "ts": c.get("ts"),
                             "verb": str(a.get("verb") or "") if tool == "act" else "say",
                             "target": str(a.get("target") or a.get("to") or ""), "ok": r.get("ok", True) is not False}
        if tool == "say":
            m["text"] = str(a.get("text") or "")[:200]
        for k, v in (a.get("params") or {}).items():
            m["param." + str(k)] = v
        res = r.get("result")
        if isinstance(res, dict):
            for k, v in res.items():
                if isinstance(v, (str, int, float, bool)) or v is None:
                    m[str(k)] = v
                elif isinstance(v, dict):
                    for k2, v2 in v.items():
                        if isinstance(v2, (str, int, float, bool)):
                            m[f"{k}.{k2}"] = v2
        elif res is not None:
            m["result"] = str(res)[:160]
        if not m["ok"]:
            m["refused"] = str(r.get("reason") or "")[:160]
        happened = [str(x) for x in (r.get("what happened") or []) if "(your card changed" not in str(x)]
        if happened:
            m["happened"] = " | ".join(h.split("] ", 1)[-1] for h in happened)[:300]
        out.append(m)
    return out


SHOWN = ("n", "turn", "verb", "target", "text")


def line(m: dict[str, Any]) -> str:
    head = f"#{m['n']} t{m.get('turn')} {m['verb']}" + (f" {m['target']}" if m.get("target") else "")
    if m.get("text"):
        head += f" \"{m['text'][:80]}\""
    rest = [f"{k}={v}" for k, v in m.items() if k not in SHOWN and k not in ("ts", "ok", "happened", "refused",
                                                                            "seq", "from")]
    if not m["ok"]:
        rest.append(f"REFUSED: {m.get('refused', '')}")
    return head + (" -> " + " ".join(rest) if rest else "")


def _cmp(have: Any, want: Any) -> bool:
    if isinstance(want, str):
        w = want.strip()
        mm = re.fullmatch(r"(>=|<=|!=|>|<|=)\s*(-?\d+(?:\.\d+)?)", w)
        if mm and isinstance(have, (int, float)) and not isinstance(have, bool):
            op, x = mm.group(1), float(mm.group(2))
            return {">": have > x, "<": have < x, ">=": have >= x, "<=": have <= x, "=": have == x,
                    "!=": have != x}[op]
        if w.startswith("!"):
            return str(have).lower() != w[1:].lower()
        return str(have).lower() == w.lower() or (isinstance(have, str) and "|" in w and
                                                   have.lower() in w.lower().split("|"))
    return have == want


class PastError(ValueError):
    pass


def search(moments: list[dict[str, Any]], verb: str = "", where: dict[str, Any] | None = None, match: str = "",
           seq: list[str] | None = None, before: int = 0, after: int = 0, count: str | list[str] | None = None,
           limit: int = LIMIT, last: int = 0) -> str:
    if not moments:
        return ("You have done nothing yet that past() can read: it reads your own deeds (act, say) and what came "
                "of each. Act first.")
    pool = moments[-last:] if last and last > 0 else moments
    fields = sorted({k for m in pool for k in m if k not in ("ts", "seq")})
    where = dict(where or {})
    for k in where:
        if k not in fields:
            raise PastError(f"no field {k!r} in your deeds; fields: {', '.join(fields)}")
    try:
        rx = re.compile(match, re.I) if match else None
    except re.error as e:
        raise PastError(f"match is a regex and this one does not compile ({e}); escape ( ) [ ] . * + ? with \\, "
                        f"or use where= for exact fields") from None
    verbs = {v.strip().lower() for v in verb.split("|") if v.strip()} if verb else set()
    seq = [str(s).strip().lower() for s in (seq or []) if str(s).strip()]
    # a run of verbs: the moment that completes it is the match, and after= shows what came next
    hits = []
    for i, m in enumerate(pool):
        if verbs and m["verb"].lower() not in verbs:
            continue
        if seq:
            if i + 1 < len(seq):
                continue
            run = [pool[j]["verb"].lower() for j in range(i + 1 - len(seq), i + 1)]
            if run != seq:
                continue
        if any(not _cmp(m.get(k), v) for k, v in where.items()):
            continue
        if rx and not rx.search(line(m) + " " + m.get("happened", "") + " " + m.get("refused", "")):
            continue
        hits.append(i)
    before, after = max(0, min(int(before or 0), MAX_CONTEXT)), max(0, min(int(after or 0), MAX_CONTEXT))
    limit = max(1, min(int(limit or LIMIT), MAX_LIMIT))
    what = []
    if verb:
        what.append(f"verb {verb}")
    if seq:
        what.append("run " + " > ".join(seq))
    if where:
        what.append("where " + ", ".join(f"{k}={v}" for k, v in where.items()))
    if match:
        what.append(f"/{match}/")
    span = f"#{pool[0]['n']}-#{pool[-1]['n']}"
    head = f"{len(hits)} of your {len(pool)} deeds ({span}) match" + (f" {'; '.join(what)}" if what else "")
    if not hits:
        return head + f". Fields you can use in where= and count=: {', '.join(fields)}."
    out = [head + "."]
    # a tally over every hit, not only the shown ones: of a field at the hit, or at the deed after= steps later
    for f in ([count] if isinstance(count, str) else list(count or [])):
        if not f:
            continue
        name, shift = f, 0
        mm = re.fullmatch(r"next\.(.+)", f)
        if mm:
            name, shift = mm.group(1), 1
        if name not in fields:
            raise PastError(f"no field {name!r} to count; fields: {', '.join(fields)} (next.<field> counts the deed "
                            f"after each match)")
        c = Counter("-" if pool[i + shift].get(name) is None else str(pool[i + shift].get(name))
                    for i in hits if i + shift < len(pool))
        n = sum(c.values())
        out.append(f"count {f}: " + ", ".join(f"{k} {v}/{n}" for k, v in c.most_common()))
    shown = hits[-limit:]
    if len(hits) > len(shown):
        out.append(f"(the last {len(shown)} shown; limit= up to {MAX_LIMIT}, last= to look at only recent deeds)")
    for i in shown:
        if before or after:
            out.append("--")
        for j in range(max(0, i - before), i):
            out.append("   " + line(pool[j]))
        out.append(">> " + line(pool[i]) if (before or after) else line(pool[i]))
        for j in range(i + 1, min(len(pool), i + 1 + after)):
            out.append("   " + line(pool[j]))
    return "\n".join(out)


WORLD_FILE = "past_world.json"
PAGE, MAX_PAGES = 500, 200


def world_deeds(events: list[dict[str, Any]], me: str, names: dict[str, str]) -> list[dict[str, Any]]:
    """The body's own deeds from the world's history, in the shape deeds() gives, with what each caused: what a body
    did before this harness ran it. A body that lived weeks in a world before it got a seat would otherwise start with
    an empty past."""
    out, by_seq = [], {}
    for e in sorted(events, key=lambda e: e.get("seq") or 0):
        kind, p = e.get("kind"), e.get("payload") or {}
        if e.get("actor") == me and kind in ("act", "say"):
            t = float(e.get("t") or 0)
            tgt = str(p.get("target") or e.get("target") or "")
            m: dict[str, Any] = {"n": 0, "turn": f"d{int(t // 86400) + 1}", "seq": e.get("seq"),
                                 "verb": str(p.get("verb") or "") if kind == "act" else "say",
                                 "target": names.get(tgt, tgt), "ok": True, "from": "world"}
            if kind == "say":
                m["text"] = str(p.get("text") or "")[:200]
            for k, v in (p.get("params") or {}).items():
                if isinstance(v, (str, int, float, bool)):
                    m["param." + str(k)] = v
            out.append(m)
            by_seq[e.get("seq")] = m
        elif e.get("caused_by") in by_seq and kind not in ("act", "say"):
            m = by_seq[e["caused_by"]]
            bits = [f"{k}={v}" for k, v in p.items() if isinstance(v, (str, int, float, bool)) and k != "target"]
            h = f"{kind}" + (f" ({', '.join(bits)[:120]})" if bits else "")
            m["happened"] = (m.get("happened", "") + " | " + h).strip(" |")[:300]
            m.setdefault("outcome", kind)
    return out


async def import_world(door: Any, me: str, names: dict[str, str], seat_dir: Path) -> str:
    """Page through history(touching=me) and keep the body's own deeds in past_world.json. Run again, it starts over:
    the world's log is the source, the file a copy. Deeds the seat logs after the import come from calls.jsonl."""
    events, since, pages = [], 0, 0
    while pages < MAX_PAGES:
        r = await door.op("history", since=since, touching=me, limit=PAGE)
        if isinstance(r, dict):
            if r.get("ok") is False:
                return f"the world refused history: {r.get('reason')}"
            r = r.get("events") or r.get("result") or []
        if not isinstance(r, list) or not r:
            break
        events += [e for e in r if isinstance(e, dict)]
        top = max(int(e.get("seq") or 0) for e in r if isinstance(e, dict))
        if top <= since:
            break
        since, pages = top, pages + 1  # a short page is not the end: the engine's own kinds are skipped in it
    moments = world_deeds(events, me, names)
    (seat_dir / WORLD_FILE).write_text(json.dumps({"at": time.time(), "until": since, "events": len(events),
                                                   "moments": moments}), encoding="utf-8")
    more = " (stopped at the page limit: older deeds are in)" if pages >= MAX_PAGES else ""
    return f"{len(moments)} deeds from {len(events)} events in the world's history, up to seq {since}{more}"


def moments(p: Path) -> list[dict[str, Any]]:
    """Every deed past() searches: the world's copy of what the body did before (past_world.json), then the seat's own
    log of what it did after that copy was made."""
    w = p.with_name(WORLD_FILE)
    old, at = [], 0.0
    if w.exists():
        try:
            d = json.loads(w.read_text(encoding="utf-8"))
            old, at = list(d.get("moments") or []), float(d.get("at") or 0)
        except (OSError, ValueError):
            pass
    new = deeds(calls(p))
    # where the seat's own log begins inside the world's copy: its first line said, found word for word there, less
    # the deeds the world logged before it (refusals are never in the world's log)
    cut, k = None, 0
    for m in new:
        if m["verb"] == "say" and m.get("text"):
            i = next((i for i, w in enumerate(old) if w.get("verb") == "say" and w.get("text") == m["text"]), None)
            if i is not None:
                cut = max(0, i - k)
            break
        k += 1 if m["ok"] else 0
    if cut is not None:
        old = old[:cut]
    elif old:
        new = [m for m in new if (m.get("ts") or 0) > at]
    out = old + new
    for i, m in enumerate(out, 1):
        m["n"] = i
    return out


def past(p: Path, args: dict[str, Any]) -> str:
    try:
        return search(moments(p), verb=str(args.get("verb") or ""), where=args.get("where") or None,
                      match=str(args.get("match") or ""), seq=args.get("seq") or None,
                      before=args.get("before") or 0, after=args.get("after") or 0, count=args.get("count"),
                      limit=args.get("limit") or LIMIT, last=args.get("last") or 0)
    except PastError as e:
        return json.dumps({"ok": False, "reason": str(e)})
    except (TypeError, ValueError) as e:
        return json.dumps({"ok": False, "reason": f"bad arguments for past: {e}; where= is an object like "
                                                  f"{{'they': 'guard', 'took': '>0'}}, seq= a list of verbs"})
