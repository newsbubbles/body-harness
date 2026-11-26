from body_harness import menu, scene


def bundle():
    return {
        "t": 3600, "you": {"id": "e1", "name": "Fen", "type": "folk",
                           "state": {"hp": 4, "max_hp": 6, "memory": [{"text": "the lantern is Ossa's"}]
                                     + [{"text": f"note {i}"} for i in range(10)]}},
        "here": {"id": "p1", "name": "The Lamp"},
        "present": [{"id": "e2", "type": "folk", "name": "Brann", "state": {}, "condition": "fine"},
                    {"id": "e3", "type": "lantern", "name": "brass lantern", "state": {}},
                    {"id": "e4", "type": "folk", "name": "Ossa", "state": {}, "condition": "fine"}],
        "holding": [{"id": "e5", "type": "satchel", "name": "Pouch of salve", "state": {}}],
        "exits": [{"to": "p2", "name": "Quay"}],
        "knows": [{"who": "e2", "count": 3, "standing": 1, "met_t": 0, "last_t": 100},
                  {"who": "e9", "count": 9, "standing": 0, "met_t": 0, "last_t": 50}],
        "relations": [{"relation": "owes", "direction": "out", "with": "e4", "name": "Ossa", "data": {"coin": 2}}],
        "goals": [{"id": "g1", "text": "find salve for Brann", "status": "active"}],
        "affordances": [{"verb": "go", "target_name": "Quay", "ok": True},
                        {"verb": "tend", "target_name": "Brann", "ok": False, "reason": "nothing to tend with"}],
        "events": [{"seq": 7, "t": 3500, "kind": "say", "actor": "e2", "target": "e1",
                    "payload": {"text": "Ossa says you have salve?", "to": "e1"}}],
    }


def test_the_wake_is_numbered_lists_in_engine_terms_with_world_labels():
    m = menu.Menu(bundle(), scene.Names(), "e1")
    text = m.render()
    assert "1 The Lamp (2 folk, 1 lantern) (3):" in text and "[lantern]" in text
    assert "2.1 Pouch of salve" in text and "3.1 Quay" in text and "owes" in text
    assert "brass lantern [lantern]" in text and "no meeting on record" in text.split("Ossa", 1)[1].split("\n")[0]
    assert "5+ 3 more" in text  # 11 notes, 8 shown
    assert 'menu(open="1.2")' in text


def test_items_open_by_id_through_read_ops_and_lists_page():
    m = menu.Menu(bundle(), scene.Names(), "e1")
    assert m.resolve("2.1") == ("op", "inspect", {"entity": "e5"})
    assert m.resolve("1.1")[:2] == ("op", "recall")
    more = m.resolve("5")
    assert more[0] == "text" and "5.11 the lantern is Ossa's" in more[1]  # newest first: the oldest last and "that is all" in more[1]
    assert "lantern" in m.resolve("5", find="lantern")[1] and "note 3" not in m.resolve("5", find="lantern")[1]
    assert "nothing to tend with" in m.resolve("7")[1]  # the verbs' more: what you cannot do, and why
    assert m.resolve("8")[:2] == ("op", "history")  # said to you: older talk from the log
    assert m.resolve("1.9")[0] == "error" and m.resolve("42")[0] == "error"
    assert m.resolve("4", by="nonsense")[0] == "error"


def test_rank_puts_what_goals_notes_and_lines_name_first():
    plain = menu.Menu(bundle(), scene.Names(), "e1")
    ranked = menu.Menu(bundle(), scene.Names(), "e1", rank=True)
    assert plain.widgets[4].items[0].line == "note 9"  # newest first
    assert ranked.widgets[4].items[0].line == "the lantern is Ossa's"  # the oldest note, but the line names Ossa
    assert ranked.widgets[0].items[0].eid in ("e2", "e4")  # Brann spoke and is in the goal; Ossa is named in the line


def test_preopen_opens_the_speaker_who_is_named_and_what_a_goal_needs():
    got = dict(menu.Menu(bundle(), scene.Names(), "e1").preopen())
    labels = " | ".join(got)
    assert "Brann" in labels and "Ossa" in labels and "Pouch of salve" in labels
    assert "your older talk with Brann" in labels and "your own deeds in the world's log" in labels
    talk = got["your older talk with Brann"]
    assert talk[:2] == ("op", "history") and talk[3]["with"] == "e2"


def test_pick_keeps_talk_with_one_other_and_a_bodys_own_deeds():
    evs = [{"kind": "say", "actor": "e2", "payload": {"to": "e1"}}, {"kind": "say", "actor": "e4", "payload": {"to": "e1"}},
           {"kind": "say", "actor": "e1", "payload": {"to": "e2"}}, {"kind": "tended", "actor": "e1", "target": "e2"},
           {"kind": "remembered", "actor": "e1"}, {"kind": "moved", "actor": "e4"}]
    assert [e["actor"] for e in menu.Menu.pick(evs, {"with": "e2"})] == ["e2", "e1", "e1"]
    assert [e["kind"] for e in menu.Menu.pick(evs, {"deeds_of": "e1"})] == ["tended"]
