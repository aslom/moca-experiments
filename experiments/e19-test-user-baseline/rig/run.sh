#!/usr/bin/env bash
# E19: read the cluster's settings, then run the baseline driver on a free-threaded interpreter.
#
#   KUBECONFIG=<kubeconfig> SH_SINGLE_NAMESPACE=<ns> MOCACTL_MJS=<path/to/mocactl.mjs> \
#     rig/run.sh --users 10
#
# Every argument is passed to rig/baseline.py. Nothing here is written to the repository; the
# shared secret only ever reaches the driver's environment.
set -euo pipefail

# No apostrophes in a ${VAR:?message}: bash quote-processes the message, so a lone ' there opens a
# quoted section that swallows the rest of the script.
: "${KUBECONFIG:?set KUBECONFIG to the kubeconfig of the cluster under test}"
: "${SH_SINGLE_NAMESPACE:=moca-sbx}"
: "${MOCACTL_MJS:?set MOCACTL_MJS to mocactl.mjs built from the test-user branch (pnpm --filter @moca/mocactl build)}"
PY="${PY:-3.14t}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "${MOCACTL_SBX_URL:-}" ]]; then
  domain="$(kubectl get ingresses.config/cluster -o jsonpath='{.spec.domain}')"
  export MOCACTL_SBX_URL="https://moca-control-plane.${domain}"
fi

# The installer only looks in secret/moca-test-users; older stacks keep the key in secret/moca-mu1.
if [[ -z "${MOCA_TEST_USER_SECRET:-}" ]]; then
  MOCA_TEST_USER_SECRET="$(kubectl -n "$SH_SINGLE_NAMESPACE" get secret moca-test-users \
    -o jsonpath='{.data.SH_TEST_USER_SECRET}' | base64 -d)"
  export MOCA_TEST_USER_SECRET
fi
if [[ -z "$MOCA_TEST_USER_SECRET" ]]; then
  echo "e19: no SH_TEST_USER_SECRET in secret/moca-test-users; check secret/moca-mu1" >&2
  exit 1
fi

echo "e19: control plane $MOCACTL_SBX_URL, namespace $SH_SINGLE_NAMESPACE, secret length ${#MOCA_TEST_USER_SECRET}"

# --python <N>t selects the free-threaded build: the point of the exercise, so --require-nogil
# makes a silent fall back to a GIL interpreter an error rather than a quietly serialised run.
exec uv run --python "$PY" --script "$here/baseline.py" --require-nogil "$@"
