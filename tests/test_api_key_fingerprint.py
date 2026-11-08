"""SDK-5 — no character of the API key reaches a user-visible string.

Before the fix the SDK printed three different fragments of the key:
``api_key[:10]`` in the status snapshot, ``api_key[:10]`` in the error
context, and ``api_key[:8] + "***"`` in the transport's debug log. A
prefix of a credential is a disclosure, and "the full key never leaves
the SDK" was not a claim anyone could check.

After the fix every one of those places prints the SAME 8-character
SHA-256 fingerprint of the key — a correlation handle, not a fragment.

Every key in this file is fake.
"""

from __future__ import annotations

import logging

import pytest

from nullrun.observability.redaction import api_key_fingerprint

# A fake key. It is long enough to have a prefix and a suffix and it
# is not a credential.
FAKE_KEY = "nr_live_FAKEKEYFORTESTSONLY_0123456789"

# sha256(FAKE_KEY)[:8] — hardcoded so the test does not read the
# helper it is checking.
FAKE_KEY_FINGERPRINT = "956dceaa"


@pytest.fixture(autouse=True)
def _isolated_wal(tmp_path, monkeypatch):
    """The transport touches the WAL on construction. Keep it in tmp."""
    monkeypatch.setenv("NULLRUN_WAL_PATH", str(tmp_path / "sdk.wal"))


class TestTheFingerprintHelper:
    def test_it_is_the_sha256_prefix(self):
        import hashlib

        expected = hashlib.sha256(FAKE_KEY.encode()).hexdigest()[:8]
        assert api_key_fingerprint(FAKE_KEY) == expected

    def test_it_is_eight_characters(self):
        assert len(api_key_fingerprint(FAKE_KEY)) == 8

    def test_a_missing_key_has_no_fingerprint(self):
        assert api_key_fingerprint(None) is None
        assert api_key_fingerprint("") is None

    def test_two_different_keys_do_not_share_a_fingerprint(self):
        assert api_key_fingerprint(FAKE_KEY) != api_key_fingerprint(
            "nr_live_OTHERKEYFORTESTSONLY_012345678"
        )


class TestTheStatusString:
    """The audit's verification: ``NullRunStatus.__str__`` must not
    contain more than the agreed number of key characters — and the
    agreed number is zero."""

    def test_the_status_carries_no_character_of_the_key(self, make_runtime):
        rt = make_runtime(api_key=FAKE_KEY)
        s = rt.status()

        rendered = str(s)
        assert FAKE_KEY not in rendered
        assert "nr_live_" not in rendered
        assert "FAKEKEY" not in rendered
        assert "0123456789" not in rendered

    def test_the_status_carries_the_fingerprint(self, make_runtime):
        rt = make_runtime(api_key=FAKE_KEY)
        s = rt.status()

        assert s.api_key_prefix == FAKE_KEY_FINGERPRINT
        assert FAKE_KEY_FINGERPRINT in str(s)

    def test_no_prefix_of_the_key_survives(self, make_runtime):
        """Every 4-character window of the key must be absent. A test
        that only checks the whole key would pass on a slice."""
        rt = make_runtime(api_key=FAKE_KEY)
        rendered = str(rt.status())

        key = FAKE_KEY
        for i in range(len(key) - 3):
            window = key[i : i + 4]
            assert window not in rendered, f"the key window {window!r} leaked"


class TestTheErrorContext:
    def test_the_error_context_carries_the_same_fingerprint(
        self, make_runtime, monkeypatch
    ):
        from nullrun.observability.error_hooks import ErrorContext

        rt = make_runtime(api_key=FAKE_KEY)
        ctx = ErrorContext(stage="init", api_key_prefix=api_key_fingerprint(FAKE_KEY))
        assert ctx.api_key_prefix == FAKE_KEY_FINGERPRINT
        assert rt.status().api_key_prefix == ctx.api_key_prefix

    def test_the_emitted_error_context_carries_no_key_character(
        self, make_runtime, monkeypatch
    ):
        """The runtime's own error emitter is the path that ships the
        context to the operator's hook."""
        from nullrun.observability.error_hooks import ErrorContext, register_hook

        seen: list[ErrorContext] = []

        def _hook(err, ctx) -> None:
            seen.append(ctx)

        unregister = register_hook(_hook)
        try:
            rt = make_runtime(api_key=FAKE_KEY)
            rt._emit_sdk_error(
                RuntimeError("boom"), stage="auth", tool_name="charge_card"
            )
        finally:
            unregister()

        assert seen, "the error hook was never called"
        for ctx in seen:
            assert ctx.api_key_prefix == FAKE_KEY_FINGERPRINT
            assert "FAKEKEY" not in repr(ctx)


class TestTheTwoPlacesAgree:
    """The transport used to print eight characters, the status ten."""

    def test_the_transport_log_and_the_status_carry_the_same_fingerprint(
        self, make_runtime, caplog
    ):
        from nullrun.transport import Transport

        with caplog.at_level(logging.DEBUG, logger="nullrun.transport"):
            Transport(api_url="https://api.test.nullrun.io", api_key=FAKE_KEY)

        transport_lines = [
            r.getMessage() for r in caplog.records if r.name == "nullrun.transport"
        ]
        assert transport_lines, "the transport logged nothing"
        joined = "\n".join(transport_lines)

        assert f"api_key_fp={FAKE_KEY_FINGERPRINT}" in joined
        assert "nr_live_" not in joined
        assert "FAKEKEY" not in joined

        rt = make_runtime(api_key=FAKE_KEY)
        assert rt.status().api_key_prefix == FAKE_KEY_FINGERPRINT
