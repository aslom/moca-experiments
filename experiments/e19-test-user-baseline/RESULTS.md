# E19 — Results

Measured 2026-11-24. One run, N = 10 test users, `test:1`..`test:10`.

Every number here comes from [`results/n10/summary.json`](results/n10/summary.json); the
per-user, per-operation times behind them are in
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

- **One run.** N = 10, one sample per user per operation, one point in time. The percentiles are
  indications over ten values.
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
