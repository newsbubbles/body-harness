# Imagination

A seat can own a small embody world of its own, in the harness's process, to try a plan before doing it, and to
learn from the difference between what it imagined and what really happened.

```bash
uv tool install "body-harness[imagine] @ git+https://github.com/newsbubbles/body-harness"
bh config sefa imagine=true
```

The `imagine` extra installs the [embody](https://github.com/newsbubbles/embody) engine (pure Python, no
dependencies of its own). Without it the three tools (`imagine`, `reflect`, `relive`) are simply not offered.

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
4. **The seat plays everyone else.** This world is the seat's own, so the others do what it says. When it does not
   know what someone will do, it gives odds instead of one verb, `{actor: "Oren", verb: {"guard": 3, "strike": 1}}`,
   and each trial draws one. Any step can also carry `odds: {"his": {"guard": 3, "throw": 1}}`, which its rules read
   as `ctx.params.his`. The answer counts what was drawn. The seat's own steps take one verb: odds are for others.
5. **The harness binds it.** When the seat really does the first of its own steps in a plan it imagined lately, that imagining
   becomes a prediction.
6. **The harness scores it.** When the plan's run ends (a deed outside the plan, or the turn ends), the harness reads
   the seat and the others in the plan with `inspect`, a read that leaves no trace, and scores every number against
   the imagined spread as surprise in bits: -log2 of how often the trials came out at least that far off, with half
   a trial added so a value never imagined is finite. The model never grades itself.
7. **Reflection.** A surprise of `surprise_bits` (4) or more goes back to the seat with the rules and beliefs the
   imagining used, and with the evidence: per number, how much it moved per step of the seat's own, imagined against
   real, over every plan of that kind it really did. The seat decides what was wrong, in this order: a belief about
   someone's numbers, then a rule (only when the same thing keeps happening over several tries), then luck. It fixes
   its world and records it with `reflect(id, verdict, lesson)`. The harness says when a verdict changed nothing, and
   calls a new rule from fewer than 3 real tries a guess. The last 4 lessons per kind of plan ride on every later
   imagining, and a calibration line per kind of plan says how often reality fell inside the imagined 80% band.
8. **Re-living.** `relive()` plays every moment the seat lived again, from the numbers it saw then and its deeds as
   it did them, through the rules and beliefs it has now. See below.

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
- **It asked for 3 trials** and got three stories. At least 40 are run (at 20, a value never imagined and one
  imagined once read almost the same surprise).
- **It wrote rules as bodies with no `def` lines**, then as one line (`if a: x; if b: y`). Both shapes are read.
- **A rule that crashed** (it set `ctx.result`, which does not exist) had its crashes counted as how fights end, and
  the surprise that followed was put down to the partner. A rule is now tried before it is kept; a crash during
  trials is an error and nothing is recorded. A rule that only lacks a number someone has no value for yet is kept
  with a note to give it in `setup`.
- **It rested in the same turn** after sparring, so a next-wake read saw it whole again. The plan is now scored the
  moment its run ends.
- **Each later spar bound an older imagining**, and a plan of three spars was scored after one. A deed that continues
  a bound plan now counts as its next step, and a plan is scored only when the seat did as many of its own steps as
  it imagined; otherwise it is told the plan was not scored, and why. A different plan is not a wrong imagination.

## What the 50-turn run taught

The same seat then sparred for 50 turns, for $0.11 of model calls. It imagined 23 plans and acted on 20. Seven were
scored and 12 were partial (it did fewer steps than it imagined). Its own hp fell inside the imagined band 3 times of
7, its partner's 5 of 7, with no trend from first to last.

All 7 of its verdicts were "rule". It never changed a belief: it believed its partner hit with +3 for 1d6, and the
partner really hit with +7 for 1d8+2. Its lessons were stories that fit one result ("he hits harder when he is low").
The wake told it how surprised to be, but not what the numbers were.

