#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""E19 baseline driver: N test users drive the sbx lifecycle concurrently, one OS thread each.

Run it with the free-threaded ("nogil") interpreter, which is what makes the N user threads
genuinely concurrent rather than interleaved under one GIL:

    uv run --python 3.14t rig/baseline.py --users 10

Each user gets its own XDG_CONFIG_HOME, as TESTING-SANDBOX-ONLY.md requires for parallel runs,
and its own sandbox name, so no two users touch the same profile directory or sandbox.

Why the login is an HTTP call and not `mocactl sbx login --test-user N`
----------------------------------------------------------------------
The control plane serves `POST /v1/auth/test-token` on BOTH profiles (`routes.ts`: "BOTH, so a
sandbox-only stack can serve test logins as well"), and a sandbox-profile stack with
SH_SANDBOX_TEST_USERS_ENROLLED=true answers it with a token. The CLI, however, refuses before
it asks: `cmdLoginTestUser` returns a usage error unless `/v1/discovery` reports
`profile: combined`. So on a sandbox-only stack the documented `sbx login --test-user N` fails
client-side with "serves no test users" even though the server would have logged the user in.

This driver therefore does what the CLI would have done on success: it posts to
`/v1/auth/test-token` and writes the reply to `$XDG_CONFIG_HOME/mocactl/sbx-auth.json` in the
CachedAuth shape (`config.ts`), mode 0600. Every command after login is the real CLI. The
`login` latency reported here is that one HTTP round trip, so it is NOT comparable to the other
operations, which each pay a `node` start-up; the summary says so.

Output: a line per user as it finishes, then per-operation percentiles. With --out DIR it also
writes params.json, record.json (every operation, every user) and summary.json.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

# The lifecycle from TESTING-SANDBOX-ONLY.md "Testing a test user": login, create, run id,
# run uname -a, ls, rm. `login` is handled separately (see the module docstring); the rest are
# argv templates where {name} is the per-user sandbox name.
OPS: list[tuple[str, list[str]]] = [
    ("create", ["sbx", "create", "--template", "{template}", "--name", "{name}"]),
    ("run_id", ["sbx", "run", "--sandbox", "{name}", "--", "id"]),
    ("run_uname", ["sbx", "run", "--sandbox", "{name}", "--", "uname", "-a"]),
    ("ls", ["sbx", "ls"]),
    ("rm", ["sbx", "rm", "--force", "{name}"]),
]
OP_NAMES = ["login"] + [name for name, _ in OPS]


@dataclass
class OpResult:
    user: int
    op: str
    ms: float
    ok: bool
    exit_code: int | None = None
    error: str | None = None


@dataclass
class UserResult:
    user: int
    subject: str | None = None
    sandbox_name: str | None = None
    ok: bool = True
    total_ms: float = 0.0
    ops: list[OpResult] = field(default_factory=list)


def mint_token(url: str, index: int, secret: str, timeout: float, insecure: bool) -> dict:
    """`POST /v1/auth/test-token` — the request `ControlPlaneClient.testUserToken` would send."""
    body = json.dumps({"index": index, "secret": secret}).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/auth/test-token",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    ctx = ssl._create_unverified_context() if insecure else None
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.load(resp)


def discovery(url: str, timeout: float, insecure: bool) -> dict:
    """`GET /v1/discovery`: the profile and the control-plane version, for the record's provenance."""
    ctx = ssl._create_unverified_context() if insecure else None
    req = urllib.request.Request(url.rstrip("/") + "/v1/discovery")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            d = json.load(resp)
        return {"profile": d.get("profile"), "version": d.get("version")}
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
        return {"profile": None, "version": None, "error": f"{type(e).__name__}"}


def write_sbx_auth(config_home: Path, url: str, login: dict) -> None:
    """The CachedAuth shape mocactl reads from `$XDG_CONFIG_HOME/mocactl/sbx-auth.json`."""
    auth = {
        "apiToken": login["token"],
        "subject": login["subject"],
        "roles": login.get("roles", []),
        "expiresAt": login["expiresAt"],
        "controlPlaneUrl": url,
    }
    if login.get("displayName"):
        auth["displayName"] = login["displayName"]
    if login.get("refreshToken"):
        auth["refreshToken"] = login["refreshToken"]
    if login.get("refreshExpiresAt") is not None:
        auth["refreshExpiresAt"] = login["refreshExpiresAt"]
    d = config_home / "mocactl"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "sbx-auth.json"
    f.write_text(json.dumps(auth, indent=2) + "\n")
    f.chmod(0o600)


def run_cli(argv: list[str], env: dict[str, str], timeout: float) -> tuple[int, str]:
    """One mocactl invocation. Returns (exit code, the tail of its output for an error message)."""
    try:
        p = subprocess.run(
            argv,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:g}s"
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode, out.strip()[-400:]


def drive_user(args: argparse.Namespace, index: int, secret: str, report) -> UserResult:
    """One test user's whole lifecycle, start to finish, on this thread."""
    res = UserResult(user=index, sandbox_name=f"{args.prefix}{index}")
    config_home = Path(tempfile.mkdtemp(prefix=f"e19-u{index}-"))
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(config_home)
    env["MOCACTL_SBX_URL"] = args.url
    # Keep each user's state dir apart too: the flight recorder and transcripts live there.
    env["XDG_STATE_HOME"] = str(config_home / "state")
    env.pop("MOCA_SANDBOX", None)

    user_start = time.perf_counter()

    t0 = time.perf_counter()
    try:
        login = mint_token(args.url, index, secret, args.timeout, args.insecure)
        ms = (time.perf_counter() - t0) * 1000
        write_sbx_auth(config_home, args.url, login)
        res.subject = login["subject"]
        res.ops.append(OpResult(index, "login", ms, True))
        report(index, "login", ms, True, None)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, KeyError, ValueError) as e:
        ms = (time.perf_counter() - t0) * 1000
        detail = e.read()[:300].decode(errors="replace") if isinstance(e, urllib.error.HTTPError) else str(e)
        res.ok = False
        res.ops.append(OpResult(index, "login", ms, False, error=f"{type(e).__name__}: {detail}"))
        res.total_ms = (time.perf_counter() - user_start) * 1000
        report(index, "login", ms, False, detail)
        return res

    base = [args.node, args.mocactl]
    created = False
    for op, template in OPS:
        if op == "rm" and not created:
            break  # nothing to delete
        argv = base + [a.format(name=res.sandbox_name, template=args.template) for a in template]
        t0 = time.perf_counter()
        code, out = run_cli(argv, env, args.timeout)
        ms = (time.perf_counter() - t0) * 1000
        # mocactl's sbx commands report failure in their output, and exit 0 on some paths, so a
        # non-zero code is not the only signal: treat an "sbx: " error prefix as a failure too.
        ok = code == 0 and not out.startswith("sbx: ")
        res.ops.append(OpResult(index, op, ms, ok, exit_code=code, error=None if ok else out))
        report(index, op, ms, ok, None if ok else out)
        if op == "create" and ok:
            created = True
        if not ok:
            res.ok = False
            if op == "create":
                break  # without a sandbox the rest is meaningless
            if args.stop_on_error:
                break

    # Always try to clean up a sandbox we created but did not reach `rm` for, so a failed run
    # does not leave sandboxes behind for the next one. Not timed: it is not part of the measure.
    if created and not any(o.op == "rm" and o.ok for o in res.ops):
        run_cli(base + ["sbx", "rm", "--force", res.sandbox_name], env, args.timeout)

    res.total_ms = (time.perf_counter() - user_start) * 1000
    return res


