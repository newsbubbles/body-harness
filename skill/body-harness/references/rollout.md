# Rolling out a harness change

A new harness version or feature reaches running seats only when their operator moves them. Do it one step at a
time, so a bad change shows on one seat and is easy to take back.

## Steps

1. **Read what changed.** The commit messages and `docs/` say what a feature does, which setting turns it on, and
   what it was measured on. A feature that is not measured yet is not ready for live seats.
2. **Update the harness** where the seats run
   (`uv tool install --force git+https://github.com/newsbubbles/body-harness`, or a pull of your checkout), then
   restart the supervisor so seats load the new code (`bh service stop`, then `bh service run`, or restart the
   scheduled task). Seats keep their state across a restart.
3. **One seat first.** Turn the feature on for one seat that players meet often, and restart it. Write down the
   setting that turns it off.
4. **Watch a day.** Compare its turns with the day before: spend per turn, escalations, nudges, refusals, and above
   all what players got from it (`bh log`). A wake that got longer costs every turn.
5. **The rest.** Turn it on for the other seats, or in `$BH_HOME/config.json` for all. Tell the person what changed
   in one or two lines.
6. **If it goes wrong**, set the feature back to `false` for the seats it hurts, and report what you saw
   ([watching.md](watching.md)). Restart the seats so they read the change.

## Per feature

| feature | turn on | watch for | turn off |
|---|---|---|---|
| menu stack | `menu=true menu_rank=true menu_preopen=true menu_lists=true` | seats answering from their log (old agreements, own deeds); wake size in `usage.jsonl` | `menu=false` |
| reflexes | `reflexes=true judge=...` | `bh reflexes <seat>`: rules that fired wrongly; turns saved | `reflexes=false` |
| escalation | `escalate='["p:m"]'` | how often `escalated` shows in `ledger.jsonl`, and its cost | `escalate='[]'` |

The world's engine is upgraded by the world's host, not by you. A harness feature that uses a newer engine op checks
for it and falls back on an older engine, so the two need not move together.
