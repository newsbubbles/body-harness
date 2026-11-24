# Imagination

A seat can own a small embody world of its own, in the harness's process, to try a plan before doing it, and to
learn from the difference between what it imagined and what really happened.

```bash
uv tool install "body-harness[imagine] @ git+https://github.com/newsbubbles/body-harness"
bh config sefa imagine=true
```

The `imagine` extra installs the [embody](https://github.com/newsbubbles/embody) engine (pure Python, no
dependencies of its own). Without it the two tools are simply not offered.

## The loop

1. **Rules as the seat believes them.** `imagine(rules={verb: {check, apply}})`: `check` is when it can happen
   (return True or why not), `apply` is what happens. They are embody scripts, a Python subset the engine interprets
   itself: no imports, a step budget, dice from `ctx.rand.roll("1d20")`. Name each rule after the real verb it
   stands for.
2. **What it believes of people.** `setup={name: {hp, ac, atk, gold...}}`. The seat's own body, and every number
   its card shows of the people here, come from the real card and win over belief. Rules and beliefs are kept.
3. **A spread, not a story.** `imagine(plan=[{actor, verb, target}, ...], rounds=N)` builds that world fresh for
   each of 40 trials with a new seed and plays the plan through. The answer is how every number came out (the 10 to
   90% band, the median, counts) and how the plans ended.
4. **The harness binds it.** When the seat really does the first step of a plan it imagined lately, that imagining
   becomes a prediction.
5. **The harness scores it.** When the plan's run ends (a deed outside the plan, or the turn ends), the harness reads
   the seat and the others in the plan with `inspect`, a read that leaves no trace, and scores every number against
   the imagined spread as surprise in bits: -log2 of how often the trials came out at least that far off, with half
   a trial added so a value never imagined is finite. The model never grades itself.
6. **Reflection.** A surprise of `surprise_bits` (4) or more goes back to the seat with the rules and beliefs the
   imagining used. The seat decides what was wrong (a rule, a belief about someone, or luck), fixes its world, and
   records it with `reflect(id, verdict, lesson)`. Lessons ride on every later imagining, and a calibration line per
   kind of plan says how often reality fell inside the imagined 80% band.

## An example

```python
imagine(
  rules={"spar": {"check": "return ctx.actor.state.hp > 4",
                  "apply": "if ctx.rand.roll('1d20') + ctx.actor.state.atk >= ctx.target.state.ac: "
                           "ctx.target.state.hp -= ctx.rand.roll('1d6')"}},
  setup={"Brann": {"ac": 12, "atk": 3}, "me": {"ac": 12, "atk": 3}},
  plan=[{"actor": "me", "verb": "spar", "target": "Brann"},
        {"actor": "Brann", "verb": "spar", "target": "me"}],
  rounds=3)
```

Called with nothing, `imagine()` shows the seat's rules and beliefs, what its card shows, and a starter named after
its own verbs.

## What the first live test taught

A seat on deepseek-v4-flash sparred a partner who was secretly much stronger than it was told. Each way it broke its
own imagination is now handled:

- **It copied the starter's verb name** (`attack`) when its real verb was `spar`, so nothing could be compared. The
  starter is named after the seat's verbs, and a plan whose first verb is not a real one says so.
- **It asked for 3 trials** and got three stories. At least 20 are run.
- **It wrote rules as bodies with no `def` lines**, then as one line (`if a: x; if b: y`). Both shapes are read.
- **A rule that crashed** (it set `ctx.result`, which does not exist) had its crashes counted as how fights end, and
  the surprise that followed was put down to the partner. A rule is now tried before it is kept; a crash during
  trials is an error and nothing is recorded. A rule that only lacks a number someone has no value for yet is kept
  with a note to give it in `setup`.
- **It rested in the same turn** after sparring, so a next-wake read saw it whole again. The plan is now scored the
  moment its run ends.

## What is kept

`imagination.json` in the seat holds the rules, beliefs and lessons. `predictions.jsonl` holds each imagining (the
plan, every trial's numbers, the rules and beliefs it used), when it was acted on, how it scored, and what the seat
made of it. Imagined outcomes never reach the world.
