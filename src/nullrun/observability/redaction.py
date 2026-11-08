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
import re

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


# Credential shapes, matched on the VALUE rather than on the key it
# travels under. Key-NAME redaction (``SENSITIVE_ARG_KEYS``) cannot
# catch a secret that arrives in a benign key -- ``{"note": "<key>"}``
# prints the key in full.
#
# Every pattern here is deliberately narrow: it must not match a
# value an ordinary tool call carries. A shape that cannot be told
# apart from ordinary data is not a shape -- it is a guess, and a
# guess that scrubs real data is a bug of its own.
#
# Most of the shapes carry NO leading word boundary on purpose. A
# boundary would make the pattern miss a credential that sits right
# after a letter -- ``"prefix" + key`` -- and that is exactly the
# leak this pass exists to close. The one exception is ``sk-``,
# whose boundary keeps ``risk-assessment-report`` out of the scrub.
_SECRET_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # This product's own keys: ``nr_live_`` / ``nr_test_`` + the random part.
    (re.compile(r"nr_(?:live|test)_[A-Za-z0-9_-]{8,}"), "***"),
    # A JSON Web Token: a base64url header starting with ``eyJ``, two dots.
    (re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"), "***"),
    # An AWS access key id: a four-character prefix plus sixteen
    # uppercase alphanumerics.
    (re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"), "***"),
    # The OpenAI-style secret key: ``sk-`` plus a long opaque tail.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "***"),
    # A PEM private key, whole block first, bare header second.
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,8192}?"
            r"-----END [A-Z ]*PRIVATE KEY-----"
        ),
        "***",
    ),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "***"),
    # An HTTP bearer credential.
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"), "Bearer ***"),
)


def scrub_secret_values(text: str) -> str:
    """Replace credential-shaped substrings in ``text`` with ``***``.

    This is the value-shaped half of redaction. The key-name half
    (``SENSITIVE_ARG_KEYS``) knows WHICH arguments are sensitive; this
    pass catches a credential that arrives under a name nobody listed.

    The text is scanned as-is, so the caller must run this BEFORE
    truncating: a credential that straddles the cut is caught whole.
    """
    scrubbed = text
    for pattern, replacement in _SECRET_VALUE_PATTERNS:
        scrubbed = pattern.sub(replacement, scrubbed)
    return scrubbed
