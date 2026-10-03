#!/usr/bin/env bash
# Linux gate for the WAL / DLQ layer.
#
# Why this is a script in the repository and not a command someone remembers:
# the whole point of running it on Linux is the coverage Windows cannot
# provide. `fcntl.flock` does not exist on win32, so every test that needs a
# second process — or a second thread holding its own descriptor — to contend
# for the lock is `skipif win32`. On a Windows-only run those tests are not
# "passing"; they are absent, and the suite reports green. The bug this gate
# exists to catch is already a historical fact: the first implementation
# took the lock non-blocking and skipped the write on contention, which on a
# 4-worker deployment meant DLQ writes that never landed and no alert.
#
# `--repeat` is not optional politeness. Contention bugs are timing bugs, and
# a single green run of a lock test is weak evidence: the barrier release in
# `test_flock_excludes_two_threads_of_one_process` has to actually collide to
# prove anything. Twenty consecutive runs is the cheapest way to turn "did not
# happen on my machine" into a number.
#
# Usage:
#   scripts/linux_gate.sh                 # 20 runs of the WAL hygiene file
#   REPEAT=50 scripts/linux_gate.sh      # more
#   REPEAT=1 scripts/linux_gate.sh       # smoke
#
# Requires Docker. Runs the suite against the repo mounted read-only-ish at
# /app with the image's own Python, so nothing local leaks in.

set -euo pipefail

REPEAT="${REPEAT:-20}"
IMAGE="${IMAGE:-python:3.11-slim}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${TARGET:-tests/test_wal_hygiene.py}"

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker is required — this gate asserts POSIX behaviour that" >&2
  echo "       Windows cannot execute, and running it anywhere else is a" >&2
  echo "       false pass. Install Docker or run this in CI." >&2
  exit 2
fi

# The container needs the `dev` extra for pytest. Kept in one place so a
# dependency change is a one-line diff rather than an argument list.
RUNNER="python -m pytest -q ${TARGET}"

echo "linux gate: ${REPEAT} run(s) of ${TARGET} in ${IMAGE}"
failures=0
for i in $(seq 1 "$REPEAT"); do
  if out="$(
    MSYS_NO_PATHCONV=1 docker run --rm \
      -v "${REPO_ROOT}:/app" -w /app \
      -e PYTHONDONTWRITEBYTECODE=1 \
      "${IMAGE}" sh -c "pip install -q -e '.[dev]' && ${RUNNER}" 2>&1
  )"; then
    printf '  run %2d/%s  ok    %s\n' "$i" "$REPEAT" "$(printf '%s' "$out" | tail -1)"
  else
    failures=$((failures + 1))
    printf '  run %2d/%s  FAIL\n' "$i" "$REPEAT"
    printf '%s\n' "$out" | tail -40
  fi
done

echo
if [ "$failures" -ne 0 ]; then
  echo "linux gate: ${failures}/${REPEAT} run(s) failed. Not a flake verdict —" >&2
  echo "            a lock or concurrency bug is order-dependent, and an" >&2
  echo "            intermittent failure in THIS file is the signature." >&2
  exit 1
fi
echo "linux gate: ${REPEAT}/${REPEAT} clean."
