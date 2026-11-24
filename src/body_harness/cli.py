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
    if s.exists and not args.force:
        print(f"seat {args.name} already exists (pass --force to take a new invite into it)", file=sys.stderr)
        return 2
    s.create()
    async with httpx.AsyncClient() as client:
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
        j = await door.op("join", entity=args.entity) if args.entity else await door.op("join")
        if isinstance(j, dict) and j.get("ok") is False:
            print(f"saved the seat, but join was refused: {j.get('reason')}", file=sys.stderr)
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


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="bh", description="Run minds and players in embody worlds.")
    ap.add_argument("--home", help="where seats live (default $BH_HOME or ~/.body-harness)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("join", help="take an invite into a new seat")
    c.add_argument("invite", help="the invite link, or a file holding the link or the page")
    c.add_argument("--name", required=True, help="the seat's name here (lowercase)")
    c.add_argument("--model", default="", help="provider:model, for example openrouter:deepseek/deepseek-chat-v3.1")
    c.add_argument("--entity", help="which body, when the grant names several")
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
    c = sub.add_parser("usage", help="spend by seat and model")
    c.add_argument("--since", default="", help="30m, 24h, 7d")
    c = sub.add_parser("config", help="show or set a seat's settings: key=value (JSON values work)")
    c.add_argument("name")
    c.add_argument("pairs", nargs="*")
    args = ap.parse_args(argv)
    h = seats.home(args.home)

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
