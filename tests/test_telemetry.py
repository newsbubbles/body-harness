import asyncio
import json

import httpx

from body_harness import telemetry


def test_off_without_a_url_and_nothing_spooled(tmp_path):
    s = telemetry.Sender(tmp_path / "t.jsonl", {})
    s.emit("turn", {"turn": 1})
    assert not s.on and not (tmp_path / "t.jsonl").exists()


def test_counts_sends_no_words_and_lines_cuts_them(tmp_path):
    cfg = {"telemetry": {"url": "http://127.0.0.1:1"}, "menu": True, "extra_prompt": "secret words"}
    s = telemetry.Sender(tmp_path / "t.jsonl", cfg, {"seat": "fen"})
    s.emit("turn", {"turn": 1}, {"note": "Brann owes me two coin"})
    row = json.loads((tmp_path / "t.jsonl").read_text(encoding="utf-8"))
    assert "note" not in row and row["switches"]["menu"] and row["seat"] == "fen"
    assert "secret" not in json.dumps(row)
    cfg["telemetry"]["detail"] = "lines"
    s = telemetry.Sender(tmp_path / "u.jsonl", cfg)
    s.emit("turn", {"turn": 1}, {"note": "x" * 300})
    assert len(json.loads((tmp_path / "u.jsonl").read_text(encoding="utf-8"))["note"]) == 120


def test_outcome_reads_refusals_and_their_codes():
    assert telemetry.outcome('{"ok": true, "t": 3}') == (True, None, None)
    assert telemetry.outcome('{"ok": false, "reason": "no such verb", "code": "no_verb"}') == (
        False, "no_verb", "no such verb")
    assert telemetry.outcome('(harness: x)\n{"refused": "list 9 has 0 items"}')[0] is False


def test_flush_posts_batches_and_keeps_what_the_server_did_not_take(tmp_path):
    got = []

    def handler(req: httpx.Request) -> httpx.Response:
        if not got:
            got.append(json.loads(req.content)["events"])
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(503)

    s = telemetry.Sender(tmp_path / "t.jsonl", {"telemetry": {"url": "http://t"}})
    for i in range(telemetry.BATCH + 10):
        s.emit("tool_call", {"i": i})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await s.flush(c)

    assert asyncio.run(go()) == telemetry.BATCH
    left = (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(got[0]) == telemetry.BATCH and len(left) == 10 and json.loads(left[0])["i"] == telemetry.BATCH


def test_setup_keeps_only_safe_settings_and_nudges_go_by_kind():
    cfg = {"model": "p:m", "menu": True, "max_steps": 16, "extra_prompt": "words", "api_key_env": "X",
           "providers": {"a": 1}, "escalate": ["p:n"], "default_until": "to_me()"}
    assert telemetry.safe_setup(cfg) == {"model": "p:m", "menu": True, "max_steps": 16, "escalate": ["p:n"]}
    assert telemetry.kinds(["stuck: 'salt road' in 5 notes", "last_step: step 16", "Brann waits"]) == [
        "stuck", "last_step", "other"]


def test_context_counts_each_part_of_a_call():
    msgs = [{"role": "system", "content": "abc"}, {"role": "user", "content": "wake!"},
            {"role": "assistant", "content": "", "tool_calls": []}, {"role": "tool", "content": "xy"}]
    c = telemetry.context(msgs, [{"t": 1}])
    assert (c["system"], c["wake"], c["tool_results"]) == (3, 5, 2) and c["tools"] > 0 and c["history"] == 2


def test_a_batch_the_server_never_takes_is_sent_one_by_one_and_the_refused_dropped(tmp_path):
    seen = []

    def handler(req):
        evs = json.loads(req.content)["events"]
        if len(evs) > 1:
            return httpx.Response(400)
        seen.append(evs[0]["i"])
        return httpx.Response(422 if evs[0]["i"] == 1 else 200)

    s = telemetry.Sender(tmp_path / "t.jsonl", {"telemetry": {"url": "http://t"}})
    for i in range(3):
        s.emit("tool_call", {"i": i})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await s.flush(c)

    asyncio.run(go())
    assert seen == [0, 1, 2] and (tmp_path / "t.jsonl").read_text(encoding="utf-8") == ""


def test_tool_calls_carry_their_step_and_order_in_the_turn(tmp_path, monkeypatch):
    # Glass Reach host, 27 Nov: step was null on every tool_call event, so a turn's calls could not be put in order
    import sys
    from pathlib import Path

    import pytest
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    suite = pytest.importorskip("suite")
    from embody.openapi import spec

    from body_harness.loop import Mind
    from body_harness.seat import Seat
    from body_harness.usage import Usage
    from body_harness.seat import write_json
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    seat = Seat(tmp_path, "fen").create()
    write_json(seat.p("door.json"), {"base": suite.LocalDoor.base, "token": ""})
    write_json(seat.p("config.json"), {"model": "openrouter:a/cheap", "telemetry": {"url": "http://t"}})
    suite.seat_state(seat, turns=40, read_handbook=True)
    w = suite.World(tmp_path / "world.db")
    w.settle()
    script = [[("say", {"text": "Morning."}), ("memo", {"text": "said morning"})],
              [("sleep", {"until": "to_me()", "note": "greeted the room"})]]

    async def chat(client, messages, tools, choice=None):
        calls = script.pop(0) if script else [("sleep", {"until": "to_me()", "note": "done"})]
        return {"content": "", "tool_calls": [{"id": f"c{i}", "type": "function", "function": {
            "name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]}, Usage()

    async def go():
        async with httpx.AsyncClient() as c:
            m = Mind(seat, c)
            m.door = suite.LocalDoor(w.fen, spec([{"name": "say", "description": "Speak.", "input_schema": {
                "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}], role="mind"))
            await m.start()
            m.model.chat = chat
            await m.turn("someone spoke", [])
            m.tele._task.cancel()

    asyncio.run(go())
    rows = [json.loads(x) for x in seat.p("telemetry.jsonl").read_text(encoding="utf-8").splitlines()]
    tc = [r for r in rows if r["kind"] == "tool_call"]
    assert [(r["tool"], r["step"], r["call"]) for r in tc] == [("say", 0, 0), ("memo", 0, 1), ("sleep", 1, 2)]
    assert [r["woke"] for r in rows if r["kind"] == "turn"] == ["event"]


def test_a_turn_says_what_woke_it_in_counts():
    from body_harness import scene
    from body_harness.loop import woke
    n = scene.Names({"e1": "Ossa", "e4": "Wick", "e5": "Tam"})
    said = {"kind": "say", "actor": "e4", "payload": {"text": "Ossa, the cart", "to": "e1"}}
    assert woke("what you waited for happened (to_me())", [said], "e1", "Ossa", n) == {
        "woke": "spoken_to", "woke_by": "say", "woke_seat": 1, "woke_actor": "Wick"}
    walk = {"kind": "arrived", "actor": "e5", "by_player": True}
    assert woke("what you waited for happened (arrived())", [walk], "e1", "Ossa", n)["woke_seat"] == 0
    assert woke("time is up on your watch ('x' did not happen)", [], "e1", "Ossa", n)["woke"] == "timeout"
    assert woke("your operator wrote to you (see the agenda)", [said], "e1", "Ossa", n)["woke_by"] == ""
    assert woke("what you waited for happened (x)", [walk], "e1", "Ossa", n, reflexed=True)["woke"] == "reflex"
