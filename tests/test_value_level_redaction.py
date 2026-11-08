"""Redaction is not only about the NAME an argument arrives under.

``SENSITIVE_ARG_KEYS`` masks the arguments whose name someone listed.
It cannot catch a secret that arrives under a name nobody listed --
``{"note": "<api key>"}`` prints the key in full, and the key-name
pass looks straight past it.

This file pins the value-shaped half: the credential itself is
matched, whatever key it travels in. The shapes are deliberately
narrow -- a shape that cannot be told apart from ordinary data is a
guess, and a guess that scrubs real data is a bug of its own.
"""

from __future__ import annotations

import pytest

from nullrun.decorators import _safe_kwargs, _safe_repr
from nullrun.observability.redaction import scrub_secret_values

# A key in the shape this product mints. The random part is long
# enough to match the pattern and is not a real credential.
_OWN_KEY = "nr_live_0123456789abcdefghij"
_JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP"
_AWS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
_OPENAI_KEY = "sk-abcdef0123456789abcdef0123456789"
_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEAx1234567890abcdef\n"
    "-----END RSA PRIVATE KEY-----"
)


class TestValueShapes:
    @pytest.mark.parametrize(
        "secret",
        [_OWN_KEY, _JWT, _AWS_KEY_ID, _OPENAI_KEY, _PEM],
    )
    def test_a_credential_is_scrubbed_whatever_the_key(self, secret):
        """The secret travels under a name nobody listed."""
        rendered = _safe_kwargs({"note": secret})
        assert secret not in str(rendered), (
            f"a credential survived the value pass under a benign key: {secret!r}"
        )
        assert "***" in str(rendered)

    def test_a_bearer_credential_is_scrubbed(self):
        rendered = _safe_kwargs({"headers": f"Bearer {_JWT}"})
        assert _JWT not in str(rendered)

    def test_ordinary_data_is_untouched(self):
        """A scrubber that eats real data is its own defect."""
        rendered = _safe_kwargs({"query": "SELECT id FROM users WHERE name = 'x'"})
        assert "SELECT id FROM users" in str(rendered)

    @pytest.mark.parametrize(
        "value",
        [
            "task-abcdef0123456789abcdef0123456789",
            "sk-short",
            "AKIASHORT",
            "eyJnotajwt",
            "risk-assessment-report",
        ],
    )
    def test_a_near_miss_is_not_scrubbed(self, value):
        """The patterns are anchored so an ordinary word that happens
        to contain the shape survives."""
        assert scrub_secret_values(value) == value

    def test_the_key_name_pass_still_works(self):
        """The value pass is added, not substituted."""
        assert _safe_kwargs({"password": "hunter2"})["password"] == "***"


class TestOrder:
    def test_the_scrub_runs_before_the_truncation(self):
        """A credential that straddles the cut must be caught whole.

        The credential starts INSIDE the truncated region and runs
        past it. Truncate-then-scrub would cut the credential in half
        and scrub neither half -- the first eighteen characters would
        reach the audit log."""
        payload = "x" * 30 + _OWN_KEY
        rendered = _safe_repr(payload)
        assert "nr_live_" not in rendered
        assert _OWN_KEY not in rendered

    def test_a_short_credential_is_scrubbed_too(self):
        rendered = _safe_repr(_OWN_KEY)
        assert _OWN_KEY not in rendered