class OrderedReporter:
    """Prints one line per user **in user order**, as soon as that user's turn comes up.

    The users run concurrently, so they finish out of order: at N=100 user 97 may well land
    before user 12. Printing on completion would give a list no reader can scan against
    `test:1..N`, so a finished user is held until every user before it has printed. The cost is
    that one slow user holds back the lines behind it; what arrives is still a stream, and it is
    in the order the reader expects. `--progress ops` is there for when the raw completion order
    is what you want to see.
    """

    def __init__(self, indices: list[int], emit) -> None:
        self._slot_of = {ix: i for i, ix in enumerate(indices)}
        self._held: list[UserResult | None] = [None] * len(indices)
        self._next = 0
        self._lock = threading.Lock()
        self._emit = emit

    def done(self, res: UserResult) -> None:
        with self._lock:
            self._held[self._slot_of[res.user]] = res
            while self._next < len(self._held) and self._held[self._next] is not None:
                self._emit(self._held[self._next])
                self._next += 1


def user_line(res: UserResult, width: int) -> str:
    per_op = "  ".join(f"{o.op}={o.ms:.0f}" for o in res.ops)
    line = (
        f"  test:{str(res.user):<{width}} {'ok  ' if res.ok else 'FAIL'}"
        f" total={res.total_ms:>8.0f} ms   {per_op}"
    )
    if not res.ok:
        err = next((o.error for o in res.ops if o.error), None)
        if err:
            line += f"   <- {err.splitlines()[0][:120]}"
    return line


