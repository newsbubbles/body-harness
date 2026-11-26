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