Now the wake carries the evidence lines, the verdict order puts belief first, a partial plan still counts as
evidence, and the trial floor is 40.

## What the Dojo taught

Dice put a noise floor under every surprise: a seat cannot tell a wrong belief from a bad roll in a few tries. So the
next test world had no dice. The Dojo is a sparring floor run like a fighting game: strike, throw, guard and a special
move, each beating some and losing to others, with a cooldown on the special. The master, Oren, picks by a fixed
policy the seats are not told (a special when it is ready and the student is low, a counter to a move repeated twice,
otherwise a cycle), and his damage numbers are secret. Each student has its own floor and its own Oren, so seats run
side by side. A test world where nothing moves unless the seat does sets `min_minutes` to 0, and a seat wakes again
at once.

**Seats that are not asked to imagine mostly do not.** Three seats on deepseek-v4-flash with imagination on and no
brief: two never imagined, and one imagined once in 50 turns. Trying the real move costs one call and answers for
sure; an imagining costs more calls and answers with a spread. A standing brief in the seat's `extra_prompt` (imagine
each exchange first, play Oren with odds and change them as you learn, reflect on surprises) changed that.

**Four briefed seats, 50 turns each:**

- They imagined 197 times, and 182 of those gave odds for Oren's next move rather than one guess.
- 130 imaginings were scored. Both fighters' hp came out inside the imagined 80% band 77 times of 130 in the first
  half of each seat's scored moments, and 92 of 129 in the second half. The mean surprise of the worst number in a
  moment fell from 4.06 to 3.78 bits. Three seats improved; one got worse (hp inside 25 of 34, then 22 of 33).
- 50 of their 59 verdicts were "belief", 7 "rule", 2 "luck". After the 50-turn run above, where every verdict was
  "rule", this is the change the evidence lines and the verdict order were for.
- One seat found three of Oren's four secret damage numbers exactly (strike 4, throw 5, guard 3). None found the
  special's 9. One kept a belief about where Oren was in his cycle, which is the policy itself.
- They called `relive` once among them. Re-living was in the brief; they reached for reflection instead.

## Re-living

`relive()` re-lives every moment the seat lived (up to the last 40): an imagining it acted on, from the numbers it
saw then, with its own deeds as it really did them (each step is logged), through the rules and beliefs it holds
now. It answers how many of those moments its imagination now sees coming, and how many it did when it lived them:

```
re-lived 20 moments with your imagination now: 11 of 20 came out inside your imagined 80% band, mean surprise
2.6 bits. Of the 17 scored when you lived them: then 9 inside at 2.9 bits, now 8 at 3.0
```

That is a learning curve the harness measures from the seat's own past, not one the seat reports. A changed belief
that does not move it was not the fix. The line above is a real one, from a Dojo seat after its 50 turns.

It came out flat for all four Dojo seats, while their forward numbers improved. The reason is what re-living keeps:
a past guess about someone else is replayed as it was made, and those seats did their learning in their guesses about
Oren (the odds), not in their rules. Re-living also cannot replay a moment from before a seat gave odds once its rules
read `ctx.params.his`: for one seat that was 29 of 35 moments. The next build keeps each character's mind as a rule of
its own, so re-living can ask today's mind what Oren would have done.

A seat's own mistake can show here too. One seat's re-live came out worse than its first try, because its newest
strike rule did nothing. A kept rule whose apply is empty now says so, and names the rule it replaced.

`relive(id, after=N, instead=[{actor, verb, target}])` replays one moment for N of the seat's real steps and then
imagines another way from there: what if it had guarded instead. That part is imagined, not scored.

## What is kept

`imagination.json` in the seat holds the rules, beliefs and lessons. `predictions.jsonl` holds each imagining (the
plan, every trial's numbers, the rules and beliefs it used), when it was acted on, how it scored, and what the seat
made of it. Imagined outcomes never reach the world.
