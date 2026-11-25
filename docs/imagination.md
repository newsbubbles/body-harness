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
   it did them, through the rules, beliefs and minds it has now. See below.
9. **Kept minds.** What the seat learns of how someone chooses it can keep as that one's mind, a rule of its own
   that picks their next move. Imagining and re-living ask it. See below.

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

## What the Purse taught

Predicting well is not playing well. The Purse is the Dojo with stakes: put Oren down and earn 5 coins, go down and
pay 5, and the bench costs 1. Nine seats on deepseek-v4-flash, 40 turns each, the same goal (end with as many coins
as you can), in three groups of three. One group was briefed to imagine every exchange first, one had no imagination,
and one had no imagination but `past` (a search over its own deeds and what came of each, see
[how-it-works.md](how-it-works.md#two-memories)):

| | Coins (from 20) | Exchanges | Bouts won | Went down | Model cost |
|---|---|---|---|---|---|
| imagine every exchange | 28, 64, 28 | 115 | 13 | 0 | $0.38 |
| no imagination | 64, 40, 54 | 316 | 34 | 9 | $0.27 |
| `past` search | 91, 74, 97 | 351 | 45 | 2 | $0.22 |

- **Lining up the past paid most.** The `past` seats called it 39 to 68 times each, found Oren's pattern early, and
  won 48 to 57 of their last 52 to 66 exchanges. They also spent the least.
- **Imagining made each exchange good and the seat slow.** The imagining seats never went down, and earned about as
  much per exchange as the `past` seats, but each exchange cost an imagining, so they fought a third as often. Here
  Oren waits and trying is cheap, so that is a bad trade. Imagination should pay where a mistake costs more than a
  turn, or cannot be undone.
- One imagining seat did as well as the best plain seat (64 coins); two fought 22 and 30 exchanges in 40 turns.

Three seats a group, one model and one opponent with a fixed pattern is a small test. A world where the others have
minds of their own, lie, and want things is the next one.

## What the Strongbox taught (a first run)

The Strongbox puts five villagers and a rival around a box buried under one of six spots. Each villager knows about one
spot and tells it for a coin: one always tells the truth, two always lie, one tells the truth except that she vouches
for her lying brother, and one guesses. A rival digs a spot after every second deed of the seat. A dig costs 5 coins
and the box pays 15; each round a new box is buried and the villagers stay who they are. Played in memory first, a
seeker who knows who lies ends 30 rounds near +150 coins, blind digging near -45, believing everyone near -82.

Nine seats, the same three groups as the Purse, 40 turns each:

| | Coins (from 20) | Rounds won | Imagined / searched |
|---|---|---|---|
| asked to imagine | 93, 121, 134 | 14 of 17, 21 of 25, 23 of 29 | 5, 1, 0 imaginings |
| no imagination | 0, 81, 173 | 3 of 8, 14 of 19, 29 of 33 | |
| `past` search | 83, -5, 0 | 14 of 17, 0 of 3, 2 of 6 | 44, 41, 34 searches |

- **It does not separate the groups.** Seats in one group ranged from 0 to 173 coins. Three a group is too few here.
- **Seats asked to imagine barely did** (6 imaginings among three seats in 120 turns). A world of rules and dice
  does not fit the question "who is lying to me", and the seats did not reach for it.
- **The best seats learned the easy half:** Ada and Cass tell the truth, so ask them first; they won 85 to 90% of
  rounds. No seat learned to use a liar by turning his answer around, and `vouch` was used 9 times in all. Questions
  per round did not fall from the first half of a seat's rounds to the second (3.4 to 4.8 throughout).
- **One seat stalled on the world's words:** told the rival digs "every 2 hours", it slept waiting for him for its
  last turns, and he digs only when the seat acts. The description now says so. Another read and searched far more
  than it dug (3 digs in 40 turns), as the imagining seats did in the Purse.

### The second run: one notebook for all

Fifteen seats, five a group, 50 turns each. Every group came out the same: rounds won 150 of 183 asked to imagine,
142 of 176 with no imagination, 161 of 195 with `past`; about one dig a round in all three. Seats now did what no seat
did in the first run: 3 to 7 of each seat's winning digs went where a liar had said the box was not.

They did not each learn that. One seat of the first run had written who lies and who tells the truth to the craft
shelf, the notebook every seat in one harness home shares, and its index rides every wake. All fifteen seats started
the second run with it, and seven of them added to it. So the run measured a shared notebook, not the three ways of
thinking: a seat with the answer in its index needs neither imagining nor search. Two lessons:

- **A test gives each group its own harness home,** so nothing one seat writes reaches another group.
- **The craft shelf spreads a lesson fast.** Here the lesson was true and every seat used it. A false one would
  spread as fast, and craft is meant for lessons about playing well, not for facts about one world's people.

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

It came out flat for all four Dojo seats, while their forward numbers improved. Re-living replays a past guess about
someone else as it was made, so the first idea was that the seats had learned Oren in their guesses, which re-living
froze. A kept mind (below) tested that, and it was wrong. The seats' own rules were what held them back:

- **With each seat's own rules,** no mind moved its re-living, not even one that plays Oren's real policy. The rules
  wrote damage as fixed tables, some matchups missing, and those decide the hit points.
- **With the Dojo's exact rules and damage numbers,** and the seats' own guesses as they made them, about 17 moments
  each: one seat went from 1 of 17 inside the band when it lived them to 12 of 17 now; another from 3 of 14 to 10.
- **With exact rules and a mind that plays Oren's real policy,** the mean surprise was the lowest of the three ways
  for three of the four seats (0.6 bits against 1.1 for a mind that picks any move evenly, for one of them). For the
  fourth it was 1.0 against 0.9.

So the next lesson for a seat is its rules first. Re-living says where a rule is off; a mind only helps once the rules
are right. Re-living also cannot replay a moment whose rule reads a choice the moment did not give: for one seat, 29
of 35 moments from before it gave odds. The answer now says how many it could not re-live, and why. A kept mind gives
that choice to those moments too.

A seat's own mistake can show here too. One seat's re-live came out worse than its first try, because its newest
strike rule did nothing. A kept rule whose apply is empty now says so, and names the rule it replaced.

`relive(id, after=N, instead=[{actor, verb, target}])` replays one moment for N of the seat's real steps and then
imagines another way from there: what if it had guarded instead. That part is imagined, not scored.

## Kept minds

A guess about someone, given as odds, is made fresh each time. When the seat learns how someone chooses, it can keep
that as their mind, the way it keeps a rule:

```python
imagine(minds={"Oren": {"as": "his", "choose": """
beats = {"strike": "guard", "throw": "strike", "guard": "throw", "special": "guard"}
t = ctx.params.theirs
if len(t) >= 2 and t[-1] == t[-2] == ctx.params.against:
    return beats[t[-1]]
return {"guard": 2, "strike": 1, "throw": 1}"""}})
```

A mind is an embody script, `def choose(ctx)` or just its body, that returns a move or odds. It sees:

| | |
|---|---|
| `ctx.actor` | the character, as the seat believes and sees them |
| `ctx.target` | the one they face |
| `ctx.params.theirs` | what that one did lately: for the seat, its real deeds before this moment, then this trial's |
| `ctx.params.n` | how many deeds that is in all |
| `ctx.params.mine` | what the character did in this trial |
| `ctx.params.against` | the move it answers, when it chooses inside someone's step |

A step for them with no verb is then theirs to choose. With `as`, a rule that reads `ctx.params.his` gets the mind's
choice whenever a step gives none. A mind is tried before it is kept, and the note says what it would pick now.

Re-living asks today's minds in place of the guesses made then, and the answer says which minds chose and how often.
A mind is a belief about someone written as a rule, so it can be checked the same way: re-live with it, and see.

## What is kept

`imagination.json` in the seat holds the rules, beliefs, minds and lessons. `predictions.jsonl` holds each imagining (the
plan, every trial's numbers, the rules and beliefs it used), when it was acted on, how it scored, and what the seat
made of it. Imagined outcomes never reach the world.