def redact_url(url: str) -> str:
    """A control-plane URL with the cluster domain replaced by a placeholder.

    CONVENTIONS.md: "No hosts or credentials ... docs use placeholders". The first label of the
    host is the service (`moca-control-plane`) and is not a secret or an address by itself, so it
    stays; everything after it is the cluster's domain and does not go into the repository.
    """
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if not host:
        return "<CONTROL_PLANE_URL>"
    first, _, rest = host.partition(".")
    shown = f"{first}.<ROUTE_DOMAIN>" if rest else "<CONTROL_PLANE_HOST>"
    port = f":{parsed.port}" if parsed.port else ""
    return urllib.parse.urlunsplit((parsed.scheme, shown + port, parsed.path, "", ""))


def pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile: at these sample sizes interpolation would invent precision."""
    if not values:
        return float("nan")
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(q / 100 * (len(s) - 1)))))
    return s[k]


def summarize(users: list[UserResult], wall_ms: float, args: argparse.Namespace, disco: dict) -> dict:
    by_op: dict[str, list[float]] = {name: [] for name in OP_NAMES}
    fails: dict[str, int] = {name: 0 for name in OP_NAMES}
    for u in users:
        for o in u.ops:
            if o.ok:
                by_op[o.op].append(o.ms)
            else:
                fails[o.op] += 1
    ops_summary = {}
    for name in OP_NAMES:
        v = by_op[name]
        ops_summary[name] = {
            "n": len(v),
            "failed": fails[name],
            "min_ms": round(min(v), 1) if v else None,
            "p50_ms": round(pct(v, 50), 1) if v else None,
            "p90_ms": round(pct(v, 90), 1) if v else None,
            "p99_ms": round(pct(v, 99), 1) if v else None,
            "max_ms": round(max(v), 1) if v else None,
            "mean_ms": round(statistics.fmean(v), 1) if v else None,
        }
    totals = [u.total_ms for u in users if u.ok]
    return {
        "users": len(users),
        "users_ok": sum(1 for u in users if u.ok),
        "users_failed": sum(1 for u in users if not u.ok),
        "concurrency": getattr(args, "in_flight", args.users),
        "gil_enabled": gil_enabled(),
        "python": sys.version.split()[0],
        "cluster": args.label or None,
        "control_plane_profile": disco.get("profile"),
        "control_plane_version": disco.get("version"),
        "wall_ms": round(wall_ms, 1),
        "per_user_total_ms": {
            "min": round(min(totals), 1) if totals else None,
            "p50": round(pct(totals, 50), 1) if totals else None,
            "p90": round(pct(totals, 90), 1) if totals else None,
            "p99": round(pct(totals, 99), 1) if totals else None,
            "max": round(max(totals), 1) if totals else None,
            "mean": round(statistics.fmean(totals), 1) if totals else None,
        },
        "ops": ops_summary,
    }


def gil_enabled() -> bool | None:
    f = getattr(sys, "_is_gil_enabled", None)
    return f() if f else None


def print_stats(summary: dict) -> None:
    print()
    print("=" * 76)
    gil = summary["gil_enabled"]
    mode = "free-threaded (GIL off)" if gil is False else "GIL on" if gil else "unknown"
    conc = summary["concurrency"]
    at_once = "all at once" if conc >= summary["users"] else f"at most {conc} at once"
    print(f"E19 baseline: {summary['users']} users ({at_once}), python {summary['python']}, {mode}")
    print(
        f"  ok {summary['users_ok']}/{summary['users']}"
        f"   wall {summary['wall_ms'] / 1000:.1f}s"
        f"   control plane {summary['control_plane_profile']} {summary['control_plane_version']}"
    )
    print("=" * 76)
    print(
        f"{'operation':<12}{'n':>5}{'fail':>6}{'min':>10}{'p50':>10}{'p90':>10}"
        f"{'p99':>10}{'max':>10}{'mean':>10}"
    )
    for name in OP_NAMES:
        s = summary["ops"][name]
        if not s["n"] and not s["failed"]:
            continue
        cells = "".join(
            f"{(f'{s[k]:.0f}' if s[k] is not None else '-'):>10}"
            for k in ("min_ms", "p50_ms", "p90_ms", "p99_ms", "max_ms", "mean_ms")
        )
        print(f"{name:<12}{s['n']:>5}{s['failed']:>6}{cells}")
    t = summary["per_user_total_ms"]
    cells = "".join(
        f"{(f'{t[k]:.0f}' if t[k] is not None else '-'):>10}"
        for k in ("min", "p50", "p90", "p99", "max", "mean")
    )
    print(f"{'-' * 83}")
    print(f"{'user total':<12}{summary['users_ok']:>5}{summary['users_failed']:>6}{cells}")
    print("all times are milliseconds, wall clock. `login` is one HTTP call; every other row is a")
    print("mocactl process, so each carries ~node start-up. See rig/baseline.py for why.")


def main() -> int:
    ap = argparse.ArgumentParser(description="E19: N test users drive the sbx lifecycle in parallel")
    ap.add_argument("--users", type=int, default=10, help="how many test users (default 10)")
    ap.add_argument("--first-user", type=int, default=1, help="first test-user index (default 1)")
    ap.add_argument("--prefix", default="tu", help="sandbox name prefix (default tu)")
    ap.add_argument("--template", default="default", help="sandbox template (default: default)")
    ap.add_argument("--url", default=os.environ.get("MOCACTL_SBX_URL", ""), help="control-plane URL (or MOCACTL_SBX_URL)")
    ap.add_argument("--mocactl", default=os.environ.get("MOCACTL_MJS", ""), help="path to mocactl.mjs built from the test-user branch (or MOCACTL_MJS)")
    ap.add_argument("--node", default=os.environ.get("NODE", "node"), help="node binary (default node)")
    ap.add_argument("--timeout", type=float, default=180.0, help="per-operation timeout in seconds (default 180)")
    ap.add_argument("--stop-on-error", action="store_true", help="abandon a user's remaining operations after a failure")
    ap.add_argument("--out", default="", help="directory to write params.json, record.json and summary.json")
    ap.add_argument("--label", default=os.environ.get("E19_CLUSTER_LABEL", ""), help="a name for the cluster, recorded instead of its host (or E19_CLUSTER_LABEL)")
    ap.add_argument("--require-nogil", action="store_true", help="exit 2 unless the interpreter is free-threaded")
    ap.add_argument(
        "--progress",
        choices=("user", "ops", "none"),
        default="user",
        help="user: one line per user, in user order, as each finishes (default). "
        "ops: a line per operation in completion order, which at large N is a lot of output. "
        "none: only the final table.",
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=0,
        help="how many users may be in flight at once (default 0 = all of them). "
        "Below --users this separates 'N users' from 'N at once'.",
    )
    args = ap.parse_args()
    if args.users < 1:
        print("e19: --users must be at least 1", file=sys.stderr)
        return 2
    if args.concurrency < 0:
        print("e19: --concurrency cannot be negative", file=sys.stderr)
        return 2
    in_flight = args.concurrency if 0 < args.concurrency < args.users else args.users
    args.in_flight = in_flight

    secret = os.environ.get("MOCA_TEST_USER_SECRET", "")
    # Same reason headless.ts gives for keeping it out of a flag: a flag puts the shared secret in
    # the shell history and in every `ps` listing of the machine running the test.
    if not secret:
        print("e19: MOCA_TEST_USER_SECRET is not set (the cluster's SH_TEST_USER_SECRET)", file=sys.stderr)
        return 2
    if not args.url:
        print("e19: no control-plane URL: pass --url or set MOCACTL_SBX_URL", file=sys.stderr)
        return 2
    if not args.mocactl or not Path(args.mocactl).is_file():
        print(
            "e19: --mocactl must point at mocactl.mjs built from the test-user branch "
            "(pnpm --filter @moca/mocactl build)",
            file=sys.stderr,
        )
        return 2
    if args.require_nogil and gil_enabled() is not False:
        print(
            "e19: this interpreter is not free-threaded; run it as "
            "`uv run --python 3.14t rig/baseline.py`",
            file=sys.stderr,
        )
        return 2
    args.insecure = os.environ.get("E19_INSECURE_TLS") == "1"

    indices = list(range(args.first_user, args.first_user + args.users))
    width = len(str(indices[-1]))
    gil = gil_enabled()
    mode = "free-threaded" if gil is False else "GIL on" if gil else "unknown GIL state"
    disco = discovery(args.url, args.timeout, args.insecure)
    print(f"e19: {args.users} users (test:{indices[0]}..test:{indices[-1]}) against {args.url}")
    print(f"e19: control plane profile {disco.get('profile')}, version {disco.get('version')}")
    print(f"e19: python {sys.version.split()[0]} {mode}, one thread per user")
    if in_flight < args.users:
        print(f"e19: at most {in_flight} users in flight at once")
    print()

    lock = threading.Lock()
    counter = {"done": 0}

    def report(user: int, op: str, ms: float, ok: bool, err: str | None) -> None:
        """Per-operation progress. Only in --progress ops: at N=100 this is 600 lines."""
        if args.progress != "ops":
            return
        with lock:
            mark = "ok " if ok else "FAIL"
            line = f"  test:{user:<{width}} {op:<10} {ms:>9.0f} ms  {mark}"
            if err:
                line += f"  {err.splitlines()[0][:150]}"
            print(line, flush=True)

    def emit_user(res: UserResult) -> None:
        counter["done"] += 1  # only ever called under OrderedReporter's lock
        print(f"{user_line(res, width)}   [{counter['done']}/{args.users}]", flush=True)

    ordered = OrderedReporter(indices, emit_user) if args.progress == "user" else None
    if ordered:
        print(f"per-user results, in user order as each finishes (of {args.users}):")

    results: list[UserResult | None] = [None] * len(indices)
    threads: list[threading.Thread] = []
    gate = threading.Semaphore(in_flight)

    def worker(slot: int, index: int) -> None:
        with gate:
            res = drive_user(args, index, secret, report)
        results[slot] = res
        if ordered:
            ordered.done(res)

    t_start = time.perf_counter()
    for slot, index in enumerate(indices):
        t = threading.Thread(target=worker, args=(slot, index), name=f"u{index}")
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    wall_ms = (time.perf_counter() - t_start) * 1000

    users = [r for r in results if r is not None]
    # In --progress user the lines are already out, in order, so repeating them here would double
    # a hundred-line block for nothing.
    if args.progress != "user":
        print()
        print("per-user totals:")
        for u in sorted(users, key=lambda u: u.user):
            print(user_line(u, width))

    failed = [u for u in users if not u.ok]
    if failed and args.progress == "none":
        print()
        print(f"failures ({len(failed)}):")
        for u in failed:
            print(user_line(u, width))

    summary = summarize(users, wall_ms, args, disco)
    print_stats(summary)

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        params = {k: v for k, v in vars(args).items() if k not in ("mocactl", "url")}
        params["mocactl"] = Path(args.mocactl).name
        # CONVENTIONS.md: no hosts in a committed record. The URL is still identified -- its
        # scheme, its service name and any path prefix are what distinguish two control planes
        # (config.ts canonicalises on exactly those) -- with only the cluster domain withheld.
        params["url"] = redact_url(args.url)
        params["cluster"] = args.label or "unlabelled (pass --label)"
        params["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - wall_ms / 1000))
        (out / "params.json").write_text(json.dumps(params, indent=2) + "\n")
        (out / "record.json").write_text(
            json.dumps({"users": [asdict(u) for u in sorted(users, key=lambda u: u.user)]}, indent=2) + "\n"
        )
        (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(f"\nwrote {out}/params.json, record.json, summary.json")

    return 0 if summary["users_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
