# Watching seats

## Every day

1. `bh status`: a seat with no turn for far longer than its watch, or with unread reports, needs a look.
2. `bh outbox <seat>` for each seat with reports. A seat uses `tell(to='operator')` when it is stuck, needs a person,
   or has something only you can do. Answer with `bh tell <seat> "..."`.
3. `bh usage --since 24h`: spend by seat and model. A seat costing far more than its neighbours is usually waking too
   often (a wide watch) or escalating.

## Signs in the logs

| where | what to look for | what it usually means |
|---|---|---|
| `ledger.jsonl` `escalated` | on most turns | the baseline model keeps failing one thing: find which calls in `calls.jsonl` |
| `ledger.jsonl` `nudges` | the same nudge turn after turn | the seat ignores a guard: a harness case to report |
| `ledger.jsonl` `note` | the same two words in many notes | a lead going nowhere; the harness already tells the seat to ask the owner |
| `ledger.jsonl` `reason` | "the turn ran out of steps" | the seat looped on reads; check what it was trying to find |
| `invented.jsonl` | many lines | the model guesses tool names; a different model or the menu stack |
| `calls.jsonl` refusals | the same refusal repeated | the seat does not read the world's answer; quote it when you report |
| `reflexes.jsonl` | rules that fire on the wrong moment | a rule's `if=` too wide; the seat writes new ones each sleep |

`bh log <seat> --full` shows a turn with everything the world answered. That and the turn number are what a report
needs.

## Reporting a harness failure

Give the harness's maintainers: the seat's model and settings (`bh config <seat>`), the turn numbers, what the seat
did, what it should have done, and where the truth was (on the wake, in the world's log, nowhere). Leave out tokens,
the character's private notes and anything a player said that is not needed to show the failure. A failure that
comes back is turned into an eval case, so the next harness version is measured on it.
