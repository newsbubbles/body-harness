"""The behavior graph curriculum: a model under test writes its body's graph from a scene and an intent, with the
real behavior tool (help, apply, view, run), then the graph runs on the scenario's wakes against the real judge.

    python evals/behavior/run.py --env-file PATH --out DIR --models openrouter:a,openrouter:b --levels 1,2,3
        [--variant V1] [--reps 1] [--cap 0.50]

One row per (scenario, model, variant, rep) goes to DIR/results.jsonl and each authoring transcript to
DIR/transcripts/. Nothing here talks to a world: the ops are the scenario file's."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from body_harness import behavior as bv
from body_harness import reflexes as rx
from body_harness.loop import clean_calls
from body_harness.providers import Model, ModelError
from body_harness.tools import parse_args

HERE = Path(__file__).parent
JUDGE = "openrouter:typesafe/jev-1.14"

VARIANTS = {
    # V0: the tool's own description and help are all the model gets
    "V0": "Before you sleep you may give your body a behavior graph with the behavior tool, so it handles what you "
          "expect without you. Then call sleep.",
    # V1: plus where checks and questions go, and a nudge to try the graph before sleeping
    "V1": "Before you sleep, give your body a behavior graph with the behavior tool, so it handles what you expect "
          "without you. Read behavior(action='help') first. Facts the world knows exactly (who arrived or left, who "
          "spoke to you, a word said, the hour) are checks, which are free and exact; only meaning (what someone "
          "wants, whether it is a threat, whether it is news) is an ask. Put checks before asks. Keep every ask "
          "narrow. Whatever you did not plan for should wake you. Then call sleep.",
    # V2: plus trying the graph on the moments you expect before you sleep
    "V2": "Before you sleep, give your body a behavior graph with the behavior tool, so it handles what you expect "
          "without you. Read behavior(action='help') first. Facts the world knows exactly (who arrived or left, who "
          "spoke to you, a word said, the hour) are checks, which are free and exact; only meaning (what someone "
          "wants, whether it is a threat, whether it is news) is an ask. Put checks before asks. Keep every ask "
          "narrow. Whatever you did not plan for should wake you. When the graph is set, try it with "
          "behavior(action='run', moment=[...]) on two or three moments you expect, one of which should wake you, "
          "and fix what it gets wrong. Then call sleep.",
}

SLEEP = {"type": "function", "function": {"name": "sleep", "description": (
    "Sleep. Your body runs its behavior graph each time your watch fires (until=) or the minutes run out."),
    "parameters": {"type": "object", "properties": {
        "minutes": {"type": "number"},
        "until": {"type": "string", "description": "a watch expression, e.g. \"to_me() or arrived('')\"; "
                                                   "leave out to wake the body on anything here"},
        "waiting_for": {"type": "string", "description": "what you are waiting for and why, in words"}},
        "required": ["minutes", "waiting_for"]}}}


def tools(ops: dict[str, Any]) -> list[dict[str, Any]]:
    s = bv.spec(ops)
    return [{"type": "function", "function": {"name": s["name"], "description": s["description"],
                                              "parameters": s["parameters"]}}, SLEEP]


def bundle(sc: dict[str, Any], time_of_day: str, present: list[str]) -> dict[str, Any]:
    return {"you": {"name": sc["you"]}, "here": {"name": sc["place"]}, "ambient": {"time_of_day": time_of_day},
            "present": [{"name": p} for p in present], "goals": [{"text": x} for x in sc["goals"]]}


def wake_message(sc: dict[str, Any], world: str) -> str:
    out = [f"You are {sc['who']}, in {world}.", f"Where: {sc['place']}. Time of day: {sc['time']}.",
           f"Here with you: {', '.join(sc['present']) or 'no one'}.", "Your goals: " + "; ".join(sc["goals"]) + ".",
           f"Now: {sc['situation']}"]
    if sc.get("start_graph"):
        out.append("Your body's behavior graph now:\n" + bv.show(sc["start_graph"]))
    out.append(f"What you mean to do: {sc['intent']}")
    return "\n".join(out)


class Spend:
    def __init__(self, cap: float):
        self.cap, self.author, self.judge = cap, 0.0, 0.0

    @property
    def total(self) -> float:
        return self.author + self.judge

    def check(self) -> None:
        if self.total > self.cap:
            raise SystemExit(f"spend cap reached: ${self.total:.4f} > ${self.cap:.2f}; stopping")


def make_judge(j: rx.Judge, client: httpx.AsyncClient, spend: Spend, log: list[dict[str, Any]]) -> bv.Judge:
    async def judge(st: dict[str, Any], qs: dict[str, Any]) -> tuple[dict[str, Any], float]:
        raw, cost, secs = await j.answers(client, st, qs)
        spend.judge += cost
        log.append({"qs": {k: q["instructions"][:160] for k, q in qs.items()}, "raw": raw, "cost": cost,
                    "secs": round(secs, 2)})
        return bv.parse(raw), cost
    return judge


async def author(m: Model, client: httpx.AsyncClient, sc: dict[str, Any], data: dict[str, Any], variant: str,
                 judge: bv.Judge, spend: Spend, max_steps: int) -> dict[str, Any]:
    """The model under test writes the graph. Returns what it set, how it got there, and its transcript."""
    ops = data["ops"]
    g = copy.deepcopy(sc.get("start_graph")) if sc.get("start_graph") else None
    sys_prompt = f"You are the mind of a character in a story world. {VARIANTS[variant]}"
    msgs: list[dict[str, Any]] = [{"role": "system", "content": sys_prompt},
                                  {"role": "user", "content": wake_message(sc, data["world"])}]
    stats = {"steps": 0, "help": 0, "apply": 0, "apply_errors": 0, "views": 0, "trials": 0, "nudges": 0,
             "bad_calls": 0, "slept": False, "until": None, "waiting_for": "", "minutes": None, "ops_used": [],
             "errors": []}
    me = sc["you"]
    b = bundle(sc, sc["time"], sc["present"])
    for _ in range(max_steps):
        stats["steps"] += 1
        msg, use = await m.chat(client, msgs, tools(ops))
        spend.author += use.cost
        spend.check()
        calls = msg.get("tool_calls") or []
        msgs.append({"role": "assistant", "content": msg.get("content") or "",
                     **({"tool_calls": clean_calls(calls)} if calls else {})})
        if not calls:
            stats["nudges"] += 1
            msgs.append({"role": "user", "content": "Call behavior or sleep."})
            continue
        done = False
        for c in calls:
            fn = c.get("function") or {}
            args, note, err = parse_args(fn.get("arguments"))
            if args is None:
                stats["bad_calls"] += 1
                msgs.append({"role": "tool", "tool_call_id": c.get("id"), "content": err})
                continue
            if note:
                stats["repaired"] = stats.get("repaired", 0) + 1
            if fn.get("name") == "sleep":
                stats.update(slept=True, until=args.get("until"), waiting_for=str(args.get("waiting_for") or ""),
                             minutes=args.get("minutes"))
                msgs.append({"role": "tool", "tool_call_id": c.get("id"), "content": "asleep"})
                done = True
                continue
            if fn.get("name") != "behavior":
                stats["bad_calls"] += 1
                msgs.append({"role": "tool", "tool_call_id": c.get("id"), "content": "no such tool: behavior, sleep"})
                continue
            a = str(args.get("action") or "")
            if a in ("help", "apply", "run"):
                stats[{"help": "help", "apply": "apply", "run": "trials"}[a]] += 1
            elif a == "view":
                stats["views"] += 1
            if a == "apply":
                stats["ops_used"] += [str(o.get("op")) for o in args.get("ops") or [] if isinstance(o, dict)]
            g2, text, _ = await bv.handle(g, args, ops, {}, me, b, judge, now=[], plan=sc["intent"],
                                          waiting_for=stats["waiting_for"])
            if a == "apply":
                if text.startswith("nothing changed"):
                    stats["apply_errors"] += 1
                    stats["errors"].append(text[:400])
                else:
                    g = g2
            msgs.append({"role": "tool", "tool_call_id": c.get("id"), "content": text})
        if done:
            break
    return {"graph": g, "stats": stats, "transcript": msgs}


def present_after(sc: dict[str, Any], moment: list[dict[str, Any]], present: list[str]) -> list[str]:
    p = list(present)
    for m in moment:
        if m.get("arrives") and m["who"] not in p:
            p.append(m["who"])
        if "leaves" in m and m.get("who") in p:
            p.remove(m["who"])
    return p


def matches(r: dict[str, Any], want: dict[str, Any]) -> bool:
    end = "WAKE" if r["end"] == "THINK" else r["end"]
    if "end" in want and want["end"] != end:
        return False
    if "deeds" in want and len(r["deeds"]) != want["deeds"]:
        return False
    keys = [k for k in ("op", "verb", "to", "text_has", "target_has") if k in want]
    if not keys:
        return True
    for d in r["deeds"]:
        a = d["args"]
        ok = True
        for k in keys:
            if k == "op":
                ok &= d["op"] == want[k]
            elif k == "verb":
                ok &= str(a.get("verb") or "").lower() == want[k]
            elif k == "to":
                ok &= want[k].lower() in str(a.get("to") or a.get("target") or "").lower()
            elif k == "text_has":
                ok &= want[k].lower() in str(a.get("text") or "").lower()
            elif k == "target_has":
                ok &= want[k].lower() in str(a.get("target") or "").lower()
        if ok:
            return True
    return False


def miss_kind(r: dict[str, Any], wants: list[dict[str, Any]]) -> str:
    """How a wrong run went wrong: a safe miss costs a mind turn, the others do the wrong thing in the world."""
    end = "WAKE" if r["end"] == "THINK" else r["end"]
    wants_deed = any(k in w for w in wants for k in ("op", "verb", "text_has", "target_has"))
    wants_wake = any(w.get("end") == "WAKE" for w in wants)
    if r["deeds"]:
        return "wrong deed"
    if end == "WAKE":
        return "woke instead of acting" if wants_deed else "woke for nothing"
    return "slept through" if wants_wake or wants_deed else "other"


async def execute(g: dict[str, Any] | None, sc: dict[str, Any], judge: bv.Judge, waiting_for: str
                  ) -> list[dict[str, Any]]:
    rows = []
    for ri, wakes in enumerate(sc["runs"]):
        gg = copy.deepcopy(g) if g else None
        if gg:
            gg.pop("resume", None)
            gg["fired"] = {}
        present = list(sc["present"])
        for wi, w in enumerate(wakes):
            present = present_after(sc, w["events"], present)
            evs = bv.events_from(w["events"], sc["you"])
            b = bundle(sc, w["time"], present)
            if not gg or not gg.get("nodes"):
                r = {"end": "WAKE", "why": "no graph", "path": [], "deeds": [], "resume": None, "asked": 0}
            else:
                v = bv.view(evs, sc["you"], None, b, woke="timeout" if w["trigger"] == "timeout" else "event")
                st = bv.judge_state(sc["you"], b, "", waiting_for, bv.lines_of(evs, sc["you"]))
                r, _ = await bv.run(gg, v, judge, st)
                gg["fired"] = r["fired"]
                if r["resume"]:
                    gg["resume"] = r["resume"]
                else:
                    gg.pop("resume", None)
            ok = any(matches(r, x) for x in w["want"])
            rows.append({"run": ri, "wake": wi, "ok": ok, "miss": "" if ok else miss_kind(r, w["want"]),
                         "end": r["end"], "path": r["path"], "deeds": r["deeds"], "asked": r.get("asked", 0),
                         "why": r.get("why", ""), "want": w["want"]})
    return rows


def graph_shape(g: dict[str, Any] | None, ops: dict[str, Any]) -> dict[str, Any]:
    if not g or not g.get("nodes"):
        return {"nodes": 0}
    kinds: dict[str, int] = {}
    for n in g["nodes"].values():
        kinds[n.get("kind", "?")] = kinds.get(n.get("kind", "?"), 0) + 1
    into: dict[str, int] = {}
    for n in g["nodes"].values():
        for _, t in bv.edges(n):
            into[t] = into.get(t, 0) + 1
    _, warns = bv.problems(g, {}, ops)
    return {"nodes": len(g["nodes"]), "kinds": kinds, "exact_hints": sum("looks like a fact" in w for w in warns),
            "shared": sum(1 for t, c in into.items() if c > 1 and t in g["nodes"]),
            "string_checks": sum(1 for n in g["nodes"].values() if n.get("kind") == "check" and isinstance(n.get("if"), str))}


async def stub(st: dict[str, Any], qs: dict[str, Any]) -> tuple[dict[str, Any], float]:
    return {k: ({"p": 0.9} if q["type"] == "noul" else {"choice": next(iter(q["criteria"])), "p": 0.9})
            for k, q in qs.items()}, 0.0


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--models", required=True)
    ap.add_argument("--levels", default="1,2,3")
    ap.add_argument("--variant", default="V1")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--cap", type=float, default=0.5)
    ap.add_argument("--max-steps", type=int, default=10)
    ap.add_argument("--judge", default=JUDGE)
    ap.add_argument("--stub-judge", action="store_true", help="no judge calls: every yes/no answers p=0.9 (plumbing)")
    a = ap.parse_args()
    data = json.loads((HERE / "scenarios.json").read_text(encoding="utf-8"))
    levels = {int(x) for x in a.levels.split(",")}
    out = Path(a.out)
    (out / "transcripts").mkdir(parents=True, exist_ok=True)
    cfg = {"env_files": [a.env_file], "max_tokens": 4000}
    spend = Spend(a.cap)
    j = None if a.stub_judge else rx.Judge(a.judge, cfg)
    ref = json.loads((HERE / "reference.json").read_text(encoding="utf-8"))
    async with httpx.AsyncClient() as client:
        for spec in a.models.split(","):
            # 'reference' runs the hand-written graphs: the ceiling, so a judge miss is not read as an author's
            m = None if spec == "reference" else Model.from_config(spec, cfg)
            for sc in [s for s in data["scenarios"] if s["level"] in levels]:
                for rep in range(a.reps):
                    jlog: list[dict[str, Any]] = []
                    judge = stub if a.stub_judge else make_judge(j, client, spend, jlog)
                    t0, a0, j0 = time.time(), spend.author, spend.judge
                    if m is None:
                        au = {"graph": ref[sc["id"]], "stats": {"waiting_for": sc["intent"]}, "transcript": []}
                    else:
                        try:
                            au = await author(m, client, sc, data, a.variant, judge, spend, a.max_steps)
                        except ModelError as e:
                            au = {"graph": None, "stats": {"error": str(e)[:300]}, "transcript": []}
                    rows = await execute(au["graph"], sc, judge, au["stats"].get("waiting_for", ""))
                    tag = f"{sc['id']}__{spec.split(':', 1)[-1].replace('/', '_')}__{a.variant}__{rep}"
                    (out / "transcripts" / f"{tag}.json").write_text(json.dumps(
                        {"graph": au["graph"], "transcript": au["transcript"], "judge": jlog}, indent=1),
                        encoding="utf-8")
                    row = {"scenario": sc["id"], "level": sc["level"], "model": spec, "variant": a.variant, "rep": rep,
                           "score": sum(r["ok"] for r in rows), "of": len(rows),
                           "misses": [r["miss"] for r in rows if not r["ok"]], "author": au["stats"],
                           "shape": graph_shape(au["graph"], data["ops"]), "wakes": rows,
                           "cost_author": round(spend.author - a0, 5), "cost_judge": round(spend.judge - j0, 5),
                           "secs": round(time.time() - t0, 1)}
                    with (out / "results.jsonl").open("a", encoding="utf-8") as f:
                        f.write(json.dumps(row) + "\n")
                    st = au["stats"]
                    print(f"{sc['id']:<9} {spec.split(':', 1)[-1]:<32} {a.variant} {row['score']}/{row['of']}  "
                          f"nodes={row['shape'].get('nodes')} {row['shape'].get('kinds', {})} "
                          f"steps={st.get('steps')} applies={st.get('apply')} errs={st.get('apply_errors')} "
                          f"trials={st.get('trials')} slept={st.get('slept')} misses={row['misses']} "
                          f"${row['cost_author'] + row['cost_judge']:.4f}", flush=True)
    print(f"spent ${spend.total:.4f} (author ${spend.author:.4f}, judge ${spend.judge:.4f})")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
