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
import random
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
# 40 trials at least: with 20, a value never imagined and one imagined once read the same 4.4 bits
MIN_TRIALS, MAX_TRIALS, MAX_ROUNDS, BUDGET_S = 40, 200, 30, 8.0
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


def _my_steps(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """The steps of an imagined plan that are the seat's own deeds (the first step's actor is the seat)."""
    who = (rec.get("first") or {}).get("actor")
    return [s for s in rec.get("plan") or [] if who and s.get("actor") == who]


def _odds(v: Any) -> dict[str, float] | None:
    """{choice: weight} when v is odds (every weight a positive number), else None."""
    if isinstance(v, dict) and v and all(_num(w) and w > 0 for w in v.values()):
        return {str(k): float(w) for k, w in v.items()}
    return None


def _draw(rng: Any, odds: dict[str, float]) -> str:
    return rng.choices(list(odds), weights=list(odds.values()))[0]


def _verbs(v: Any) -> list[str]:
    """The verbs a step may take: one, or each one its odds name."""
    return list(v) if isinstance(v, dict) else [v]


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
        self.partials: list[str] = []  # plans not scored on the last score(), because what was done differed
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

    @staticmethod
    def _choose(rng: Any, actor: str, verb: Any, params: dict[str, Any], odds: dict[str, dict[str, float]],
                drawn: Counter | None = None) -> tuple[str, dict[str, Any]]:
        """One trial's draw of a step's guesses: the verb from its odds, and each param given as odds."""
        if isinstance(verb, dict):
            verb = _draw(rng, verb)
            if drawn is not None:
                drawn[f"{actor} {verb}"] += 1
        if odds:
            params = dict(params)
            for k, o in odds.items():
                params[k] = _draw(rng, o)
                if drawn is not None:
                    drawn[f"{k}={params[k]}"] += 1
        return verb, params

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
            key = re.search(r"key '(\w+)' not found", broke)
            if key and re.search(r"ctx\.params\W+" + key.group(1), source):
                # tried with no params, a rule that reads a choice made in the step is missing only that choice
                notes.append(f"rule {verb} kept ({self.spec['rules'][verb]['id']}); it reads ctx.params."
                             f"{key.group(1)}: give it in each step, as params= or as odds=")
                continue
            if "NoneType" in broke:  # a number nobody has yet: a belief to set, not a broken rule
                notes.append(f"rule {verb} kept ({self.spec['rules'][verb]['id']}), but it reads a number someone "
                             f"here has no value for yet ({broke.split(':', 1)[-1].strip()}): give it with setup")
                continue
            if broke:
                self.spec["rules"].pop(verb) if old is None else self.spec["rules"].__setitem__(verb, old)
                notes.append(f"rule {verb} not kept: {broke}")
                continue
            body = source.split("def apply(ctx):", 1)[-1].split("\ndef ", 1)[0]
            if not [ln for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")
                    and ln.strip() != "pass"]:
                # Suri replaced a strike that dealt damage with one that did nothing, and her re-living said her
                # imagination had got worse without saying why
                notes.append(f"rule {verb} kept ({self.spec['rules'][verb]['id']}), but its apply does nothing: "
                             f"everything you imagine with {verb} stays as it was" +
                             (f". It replaced {old['id']}, which did something: imagine() shows rules"
                              if old and old["source"] != source else ""))
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
        recs = self.records()
        ev = {d: e for d in dict.fromkeys(r["domain"] for r in recs.values()) if (e := self.evidence(d, recs))}
        if ev:
            out["evidence"] = ev
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
            # the seat owns this world: whoever else is in it does what the seat guesses they would, and a guess it is
            # unsure of comes as odds, drawn afresh each trial (Rin could not say what Oren would pick, only guess)
            vo = _odds(s.get("verb"))
            verb: Any = {_key(k): w for k, w in vo.items()} if vo else _key(str(s.get("verb") or ""))
            odds = {str(k): o for k, v in (s.get("odds") or {}).items() if (o := _odds(v))} \
                if isinstance(s.get("odds"), dict) else {}
            unknown = [v for v in _verbs(verb) if v not in self.spec["rules"]]
            if unknown:
                return {"ok": False, "reason": f"no rule for {unknown[0]!r} in your imagination",
                        "hint": "write it first: imagine(rules={'" + unknown[0] + "': 'def check(ctx): ...'}); "
                                "imagine() with nothing shows a starter", "notes": notes}
            if actor is None or (s.get("target") and target is None):
                return {"ok": False, "reason": f"nobody called {s.get('actor') if actor is None else s.get('target')!r}"
                                               " in your imagination",
                        "hint": "add them with setup={'Name': {...what you believe...}}", "notes": notes}
            if vo and actor == me:
                return {"ok": False, "reason": "your own steps take one verb: odds are for what others might do",
                        "hint": "imagine each of your choices as its own plan, and compare them", "notes": notes}
            steps.append((actor, verb, target, s.get("params") or {}, odds))
        mine_first = next((s for s in steps if s[0] == me), None)
        if verbs and mine_first and mine_first[1] not in verbs:
            notes.append(f"{mine_first[1]!r} is not a verb you really have (you have: {', '.join(sorted(verbs))}): "
                         f"name the rule after the real verb, or the harness cannot compare this with what happens")
        start = {f"{n}.{k}": v for n, i in ids0.items() for k, v in ((w0.get(i) or {}).get("state") or {}).items()
                 if _num(v)}
        values: dict[str, list[float]] = {}
        endings: Counter = Counter()
        drawn: Counter = Counter()
        t0, done = time.time(), 0
        for seed in range(trials):
            w, ids = self._build(1000 + seed + 7919 * turn_no, me, mine, seen)
            rng = random.Random(seed + 7919 * turn_no)
            end = "the plan ran to its end"
            try:
                for _ in range(rounds):
                    for actor, verb, target, params, odds in steps:
                        verb, params = self._choose(rng, actor, verb, params, odds, drawn)
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
        # the kind of plan is named by the seat's own deeds: what others might do is part of the guess, not the kind
        domain = "+".join(sorted({s[1] for s in steps if s[0] == me} or {v for s in steps for v in _verbs(s[1])}))
        # the seat's first own deed is the one that binds this to the real world, wherever its guesses about others
        # stand in the plan
        first = mine_first or steps[0]
        rec = {"kind": "imagined", "id": pid, "ts": time.time(), "turn": turn_no, "me": me, "domain": domain,
               "plan": [{"actor": a_, "verb": v, "target": t, **({"params": p} if p else {}),
                         **({"odds": o} if o else {})} for a_, v, t, p, o in steps], "rounds": rounds,
               "trials": done, "first": {"actor": first[0], "verb": first[1], "target": first[2]},
               "values": kept, "start": {k: start[k] for k in kept if k in start},
               "endings": dict(endings.most_common(5)),
               "rules": {v: self.spec["rules"][v]["id"] for s in steps for v in _verbs(s[1])},
               "beliefs": {n: self.spec["beliefs"].get(n, {}) for n in {x for s in steps for x in (s[0], s[2]) if x}}}
        append_jsonl(self.log, rec)
        if first[0] == me:
            self.spec["open"] = [o for o in self.spec["open"] if time.time() - o["ts"] < OPEN_FOR][-4:] + \
                [{"id": pid, "ts": rec["ts"], "verb": first[1], "target": first[2]}]
        self.save()
        out: dict[str, Any] = {"ok": True, "id": pid, "trials": done, "rounds": rounds,
                               "came_out": {k: spread(v) for k, v in moved.items()},
                               "endings": {k: f"{c} of {done}" for k, c in endings.most_common(5)}}
        if drawn:
            out["your_guesses_drawn"] = {k: c for k, c in sorted(drawn.items())}
        if not moved:
            out["hint"] = "nothing changed in any trial: check that the rules' apply changes state" + (
                f"; every trial ended the same way: {next(iter(endings))}" if len(endings) == 1 else "")
        if notes:
            out["notes"] = notes
        if self.spec["lessons"]:
            out["lessons"] = self.spec["lessons"][-5:]
        ev = self.evidence(domain)
        if ev:
            out["really_seen"] = ev
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
                verbs.update(_verbs(s["verb"]))
                names |= {x for x in (s.get("actor"), s.get("target")) if x}
        return verbs, names

    def bind(self, verb: str, target: Any) -> str | None:
        """A real deed: if it is the first step of something imagined lately, that imagining is now a prediction."""
        now, v, t = time.time(), _key(verb), str(target or "").lower()
        recs = self.records() if self.spec["bound"] else {}
        for b in self.spec["bound"]:
            # the plan going on, not a new one: Tamsin's second and third spars each bound an older imagining
            if v in {s["verb"] for s in _my_steps(recs.get(b["id"]) or {})}:
                b["steps"] = b.get("steps", 1) + 1
                # each deed as done, so a re-living replays what really happened, not the plan
                append_jsonl(self.log, {"kind": "step", "id": b["id"], "ts": now, "verb": v, "target": target})
                self.save()
                return b["id"]
        for o in reversed(self.spec["open"]):
            if now - o["ts"] > OPEN_FOR or o["verb"] != v:
                continue
            ot = str(o.get("target") or "").lower()
            if ot and t and not (ot in t or t in ot):
                continue
            self.spec["open"].remove(o)
            self.spec["bound"].append({"id": o["id"], "ts": now, "steps": 1})
            append_jsonl(self.log, {"kind": "bound", "id": o["id"], "ts": now, "verb": v, "target": target})
            self.save()
            return o["id"]
        return None

    def records(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for r in read_jsonl(self.log):
            if r.get("kind") == "imagined":
                out[r["id"]] = dict(r)
            elif r.get("kind") == "step" and r.get("id") in out:
                out[r["id"]].setdefault("steps", []).append(r)
            elif r.get("id") in out:
                out[r["id"]].setdefault(r["kind"], r)
        return out

    def score(self, bundle: dict[str, Any], me: str) -> list[dict[str, Any]]:
        """Score every bound prediction against the seat's real card. Only what the card shows is scored: the seat's
        own numbers and what it holds, and the numbers it shows of others (often their hp). Returns the scored
        records."""
        self.partials: list[str] = []
        if not self.spec["bound"]:
            return []
        recs, real, now, out = self.records(), observed(bundle, me), time.time(), []
        for b in list(self.spec["bound"]):
            self.spec["bound"].remove(b)
            r = recs.get(b["id"])
            if not r:
                continue
            planned, done = len(_my_steps(r)) * int(r.get("rounds") or 1), int(b.get("steps", 1))
            if done != planned:
                # three exchanges imagined, one done, then a rest: scored, that read 4.4 bits of surprise that was
                # only a different plan. A different plan is not a wrong imagination
                # not scored, but what really came of it is still evidence: 12 of Tamsin's 20 acted plans were
                # partial, and every one of them showed what a spar really does
                append_jsonl(self.log, {"kind": "partial", "id": b["id"], "ts": now, "done": done, "of": planned,
                                        "real": {k: real[k] for k in r.get("values") or {} if k in real}})
                self.partials.append(f"Not scored ({b['id']}): you imagined {planned} of your own steps and did "
                                     f"{done}. What really happened still counts as evidence. Imagine the plan "
                                     f"you will really carry out (rounds=1 for one at a time), and the harness "
                                     f"can score it")
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

    def evidence(self, domain: str, recs: dict[str, dict[str, Any]] | None = None) -> list[str]:
        """How each number moved per step of the seat's own, imagined against real, over every plan of this kind it
        really did, scored or not. Tamsin was told how surprised to be and never what the numbers were: all seven
        of her reflections blamed a rule and invented one (Brann has a memory, hits harder when low), while the
        numbers said plainly that he hit about twice as hard as she believed."""
        imag: dict[str, list[float]] = {}
        real: dict[str, list[float]] = {}
        for r in (recs or self.records()).values():
            got = r.get("scored") or r.get("partial")
            if r.get("domain") != domain or not r.get("start") or not (got or {}).get("real"):
                continue
            planned = max(1, len(_my_steps(r)) * int(r.get("rounds") or 1))
            done = max(1, int(got.get("done") or planned))
            for k, v in got["real"].items():
                if k in r["start"] and k in (r.get("values") or {}) and _num(v):
                    real.setdefault(k, []).append((v - r["start"][k]) / done)
                    imag.setdefault(k, []).extend((x - r["start"][k]) / planned for x in r["values"][k])
        lines = []
        for k, got in real.items():
            want = sorted(imag[k])
            if not any(got) and not any(want):
                continue
            band = (want[int(0.1 * (len(want) - 1))], want[int(round(0.9 * (len(want) - 1)))])
            avg = statistics.mean(got)
            lines.append(f"{k} per step of yours: imagined {statistics.median(want):+.1f} (8 of 10 trials "
                         f"{band[0]:+.1f} to {band[1]:+.1f}), really {avg:+.1f} on average over {len(got)} "
                         f"(from {min(got):+.1f} to {max(got):+.1f})" +
                         ("" if band[0] <= avg <= band[1] else ": the real average is outside what you imagine"))
        return lines

    def wake_text(self, scored: list[dict[str, Any]]) -> str:
        if not scored and not self.partials:
            return ""
        lim = float(self.cfg.get("surprise_bits", 4))
        parts = list(self.partials)
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
                ev = self.evidence(r["domain"])
                parts.append(
                    f"SURPRISE ({r['id']}): you imagined {what} ({r['trials']} trials). " + "; ".join(lines) +
                    f". Only about 1 in {2 ** s['max_bits']:.0f} imaginings came out that far off. Rules it used: "
                    + ", ".join(f"{v} {i}" for v, i in r["rules"].items()) +
                    (f". What you believed: {beliefs}" if beliefs else "") +
                    (". Everything you have really seen of " + r["domain"] + ":\n- " + "\n- ".join(ev) + "\n"
                     if ev else ". ") +
                    "Decide what was wrong, in this order. A belief: would other numbers for someone (their attack, "
                    "defence, damage) bring the imagined spread onto the real average? Then change them with "
                    "imagine(setup=...). A rule: only when the same thing keeps happening that no numbers in your "
                    "rule could produce, over several tries, not one. Luck: one result your dice allow, with the "
                    "averages in line. A story that fits one result is not a rule. Then reflect(id='" + r["id"] +
                    "', verdict='belief'|'rule'|'luck', lesson='one line, with the numbers').")
            else:
                parts.append(f"As imagined ({r['id']}): {what}. " + "; ".join(lines) + ".")
        cal = self.calibration()
        return "\n\n(imagination) " + "\n".join(parts) + (
            "\nYour imagination so far: " + " | ".join(cal) if cal else "")

    # re-living ------------------------------------------------------------------------------------------------

    def relive(self, a: dict[str, Any], me: str) -> dict[str, Any]:
        """Live a past moment again in imagination: the numbers the seat saw then, its own deeds as it really did
        them, played through the rules and beliefs it has now. With no id, every moment it lived is re-lived and the
        answer is how many its imagination would see coming now against then: a learning curve the harness measures.
        With instead=, the replay turns into imagination at the cutoff: what if it had done something else."""
        recs = self.records()
        lived = [r for r in recs.values() if r.get("start") and (r.get("scored") or r.get("partial") or {}).get("real")]
        pid = str(a.get("id") or "").strip()
        if pid and pid != "all":
            r = recs.get(pid)
            if r not in lived:
                return {"ok": False, "reason": f"{pid!r} is not a moment you lived: re-live an imagining you acted on",
                        "hint": "relive() with no id re-lives them all"}
            return self._relive_one(r, me, a.get("after"), a.get("instead"))
        if not lived:
            return {"ok": False, "reason": "nothing lived yet: imagine a plan, then do it, and it can be re-lived"}
        rows, t0 = [], time.time()
        for r in reversed(lived[-40:]):
            x = self._relive_one(r, me)
            if x.get("ok"):
                rows.append(x)
            if time.time() - t0 > 2 * BUDGET_S:
                break
        if not rows:
            return {"ok": False, "reason": "no past moment could be re-lived with your rules now",
                    "hint": "a rule a moment used may be gone: imagine() shows your rules"}
        rows.reverse()
        line = (f"re-lived {len(rows)} moments with your imagination now: {sum(x['now_inside'] for x in rows)} of "
                f"{len(rows)} came out inside your imagined 80% band, mean surprise "
                f"{statistics.mean(x['now_bits'] for x in rows):.1f} bits")
        then = [x for x in rows if x["then_inside"] is not None]
        if then:
            line += (f". Of the {len(then)} scored when you lived them: then {sum(x['then_inside'] for x in then)} "
                     f"inside at {statistics.mean(x['then_bits'] for x in then):.1f} bits, now "
                     f"{sum(x['now_inside'] for x in then)} at {statistics.mean(x['now_bits'] for x in then):.1f}")
        worst = sorted(rows, key=lambda x: -x["now_bits"])[:5]
        return {"ok": True, "summary": line, "worst_now": worst,
                "next": "relive(id, after=N, instead=[{actor, verb, target}]): from a moment, after N of your real "
                        "steps, imagine another way"}

    def _relive_one(self, r: dict[str, Any], me: str, after: Any = None, instead: Any = None) -> dict[str, Any]:
        got = r.get("scored") or r.get("partial")
        then_me = r.get("me") or me
        mine: dict[str, Any] = {}
        seen: dict[str, dict[str, Any]] = {}
        for k, v in r["start"].items():
            n, f = k.split(".", 1)
            (mine if n == then_me else seen.setdefault(n, {}))[f] = v
        # the guesses about others stay as the seat made them then; only its rules and beliefs are today's
        plan = [(s["actor"], s["verb"], s.get("target"), s.get("params") or {}, s.get("odds") or {})
                for s in r.get("plan") or []]
        own = [s for s in plan if s[0] == (r.get("first") or {}).get("actor")]
        done = max(1, int(got.get("done") or len(own) * int(r.get("rounds") or 1)))
        # the deeds as really done: the binding one and each logged after it; older records logged only a count, and
        # their plan stands in for the rest
        deeds = [(_key(str(d.get("verb") or "")), d.get("target")) for d in [r.get("bound") or {}, *r.get("steps", [])]
                 if d.get("verb")][:done]
        for i in range(len(deeds), done):
            deeds.append(own[i % len(own)][1:3] if own else ("", None))
        cut = done if after is None else max(0, min(int(after), done))
        branch = []
        for s in (instead if isinstance(instead, list) else [instead] if isinstance(instead, dict) else []):
            if isinstance(s, dict):
                vo = _odds(s.get("verb"))
                odds = {str(k): o for k, v in (s.get("odds") or {}).items() if (o := _odds(v))}                     if isinstance(s.get("odds"), dict) else {}
                branch.append((s.get("actor") or "me", {_key(k): w for k, w in vo.items()} if vo else
                               _key(str(s.get("verb") or "")), s.get("target"), s.get("params") or {}, odds))
        missing = sorted({v for v in [*(v for s in plan for v in _verbs(s[1])), *(d[0] for d in deeds[:cut]),
                                      *(v for b in branch for v in _verbs(b[1]))] if v not in self.spec["rules"]})
        if missing:
            return {"ok": False, "id": r["id"], "reason": f"no rule now for {', '.join(missing)}",
                    "hint": "write it with imagine(rules=...) first"}
        values: dict[str, list[float]] = {}
        for seed in range(MIN_TRIALS):
            w, ids = self._build(5000 + seed, then_me, mine, seen)
            rng = random.Random(5000 + seed)
            try:
                # the plan's order with the real deeds in the seat's places; the others' steps after the last deed
                # still happen, as they did before the card was read
                k, i = 0, 0
                while plan and k <= cut:
                    actor, verb, target, params, odds = plan[i % len(plan)]
                    i += 1
                    if actor == then_me or (own and actor == own[0][0]):
                        if k == cut:
                            break
                        verb, real_t = deeds[k]
                        target = self._who(real_t, then_me, ids) or target
                        k += 1
                    verb, params = self._choose(rng, actor, verb, params, odds)
                    w.act(ids[actor], verb, ids.get(target) if target else None, params)
                    if i > 4 * len(plan) * (done + 1):
                        break
                for actor, verb, target, params, odds in branch:
                    a_ = self._who(actor, then_me, ids)
                    t_ = self._who(target, then_me, ids) if target else None
                    if a_ is None or (target and t_ is None):
                        return {"ok": False, "id": r["id"],
                                "reason": f"nobody called {actor if a_ is None else target!r} was there"}
                    verb, params = self._choose(rng, a_, verb, params, odds)
                    w.act(ids[a_], verb, ids[t_] if t_ else None, params)
            except Rejected as e:
                if crashed(e.reason):
                    return {"ok": False, "id": r["id"], "reason": f"a rule breaks: {e.reason}",
                            "hint": "fix it with imagine(rules=...)"}
            except (ScriptError, KeyError) as e:
                return {"ok": False, "id": r["id"], "reason": f"could not re-live it: {e}"}
            for n, i_ in ids.items():
                for f, v in ((w.get(i_) or {}).get("state") or {}).items():
                    if _num(v):
                        values.setdefault(f"{n}.{f}", []).append(v)
        real = got["real"]
        moment = (f"turn {r.get('turn')}: " + ", ".join(d[0] for d in deeds) + " from " +
                  ", ".join(f"{k} {v}" for k, v in r["start"].items() if k in real))
        if branch:
            return {"ok": True, "id": r["id"], "moment": moment,
                    "replayed": f"{cut} of your {done} real steps, then what you gave instead (imagined, not scored)",
                    "came_out": {k: spread(v) for k, v in values.items() if k in real}, "really": real}
        now = {}
        for k, v in real.items():
            if k in values:
                bits, inside = surprise(values[k], v)
                now[k] = {"imagined": spread(values[k])["band"], "real": v, "bits": bits, "inside": inside}
        if not now:
            return {"ok": False, "id": r["id"], "reason": "none of the numbers seen then came out of the re-living"}
        s = r.get("scored")
        return {"ok": True, "id": r["id"], "moment": moment, "now": now,
                "now_inside": all(x["inside"] for x in now.values()), "now_bits": max(x["bits"] for x in now.values()),
                "then_inside": all(s["inside"].get(k, True) for k in now) if s else None,
                "then_bits": max(s["bits"].get(k, 0) for k in now) if s else None}

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
            tag = f"[{r['domain']}] "
            if lesson and verdict != "luck":
                # the last few per kind of plan: Tamsin's seven spar lessons contradicted each other and all rode along
                same = [x for x in self.spec["lessons"] if x.startswith(tag)][-3:]
                self.spec["lessons"] = ([x for x in self.spec["lessons"] if not x.startswith(tag)] + same +
                                        [tag + lesson])[-12:]
                self.save()
            out: dict[str, Any] = {"ok": True, "kept": lesson if verdict != "luck" else None,
                                   "calibration": self.calibration()}
            # a verdict says what to change: check it was changed
            if verdict == "belief" and all(self.spec["beliefs"].get(n, {}) == b for n, b in r["beliefs"].items()):
                out["note"] = ("your beliefs are as they were when you imagined this: change the numbers with "
                               "imagine(setup={name: {...}}), or the next imagining makes the same mistake")
            elif verdict == "rule":
                if all((self.spec["rules"].get(v) or {}).get("id") == i for v, i in r["rules"].items()):
                    out["note"] = ("your rules are as they were when you imagined this: fix one with "
                                   "imagine(rules=...), or the next imagining makes the same mistake")
                seen = sum(1 for x in recs.values() if x.get("domain") == r["domain"] and
                           ((x.get("scored") or x.get("partial") or {}).get("real")))
                if seen < 3:
                    out["note"] = (out.get("note", "") + "; " if out.get("note") else "") + (
                        f"a new rule from {seen} real {r['domain']} is a guess: check first whether other numbers "
                        f"for someone (a belief) explain it")
            return out
        scored = sorted((r for r in recs.values() if "scored" in r), key=lambda r: -r["scored"]["max_bits"])[:8]
        return {"ok": True, "calibration": self.calibration(), "lessons": self.spec["lessons"][-8:],
                "evidence": {d: self.evidence(d, recs) for d in dict.fromkeys(r["domain"] for r in scored)},
                "imaginings": [{"id": r["id"], "plan": r["domain"], "first": r["first"],
                                "imagined": {k: spread(r["values"][k])["band"] for k in r["scored"]["real"]},
                                "real": r["scored"]["real"], "bits": r["scored"]["bits"],
                                "reflected": (r.get("reflected") or {}).get("verdict")} for r in scored]}
