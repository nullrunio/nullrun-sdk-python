"""Where the SDK's write-ahead log lives by default.

SDK-4 (security audit 2026-11-06): the WAL used to live at a FIXED
path in the shared OS temp directory — ``<tempdir>/nullrun.wal`` — for
every user and every org on the host. Two runtimes therefore resolved
to the SAME file, and the atomic whole-file replace in
``Transport._persist_to_wal`` destroyed the other runtime's undelivered
events. Spend that had already happened was then never booked.

The default is now scoped per user AND per API KEY::

    <tempdir>/nullrun-<user>-<key>.wal

Why the API KEY and not the org id
----------------------------------
The path is resolved ONCE, when the transport is constructed, and never
changes afterwards. The org id is only known AFTER authentication,
which happens later; a path that changed at authentication would
orphan every file written before it, and no test would catch it — the
path is "right" at every instant and the money is lost anyway.

The API key is known at construction and never changes, and one API key
belongs to exactly one organisation, so a key-scoped WAL is an
org-scoped WAL in money terms. Two organisations therefore never share
a WAL. Two workers of the SAME organisation using DIFFERENT keys never
share one either — that is the common multi-worker case, and it is
covered by the default.

The digest is not decoration. Org ids and keys come from the server,
and a readable slug alone would let two different values collapse onto
one path — the exact collision this module exists to remove.

``NULLRUN_WAL_PATH`` still wins outright. It MUST be unique per
runtime: pointing two runtimes at one path collides exactly like the
old shared default did, and the SDK cannot detect it.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import tempfile

__all__ = [
    "WAL_PATH_ENV_VAR",
    "LEGACY_WAL_BASENAME",
    "user_token",
    "scope_token",
    "default_wal_path",
    "key_scoped_wal_path",
    "legacy_wal_path",
    "scoped_wal_paths",
]


# The env var that overrides the default path entirely.
WAL_PATH_ENV_VAR = "NULLRUN_WAL_PATH"

# The unscoped path the SDK used before SDK-4. Never written to any
# more; still reported so an operator can find and inspect the events
# an older SDK left there.
LEGACY_WAL_BASENAME = "nullrun.wal"

# How many hex characters of the digest are kept. Sixteen is enough to
# make a collision a non-event and short enough to read out loud.
_DIGEST_LEN = 16

# The prefix every scoped WAL file carries. The operator greps for it.
_SCOPED_PREFIX = "nullrun-"


def _digest(value: str) -> str:
    """A short, stable digest of ``value``."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_DIGEST_LEN]


def user_token() -> str:
    """A short token identifying the OS user this process runs as.

    POSIX uid where the platform exposes one. Elsewhere a digest of the
    account name: the raw name is not a secret, but it does not belong
    in a temp directory other users can list.
    """
    getuid = getattr(os, "getuid", None)
    if callable(getuid):
        return f"u{getuid()}"
    try:
        account = getpass.getuser()
    except Exception:  # noqa: BLE001 — no account name is not fatal here
        account = ""
    return "u" + _digest(account)


def scope_token(
    api_key: str | None = None, org_id: str | None = None
) -> str:
    """A short token identifying whose spend a WAL file holds.

    The API key when there is one — it is known before the org is, and
    one key belongs to exactly one org, so the two scopes describe the
    same money. The org id is the fallback for a transport that has no
    key (it cannot book anything). Both are reduced to a digest: the
    value itself never reaches a path that every user on the host can
    list.
    """
    if api_key:
        return "k" + _digest(api_key)
    if org_id:
        return "o" + _digest(org_id)
    return "x"


def default_wal_path(
    api_key: str | None = None, org_id: str | None = None
) -> str:
    """The default WAL base path for one user and one api key."""
    return os.path.join(
        tempfile.gettempdir(),
        f"{_SCOPED_PREFIX}{user_token()}-{scope_token(api_key, org_id)}.wal",
    )


def key_scoped_wal_path(api_key: str | None) -> str:
    """The WAL base path for one user and one api key."""
    return default_wal_path(api_key=api_key)


def legacy_wal_path() -> str:
    """The unscoped path the SDK used before SDK-4."""
    return os.path.join(tempfile.gettempdir(), LEGACY_WAL_BASENAME)


def scoped_wal_paths() -> list[str]:
    """Every WAL base path this user owns, sorted.

    Only the base files are listed; the sibling files (``.1``,
    ``.inflight``, ``.holdover``, ``.dlq``, ``.lock``) hang off a base
    and are not separate WALs.
    """
    temp_dir = tempfile.gettempdir()
    prefix = f"{_SCOPED_PREFIX}{user_token()}-"
    try:
        names = sorted(os.listdir(temp_dir))
    except OSError:
        return []
    return [
        os.path.join(temp_dir, name)
        for name in names
        if name.startswith(prefix) and name.endswith(".wal")
    ]
