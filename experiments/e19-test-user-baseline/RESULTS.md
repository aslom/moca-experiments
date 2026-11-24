# E19 — Results

Measured 2026-11-24. Three rungs, in the order they ran:

| Rung | What it asked | Outcome | Record |
|---|---|---|---|
| [N = 10](#the-table) | the baseline lifecycle, and does it hold at 10 at once | all nine predictions held; ten users cost what one costs | [`results/n10/`](results/n10) |
| [N = 100](#n--100-the-service-says-no-at-40) | does it still hold at 100 | 60 of 100 refused at `create`; the service caps live sandboxes at 40 | [`results/n100/`](results/n100) |
| [C = 1…40 ladder](#the-ladder-c--1-5-10--40) | how a user's experience changes as the crowd grows | zero failures at every level; median degrades 1.20x, and the tail breaks at the cap | [`results/ladder/`](results/ladder) |

**If you read one thing:** below the cap this deployment degrades gently and predictably — a
user alone waits 8.5 s for the whole lifecycle, a user among forty waits 10.2 s. The number that
misbehaves is not the median but the tail, and only at the cap: see
[the stall](#one-user-in-forty-waits-20-seconds-for-one-command).

## N = 10

Every number in this section comes from [`results/n10/summary.json`](results/n10/summary.json);
the per-user, per-operation times behind them are in
[`results/n10/record.json`](results/n10/record.json), and the run's settings in
[`results/n10/params.json`](results/n10/params.json).

## Setting

| | |
|---|---|
| Cluster | OpenShift `ykt1`, namespace `moca-sbx` |
| Control plane | profile `sandbox`, version `main-3b53f15` |
| Workers | one `sbx-worker` replica |
| Template / mode | `default`, `per-exec` |
| Test users | `SH_TEST_USERS=1000`, `SH_SANDBOX_TEST_USERS_ENROLLED=true` |
| Driver | cpython 3.14.10 free-threaded, GIL off, one OS thread per user |
| Driver host | laptop, macOS arm64, reaching the control plane over its public Route |
| Client | `mocactl.mjs` built from `aslom/moca@test-user-deployment` |

## The table

All times are milliseconds, wall clock, N = 10 samples per row. Percentiles are nearest-rank,
so p90 over ten samples is the ninth value.

| Operation | n | failed | min | p50 | p90 | max | mean |
|---|---|---|---|---|---|---|---|
| `login` | 10 | 0 | 568 | 582 | 584 | 584 | 580 |
| `create` | 10 | 0 | 783 | 804 | 870 | 882 | 821 |
| `run -- id` | 10 | 0 | 2153 | 2219 | 2254 | 2261 | 2222 |
| `run -- uname -a` | 10 | 0 | 2079 | 2152 | 2191 | 2202 | 2149 |
| `ls` | 10 | 0 | 760 | 775 | 812 | 818 | 783 |
| `rm --force` | 10 | 0 | 2425 | 2464 | 2504 | 2507 | 2464 |
| **whole lifecycle, per user** | 10 | 0 | 8930 | 9043 | 9109 | 9135 | 9022 |

Wall clock for all ten users together: **9138 ms**.

Read the rows against each other with care. `login` is one HTTPS round trip. Every other row is
a separate `mocactl` process, so each carries a Node start-up — of the ~780 ms `ls` takes, most
is the client starting, since `ls` is a single list call. The `run` and `rm` rows are the ones
with real server-side work in them.

## Verdict on each sealed prediction

All nine held. Bars are from [`predictions.json`](predictions.json), sealed in commit `eb4636b`
before this run.

| | Claim | Bar | Measured | Verdict |
|---|---|---|---|---|
| P1 | no user fails | `users_failed == 0` | 0 | **held** |
| P2 | `login` p90 | ≤ 1500 | 584 | **held** |
| P3 | `create` p90 | ≤ 2000 | 870 | **held** |
| P4 | `run_id` p90 | ≤ 4500 | 2254 | **held** |
| P5 | `run_uname` p90, and its p50 ≤ `run_id`'s | ≤ 4000; 2152 ≤ 2219 | 2191 | **held** |
| P6 | `ls` p90 | ≤ 1500 | 812 | **held** |
| P7 | `rm` p90 | ≤ 4500 | 2504 | **held** |
| P8 | ten concurrent users cost far less than ten sequential | wall ≤ 30000 | 9138 | **held** |
| P9 | free-threaded interpreter | `gil_enabled == false` | `false` | **held** |

Every bar was set with roughly 50% headroom over an N = 2 trial, and the measured values came in
at or below the N = 2 numbers rather than near the bars. The bars were, in hindsight, slack: they
were set to catch queueing at a single worker, and no queueing appeared. A future rung should
tighten them, or raise N until something bends.

## What the result says

**Ten concurrent users are as cheap as one.** Wall clock for all ten, 9138 ms, is within 1% of a
single user's own lifecycle time, 9043 ms at p50. Ten sequential users would have cost about
90 s. Nothing in the path serialised at N = 10 — not the login endpoint, not `create`, and not
the single `sbx-worker` replica the `run` calls shared.

**The spread is very narrow.** Across ten users, each operation's max is within 5% of its min
(`create` is widest at 13%). That is a system with slack at this load, not one at a knee. It also
means N = 10 does not locate any limit; it establishes that the limit is above 10.

**The two `run` calls dominate.** `run_id` and `run_uname` together are ~4.4 s of a ~9.0 s
lifecycle, and `rm` adds another ~2.5 s. `create`, at ~820 ms, is cheap — on this tier the
sandbox is not what costs; executing in it is.

**Warm is only slightly cheaper than cold.** `run_uname`, the second command in the same sandbox,
runs 67 ms faster at p50 than the first (2152 vs 2219 ms, 3%). The sealed direction held, but the
size of it is within the run-to-run noise of this rig and should not be quoted as a warm-path
saving. Isolating that needs a design that controls for it; E17 is where that question lives.

## N = 100: the service says no at 40

Every number here comes from [`results/n100/summary.json`](results/n100/summary.json).
Predictions: [`predictions-n100.json`](predictions-n100.json), sealed in commit `4f457cb` before
the run. **Eight held, two falsified.**

`40 of 100` users completed. The other **60 were refused at `create`**, all of them with the same
error and none with any other:

```
sbx: the sandbox service is at capacity
```

All 100 logins succeeded. The refusal is `quota_exceeded` from
`packages/control-plane/src/sandbox-handlers.ts`, raised when `store.create` reports
`quota_total` — the deployment-wide cap on live sandboxes, `cfg.maxTotal`. In
`sandbox-config.ts` that is:

```ts
maxTotal: positive(env, 'SH_SANDBOX_MAX_TOTAL', 40),
```

On this cluster `SH_SANDBOX_MAX_TOTAL` is **unset**: the control plane reads it from
`sbx-settings` through an `optional: true` configMap ref, and that configMap has no such key, so
the code default of 40 applies. Exactly 40 users succeeded. `SH_SANDBOX_MAX_PER_SUBJECT` is
likewise unset and defaults to 3, but it never bound here — each test user asked for one sandbox.

| | Claim | Bar | Measured | Verdict |
|---|---|---|---|---|
| N100-P1 | no user fails | `users_failed == 0` | **60** | **falsified** |
| N100-P2 | `login` p90 | ≤ 2500 | 1076 | held |
| N100-P3 | `create` p90 | ≤ 6000 | 1338 | held |
| N100-P4 | `run_id` p90 | ≤ 20000 | 5506 | held |
| N100-P5 | `run_uname` p90 | ≤ 20000 | 2703 | held |
| N100-P6 | `ls` p90 | ≤ 4000 | 1026 | held |
| N100-P7 | `rm` p90 | ≤ 15000 | 2667 | held |
| N100-P8 | wall ≤ 180 s | ≤ 180000 | 13377 | held |
| N100-P9 | free-threaded | `false` | `false` | held |
| N100-P10 | wall ≥ 2x the N = 10 wall | ≥ 18277 | **13377** | **falsified** |

**N100-P2 through N100-P7 are survivorship numbers and should not be quoted as N = 100
latencies.** They are percentiles over the 40 users that got a sandbox. The 60 refusals are
excluded from the `create` row entirely — and they were not fast: a refused `create` took 1444 ms
at the fastest, 3276 ms at the median and 5333 ms at the slowest, against 1050 ms for a successful
one. Being told no cost about three times as long as being served.

**N100-P10 was falsified for a reason that voids the test rather than answering it.** Wall clock
did not double because only 40 users ever reached `run`; the other 60 stopped after about 4 s. The
rung cannot say whether 100 concurrent users would have queued, because 100 concurrent users never
happened. A single large N cannot separate "the service slows down" from "the service says no" —
which is why the ladder below exists.

Two smaller observations that do survive:

- **Login scales but is not free.** All 100 logins succeeded, and `login` p50 went from 582 ms at
  N = 10 to 1032 ms at N = 100 — 100 near-simultaneous token signings against one control-plane
  pod, at 1.8x the cost and no failures.
- **The 40 winners are not the first 40.** `test:1` was refused and `test:2` succeeded; the
  survivors are scattered across the range. The cap is a race at `store.create`, not a queue, so
  which user loses is arbitrary.

## The ladder: C = 1, 5, 10 … 40

Every number here comes from [`results/ladder/ladder.json`](results/ladder/ladder.json), with a
directory per level (`results/ladder/c001/` … `c040/`) holding that level's `summary.json`,
`record.json` and `params.json`. Predictions:
[`predictions-ladder.json`](predictions-ladder.json), sealed in commit `3f8df75` before the run.
**Four held, three falsified.**

Nine levels, each its own rung: C users, all C in flight, every sandbox deleted before the next
level. 40 is the top because above it `create` is refused rather than slowed. **No user failed at
any level**, including C = 40, which asks for exactly as many live sandboxes as the cap allows.

| C | ok | wall s | lifecycle p50 | p90 | max | vs C=1 | `login` | `create` | `run_id` | `run_uname` | `ls` | `rm` |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1/1 | 8.5 | 8470 | 8470 | 8470 | 1.00x | 563 | 705 | 2106 | 1996 | 697 | 2402 |
| 5 | 5/5 | 8.6 | 8587 | 8596 | 8596 | 1.01x | 565 | 749 | 2049 | 2059 | 737 | 2423 |
| 10 | 10/10 | 9.0 | 8890 | 9003 | 9003 | 1.05x | 566 | 821 | 2140 | 2102 | 766 | 2452 |
| 15 | 15/15 | 9.1 | 8936 | 8980 | 9134 | 1.06x | 568 | 863 | 2076 | 2072 | 781 | 2482 |
| 20 | 20/20 | 9.2 | 8938 | 9164 | 9230 | 1.06x | 567 | 1018 | 2069 | 2095 | 767 | 2444 |
| 25 | 25/25 | 9.5 | 9180 | 9465 | 9536 | 1.08x | 577 | 1027 | 2098 | 2126 | 793 | 2484 |
| 30 | 30/30 | 10.0 | 9444 | 9831 | 9979 | 1.11x | 577 | 1099 | 2251 | 2105 | 770 | 2487 |
| 35 | 35/35 | 10.7 | 9772 | 10470 | 10711 | 1.15x | 583 | 1167 | 2310 | 2082 | 777 | 2541 |
| 40 | 40/40 | **27.5** | 10153 | 10730 | **27481** | 1.20x | 583 | 1230 | 2311 | 2090 | 839 | 2621 |

Milliseconds except `wall s`. "lifecycle" is the whole six-step sequence per user; the operation
columns are that operation's p50 at that level.

### The median degrades gently, and in a straight line

From alone to forty-at-once, a user's whole lifecycle goes **8470 → 10153 ms, a factor of 1.20**.
The climb is smooth and almost linear in C — roughly +4% per ten concurrent users — with no knee
anywhere below the cap. Forty users at once is not forty times the wait; it is a fifth more.

### `create` degrades most, not `run` — L-P4 falsified

The sealed guess was that `run_id` would absorb the contention, being the command that reaches the
single `sbx-worker` replica. It did not. Ranked by how much each operation's p50 grew from C = 1
to C = 40:

| Operation | C=1 | C=40 | Growth |
|---|---|---|---|
| `create` | 705 | 1230 | **1.74x** |
| `ls` | 697 | 839 | 1.20x |
| `run_id` | 2106 | 2311 | 1.10x |
| `rm` | 2402 | 2621 | 1.09x |
| `run_uname` | 1996 | 2090 | 1.05x |
| `login` | 563 | 583 | 1.04x |

The contention is in the **control plane**, not the worker. `create` and `ls` are the two
control-plane-only operations in the sequence, and they are the top two; `create` is also the one
that takes the cap's lock, which it contends for harder as the service fills. The two `run` calls,
which actually execute in a microVM on the shared worker, barely move. On this deployment the
worker had room at 40 concurrent users; the bookkeeping did not.

Only `create` is convincing on its own, though. It climbs monotonically at every level — 705, 749,
821, 863, 1018, 1027, 1099, 1167, 1230 — which is a trend, not a pair of endpoints. `ls` ends 1.20x
up but wanders on the way (767 at C = 20, 770 at C = 30, 777 at C = 35), so its ratio rests on two
samples and should be read as "did not clearly degrade" rather than second place.

### Login barely notices 40 — L-P3 falsified

Predicted at least 1.3x; measured **1.04x** (563 → 583 ms). The 1.8x seen at N = 100 does not
appear at 40, so whatever makes 100 simultaneous logins cost more sets in somewhere above 40.

### One user in forty waits 20 seconds for one command

This is the finding the median hides, and the reason C = 40's wall clock is 27.5 s while its
lifecycle p90 is 10.7 s. At C = 40, and at **no level below it**, exactly one operation stalled:

```
C=40  test:22  run_uname  20717 ms  ok=True
```

Everything else in that run finished inside 3.2 s. The level's own `run_uname` p50 was 2090 ms, so
this one call took **ten times** the median, and `test:22`'s whole lifecycle came to 27481 ms
against a p50 of 10153 ms.

It succeeded, and that is the point. The shape matches the client's busy-retry path:
`packages/mocactl/src/sbx/run.ts` retries a command the sandbox *refused before starting*
`MOCACTL_SBX_BUSY_RETRIES` times, default 4, backing off `1, 2, 4, 8` seconds with jitter — 15 s of
sleeping plus the attempts themselves, which lands squarely on 20.7 s. So at the cap the worker
did refuse an `Exec`, and the client converted that refusal into a silent 20-second wait instead
of an error.

Three consequences:

- **The failure mode at saturation is a stall, not an error.** A user sees one command hang for
  20 s and then work. Nothing is logged as failed; `users_failed` is 0. A test that only checks
  exit codes would call this level healthy.
- **p90 does not show it and p99 barely does.** Over 40 samples the ninth-from-top value is
  10730 ms; the stall is only visible in max and p99. Any summary of this deployment quoting p50
  and p90 would report C = 40 as fine.
- **Rare, not systematic.** A second run of the whole ladder saw no stall at all
  ([below](#repeated-a-second-ladder-same-cluster-same-day)), so this is one occurrence in two
  runs of 40 users at the cap — it can happen there, but not reliably. A real rate needs many more
  repeats at C = 35 and C = 40.

### Verdict on each sealed prediction

| | Claim | Bar | Measured | Verdict |
|---|---|---|---|---|
| L-P1 | no level has a failure | 0 levels | 0 | **held** |
| L-P2 | lifecycle p50 at C=40 ≤ 2x C=1 | ≤ 2.0 | 1.199x | **held** |
| L-P3 | `login` p50 at C=40 ≥ 1.3x C=1 | ≥ 1.3 | **1.036x** | **falsified** |
| L-P4 | `run_id` degrades most | `run_id` | **`create`** | **falsified** |
| L-P5 | wall at C=40 ≤ 3x C=1 | ≤ 3.0 | **3.244x** | **falsified** |
| L-P6 | lifecycle p50 at C=40 > C=5 | ≥ 1.0 | 1.182x | **held** |
| L-P7 | free-threaded at every level | `false` | `false` | **held** |

L-P5 is falsified by the single stall and nothing else: strip `test:22` and C = 40's wall would be
about 11 s, or 1.3x. Reported as falsified rather than rewritten, but the bar was measuring one
outlier, not the throughput it was meant to describe.

L-P4's failure is the most useful result in the rung: it moves the suspected bottleneck from the
worker to the control plane, which is the opposite of what the N = 10 and N = 100 rungs suggested.

### Repeated: a second ladder, same cluster, same day

The ladder was run a second time, unchanged, while reproducing these steps from a fresh shell.
Its record is [`results/ladder-run2/`](results/ladder-run2) (its `params.json` says
`out: results/ladder`, because that is where it was written before being set aside — the two runs
are otherwise identical in settings). It was not planned, and it is not a sealed rung; it is
reported because it settles two of the caveats below.

| C | lifecycle p50, run 1 | run 2 | Δ |
|---|---|---|---|
| 1 | 8470 | 8459 | −0.1% |
| 5 | 8587 | 8524 | −0.7% |
| 10 | 8890 | 8653 | −2.7% |
| 15 | 8936 | 8774 | −1.8% |
| 20 | 8938 | 8995 | +0.6% |
| 25 | 9180 | 8914 | −2.9% |
| 30 | 9444 | 9145 | −3.2% |
| 35 | 9772 | 9252 | −5.3% |
| 40 | 10153 | 9533 | −6.1% |

**What reproduced.** Zero failures at all nine levels, again, including C = 40 at the cap. The
gentle climb: 1.127x from C = 1 to C = 40, against 1.199x in run 1. And the result that matters —
`create` is again the worst-degraded operation, again monotonically at every level
(701, 734, 756, 801, 834, 878, 910, 934, 1019 ms), so L-P4's falsification and "the bottleneck
below the cap is the control plane, not the worker" rest on two independent runs rather than one.

**What did not.** The 20-second stall **did not recur**. No operation anywhere in run 2 exceeded
5 s, and C = 40's slowest user finished in 10160 ms against 27481 ms in run 1 — which is why
run 2's C = 40 wall clock is 10.2 s rather than 27.5 s. So the stall is a real but *occasional*
event at the cap, not a property of C = 40: one occurrence in two runs of 40 users each. That is
still not a rate, but it does bound it well below "every time".

**What run 2 weakens.** The `create`-versus-`run_id` gap is narrower than run 1 implied: 1.454x
against 1.174x here, where run 1 had 1.744x against 1.097x. `create` stays first and `run_id`
stays behind `ls`, but the size of the control-plane effect is uncertain to roughly a factor of
two between runs. Run 2 is also uniformly a little faster at every level, so some of run 1's
absolute numbers reflect the cluster that minute, not the deployment.

Both runs are kept. The N = 10 and N = 100 rungs were not repeated.

### What this rung does not say

- **One sealed run per level.** Each level of the sealed ladder is a single sample of C users. The
  smooth p50 climb is believable because nine levels agree and a second run reproduced it; the
  single stall is still not a rate, only bounded at one occurrence in two runs.
- **Levels ran in sequence on a warming cluster.** C = 40 ran last, about two minutes after C = 1.
  Drift over those two minutes is not separated from the effect of C.
- **The cap was never raised.** Everything above is for `SH_SANDBOX_MAX_TOTAL = 40`. Whether the
  control-plane contention in `create` keeps growing, or the worker becomes the limit, past 40 is
  untested — raise the cap and re-run to find out.
- **Still one worker replica, still over the public Route, still CLI start-up in every row.** The
  caveats on the N = 10 rung all apply unchanged.

## The deployment doc's own sequence does not run as written

This is the finding most likely to cost someone else time, so it is reported here and not only in
the SPEC.

On a sandbox-profile control plane, `mocactl sbx login --test-user N` fails from the client,
before any request is sent:

```
sbx: <url> serves no test users: only a combined control plane does
(a sandbox-profile one admits enrolled github:/oidc: subjects only, ADR-0045)
```

The server does not agree with its own client. In the same tree,
`packages/control-plane/src/routes.ts` mounts `POST /v1/auth/test-token` with `profiles: BOTH`,
and the comment there states the intent outright: "BOTH, so a sandbox-only stack can serve test
logins as well." Posting to that route with the shared secret against this stack returns **200**
and a usable token for `test:<N>`; the sandboxes in the table above were all created by logins
obtained that way.

The refusal is a stale profile gate in `cmdLoginTestUser`
(`packages/mocactl/src/sbx/commands.ts`), which returns a usage error unless `/v1/discovery`
reports `profile: combined`. So `TESTING-SANDBOX-ONLY.md`'s quick start, step 5, cannot succeed on
the very stack the rest of the document builds — and the symptom points at the deployment, which
is fine, rather than at the client, which is where the problem is.

The driver works around it without patching MOCA: it mints the token over HTTP and writes
`$XDG_CONFIG_HOME/mocactl/sbx-auth.json` in the `CachedAuth` shape, mode 0600, which is what the
CLI would have written on success. Operations 2–6 are the unmodified client.

Two things follow for anyone reading this later:

- If the client gate is relaxed to match `routes.ts`, the driver's `login` step should go back to
  being `sbx login --test-user N`. Its measured latency will then rise by a Node start-up and stop
  being comparable to the 584 ms here.
- Until then, `--test-user` is unusable on a sandbox-only stack through the CLI, and any test that
  needs many users on such a stack has to do what this one does.

## Caveats

These apply to every rung. The ladder's own are in
[what this rung does not say](#what-this-rung-does-not-say).

- **One run per rung.** One sample per user per operation, one point in time. The percentiles are
  indications over tens of values, not estimates.
- **Shared cluster.** `moca-sbx` was not reserved. Other activity would inflate these numbers;
  none was visible, but none was excluded either.
- **One worker replica.** The concurrency result (P8) is for ten users against one `sbx-worker`.
  It says that replica was not the bottleneck at N = 10. It says nothing about where it becomes
  one.
- **Driver position is in every number.** A laptop reaching an OpenShift Route over the internet.
  Numbers taken from inside the cluster would be smaller and are not comparable to these.
- **CLI start-up is in every row but `login`.** This is intentional (SPEC, "What is measured and
  what is not"), but it means no row here is a server-side latency.
- **`login` is not the documented command.** See above.
