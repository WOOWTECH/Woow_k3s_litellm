#!/usr/bin/env bash
# Compare k8s/ with what is running. Exit 0 = identical, 1 = drift (diff printed).
# Secrets are not compared (they are not in k8s/).
#   CONTEXT=woow-k3s scripts/check-drift.sh
set -euo pipefail

CONTEXT="${CONTEXT:-woow-k3s}"
cd "$(dirname "$0")/.."

if kubectl --context "$CONTEXT" diff -f k8s/; then
  echo "k8s/ is in sync with context ${CONTEXT}"
else
  rc=$?
  [ "$rc" -eq 1 ] && echo "DRIFT: k8s/ differs from context ${CONTEXT} (see diff above)" >&2
  exit "$rc"
fi
