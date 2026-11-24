"""Imagination: a small embody world each seat owns, to try a plan before doing it, and to learn from the difference.

The seat writes the rules as it believes them (embody scripts: check and apply, dice from ctx.rand) and what it
believes of the people around it (hp, ac, gold...). Its own body comes from its real card every time. imagine(plan)
builds that world fresh for each trial with a new seed and plays the plan through, so the answer is a spread of
outcomes, not one story. Nothing reaches the real world: the engine runs in this process, from the embody-engine
package (pip extra `imagine`), and its scripts are interpreted by the engine itself (no eval, a step budget).

The part that matters is after. When the seat really does the first step of a plan it imagined, the harness binds
that imagining as a prediction. On the next wake it reads what really came of it from the seat's own card and
scores it: how far inside the imagined spread the real value fell, as surprise in bits (-log2 of how often the
imaginings were at least that far out). The harness computes it, not the model. A big surprise goes on the wake
with the rules and beliefs the imagining used, and the seat decides what was wrong (a rule, a belief about someone,
or luck) with reflect(). Lessons ride on every later imagining, and a calibration line per kind of plan says where
the seat's imagination can be trusted.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import textwrap
import time
from collections import Counter
from typing import Any

from .seat import Seat, append_jsonl, read_json, read_jsonl, write_json

try:
    from embody.script import ScriptError
    from embody.store import Store
    from embody.world import Rejected, World
except ImportError:  # the extra is not installed: the tools are not offered
    World = None  # type: ignore


def available() -> bool:
    return World is not None


STARTER = {
    "attack": ("def check(ctx):\n"
               "    if ctx.actor.state.hp <= 0:\n"
               "        return \"you are down\"\n"
               "    if ctx.target.state.hp <= 0:\n"
               "        return \"they are down\"\n"
               "    return True\n\n"
               "def apply(ctx):\n"
               "    if ctx.rand.roll(\"1d20\") + ctx.actor.state.atk >= ctx.target.state.ac:\n"
               "        ctx.target.state.hp = ctx.target.state.hp - ctx.rand.roll(\"1d6\")\n"),
}
MIN_TRIALS, MAX_TRIALS, MAX_ROUNDS, BUDGET_S = 20, 200, 30, 8.0
FIGHT = ("attack", "spar", "fight", "strike", "hit", "duel", "brawl")
OPEN_FOR, STALE_AFTER = 1800.0, 6 * 3600.0


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "thing"


def numbers(e: dict[str, Any]) -> dict[str, Any]:
    """The numbers one entry of the card shows: 'hp': '12/12' in the brief view, state.hp and the rest of its
    numeric state in the full one."""
    out: dict[str, Any] = {}
    for src in (e.get("state") or {}, e):
        for k, v in src.items():
            hp = re.match(r"\s*(-?\d+)\s*/\s*(\d+)\s*$", v) if k == "hp" and isinstance(v, str) else None
            if hp:
                out["hp"], out["max_hp"] = int(hp.group(1)), int(hp.group(2))
            elif _num(v) and (src is not e or k in ("hp", "max_hp")):
                out[_key(k)] = v
    return out


def body_state(bundle: dict[str, Any]) -> dict[str, Any]:
    """What the seat's real card says of its own body, as imagination state: its numbers and a count per held thing."""
    out = numbers(bundle.get("you") or {})
    for item in bundle.get("holding") or []:
        text = item.get("name", "") if isinstance(item, dict) else str(item)
        m = re.match(r"(.+?)\s+x(\d+)$", text.strip())
        name, n = (m.group(1), int(m.group(2))) if m else (text, 1)
        if name:
            out[_key(name)] = out.get(_key(name), 0) + n
    return out


