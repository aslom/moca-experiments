# E19 — Test-user baseline: what does one `sbx` lifecycle cost, and does it hold at N users?

Version 0.1. Written before the measured run.

## Question

On a sandbox-only MOCA stack with test users enrolled, what is the latency of each step of the
`sbx` lifecycle from `TESTING-SANDBOX-ONLY.md` — login, create, `run -- id`, `run -- uname -a`,
`ls`, `rm` — and does it stay flat when N users drive it at once instead of one after another?

This is a **baseline**, not a comparison: there is no second arm. Its output is the reference
table that later experiments on this stack cite, and a check that the test-user path itself
works for more than the single `test:1000` the deployment doc exercises.

## Why it exists

`TESTING-SANDBOX-ONLY.md` verifies the lifecycle for exactly one test user and says explicitly
that it supplies no loop: "There is no loop script in this document ... the exact loop is yours
to write." It names the two ingredients for a parallel run — `SH_TEST_USERS` authorising the
range and a separate `XDG_CONFIG_HOME` per user — and stops there. E19 writes that loop, and
reports what it measures.

## Design

### Arms

One: the SBX per-`Exec` microVM tier as deployed, template `default`, mode `per-exec`.

### The unit of work

Per user, in order, as in the doc's "Testing a test user" section:

| # | Operation | What it is |
|---|---|---|
| 1 | `login` | a test-user login for `test:<N>` |
| 2 | `create` | `sbx create --template default --name tu<N>` |
| 3 | `run_id` | `sbx run --sandbox tu<N> -- id` |
| 4 | `run_uname` | `sbx run --sandbox tu<N> -- uname -a` |
| 5 | `ls` | `sbx ls` |
| 6 | `rm` | `sbx rm --force tu<N>` |

Each is timed separately, wall clock, with `time.perf_counter()` around it. A user whose
`create` fails is not asked to `run`; a user that created a sandbox always has it deleted, even
after a failure, so a run leaves nothing behind.

### Concurrency

N users, one OS thread each, all started before any is joined. The driver runs on a
**free-threaded (`nogil`) CPython 3.14** build so the N threads are genuinely concurrent. The
work each thread does is dominated by a subprocess and a network wait, so a GIL build would
very likely produce the same numbers — the free-threaded interpreter is a property of the rig
asked for here, not a claimed cause of any result. The driver records
`sys._is_gil_enabled()` in `summary.json` so a reader can tell which build produced a table,
and `rig/run.sh` passes `--require-nogil` so a silent fall back to a GIL interpreter is an
error rather than a quietly serialised run.

N = 10 for the measured run.

### Isolation

Each user gets a fresh `XDG_CONFIG_HOME` (and `XDG_STATE_HOME`) from `mkdtemp`, which is what
the deployment doc requires for parallel runs — "one profile directory per test user, so runs
do not clobber each other" — and a sandbox name of its own, `tu<N>`.

## What is measured and what is not

`login` is **one HTTPS round trip** to `POST /v1/auth/test-token`. Operations 2–6 are each a
separate `mocactl` process, so each carries a Node start-up. The two are therefore not
comparable to one another, and neither is an isolated server-side latency: this is deliberate,
because what the doc tells a tester to run is the CLI, and the CLI's cost is part of what a
tester waits for. A server-side-only measure would need the `sandbox/v1` API directly and is
out of scope here.

Cold versus warm is not controlled. The first `run` in a fresh sandbox pays for whatever the
per-`Exec` tier does on first use; `run_uname` immediately after `run_id` sees a warmer path.
Both are reported; neither is isolated.

## The login is not the documented command, and why

The documented command is `mocactl sbx login --test-user N`. On this stack it fails, from the
client, before any request is sent:

```
sbx: <url> serves no test users: only a combined control plane does
(a sandbox-profile one admits enrolled github:/oidc: subjects only, ADR-0045)
```

The server disagrees with its own client. In the same tree, `packages/control-plane/src/routes.ts`
mounts `POST /v1/auth/test-token` with `profiles: BOTH`, and says why in a comment: "BOTH, so a
sandbox-only stack can serve test logins as well." Posting to it with the shared secret against
this stack returns 200 and a token for `test:<N>`. The refusal is a stale gate in
`cmdLoginTestUser` (`packages/mocactl/src/sbx/commands.ts`), which returns a usage error unless
`/v1/discovery` reports `profile: combined`.

So the driver does what the CLI would have done had it not refused: it posts to
`/v1/auth/test-token` itself and writes the reply into
`$XDG_CONFIG_HOME/mocactl/sbx-auth.json` in the `CachedAuth` shape (`packages/mocactl/src/config.ts`),
mode 0600. Every operation after login is the unmodified CLI reading that file. No MOCA source
is patched.

This is worth reporting on its own: the deployment doc's quick-start sequence does not run as
written on a sandbox-profile stack, and the cause is the client, not the deployment.

## Pass and fail criteria

The baseline **holds** if, for N = 10:

1. every user completes all six operations without a failure, and
2. no operation's p90 exceeds its sealed bar in `predictions.json`, and
3. wall clock for the whole N = 10 run does not exceed the sealed concurrency bar — that is,
   ten concurrent users do not cost ten sequential users.

A failed prediction is reported as falsified in `RESULTS.md`, not rewritten.

## Threats to the result

- **One cluster, one run.** N = 10 with one sample per user per operation. A p90 over ten
  samples is the ninth value; it is an indication, not a tight estimate.
- **Shared cluster.** `moca-sbx` is not reserved for this run. Other load inflates it.
- **One `sbx-worker` replica** at the time of writing, so a per-user `run` contends for one
  worker. This is the deployment as it stands, not a tuned rig, and it bounds what the
  concurrency criterion can show.
- **Network position.** The driver runs on a laptop reaching an OpenShift Route over the
  internet, so every number includes that path. Numbers from a different vantage point are not
  comparable to these.
- **Test-user indices are reused across runs.** `test:1..10` by default; a previous run's
  sandbox of the same name would collide. The driver deletes what it creates, and a stale
  `tu<N>` makes `create` fail loudly rather than silently sharing.

## Sealed predictions

`predictions.json`, sealed before the measured run, calibrated from an N = 2 trial on the same
stack (`test:801`, `test:802`).
