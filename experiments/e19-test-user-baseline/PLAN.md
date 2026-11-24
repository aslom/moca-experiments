# E19 — how to build and run it

## What you need

- A sandbox-only MOCA stack with test users enrolled: `SH_TEST_USERS` ≥ N and
  `SH_SANDBOX_TEST_USERS_ENROLLED=true`. Check both without guessing:

  ```bash
  export KUBECONFIG=<kubeconfig>
  export SH_SINGLE_NAMESPACE=moca-sbx
  kubectl -n "$SH_SINGLE_NAMESPACE" get cm moca-settings -o jsonpath='{.data.SH_TEST_USERS}'; echo
  kubectl -n "$SH_SINGLE_NAMESPACE" get cm sbx-settings -o jsonpath='{.data.SH_SANDBOX_TEST_USERS_ENROLLED}'; echo
  ```

  `SH_SANDBOX_TEST_USERS_ENROLLED` must read literally `true`; `1` and `TRUE` do nothing.

- `kubectl` access to that namespace, only to read the shared secret. The driver itself needs
  no cluster access.
- `uv`, and a free-threaded CPython: `uv python install 3.14t`.
- `mocactl` built from a branch that has `sbx` — the installed binary will not do. See below.

## Step 1 — build the client

`mocactl.mjs` is built from the MOCA repo, not this one, and is not committed here.

```bash
git clone --branch test-user-deployment https://github.com/aslom/moca.git moca-src
cd moca-src
pnpm install
pnpm --filter @moca/mocactl build        # -> packages/mocactl/dist/mocactl.mjs
export MOCACTL_MJS="$PWD/packages/mocactl/dist/mocactl.mjs"
```

Check it has the sbx commands: `node "$MOCACTL_MJS" sbx --help`.

Note what the driver does **not** need from this build: `sbx login --test-user N` refuses to run
against a sandbox-profile control plane (SPEC.md, "The login is not the documented command").
The driver mints the token itself. Everything after login is this binary, unpatched.

## Step 2 — check the secret is where the driver will look

```bash
kubectl -n "$SH_SINGLE_NAMESPACE" get secret moca-test-users \
  -o jsonpath='{.data.SH_TEST_USER_SECRET}' | base64 -d | wc -c     # 64 for a hex-32 value
```

Empty means this stack keeps the key in `secret/moca-mu1` instead; export
`MOCA_TEST_USER_SECRET` by hand in that case and `rig/run.sh` will use it.

## Step 3 — a two-user trial, on indices the measured run does not use

```bash
cd experiments/e19-test-user-baseline
KUBECONFIG=<kubeconfig> SH_SINGLE_NAMESPACE=moca-sbx MOCACTL_MJS=<path> \
  ./rig/run.sh --users 2 --first-user 801
```

Expect six `ok` lines per user and a table at the end. If `login` fails with 401, the secret
does not match the cluster. If it fails with 404, the stack has no test users
(`SH_TEST_USERS` unset) — the route is genuinely unmounted, not merely refusing.

## Step 4 — seal the predictions

`predictions.json` is committed before the measured run. Already done for the run in
`RESULTS.md`; redo it if you change the bars.

## Step 5 — the measured run

```bash
KUBECONFIG=<kubeconfig> SH_SINGLE_NAMESPACE=moca-sbx MOCACTL_MJS=<path> \
  ./rig/run.sh --users 10 --first-user 1 --out results/n10
```

`--out` writes `params.json`, `record.json` (every operation of every user) and `summary.json`.
Exit status is 0 only if every user completed.

## Step 6 — write it up

`RESULTS.md`: the table, the verdict on each sealed prediction, and the caveats. Every number
traces to a file under `results/`.

## Re-running

The driver deletes each sandbox it creates, including after a failure, so indices are reusable.
If a run is killed mid-flight, a `tu<N>` may survive; it makes the next `create` for that index
fail loudly rather than silently sharing. Clean up by hand:

```bash
XDG_CONFIG_HOME=$(mktemp -d) node "$MOCACTL_MJS" sbx ls      # after a login for that user
```

## Knobs

| Flag | Default | What it does |
|---|---|---|
| `--users N` | 10 | how many test users, one thread each |
| `--first-user N` | 1 | first index, so trial runs can avoid the measured range |
| `--prefix S` | `tu` | sandbox name prefix |
| `--template ID` | `default` | sandbox template |
| `--timeout S` | 180 | per-operation timeout, seconds |
| `--stop-on-error` | off | abandon a user's remaining operations after a failure |
| `--out DIR` | — | write `params.json`, `record.json`, `summary.json` |
| `--label S` | — | a name for the cluster, recorded instead of its host |
| `--require-nogil` | set by `run.sh` | fail rather than run under a GIL |
| `--progress user\|ops\|none` | `user` | see below |
| `--concurrency K` | 0 (= all) | how many users may be in flight at once |
| `PY=<ver>` | `3.14t` | the interpreter `run.sh` asks `uv` for |

### `--progress`

- `user` (default): one line per user **in user order** — `test:1`, then `test:2`, and so on —
  printed as each becomes available. The users finish out of order, so a finished user is held
  until every user before it has printed; the result is a stream a reader can scan against
  `test:1..N`, at the cost of one slow user holding back the lines behind it. Each line ends with
  a `[done/total]` counter, which is the completion order.
- `ops`: a line per operation, in completion order, as it happens. Six lines per user — 600 at
  N = 100 — so this is for small N and for watching where a hang is.
- `none`: only the final table, plus a list of failures if there were any.

### `--concurrency`

By default all N users are in flight at once, which is the measurement the baseline is about. Set
`--concurrency K` to hold at most K in flight and the rig separates "N users" from "N at once" —
useful for telling a server-side limit from a limit of the machine running the driver. The value
used is recorded as `concurrency` in `summary.json`.

Note what N = 100 asks of the driver host: 100 users in flight means up to 100 concurrent `node`
processes, about 93 MB resident each. On the host used for the N = 100 rung (18 cores, 64 GB) that
is comfortable, but on a smaller machine the driver, not the cluster, becomes the bottleneck —
and the table would then be measuring the laptop. Check before trusting a large rung.

## Rungs

| N | Predictions | Record |
|---|---|---|
| 10 | [`predictions.json`](predictions.json) | [`results/n10/`](results/n10) |
| 100 | [`predictions-n100.json`](predictions-n100.json) | [`results/n100/`](results/n100) |

Each rung's predictions are committed before that rung runs. `predictions.json` is not edited to
add a rung; a new rung gets its own file, as E15 does with `predictions-metal.json`.

```bash
KUBECONFIG=<kubeconfig> SH_SINGLE_NAMESPACE=moca-sbx MOCACTL_MJS=<path> \
  ./rig/run.sh --users 100 --first-user 1 --out results/n100 --label "<cluster>"
```