def others(bundle: dict[str, Any], me: str) -> dict[str, dict[str, Any]]:
    """{name: the numbers the card shows of them} for everyone here but the seat."""
    out: dict[str, dict[str, Any]] = {}
    for e in (bundle.get("present") or []) + (bundle.get("people") or []):
        if isinstance(e, dict) and e.get("name") and e.get("name") != me:
            out[str(e["name"])] = numbers(e)
    return out


def observed(bundle: dict[str, Any], me: str) -> dict[str, Any]:
    """Every number the card shows, keyed as the imagination keys them: '<name>.<field>'."""
    out = {f"{me}.{k}": v for k, v in body_state(bundle).items()}
    for name, nums in others(bundle, me).items():
        out.update({f"{name}.{k}": v for k, v in nums.items()})
    return out


def _body(text: str, returns: bool = False) -> str:
    lines = textwrap.dedent(str(text)).strip().splitlines() or ["pass"]
    # "if a: x; if b: y" on one line is not Python, and it is how Tamsin wrote a blow and a counter: each compound
    # statement after a semicolon starts its own line
    lines = [part for ln in lines for part in re.split(r";\s*(?=(?:if|for|while|return)\b)", ln)]
    if returns and len(lines) == 1 and not re.match(r"\s*(return|if|for|while)\b", lines[0]) and \
            "=" not in re.sub(r"[=!<>]=", "", lines[0]):
        lines[0] = "return " + lines[0]  # a check given as one condition
    return textwrap.indent("\n".join(lines), "    ")


def rule_source(r: Any) -> tuple[str, str]:
    """(script source, doc) from what a model sends: a whole script, {source|script|code: ...}, or the shape cheap
    models reach for first, {check: <body>, apply: <body>} with no def lines (Tamsin's first rules, all of them)."""
    if isinstance(r, dict):
        doc = str(r.get("doc") or "")
        whole = r.get("source") or r.get("script") or r.get("code")
        if whole:
            return rule_source(str(whole))[0], doc
        parts = []
        if r.get("check"):
            parts.append("def check(ctx):\n" + _body(r["check"], returns=True))
        if r.get("apply"):
            parts.append("def apply(ctx):\n" + _body(r["apply"]))
        return "\n\n".join(parts), doc
    s = str(r or "").strip()
    if s and "def apply(" not in s and "def check(" not in s:
        return "def apply(ctx):\n" + _body(s), ""  # a bare body is what happens
    return s, ""


def crashed(reason: str) -> bool:
    """A refusal that is the rule breaking (an error inside it), not the rule's own check saying no."""
    return bool(re.search(r"failed while|has no (apply|check)\(|is not allowed|has no attribute|is not defined|"
                          r"not supported|out of fuel", str(reason)))


def _rule_id(source: str) -> str:
    return hashlib.sha1(source.encode()).hexdigest()[:6]


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def surprise(values: list[float], real: float) -> tuple[float, bool]:
    """(bits, inside the 10-90% band). Bits are -log2 of how often an imagining came out at least as far out as the
    real value on its side, both sides counted, with half a trial added so a never-seen value is finite."""
    n = len(values)
    lo = sum(v <= real for v in values)
    hi = sum(v >= real for v in values)
    p = min(1.0, 2 * (min(lo, hi) + 0.5) / (n + 1))
    s = sorted(values)
    band = (s[int(0.1 * (n - 1))], s[int(round(0.9 * (n - 1)))])
    return round(-math.log2(p), 2), band[0] <= real <= band[1]


def spread(values: list[float]) -> dict[str, Any]:
    s = sorted(values)
    n = len(s)
    out: dict[str, Any] = {"band": [s[int(0.1 * (n - 1))], s[int(round(0.9 * (n - 1)))]],
                           "median": statistics.median(s), "min": s[0], "max": s[-1]}
    if len(set(s)) <= 6:
        out["counts"] = {str(k): c for k, c in sorted(Counter(s).items())}
    return out


