# The write-ahead log, the dead-letter queue, and what an outage costs you

Every event the SDK buffers lives in a file before it is sent. That file is
the only copy of the event between `track()` and the backend accepting it,
so this page documents what it promises, what it refuses to promise, and the
one operational consequence that is not obvious until it has already cost
someone money.

## The files

One base path (`NULLRUN_WAL_PATH`, else `<tempdir>/nullrun.wal`) and four
files:

| File | Holds | Read by |
|---|---|---|
| `sdk.wal` | events awaiting delivery | the next `Transport.start()` |
| `sdk.wal.1` | the previous generation, after rotation | same |
| `sdk.wal.inflight` | the batch that was on the wire when the process died | same |
| `sdk.wal.dlq` | events the backend refused | **a human**, via `nullrun-wal` |
| `sdk.wal.lock` | the advisory lock, 0 bytes | `flock` |

The first three self-heal. The DLQ does not, and cannot: a refusal is by
definition not something a retry fixes, so re-sending a dead-lettered event
would be refused again. Automatic DLQ replay is the poison pill the DLQ
exists to break out of.

## What is guaranteed

* **At-least-once.** No recovery file is unlinked before its events are
  provably safe — either the backend accepted them or they were rewritten
  into a fresh `sdk.wal`. Re-delivery is therefore normal, not a bug, and it
  costs nothing: the backend dedups on `event_id`, which is why every event
  is assigned one before it reaches disk.
* **Never a duplicate-or-loss ambiguity.** Every event is durable — in the
  DLQ, or still covered by a retained `.inflight` — *before* `.inflight` is
  cleared. A crash in that window replays the batch and duplicates the send.
  The opposite ordering would lose it, and nothing in the system could
  reconstruct it.
* **A 2xx is not delivery.** `/track/batch` answers 200 while refusing
  individual events, naming them in `accepted_event_ids` and
  `rejection_details`. Only the events named in `accepted_event_ids` are
  confirmed. A 200 whose body cannot be read — a proxy's HTML page, an
  empty response, a backend predating that field — is not a confirmation
  either, and is treated as unconfirmed rather than delivered.

## What is refused

* **The DLQ size cap deletes nothing.** At `NULLRUN_DLQ_MAX_BYTES` (64 MB
  default) the append is refused, an ERROR is logged, and the event stays
  on the retry path. Trimming the oldest rows would destroy the only copy of
  events the SDK could not deliver, silently, with no trace beyond a shorter
  file. A DLQ that has stopped growing is visible and alertable; a DLQ that
  quietly lost yesterday's refusals is neither. Fix it with
  `nullrun-wal`, not by raising the cap and hoping.
* **No silent mode.** Every WAL file is 0600 — event payloads are prompts,
  completions and tool arguments, and `open(..., "w")` under a default umask
  leaves them world-readable in `/tmp`, on a shared volume, and in any
  container several services mount. Files left by an earlier run are
  tightened at startup, because a file's mode survives every atomic rename.

## Platform-dependent guarantees, and how to see them

Three protections are not available everywhere, so each is **probed rather
than assumed**, and the result is a metric:

| Metric | Values | Meaning |
|---|---|---|
| `wal_dir_fsync` | `"enabled"` / `"unavailable"` / `None` | whether a directory fsync works here. Without it the data blocks land but a power cut can still lose the *name* after a rename. |
| `wal_lock` | `"enabled"` / `"contended"` / `"unavailable"` | whether the cross-process WAL lock is in use. `"unavailable"` means no `fcntl.flock`: one writer per WAL path, so a multi-process deployment must set a per-worker `NULLRUN_WAL_PATH`. |
| `wal_lock_timeouts_total` | count | writes skipped because the lock stayed held for the whole timeout. Zero is healthy. |
| `dlq_bytes` / `dlq_overflow_total` | bytes / count | DLQ size, and how many appends the cap refused. |

`None` means "not probed yet" — distinct from `"unavailable"`, which means
"probed and refused". A degraded platform produces **one** warning per aspect
per `Transport`, not one per flush: it is a standing condition, and warning
on every cycle buries the log under a fact that cannot change.

All are in `metrics.to_dict()["transport"]`.

A green test run on Windows proves nothing about `flock` or a directory
fsync, because neither exists there. The POSIX-only assertions are marked
`skipif win32`; run the suite in a Linux container to exercise them.

## Multi-process deployments

The lock is taken around file *mutations* only, never across a network send —
holding it across a request would serialise every worker behind one
another's latency. What it guarantees is that the files stay parseable: two
processes cannot interleave lines inside each other's read-copy-append, and
the DLQ ends with every writer's rows.

It does not merge per-process buffers. With `gunicorn -w 4` on a shared
volume, each worker has its own in-memory buffer and its own events; the
correct configuration is a per-worker `NULLRUN_WAL_PATH`, and with a shared
one correctness rests on `event_id` dedup rather than on the lock.

## Inspecting and replaying the DLQ

```bash
nullrun-wal status                        # files, row counts, reasons, window
nullrun-wal replay --dry-run              # what would be sent
nullrun-wal replay --reason reservation_not_found --limit 100
nullrun-wal replay --execute              # actually send
```

`replay` is a dry run unless `--execute` is passed, and it removes a row
**only** after the backend confirms that exact `event_id`. Anything
unconfirmed stays on disk, including a line the tool could not parse: the
unparseable line is the one an operator most needs to look at, and
rewriting the file from the rows that did parse would delete it without
anyone noticing.

## What a long outage costs

> This section describes the interaction between the SDK's DLQ and an
> enforcement-lease / `direct` deployment, where events are buffered
> client-side and sent to NullRun when it is reachable again. `direct` mode
> is not implemented in this SDK yet; what follows is a statement about the
> two pieces that *are* implemented and will compose with it.

A `/check` mints an `execution_id` and binds it server-side under a **24-hour
TTL**. Reservations expire on a TTL too. So an outage longer than 24 hours
does not resume: every event buffered during it is refused with
`EXECUTION_NOT_BOUND` when it finally reaches the backend, because the
binding that would have authorised it no longer exists — and the backend
cannot re-mint one for a past event, since that would produce a *new*
execution and double-count against the budget.

The SDK treats that code as deterministic and parks the event on the first
response (`transport.py`, `_DETERMINISTIC_ERROR_CODES`). It is not retried,
because retrying is not slow here, it is pointless.

**The consequence to plan for:** the spend is real — the model was called —
and the period-bound counter did not account for it. Those events land in the
DLQ, which means the dashboard under-reports what you actually spent, by
exactly the amount buffered during the outage. The DLQ is the record of the
gap; the control center is not.

Two things follow. Re-sending them will not work, because the refusals are
deterministic, so the repair is not a replay — it is a decision about
accounting that has to be made deliberately. And an outage beyond 24 hours
needs a plan that does not assume the buffered events will be accepted when
it ends. If your agents can run unattended for more than a day against an
unreachable backend, size that budget on the assumption that it is spent and
unaccounted, not merely pending.
