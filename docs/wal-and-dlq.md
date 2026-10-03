# The write-ahead log, the dead-letter queue, and what an outage costs you

Every event the SDK buffers lives in a file before it is sent. That file is
the only copy of the event between `track()` and the backend accepting it,
so this page documents what it promises, what it refuses to promise, and the
one operational consequence that is not obvious until it has already cost
someone money.

## The files

One base path (`NULLRUN_WAL_PATH`, else `<tempdir>/nullrun.wal`) and five
files:

| File | Holds | Read by |
|---|---|---|
| `sdk.wal` | events awaiting delivery | the next `Transport.start()` |
| `sdk.wal.1` | the previous generation, after rotation | same |
| `sdk.wal.inflight` | the batch that was on the wire when the process died | same |
| `sdk.wal.holdover` | refusals the full DLQ had no room for | same — as a hold, never as a send |
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
* **A held refusal is durable and is never re-sent.** When the DLQ is at its
  cap the refusal goes to `sdk.wal.holdover` and stays off the send path.
  Both halves matter. Durable, because a memory-only hold is a `kill -9`
  from losing the record — and the file that looked like it covered this,
  `.inflight`, is overwritten by the next batch that goes on the wire. Never
  re-sent, because a refusal is terminal: sending it again produces the same
  refusal while occupying the head of the buffer, so the healthy events
  behind it are never delivered. On restart the holdover is recovered as a
  hold, not as a replay, and drains to the DLQ on the first flush that finds
  room.

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
| `wal_dir_fsync` | `"enabled"` / `"unavailable"` / `None` | whether a directory fsync *syscall* succeeds on this platform. It does not tell you the data survives a power cut — see the fsync scope note below. |
| `wal_lock` | `"enabled"` / `"contended"` / `"unavailable"` | whether the cross-process WAL lock is in use. `"unavailable"` means no `fcntl.flock`: one writer per WAL path, so a multi-process deployment must set a per-worker `NULLRUN_WAL_PATH`. |
| `wal_lock_timeouts_total` | count | writes skipped because the lock stayed held for the whole timeout. Zero is healthy. |
| `dlq_bytes` / `dlq_overflow_total` | bytes / count | DLQ size, and how many appends the cap refused. |
| `dlq_holdover` / `dlq_holdover_total` | count | refusals held for want of DLQ room, now and cumulative. |
| `dlq_holdover_index_truncated` | count | held events dropped from the in-memory index by `NULLRUN_DLQ_HOLDOVER_MAX_EVENTS` (default 10 000). Non-zero means `dlq_holdover` under-reports; the events are in `sdk.wal.holdover` and still reach the DLQ. |
| `dlq_holdover_persist_failures` | count | held events that could not be written to `sdk.wal.holdover` — **the only holdover counter that means possible loss**, because those rows are in memory only. |

`None` means "not probed yet" — distinct from `"unavailable"`, which means
"probed and refused". A degraded platform produces **one** warning per aspect
per `Transport`, not one per flush: it is a standing condition, and warning
on every cycle buries the log under a fact that cannot change.

All are in `metrics.to_dict()["transport"]`.

A green test run on Windows proves nothing about `flock` or a directory
fsync, because neither exists there. The POSIX-only assertions are marked
`skipif win32`; run the suite in a Linux container to exercise them.

### What the fsync probe does and does not prove

**Proves:** the `fsync(2)` syscall on a directory is reachable and returns
success on this kernel and filesystem. `test_a_posix_volume_really_does_fsync_its_directory`
asserts exactly that, by fsyncing a directory and checking for `EINVAL`
rather than by reading the test's own name as a proxy.

**Does not prove:** that any data survives power loss. That needs hardware
that lies about a flush — a `removable`/`uninterruptible` cache-volume
device, or a fault injector that cuts power mid-write. There is no such
device in CI, and no assertion in this suite simulates one. So the honest
statement is: **this is crash-safe against process death, and
fsync-correct against kernel death, and neither is a claim about power.**

The distinction matters when reading the guarantee above. Process death
(`SIGKILL`, panic, OOM) is covered — an unlinked-but-fsynced file survives
it, which is the entire WAL design. Kernel death is covered by the fsync
ordering. Power loss depends on whether the storage stack honours the flush,
which is a property of the device and the hypervisor, not of this code.

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

**This contract was not the command's original behaviour, and calling it
"pre-existing" was wrong.** `replay` arrived in `fd4e581`, and the report for
that commit described it as removing a row only after confirming the
`event_id` — which the code did not do. It derived "unconfirmed" from the
transport's in-memory buffer alone, while a refused event lives in the
scratch DLQ or the holdover and never in the buffer. A replay the backend
refused outright reported zero unconfirmed, printed `sent 3 event(s);
removed 3 row(s)`, and exited **0**: it deleted the operator's only record of
the very events the backend had just refused, and reported success doing it.
Two independent places in this file have now had a report describe behaviour
the code lacked, so every destructive operation here is pinned by a test
that asserts the *rows that remain*, not by one that asserts the command
exited cleanly.