class Imagination:
    def __init__(self, seat: Seat, cfg: dict[str, Any]):
        self.seat = seat
        self.cfg = cfg
        self.path = seat.p("imagination.json")
        self.log = seat.p("predictions.jsonl")
        self.spec: dict[str, Any] = read_json(self.path, {}) or {}
        for k, v in (("rules", {}), ("beliefs", {}), ("lessons", []), ("open", []), ("bound", [])):
            self.spec.setdefault(k, v)

    def save(self) -> None:
        write_json(self.path, self.spec)

    # the world ------------------------------------------------------------------------------------------------

    def _build(self, seed: int, me: str, mine: dict[str, Any],
               seen: dict[str, dict[str, Any]] | None = None) -> tuple[Any, dict[str, str]]:
        """A fresh world: the seat's rules, its body as its card shows it, everyone it believes in or sees (what the
        card shows of them over what it believes)."""
        seen = seen or {}
        w = World.create(Store(), "imagination", seed=seed)
        for verb, r in self.spec["rules"].items():
            w.define_script(verb, r["source"])
        w.define_schema("being", extends="creature",
                        actions={v: {"script": v, "doc": r.get("doc") or v} for v, r in self.spec["rules"].items()})
        here = w.spawn("place", w.root_id, "Here")
        ids = {me: w.spawn("being", here, me, state={**self.spec["beliefs"].get(me, {}), **mine})}
        for name in dict.fromkeys([*self.spec["beliefs"], *seen]):
            if name != me:
                ids[name] = w.spawn("being", here, name, state={**self.spec["beliefs"].get(name, {}),
                                                                **seen.get(name, {})})
        return w, ids

    def _who(self, name: Any, me: str, ids: dict[str, str]) -> str | None:
        if not name:
            return None
        n = str(name).strip()
        if n.lower() in ("me", "self", "i", me.lower()):
            return me
        hits = [k for k in ids if n.lower() in k.lower() or k.lower() in n.lower()]
        return hits[0] if hits else None

    def _dry_run(self, verb: str, me: str, mine: dict[str, Any], seen: dict[str, dict[str, Any]]) -> str:
        """Why a rule cannot run, or "": it is built, then tried a few times on someone. A refusal by its own check
        is fine; a crash is not. Tamsin's spar set ctx.result, which does not exist: 18 of 20 trials crashed, the
        crashes were counted as how fights end, and the surprise she got for it she put down to Brann."""
        for seed in range(5):
            try:
                w, ids = self._build(seed, me, mine, seen)
                other = next((i for n, i in ids.items() if n != me), ids[me])
                w.act(ids[me], verb, other, {})
            except ScriptError as e:
                return str(e)
            except Rejected as e:
                if crashed(e.reason):
                    return e.reason
        return ""

    def _write(self, rules: dict[str, Any] | None, setup: dict[str, Any] | None, me: str,
               mine: dict[str, Any], seen: dict[str, dict[str, Any]] | None = None) -> list[str]:
        seen = seen or {}
        notes = []
        for verb, r in (rules or {}).items():
            verb = _key(verb)
            if r is None:
                self.spec["rules"].pop(verb, None)
                notes.append(f"rule {verb} dropped")
                continue
            source, doc = rule_source(r)
            if not re.search(r"^def apply\(", source, re.M):
                notes.append(f"rule {verb} not kept: it needs an apply, what happens: "
                             f"{{'{verb}': {{'check': 'return ctx.actor.state.hp > 0', 'apply': "
                             f"'ctx.target.state.hp -= ctx.rand.roll(\"1d6\")'}}}}")
                continue
            old = self.spec["rules"].get(verb)
            self.spec["rules"][verb] = {"source": source, "doc": doc, "id": _rule_id(source)}
            broke = self._dry_run(verb, me, mine, seen)
            if "NoneType" in broke:  # a number nobody has yet: a belief to set, not a broken rule
                notes.append(f"rule {verb} kept ({self.spec['rules'][verb]['id']}), but it reads a number someone "
                             f"here has no value for yet ({broke.split(':', 1)[-1].strip()}): give it with setup")
                continue
            if broke:
                self.spec["rules"].pop(verb) if old is None else self.spec["rules"].__setitem__(verb, old)
                notes.append(f"rule {verb} not kept: {broke}")
                continue
            notes.append(f"rule {verb} kept ({self.spec['rules'][verb]['id']})")
        for name, st in (setup or {}).items():
            who = me if str(name).strip().lower() in ("me", "self", "i") else str(name).strip()
            if st is None:
                self.spec["beliefs"].pop(who, None)
                notes.append(f"{who} forgotten")
            elif isinstance(st, dict):
                self.spec["beliefs"].setdefault(who, {}).update({_key(k): v for k, v in st.items()})
                notes.append(f"{who}: {json.dumps(self.spec['beliefs'][who])}")
        return notes

    def show(self, me: str, mine: dict[str, Any], seen: dict[str, dict[str, Any]] | None = None,
             verbs: set[str] | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "you": {**self.spec["beliefs"].get(me, {}), **mine},
            "seen": seen or {},
            "beliefs": {k: v for k, v in self.spec["beliefs"].items() if k != me},
            "rules": {v: {"id": r["id"], "doc": r.get("doc", ""), "source": r["source"]}
                      for v, r in self.spec["rules"].items()},
        }
        if not self.spec["rules"]:
            # named after the seat's own verb: Tamsin copied a starter called attack, her world's verb was spar, and
            # her imagining could never be compared with what happened
            real = sorted(verbs or [])
            verb = next((v for v in real if v in FIGHT), real[0] if real else "attack")
            who = next(iter(seen or {}), "Someone")
            out["starter"] = {"rules": {verb: STARTER["attack"]},
                              "setup": {who: {"ac": 12, "atk": 2}, "me": {"ac": 12, "atk": 3}},
                              "plan": [{"actor": "me", "verb": verb, "target": who},
                                       {"actor": who, "verb": verb, "target": "me"}],
                              "rounds": 3,
                              "hint": "name each rule after the real verb it stands for, and write it as your "
                                      "world's handbook and what you have seen say it works, not as this example "
                                      "guesses"}
        if self.spec["lessons"]:
            out["lessons"] = self.spec["lessons"][-8:]
        cal = self.calibration()
        if cal:
            out["calibration"] = cal
        return out

    # imagining ------------------------------------------------------------------------------------------------

    def imagine(self, a: dict[str, Any], bundle: dict[str, Any], me: str, turn_no: int,
                verbs: set[str] | None = None) -> dict[str, Any]:
        mine, seen = body_state(bundle), others(bundle, me)
        notes = self._write(a.get("rules"), a.get("setup"), me, mine, seen)
        plan = a.get("plan")
        if isinstance(plan, dict):
            plan = [plan]
        if not plan:
            self.save()
            return {"ok": True, "notes": notes, **self.show(me, mine, seen, verbs)}
        refused = [n for n in notes if " not kept: " in n]
        if refused:
            # the plan would run the old rule, and its error would hide what was wrong with the new one
            self.save()
            return {"ok": False, "reason": "; ".join(refused),
                    "hint": "fix the rule and imagine again: nothing was run or recorded", "notes": notes}
        # a spread needs trials: Tamsin asked for 3 and got three stories, not odds
        trials = max(MIN_TRIALS, min(MAX_TRIALS, int(a.get("trials") or self.cfg.get("imagine_trials", 40))))
        rounds = max(1, min(MAX_ROUNDS, int(a.get("rounds") or 1)))
        w0, ids0 = self._build(0, me, mine, seen)
        steps = []
        for s in plan:
            if not isinstance(s, dict):
                continue
            actor = self._who(s.get("actor") or "me", me, ids0)
            target = self._who(s.get("target"), me, ids0) if s.get("target") else None
            verb = _key(str(s.get("verb") or ""))
            if verb not in self.spec["rules"]:
                return {"ok": False, "reason": f"no rule for {verb!r} in your imagination",
                        "hint": "write it first: imagine(rules={'" + verb + "': 'def check(ctx): ...'}); imagine() "
                                "with nothing shows a starter", "notes": notes}
            if actor is None or (s.get("target") and target is None):
                return {"ok": False, "reason": f"nobody called {s.get('actor') if actor is None else s.get('target')!r}"
                                               " in your imagination",
                        "hint": "add them with setup={'Name': {...what you believe...}}", "notes": notes}
            steps.append((actor, verb, target, s.get("params") or {}))
        if verbs and steps and steps[0][0] == me and steps[0][1] not in verbs:
            notes.append(f"{steps[0][1]!r} is not a verb you really have (you have: {', '.join(sorted(verbs))}): name "
                         f"the rule after the real verb, or the harness cannot compare this with what happens")
        start = {f"{n}.{k}": v for n, i in ids0.items() for k, v in ((w0.get(i) or {}).get("state") or {}).items()
                 if _num(v)}
        values: dict[str, list[float]] = {}
        endings: Counter = Counter()
        t0, done = time.time(), 0
        for seed in range(trials):
            w, ids = self._build(1000 + seed + 7919 * turn_no, me, mine, seen)
            end = "the plan ran to its end"
            try:
                for _ in range(rounds):
                    for actor, verb, target, params in steps:
                        w.act(ids[actor], verb, ids[target] if target else None, params)
            except Rejected as e:
                if crashed(e.reason):  # a broken rule is not how things end: nothing is recorded
                    return {"ok": False, "reason": f"your rule {verb} breaks: {e.reason}",
                            "hint": ("someone in the plan has no value for a number the rule reads: give it with "
                                     "setup={name: {...}}" if "NoneType" in e.reason else "fix it with "
                                     "imagine(rules=...)") + " and imagine again; nothing was recorded",
                            "notes": notes}
                end = f"{actor} {verb}: {e.reason}"
            except ScriptError as e:
                return {"ok": False, "reason": f"your rule {verb} breaks: {e}", "hint": "nothing was recorded",
                        "notes": notes}
            endings[end] += 1
            for n, i in ids.items():
                for k, v in ((w.get(i) or {}).get("state") or {}).items():
                    if _num(v):
                        values.setdefault(f"{n}.{k}", []).append(v)
            done += 1
            if time.time() - t0 > BUDGET_S:
                break
        moved = {k: v for k, v in values.items() if len(v) == done and (len(set(v)) > 1 or v[0] != start.get(k))}
        # every number of everyone in the plan is a prediction, moved or not: a rule that does nothing, met by a real
        # blow, is the surprise worth having
        cast = {x for s in steps for x in (s[0], s[2]) if x}
        kept = {k: v for k, v in values.items() if len(v) == done and k.split(".", 1)[0] in cast}
        pid = "p" + hashlib.sha1(f"{time.time()}{plan}".encode()).hexdigest()[:6]
        domain = "+".join(sorted({v for _, v, _, _ in steps}))
        first = steps[0]
        rec = {"kind": "imagined", "id": pid, "ts": time.time(), "turn": turn_no, "me": me, "domain": domain,
               "plan": [{"actor": a_, "verb": v, "target": t} for a_, v, t, _ in steps], "rounds": rounds,
               "trials": done, "first": {"actor": first[0], "verb": first[1], "target": first[2]},
               "values": kept, "endings": dict(endings.most_common(5)),
               "rules": {v: self.spec["rules"][v]["id"] for v in {v for _, v, _, _ in steps}},
               "beliefs": {n: self.spec["beliefs"].get(n, {}) for n in {x for s in steps for x in (s[0], s[2]) if x}}}
        append_jsonl(self.log, rec)
        if first[0] == me:
            self.spec["open"] = [o for o in self.spec["open"] if time.time() - o["ts"] < OPEN_FOR][-4:] + \
                [{"id": pid, "ts": rec["ts"], "verb": first[1], "target": first[2]}]
        self.save()
        out: dict[str, Any] = {"ok": True, "id": pid, "trials": done, "rounds": rounds,
                               "came_out": {k: spread(v) for k, v in moved.items()},
                               "endings": {k: f"{c} of {done}" for k, c in endings.most_common(5)}}
        if not moved:
            out["hint"] = "nothing changed in any trial: check that the rules' apply changes state" + (
                f"; every trial ended the same way: {next(iter(endings))}" if len(endings) == 1 else "")
        if notes:
            out["notes"] = notes
        if self.spec["lessons"]:
            out["lessons"] = self.spec["lessons"][-5:]
        if first[0] == me:
            out["next"] = (f"if you do it, act(verb='{first[1]}'" + (f", target='{first[2]}'" if first[2] else "") +
                           "): the harness will compare what really happens with this")
        return out

    # predictions ----------------------------------------------------------------------------------------------

    def bound_plans(self) -> tuple[set[str], set[str]]:
        """(verbs, names) of the plans now bound to real deeds: the deeds that continue them, and who to read."""
        recs, verbs, names = self.records(), set(), set()
        for b in self.spec["bound"]:
            for s in (recs.get(b["id"]) or {}).get("plan") or []:
                verbs.add(s["verb"])
                names |= {x for x in (s.get("actor"), s.get("target")) if x}
        return verbs, names

    def bind(self, verb: str, target: Any) -> str | None:
        """A real deed: if it is the first step of something imagined lately, that imagining is now a prediction."""
        now, v, t = time.time(), _key(verb), str(target or "").lower()
        for o in reversed(self.spec["open"]):
            if now - o["ts"] > OPEN_FOR or o["verb"] != v:
                continue
            ot = str(o.get("target") or "").lower()
            if ot and t and not (ot in t or t in ot):
                continue
            self.spec["open"].remove(o)
            self.spec["bound"].append({"id": o["id"], "ts": now})
            append_jsonl(self.log, {"kind": "bound", "id": o["id"], "ts": now, "verb": v, "target": target})
            self.save()
            return o["id"]
        return None

    def records(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for r in read_jsonl(self.log):
            if r.get("kind") == "imagined":
                out[r["id"]] = dict(r)
            elif r.get("id") in out:
                out[r["id"]].setdefault(r["kind"], r)
        return out

    def score(self, bundle: dict[str, Any], me: str) -> list[dict[str, Any]]:
        """Score every bound prediction against the seat's real card. Only what the card shows is scored: the seat's
        own numbers and what it holds, and the numbers it shows of others (often their hp). Returns the scored
        records."""
        if not self.spec["bound"]:
            return []
        recs, real, now, out = self.records(), observed(bundle, me), time.time(), []
        for b in list(self.spec["bound"]):
            self.spec["bound"].remove(b)
            r = recs.get(b["id"])
            if not r:
                continue
            if now - b["ts"] > STALE_AFTER:
                append_jsonl(self.log, {"kind": "stale", "id": b["id"], "ts": now})
                continue
            bits, inside, seen = {}, {}, {}
            for k, vals in (r.get("values") or {}).items():
                if k not in real:
                    continue
                seen[k] = real[k]
                bits[k], inside[k] = surprise(vals, real[k])
            if not seen:
                append_jsonl(self.log, {"kind": "unseen", "id": b["id"], "ts": now})
                continue
            s = {"kind": "scored", "id": b["id"], "ts": now, "after_s": round(now - b["ts"]), "real": seen,
                 "bits": bits, "inside": inside, "max_bits": max(bits.values())}
            append_jsonl(self.log, s)
            out.append({**r, "scored": s})
        self.save()
        return out

    def calibration(self) -> list[str]:
        by: dict[str, list[dict[str, Any]]] = {}
        for r in self.records().values():
            if "scored" in r:
                by.setdefault(r["domain"], []).append(r["scored"])
        lines = []
        for d, ss in sorted(by.items()):
            n = len(ss)
            inside = sum(all(s["inside"].values()) for s in ss)
            mean = statistics.mean(s["max_bits"] for s in ss)
            lines.append(f"{d}: {n} scored, {inside} of {n} inside the imagined 80% band, mean surprise {mean:.1f} bits")
        return lines

    def wake_text(self, scored: list[dict[str, Any]]) -> str:
        if not scored:
            return ""
        lim = float(self.cfg.get("surprise_bits", 4))
        parts = []
        for r in scored:
            s = r["scored"]
            what = f"{r['first']['verb']}" + (f" on {r['first']['target']}" if r["first"].get("target") else "")
            lines = []
            for k, real in s["real"].items():
                sp = spread(r["values"][k])
                lines.append(f"{k.split('.', 1)[1]} imagined {sp['band'][0]} to {sp['band'][1]} in 8 of 10 trials "
                             f"(median {sp['median']}), really {real}: {s['bits'][k]} bits")
            if s["max_bits"] >= lim:
                beliefs = "; ".join(f"{n} {json.dumps(b)}" for n, b in r["beliefs"].items() if b)
                parts.append(
                    f"SURPRISE ({r['id']}): you imagined {what} ({r['trials']} trials). " + "; ".join(lines) +
                    f". Only about 1 in {2 ** s['max_bits']:.0f} imaginings came out that far off. Rules it used: "
                    + ", ".join(f"{v} {i}" for v, i in r["rules"].items()) +
                    (f". What you believed: {beliefs}" if beliefs else "") +
                    ". Decide what was wrong: a rule (fix it with imagine(rules=...)), what you believe of someone "
                    "(imagine(setup=...)), or luck. Then reflect(id='" + r["id"] + "', verdict='rule'|'belief'|"
                    "'luck', lesson='one line') so the lesson rides on your next imaginings.")
            else:
                parts.append(f"As imagined ({r['id']}): {what}. " + "; ".join(lines) + ".")
        cal = self.calibration()
        return "\n\n(imagination) " + "\n".join(parts) + (
            "\nYour imagination so far: " + " | ".join(cal) if cal else "")

    def reflect(self, a: dict[str, Any]) -> dict[str, Any]:
        recs = self.records()
        pid = str(a.get("id") or "").strip()
        if pid:
            r = recs.get(pid)
            if not r or "scored" not in r:
                return {"ok": False, "reason": f"no scored imagining {pid!r}", "hint": "reflect() lists them"}
            verdict = str(a.get("verdict") or "").lower()
            if verdict not in ("rule", "belief", "luck"):
                return {"ok": False, "reason": "verdict is one of rule, belief, luck"}
            lesson = str(a.get("lesson") or "").strip()[:240]
            append_jsonl(self.log, {"kind": "reflected", "id": pid, "ts": time.time(), "verdict": verdict,
                                    "lesson": lesson})
            if lesson and verdict != "luck":
                self.spec["lessons"] = (self.spec["lessons"] + [f"[{r['domain']}] {lesson}"])[-12:]
                self.save()
            return {"ok": True, "kept": lesson if verdict != "luck" else None, "calibration": self.calibration()}
        scored = sorted((r for r in recs.values() if "scored" in r), key=lambda r: -r["scored"]["max_bits"])[:8]
        return {"ok": True, "calibration": self.calibration(), "lessons": self.spec["lessons"][-8:],
                "imaginings": [{"id": r["id"], "plan": r["domain"], "first": r["first"],
                                "imagined": {k: spread(r["values"][k])["band"] for k in r["scored"]["real"]},
                                "real": r["scored"]["real"], "bits": r["scored"]["bits"],
                                "reflected": (r.get("reflected") or {}).get("verdict")} for r in scored]}
