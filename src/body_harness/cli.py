"""bh: run minds and players in embody worlds.

    bh join <invite link | page file> --name sefa --model openrouter:deepseek/deepseek-chat-v3.1
    bh run sefa nico ossa          several seats, one process
    bh run --all
    bh status                      every seat: role, model, last turn, watch, spend, unread reports
    bh tell sefa "the reeve wants a word at dusk"
    bh outbox sefa                 what the seat told you, outside the fiction
    bh log sefa -n 5               its last turns
    bh usage [--since 24h]         spend by seat and model
    bh config sefa model=ollama:qwen3:8b max_steps=10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

from . import seat as seats
from .door import Door, open_invite
from .usage import fold


def _since(text: str) -> float:
    if not text:
        return 0.0
    n, unit = float(text[:-1]), text[-1]
    return time.time() - n * {"m": 60, "h": 3600, "d": 86400}.get(unit, 3600)


async def _join(args: argparse.Namespace, h: Path) -> int:
    s = seats.Seat(h, args.name)
    # a seat that already has a door joins again through it (an invite link opens once): to pick a character
    # after a refusal, for instance
    again = s.exists and s.p("door.json").exists() and not args.force
    if s.exists and not again and not args.force:
        print(f"seat {args.name} already exists (pass --force to take a new invite into it)", file=sys.stderr)
        return 2
    s.create()
    async with httpx.AsyncClient() as client:
        if again:
            door = Door.load(s.p("door.json"), client)
            print(f"seat {args.name} exists: joining again through its saved door")
        else:
            base, token, page = await open_invite(client, args.invite)
            door = Door(base, token, client)
            door.save(s.p("door.json"))
            s.p("invite.md").write_text(page, encoding="utf-8")
        cfg = seats.read_json(s.p("config.json"), {})
        cfg.update({"model": args.model or cfg.get("model") or seats.read_json(h / "config.json", {}).get("model", ""),
                    "joined": time.strftime("%Y-%m-%d %H:%M")})
        if args.entity:
            cfg["entity"] = args.entity
        if args.env_file:
            cfg["env_files"] = [str(Path(f).resolve()) for f in args.env_file]
        seats.write_json(s.p("config.json"), cfg)
        made = {"template": args.template, "name": args.char_name, "description": args.description}
        j = await door.op("join", entity=args.entity, **{k: v for k, v in made.items() if v})
        if isinstance(j, dict) and j.get("ok") is False:
            print(f"saved the seat, but join was refused: {j.get('reason')}", file=sys.stderr)
            if j.get("hint"):
                print(f"  hint: {j['hint']}", file=sys.stderr)
            o = await door.op("seats")
            if isinstance(o, dict) and (o.get("seats") or o.get("templates")):
                for x in o.get("seats") or []:
                    print(f"  seat {x.get('name')!r} at {x.get('place')}: {'open' if x.get('open') else 'taken'}",
                          file=sys.stderr)
                for x in o.get("templates") or []:
                    print(f"  template {x.get('template')!r}: {x.get('doc')}", file=sys.stderr)
                print(f"  then: bh join - --name {args.name} --entity NAME, or --template T --as NAME "
                      f'--description "..."', file=sys.stderr)
            return 1
        b = j.get("briefing") or {}
        print(f"seat {args.name}: {b.get('you') or (j.get('you') or {}).get('name')} in {b.get('world')} "
              f"({b.get('role')}); model {cfg['model'] or '(none yet: bh config ' + args.name + ' model=...)'}")
        print(f"run it: bh run {args.name}")
    return 0


def _status(h: Path) -> int:
    for s in seats.all_seats(h):
        cfg, st = s.config(), s.state()
        led = seats.read_jsonl(s.p("ledger.jsonl"))
        last = led[-1] if led else {}
        u = fold([s.p("usage.jsonl")]).get(s.name)
        unread = [m for m in seats.read_jsonl(s.p("outbox.jsonl")) if m.get("ts", 0) > st.get("outbox_read", 0)]
        b = st.get("briefing") or {}
        print(f"{s.name:14} {b.get('you', '?'):18} {b.get('role', '?'):7} {cfg.get('model', '?')}")
        if last:
            print(f"{'':14} last turn {time.strftime('%m-%d %H:%M', time.localtime(last['ts']))}: "
                  f"{str(last.get('note', ''))[:110]}")
        if st.get("watch"):
            left = (st["watch"]["deadline"] - time.time()) / 60
            print(f"{'':14} watching {st['watch']['until']!r} ({left:.0f} min left)")
        if u:
            print(f"{'':14} {u.line()}")
        if unread:
            print(f"{'':14} {len(unread)} unread report(s): bh outbox {s.name}")
    return 0


def _calibrate(args: argparse.Namespace, s: seats.Seat) -> int:
    from . import calibrate as cal
    if args.list:
        for i, w in enumerate(cal.wakes(s)):
            print(f"{i - len(cal.wakes(s))}: {time.strftime('%m-%d %H:%M', time.localtime(w.get('ts', 0)))} "
                  f"turn {w.get('turn')}: {str(w.get('reason'))[:100]}")
        return 0
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if args.sets:
        sets = cal.model_sets(Path(args.sets))
        for name in args.set_names or list(sets):
            models += sets.get(name, [])
    if not models:
        print("name the models: --models a,b or --sets file.toml [--set name]", file=sys.stderr)
        return 2
    try:
        variants: dict[str, dict] = {}
        for v in args.cfg:
            label, _, pairs = v.partition(":")
            cfg = variants.setdefault(label, {})
            for pair in filter(None, pairs.split(",")):
                k, _, val = pair.partition("=")
                try:
                    cfg[k] = json.loads(val)
                except ValueError:
                    cfg[k] = val
        rows = asyncio.run(cal.calibrate(s, list(dict.fromkeys(models)), args.wake, args.runs,
                                         variants=variants or None))
    except (ValueError, IndexError) as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(rows, indent=1, ensure_ascii=False) if args.json else cal.table(rows))
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="bh", description="Run minds and players in embody worlds.")
    ap.add_argument("--home", help="where seats live (default $BH_HOME or ~/.body-harness)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("join", help="take an invite into a new seat")
    c.add_argument("invite", help="the invite link, or a file holding the link or the page")
    c.add_argument("--name", required=True, help="the seat's name here (lowercase)")
    c.add_argument("--model", default="", help="provider:model, for example openrouter:deepseek/deepseek-chat-v3.1")
    c.add_argument("--entity", help="which body, when the grant names several or the world has open seats")
    c.add_argument("--template", help="make a new character from one of the world's templates")
    c.add_argument("--as", dest="char_name", help="the new character's name (with --template)")
    c.add_argument("--description", help="how the new character looks to others (with --template; set once)")
    c.add_argument("--env-file", action="append", help="a .env file to read the provider key from (never printed)")
    c.add_argument("--force", action="store_true")
    c = sub.add_parser("run", help="run seats until stopped")
    c.add_argument("names", nargs="*")
    c.add_argument("--all", action="store_true")
    sub.add_parser("status", help="every seat at a glance")
    sub.add_parser("seats", help="list seat names")
    c = sub.add_parser("tell", help="a message to a seat from you, its operator")
    c.add_argument("name")
    c.add_argument("text")
    c = sub.add_parser("outbox", help="what a seat told you")
    c.add_argument("name")
    c.add_argument("-n", type=int, default=20)
    c = sub.add_parser("log", help="a seat's last turns")
    c.add_argument("name")
    c.add_argument("-n", type=int, default=5)
    c.add_argument("--calls", action="store_true", help="show each tool call")
    c.add_argument("--full", action="store_true", help="each tool call with everything the world answered")
    c = sub.add_parser("usage", help="spend by seat and model")
    c.add_argument("--since", default="", help="30m, 24h, 7d")
    c = sub.add_parser("config", help="show or set a seat's settings: key=value (JSON values work)")
    c.add_argument("name")
    c.add_argument("pairs", nargs="*")
    c = sub.add_parser("calibrate", help="replay one of a seat's wakes through several models, offline, scored")
    c.add_argument("name")
    c.add_argument("--models", default="", help="provider:model,provider:model,...")
    c.add_argument("--sets", help="a .toml or .json file of named model lists")
    c.add_argument("--set", dest="set_names", action="append", help="which named list (default: every one)")
    c.add_argument("--wake", type=int, default=-1, help="which saved wake, -1 the last (--list shows them)")
    c.add_argument("--runs", type=int, default=1, help="runs per model (models are not deterministic)")
    c.add_argument("--list", action="store_true", help="list the saved wakes")
    c.add_argument("--cfg", action="append", default=[],
                   help="a harness variant: label:key=value[,key=value] (repeat to compare; JSON values work)")
    c.add_argument("--json", action="store_true")
    c = sub.add_parser("past-import", help="copy the body's own deeds from the world's history, so past() reaches "
                                           "back before this harness ran it")
    c.add_argument("name")
    c = sub.add_parser("service", help="keep seats running, restarted when they stop and after a reboot")
    c.add_argument("action", choices=["add", "remove", "status", "run", "stop", "install", "uninstall"])
    c.add_argument("names", nargs="*")
    c.add_argument("--log", help="with add: where the seat's output goes (default: the service's logs folder)")
    args = ap.parse_args(argv)
    h = seats.home(args.home)

    if args.cmd == "service":
        from .service import main as service
        return service(h, args.action, args.names, args.log)

    if args.cmd == "join":
        return asyncio.run(_join(args, h))
    if args.cmd == "run":
        chosen = seats.all_seats(h) if args.all else [seats.Seat(h, n) for n in args.names]
        missing = [s.name for s in chosen if not s.exists]
        if missing or not chosen:
            print(f"no such seat: {', '.join(missing) or '(none named)'}; bh seats lists them", file=sys.stderr)
            return 2
        from .loop import run_seats
        try:
            asyncio.run(run_seats(chosen))
        except KeyboardInterrupt:
            print("stopped")
        return 0
    if args.cmd == "status":
        return _status(h)
    if args.cmd == "seats":
        for s in seats.all_seats(h):
            print(s.name)
        return 0
    if args.cmd == "usage":
        rows = fold([x.p("usage.jsonl") for x in seats.all_seats(h)], _since(args.since))
        for name, u in sorted(rows.items(), key=lambda kv: kv[0] != "all"):
            print(f"{name:14} {u.line()}")
            for m, row in u.by_model.items():
                if name == "all":
                    print(f"{'':14}   {m}: {int(row['calls'])} calls, ${row['cost']:.4f}")
        return 0
    s = seats.Seat(h, args.name)
    if not s.exists:
        print(f"no seat {args.name}", file=sys.stderr)
        return 2
    if args.cmd == "calibrate":
        return _calibrate(args, s)
    if args.cmd == "past-import":
        st = s.state()
        if not st.get("me"):
            print(f"{args.name} has not joined yet (no body known): bh run it once first", file=sys.stderr)
            return 2
        from .past import import_world

        async def go() -> str:
            async with httpx.AsyncClient() as client:
                return await import_world(Door.load(s.p("door.json"), client), st["me"], st.get("names") or {}, s.dir)
        print(f"{args.name}: {asyncio.run(go())}")
        return 0
    if args.cmd == "tell":
        s.tell(args.text)
        print(f"told {args.name}; it wakes for it within a minute or two")
        return 0
    if args.cmd == "outbox":
        rows = seats.read_jsonl(s.p("outbox.jsonl"))[-args.n:]
        for m in rows:
            print(f"{time.strftime('%m-%d %H:%M', time.localtime(m['ts']))} [{m.get('kind')}] {m.get('text')}")
        st = s.state()
        st["outbox_read"] = time.time()
        s.save_state(st)
        return 0
    if args.cmd == "log" and args.full:
        led = seats.read_jsonl(s.p("ledger.jsonl"))[-args.n:]
        turns = {r.get("turn") for r in led}
        since = led[0]["ts"] - 3600 if led else 0
        for c2 in seats.read_jsonl(s.p("calls.jsonl")):
            if c2.get("turn") in turns and c2.get("ts", 0) >= since:
                print(f"--- turn {c2['turn']} {c2['tool']}({json.dumps(c2.get('args'), ensure_ascii=False)})")
                print(c2.get("out", ""))
        return 0
    if args.cmd == "log":
        for r in seats.read_jsonl(s.p("ledger.jsonl"))[-args.n:]:
            u = r.get("usage") or {}
            print(f"{time.strftime('%m-%d %H:%M', time.localtime(r['ts']))} turn {r.get('turn')} t={r.get('t')}: "
                  f"woke because {r.get('reason')}")
            if args.calls:
                for c2 in r.get("calls", []):
                    print(f"    {c2['tool']}({json.dumps(c2.get('args'))[:140]}) -> {c2.get('out', '')[:160]}")
            print(f"    note: {r.get('note')}  | watch {r.get('watch')!r} | "
                  f"{u.get('input_tokens', 0)} in / {u.get('output_tokens', 0)} out, ${u.get('cost', 0):.4f}")
        return 0
    if args.cmd == "config":
        cfg = seats.read_json(s.p("config.json"), {})
        for pair in args.pairs:
            k, _, v = pair.partition("=")
            try:
                cfg[k] = json.loads(v)
            except ValueError:
                cfg[k] = v
        if args.pairs:
            seats.write_json(s.p("config.json"), cfg)
        print(json.dumps(cfg, indent=1))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
