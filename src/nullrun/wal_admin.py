"""Operator tooling for the SDK's write-ahead log: ``nullrun-wal``.

The WAL has three files, and only one of them is well served:

* ``<path>`` / ``<path>.1`` — events awaiting delivery. Self-healing: they are
  replayed automatically on the next ``Transport.start()``.
* ``<path>.inflight`` — the batch that was on the wire when the process died.
  Also self-healing.
* ``<path>.dlq`` — events the backend refused and the SDK gave up on.

The DLQ is the odd one out. Nothing replays it, because a refusal is by
definition not something a retry fixes: re-sending an event the backend
already rejected would be rejected again, and the natural-looking "retry the
DLQ" loop is exactly the poison pill the DLQ exists to break out of. So the
DLQ needs a human, and a human needs a command.

What it will not do is guess. ``replay`` only removes a row once the backend
has confirmed that exact ``event_id``; anything unconfirmed stays on disk.
That is the same at-least-once discipline the transport itself keeps, and
the same dedup key makes the re-send free if it lands twice.

Usage::

    nullrun-wal status
    nullrun-wal replay --dry-run
    nullrun-wal replay --reason reservation_not_found --limit 100
    nullrun-wal replay --execute
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import tempfile
from collections import Counter
from typing import Any

__all__ = ["main", "read_dlq", "dlq_paths", "default_wal_path"]

_DEFAULT_WAL = os.path.join(tempfile.gettempdir(), "nullrun.wal")


def default_wal_path() -> str:
    """The WAL path this host would use, honouring ``NULLRUN_WAL_PATH``.

    Mirrors ``Transport._wal_path``. Duplicated rather than imported so the
    CLI can answer "where is my WAL" without constructing a Transport, which
    would create an httpx client and touch the permissions of files it has no
    business touching yet.
    """
    return os.environ.get("NULLRUN_WAL_PATH") or _DEFAULT_WAL


def dlq_paths(wal_path: str) -> dict[str, str]:
    """The four files that make up one WAL, for display and for the CLI."""
    return {
        "active": wal_path,
        "rotated": f"{wal_path}.1",
        "inflight": f"{wal_path}.inflight",
        "dlq": f"{wal_path}.dlq",
        "lock": f"{wal_path}.lock",
    }


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def read_dlq(dlq_path: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Read DLQ rows tolerantly. Returns ``(rows, unparseable_lines)``.

    A DLQ is a recovery file, so a single corrupt line must not make the rest
    unreadable — a reader that required every line to parse would turn one
    partial write into total loss. Unparseable lines are returned rather than
    dropped so the caller can say how many there are; the operator decides
    what to do with them, which is not this tool's call to make silently.
    """
    rows: list[dict[str, Any]] = []
    corrupt: list[str] = []
    try:
        with open(dlq_path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return rows, corrupt
    except OSError as e:
        print(f"error: cannot read {dlq_path}: {e}", file=sys.stderr)
        return rows, corrupt
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            corrupt.append(stripped[:200])
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
        else:
            corrupt.append(stripped[:200])
    return rows, corrupt


def _row_reason(row: dict[str, Any]) -> str:
    """The machine-readable reason. v1 rows only carry ``error``."""
    reason = row.get("reason") or row.get("error") or "unspecified"
    return str(reason)


def _row_event(row: dict[str, Any]) -> dict[str, Any] | None:
    event = row.get("event")
    return event if isinstance(event, dict) else None


def _cmd_status(args: argparse.Namespace) -> int:
    wal_path = args.wal
    paths = dlq_paths(wal_path)
    print(f"WAL: {wal_path}")
    for name, path in paths.items():
        exists = os.path.exists(path)
        print(f"  {name:<9} {path}  {os.path.getsize(path) if exists else '-'} bytes")

    rows, corrupt = read_dlq(paths["dlq"])
    if not rows and not corrupt:
        print("\nDLQ is empty.")
        return 0

    print(f"\nDLQ: {len(rows)} row(s)")
    if corrupt:
        print(f"  {len(corrupt)} unparseable line(s) — left untouched")
    for reason, count in Counter(_row_reason(r) for r in rows).most_common():
        print(f"  {count:>6}  {reason}")
    oldest = min((r.get("first_failed_at") or 0) for r in rows)
    newest = max((r.get("first_failed_at") or 0) for r in rows)
    if oldest:
        fmt = "%Y-%m-%d %H:%M:%S UTC"
        print(
            "  window: "
            f"{datetime.datetime.fromtimestamp(oldest, datetime.timezone.utc).strftime(fmt)}"
            " .. "
            f"{datetime.datetime.fromtimestamp(newest, datetime.timezone.utc).strftime(fmt)}"
        )
    print("\nReplay with: nullrun-wal replay --dry-run")
    return 0


def _select_rows(
    rows: list[dict[str, Any]], reason: str | None, limit: int | None
) -> list[dict[str, Any]]:
    selected = [r for r in rows if reason is None or _row_reason(r) == reason]
    return selected[:limit] if limit else selected


def _cmd_replay(args: argparse.Namespace) -> int:
    from nullrun.transport import Transport

    paths = dlq_paths(args.wal)
    rows, corrupt = read_dlq(paths["dlq"])
    if corrupt:
        print(
            f"note: {len(corrupt)} unparseable DLQ line(s) will not be replayed "
            "and are left in place",
            file=sys.stderr,
        )
    selected = _select_rows(rows, args.reason, args.limit)
    if not selected:
        print("nothing to replay")
        return 0

    replayable = [(r, _row_event(r)) for r in selected]
    missing = [r for r, e in replayable if e is None]
    if missing:
        print(
            f"error: {len(missing)} selected row(s) carry no event payload and "
            "cannot be replayed; they stay in the DLQ",
            file=sys.stderr,
        )
    events = [e for _, e in replayable if e is not None]
    if not events:
        return 1

    counts = Counter(_row_reason(r) for r, _ in replayable)
    print(f"selected {len(events)} event(s):")
    for reason, count in counts.most_common():
        print(f"  {count:>6}  {reason}")

    if args.dry_run or not args.execute:
        print("\ndry run — nothing sent, nothing removed. Add --execute to send.")
        return 0

    api_key = args.api_key or os.environ.get("NULLRUN_API_KEY")
    if not api_key:
        print(
            "error: no API key. Pass --api-key or set NULLRUN_API_KEY.",
            file=sys.stderr,
        )
        return 1

    # The CLI gets its OWN WAL. Pointing it at the operator's live WAL would
    # mean a replay run racing the application's flushes over the same files,
    # and would leave the application's own recovery files rewritten by a
    # one-shot maintenance command. `Transport` reads the path from the
    # environment, so the override is scoped to the block and restored after.
    previous_wal = os.environ.get("NULLRUN_WAL_PATH")
    with tempfile.TemporaryDirectory(prefix="nullrun-wal-replay-") as scratch:
        os.environ["NULLRUN_WAL_PATH"] = os.path.join(scratch, "replay.wal")
        try:
            transport = Transport(args.api_url, api_key=api_key, secret_key=args.secret_key)
            try:
                for event in events:
                    transport.track(event)
                transport._do_flush()  # no start(): we want no background thread
                unconfirmed = {str(e.get("event_id")) for e in transport._buffer}
            finally:
                transport.stop(flush=False)
        finally:
            if previous_wal is None:
                os.environ.pop("NULLRUN_WAL_PATH", None)
            else:
                os.environ["NULLRUN_WAL_PATH"] = previous_wal

    if unconfirmed:
        print(
            f"\n{len(unconfirmed)} event(s) were not confirmed and stay in the DLQ.",
            file=sys.stderr,
        )
    confirmed_ids = {
        str(_row_event(r).get("event_id"))  # type: ignore[union-attr]
        for r, _ in replayable
        if str(_row_event(r).get("event_id")) not in unconfirmed  # type: ignore[union-attr]
    }
    if not confirmed_ids:
        print("\nnothing confirmed; the DLQ is unchanged.")
        return 1

    removed = _drop_confirmed(paths["dlq"], confirmed_ids)
    print(
        f"\nsent {len(confirmed_ids)} event(s); removed {removed} row(s) from the DLQ."
    )
    return 0


def _drop_confirmed(dlq_path: str, confirmed_ids: set[str]) -> int:
    """Rewrite the DLQ without the confirmed rows. Returns how many went.

    A line-wise filter over the raw file, not a re-serialisation of the
    parsed rows: a line this tool could not parse is exactly the line an
    operator most needs to look at, and round-tripping the file through
    ``json.dumps`` of the rows that DID parse would silently delete the ones
    that did not — plus rewrite the ones that did, losing whatever formatting
    the record carried. Only lines whose ``event_id`` the backend confirmed
    are dropped; everything else, parsable or not, is copied verbatim.
    """
    if not confirmed_ids:
        return 0
    try:
        with open(dlq_path) as f:
            original = f.readlines()
    except OSError as e:
        print(f"error: cannot read {dlq_path}: {e}", file=sys.stderr)
        return 0

    kept: list[str] = []
    removed = 0
    for line in original:
        stripped = line.strip()
        drop = False
        if stripped:
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                event = parsed.get("event")
                eid = event.get("event_id") if isinstance(event, dict) else None
                drop = eid is not None and str(eid) in confirmed_ids
        if drop:
            removed += 1
        else:
            kept.append(line if line.endswith("\n") else line + "\n")

    if removed == 0:
        return 0

    tmp = f"{dlq_path}.tmp.{os.getpid()}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.writelines(kept)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dlq_path)
    except OSError as e:
        print(f"error: could not rewrite {dlq_path}: {e}", file=sys.stderr)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return 0
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nullrun-wal",
        description="Inspect and replay the NullRun SDK write-ahead log.",
    )
    parser.add_argument(
        "--wal",
        default=default_wal_path(),
        help="WAL path (default: $NULLRUN_WAL_PATH, else the tempdir).",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show WAL files and a summary of the DLQ.")

    replay = sub.add_parser(
        "replay",
        help="Re-send dead-lettered events, dropping only the confirmed ones.",
    )
    replay.add_argument(
        "--reason",
        help="Only replay rows with this exact reason (default: all).",
    )
    replay.add_argument("--limit", type=int, help="Replay at most N rows.")
    replay.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be sent. Implied unless --execute is given.",
    )
    replay.add_argument(
        "--execute",
        action="store_true",
        help="Actually send. Without it, replay is a dry run.",
    )
    replay.add_argument(
        "--api-url", default=os.environ.get("NULLRUN_API_URL", "https://api.nullrun.io")
    )
    replay.add_argument("--api-key", help="Defaults to $NULLRUN_API_KEY.")
    replay.add_argument("--secret-key", help="HMAC signing key, if configured.")

    args = parser.parse_args(argv)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "replay":
        return _cmd_replay(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