The exit code distinguishes the two "nothing happened" cases: **1** when rows
existed and none were confirmed (or all were withheld, see below), **0** when
the DLQ was empty. A scheduled repair that reports 0 while sending nothing
would be indistinguishable from one that worked.

### When a 429 says "not for an hour"

A `Retry-After` is a floor, and the SDK does not retry before it. Both RFC
7231 forms are honoured — seconds and HTTP-date — and the jitter applied on
that path is one-sided, so a fleet that got limited together spreads out
without any member retrying under the limit.

A floor also needs a ceiling, and the ceiling is **not** a shorter wait.
`NULLRUN_RETRY_AFTER_CEILING` (60s default) says how long the SDK will sit
through; past it the flush stops for that cycle and the next cycle tries
again. The events are already durable, so declining to retry costs nothing.

The alternative — clamping the wait to `max_delay` — looks identical from
outside and is not. It retries an hour early, spends the whole retry budget
re-tripping the limit it was just told to respect, and surfaces to the
operator as a client that ignores rate limits rather than one that honours a
long one. `retry_after_deferred` counts the deferrals; a sustained non-zero
value is a backend rate-limiting longer than the SDK will wait, and is a
signal to read rather than an SDK fault.

The wait itself is an `Event.wait`, not a `time.sleep`, so `Transport.stop()`
cuts it short. A process that is shutting down does not have to sit out the
remainder of a rate-limit window — which matters most exactly when the
backend is misbehaving and someone is killing the process because of it.

### Refusals `replay` will not send

Some rows are parked for a reason re-sending cannot address. `replay` leaves
those alone by default, names them on stderr, and exits 1:

| Reason | Why sending is pointless |
|---|---|
| `EXECUTION_NOT_BOUND:422` | The 24h server-side binding has expired (see below) |
| `EXECUTION_NOT_BOUND:503` | Same class, arriving as a 5xx |

`--include-terminal` overrides this and sends them anyway. Nothing is lost
either way: an unconfirmed row is never removed, so the override cannot
destroy evidence, and it cannot multiply DLQ rows either — the row already
there is the row that is kept.

Recovering this class is not a transport job. It needs a **ledger-only
ingest** on the backend: an endpoint that records the spend against the
period-bound counter *without* a live `execution_id` to attach it to, marking
the row as reconstructed. Until that exists, the honest options are (a) accept
the under-report, (b) reconcile the DLQ against the provider's own billing
export, or (c) re-run the work under a fresh gate if the cost is acceptable to
pay twice. Which one is right is a decision about accounting, not a retry.

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
accounting that has to be made deliberately (and `replay` refuses that class
by default rather than pretending otherwise). And an outage beyond 24 hours
needs a plan that does not assume the buffered events will be accepted when
it ends. If your agents can run unattended for more than a day against an
unreachable backend, size that budget on the assumption that it is spent and
unaccounted, not merely pending.

### Not covered: SIGKILL after a long outage

**This is consciously not handled, and the gap is deliberate rather than
overlooked.** There is no test that kills the process mid-outage, restarts it,
and asserts what happens to the 24-hour-old events — and no code path that
would make such a test pass. If you adopt `direct` mode expecting that case
to be handled, it will not be, and the failure is silent in the worst way:
the events are not lost, they are *refused*, which looks like correct
behaviour in the DLQ while the dashboard stays low.

What is verified instead, and what is not:

| Scenario | Status |
|---|---|
| Outage shorter than the 24h binding TTL | Events resume normally on reconnect |
| Outage longer than 24h, process alive | Events reach the backend, are refused `EXECUTION_NOT_BOUND`, land in the DLQ (`EXECUTION_NOT_BOUND:422`), and `replay` refuses to re-send them |
| Restart with a rotated WAL present | Recovery reads `.wal.1`, `.wal` and `.wal.inflight` oldest-first, so no generation is dropped — pinned by `test_recovery_reads_the_rotated_wal_not_just_the_active_one` |
| Kill during a WAL write | Not reachable as corruption: every write is tmp-file + fsync + `os.replace`, and the rename is atomic, so a kill leaves the old file or the new one, never a torn one. Directory fsync on a Linux volume is asserted in `test_a_posix_volume_really_does_fsync_its_directory` |
| Kill while a DLQ refusal is held | The refusal is in `<wal>.holdover`, not only in memory, so a fresh process over the same WAL path finds it — pinned by `test_a_held_refusal_survives_a_kill_minus_nine`. The test builds a new `Transport` and never flushes, so the dying process writes nothing; a memory-only holdover cannot pass it |
| **SIGKILL mid-outage, then restart, across the 24h boundary** | **Not tested, and not handled.** The restart behaves like a clean restart, so the same ceiling applies — but nothing in the suite exercises it, and it cannot pass without the ledger-only ingest below |

The reason the last row is left open: making it pass would mean either
holding events in memory across a kill (impossible) or re-minting bindings
for past events (which double-counts against the budget — see above). The
honest fix is the ledger-only ingest described in the replay section, not a
retry. Until that exists, the operational guidance is unchanged and is the
only thing that actually protects you: **size the budget as spent and
unaccounted, not as pending.**
