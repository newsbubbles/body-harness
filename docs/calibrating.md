# Calibrating models

Is it the model, the harness or the world? `bh calibrate` holds two of them still. A seat keeps its last few wakes
as they arrived (the scene, the events, its state). Calibrate replays one of them for each model on a copy of the
seat under `$BH_HOME/calibrate/`, with the world's writes cut off: perceive answers with the scene the seat saw, reads
that leave no trace (handbook, guide, inspect, find, recall, history, timers) go to the world, and every deed gets a
plain ok. Nothing is said or done in the world, and the live seat's "since I last looked" does not move. Each copy's
own ledger then says how the model played the same moment: tool calls, made-up tool names, bad arguments, refusals,
harness nudges, whether it answered someone who spoke to it, whether it slept on its own, its note, cost and time.

```bash
bh calibrate sefa --models openrouter:deepseek/deepseek-v4-flash,openrouter:openai/gpt-6-luna --runs 2
bh calibrate sefa --sets models.toml --set cheap --wake -2
bh calibrate sefa --sets models.toml --set cheap --cfg off:think_tool=false --cfg on:think_tool=true
```

```toml
# models.toml: named lists, for --sets models.toml --set cheap
[sets]
cheap = ["openrouter:deepseek/deepseek-v4.1-flash", "openrouter:openai/gpt-6-luna"]
other = ["openrouter:z-ai/glm-5.3-flashx", "openrouter:xiaomi/mimo-v2.6-flash"]
```

Models are not deterministic: use `--runs 2` or more before reading much into one row. To hold the model still and
change the harness instead, give variants: `--cfg off:think_tool=false --cfg on:think_tool=true` runs every model
under each. `--list` shows the saved wakes; `--json` prints every row.

Calibration answers every deed with a plain ok, so it cannot tell whether an imagined plan came true. Measure
imagination in a live world (a local one is fine): see [imagination.md](imagination.md).
