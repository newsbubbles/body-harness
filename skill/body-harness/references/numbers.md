# Numbers

Everything a seat does is counted in its own folder, so any question about spend, context or behaviour can be
answered from files, without reading what characters said.

## Per seat (`$BH_HOME/seats/<name>/`)

| file | one row per | fields worth counting |
|---|---|---|
| `usage.jsonl` | model call (turns and judge alike) | model, input, cached, output and reasoning tokens, cost, seconds, why it woke |
| `ledger.jsonl` | turn | reason, the calls made, watch set, usage, `escalated`, `nudges`, `imagined` |
| `calls.jsonl` | tool call | tool, arguments, what came back (contains speech: count it, do not publish it) |
| `reflexes.jsonl` | judge look | trigger, p per rule, what ran, cost, seconds |
| `invented.jsonl` | made-up tool name | the name and how the harness read it |

`bh usage --since 24h` and `bh status` sum the common ones. For anything else, read the JSON lines and group by the
field you want: tool mix by seat (`calls.jsonl` tool), cost per turn by model (`ledger.jsonl` usage), cache hit rate
(cached over input in `usage.jsonl`), context size over time (input per call).

## Eval runs

The harness's eval suite (`evals/suite.py`) writes `results.jsonl` in its output folder: one row per case, variant
and sample, with pass or fail, tool calls, the largest input, and cost; the run ends by printing the pass table. Keep the folders: they are the history of what each harness version did on the same cases.

## Telemetry

A seat can post these counts to a telemetry server you run (body-telemetry), for dashboards across seats. It is off
until the config sets `telemetry: {url, key_env, detail}`; `detail` is `counts` by default, which sends numbers and
names but no speech, notes or reasons. What each level sends, and the wire format, are in the harness's
`docs/telemetry.md`. Set it in `$BH_HOME/config.json` for every seat, restart the seats, and check that the server's
dashboard shows them. The files above stay the record either way.
