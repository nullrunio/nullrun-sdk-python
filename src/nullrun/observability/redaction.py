"""Credential redaction for everything the SDK prints.

SDK-5 (2026-11-06): the status string and the error context used to
carry the FIRST TEN CHARACTERS of the API key, and the transport's
debug log carried the first eight with a ``***`` tail. Two problems:

1. A prefix of a credential is a disclosure. The remaining characters
   are what an attacker needs to brute-force the rest.
2. The two places disagreed about how much they printed, so "the full
   key never leaves the SDK" was not a statement anyone could check.

A fingerprint replaces the prefix. It is derived from the key, it is
stable (so it still correlates log lines and status snapshots), and it
carries NO character of the key. The backend does the same thing --
``backend/src/auth/mod.rs::compute_fingerprint`` is SHA-256 over the
raw key -- so a log line can be correlated on both sides without
either side holding a fragment of the secret.
"""

from __future__ import annotations

import hashlib

#: Hex characters kept from the SHA-256 digest.
API_KEY_FINGERPRINT_LEN: int = 8


def api_key_fingerprint(api_key: str | None) -> str | None:
    """Return an 8-character fingerprint of ``api_key``.

    Returns ``None`` for a missing key -- a fingerprint of nothing is
    a lie. The digest is truncated to 8 hex characters: enough to
    correlate a log line with a status snapshot, not enough to be a
    credential fragment.
    """
    if not api_key:
        return None
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    return digest[:API_KEY_FINGERPRINT_LEN]
