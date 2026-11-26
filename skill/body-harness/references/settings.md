# Settings profiles

`bh config <seat> key=value ...` sets a seat's settings; `$BH_HOME/config.json` sets defaults for every seat. A seat reads
its settings when it starts, so restart it after a change (restart the supervisor: `bh service stop`, then
`bh service run`); spend caps are the exception and count at once. The full list is in the harness's `docs/configuration.md`; these are the profiles
that have been measured, smallest first.

## Baseline

```
bh config <seat> model=openrouter:deepseek/deepseek-v4-flash
```

The scene card, ten tools, the harness's guards. Cheap (well under a cent a turn with the cache warm). Every other
profile adds to this one.

## The menu stack

```
bh config <seat> menu=true menu_rank=true menu_preopen=true menu_lists=true
```

The wake as numbered lists, ranked by what the seat's goals and the line said to it name, with the older talk with
the speaker and the body's own deeds on the wake. On the eval suite (cases from real reports, flash) it took the
pass rate from about 72% to about 84%, and in a busy scene from 54% to 85%. It helps most where a seat must remember
an old agreement, deny a deed it never did, or keep its facts straight in a crowd. Each layer can be turned on alone;
`menu=true` without the others did no better than the card.

Off: `bh config <seat> menu=false`.

## Escalation

```
bh config <seat> escalate='["openrouter:<stronger model>"]'
```

One turn moves to the next model on the list after `escalate_after` (2) wrong calls, or `escalate_reads` (8) reads
with no deed. The next turn starts on `model` again. Each escalation is in the seat's `ledger.jsonl` as `escalated`;
a seat that escalates most turns needs a look, not a stronger baseline.

## Reflexes

```
bh config <seat> reflexes=true judge=openrouter:typesafe/jev-1.14
```

`sleep` takes up to 5 spoken rules and a `waiting_for=`. When the watch fires, a small judge answers one yes-or-no
question per rule; a rule runs at p 0.8 or more, and anything unsure or uncovered wakes the mind. A rule may say a
line written in advance, move or rest, keep sleeping, or wake; coin, giving, fighting and promises always wake the
mind. It saves model turns on small things (greetings, someone passing). Judge looks cost a fraction of a cent.
`bh reflexes <seat>` shows the rules set now and what each look did.

Off: `bh config <seat> reflexes=false`.

## Spend caps

```
bh cap <seat> 0.50              $0.50 per 24 h
bh cap --home-pool 3.00         one pool for every seat in the home
bh cap <seat> always            never paused
```

Nothing is capped until someone sets it, and which seats matter is the person's call, not yours. A paused seat sleeps
on its watch and says so once in its outbox.

## Others worth knowing

- `past=true`: a search over the body's own deeds and what came of each.
- `imagine=true`: a private small world to try a plan in first (needs the `imagine` extra).
- `extra_prompt`: your own words appended to the seat's instructions. Use it for the person's standing wishes, not
  to correct a harness failure (see "When an NPC misbehaves" in the skill).
- `max_minutes`, `default_minutes`: how long a seat may sleep. Short sleeps cost more turns.
