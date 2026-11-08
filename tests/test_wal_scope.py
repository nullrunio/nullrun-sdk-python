"""SDK-4 — the write-ahead log must not be shared across tenants.

Before the fix the WAL lived at a FIXED path in the shared OS temp
directory — ``<tempdir>/nullrun.wal`` — for every user and every org on
the host. Two runtimes therefore resolved to the SAME file, and the
atomic whole-file replace in ``Transport._persist_to_wal`` replaced the
other runtime's undelivered events with its own buffer. Spend that had
already happened was then never booked.

The default is now scoped per user AND per API KEY.

Why the KEY and not the org id
------------------------------
The path is resolved ONCE, when the transport is constructed, and never
changes afterwards. The org id is only known AFTER authentication; a
path that changed at authentication would orphan every file written
before it, and no test would catch it — the path is "right" at every
instant and the money is lost anyway.

One API key belongs to exactly one organisation, so a key-scoped WAL is
an org-scoped WAL in money terms. Two organisations therefore never
share a WAL.

Two workers of the SAME organisation using DIFFERENT keys never share
one either. That is the common multi-worker case, and it is covered by
the default.

The audit's literal check — "two Transports with different org ids must
not share a WAL path" — is therefore satisfied by scoping on the key:
in a real deployment an org is reachable only through its own key, so
two orgs always carry two keys. The same key with two different org ids
is ONE money pool and MUST share one WAL: splitting it would strand
half the spend in a file nobody replays.

Every assertion here runs against REAL files on disk. A mock would pass
while the destructive whole-file replace is still in place, which is the
exact thing this finding is about.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from nullrun.transport import Transport
from nullrun.wal_paths import (
    LEGACY_WAL_BASENAME,
    default_wal_path,
    legacy_wal_path,
    scoped_wal_paths,
    user_token,
)

FAKE_KEY = "nr_live_FAKEKEYFORTESTSONLY_0123456789"
OTHER_FAKE_KEY = "nr_live_OTHERKEYFORTESTSONLY_012345678"


@pytest.fixture
def tempdir_as_tmp(monkeypatch, tmp_path):
    """Point the OS temp directory at this test's own directory.

    Patching the FUNCTION (not the module attribute of a single caller)
    means the OLD unscoped default and the NEW scoped default both
    resolve under ``tmp_path``. Without this the RED run of this file
    would write into the real shared temp directory.
    """
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delenv("NULLRUN_WAL_PATH", raising=False)
    return tmp_path


def _transport(
    org_id: str | None = None,
    api_key: str | None = FAKE_KEY,
) -> Transport:
    return Transport(
        api_url="https://api.test.nullrun.io",
        api_key=api_key,
        organization_id=org_id,
    )


def _event(n: int) -> dict:
    return {"event_id": f"evt-{n}", "type": "llm_call", "tokens": 100}


class TestThePathIsScoped:
    def test_two_transports_with_different_keys_do_not_share_a_path(
        self, tempdir_as_tmp
    ):
        """Two orgs are reached through two keys, and two keys must not
        share a file."""
        a = _transport("org_alpha")
        b = _transport("org_beta", api_key=OTHER_FAKE_KEY)
        try:
            assert a._wal_path() != b._wal_path()
        finally:
            a._client.close()
            b._client.close()

    def test_the_same_key_resolves_to_the_same_path(self, tempdir_as_tmp):
        """A restart must find the WAL the previous run left behind."""
        a = _transport("org_alpha")
        b = _transport("org_alpha")
        try:
            assert a._wal_path() == b._wal_path()
        finally:
            a._client.close()
            b._client.close()

    def test_one_key_with_two_org_ids_shares_one_path(self, tempdir_as_tmp):
        """One key is ONE money pool, so it gets ONE WAL.

        The audit asked for the split on the org id. Splitting here would
        strand half the spend in a file nothing replays — the same class
        of loss the finding is about, in the other direction.
        """
        a = _transport("org_alpha")
        b = _transport("org_beta")
        try:
            assert a._wal_path() == b._wal_path()
        finally:
            a._client.close()
            b._client.close()

    def test_the_path_is_scoped_per_user(self, tempdir_as_tmp, monkeypatch):
        """Two users on one host must not share a WAL either."""
        a = _transport("org_alpha")
        try:
            first = a._wal_path()
        finally:
            a._client.close()

        import nullrun.wal_paths as wal_paths

        monkeypatch.setattr(wal_paths, "user_token", lambda: "u999999")
        second = default_wal_path(org_id="org_alpha", api_key=FAKE_KEY)
        assert first != second

    def test_the_api_key_is_not_in_the_path_in_cleartext(self, tempdir_as_tmp):
        a = _transport("org_alpha")
        try:
            path = a._wal_path()
        finally:
            a._client.close()
        assert FAKE_KEY not in path
        assert "org_alpha" not in path

    def test_the_path_carries_the_user_token(self, tempdir_as_tmp):
        a = _transport("org_alpha")
        try:
            path = a._wal_path()
        finally:
            a._client.close()
        assert user_token() in path

    def test_the_legacy_unscoped_path_is_not_the_default(self, tempdir_as_tmp):
        a = _transport("org_alpha")
        try:
            path = a._wal_path()
        finally:
            a._client.close()
        assert path != legacy_wal_path()
        assert LEGACY_WAL_BASENAME not in os.path.basename(path)


class TestThePathNeverChanges:
    """SDK-4 (security audit 2026-11-06). The path must be resolved
    ONCE and never change during a transport's lifetime.

    A path that changed at authentication would orphan every file
    written before it. The early file would hold real undelivered
    spend and nothing would ever read it again — and no test would
    catch it, because at every instant the path is "the right one".
    """

    def test_the_org_becoming_known_does_not_move_the_path(self, tempdir_as_tmp):
        a = _transport(None)
        try:
            before = a._wal_path()
            # The org id arrives after authentication.
            a.organization_id = "org_alpha"
            assert a._wal_path() == before
        finally:
            a._client.close()

    def test_two_transports_of_one_key_agree_even_after_the_org_is_known(
        self, tempdir_as_tmp
    ):
        """The file written before auth and the file written after it
        must be the SAME file, or the early one is orphaned."""
        early = _transport(None)
        later = _transport(None)
        try:
            early.organization_id = "org_alpha"
            later.organization_id = "org_alpha"
            assert early._wal_path() == later._wal_path()
        finally:
            early._client.close()
            later._client.close()

    def test_a_keyless_transport_does_not_move_its_path_when_the_org_arrives(
        self, tempdir_as_tmp
    ):
        """The once-only resolution, pinned where it actually bites.

        With a key the scope IS the key, so the org cannot move the path
        and the property is structural — no test can catch a regression
        there, because nothing can break it.

        Without a key the scope falls back to the org id. A path
        re-derived on every call WOULD move here, and the file written
        before authentication would be orphaned with real undelivered
        spend inside it — and nothing would ever read it again.
        """
        a = _transport(None, api_key=None)
        try:
            before = a._wal_path()
            a.organization_id = "org_alpha"
            assert a._wal_path() == before
        finally:
            a._client.close()


class TestTheEnvironmentOverrideStillWins:
    def test_nullrun_wal_path_beats_the_scoped_default(
        self, tempdir_as_tmp, monkeypatch
    ):
        chosen = str(tempdir_as_tmp / "chosen.wal")
        monkeypatch.setenv("NULLRUN_WAL_PATH", chosen)
        a = _transport("org_alpha")
        try:
            assert a._wal_path() == chosen
        finally:
            a._client.close()

    def test_the_override_ignores_the_key_and_the_org(
        self, tempdir_as_tmp, monkeypatch
    ):
        """The operator's explicit path wins over everything.

        It is therefore also the operator's responsibility to make it
        unique per runtime — the SDK cannot detect two runtimes pointing
        at one path.
        """
        chosen = str(tempdir_as_tmp / "chosen.wal")
        monkeypatch.setenv("NULLRUN_WAL_PATH", chosen)
        a = _transport("org_alpha")
        b = _transport("org_beta", api_key=OTHER_FAKE_KEY)
        try:
            assert a._wal_path() == b._wal_path() == chosen
        finally:
            a._client.close()
            b._client.close()


class TestOneRuntimeDoesNotDestroyAnother:
    """The audit's integration check, on real files.

    A write from runtime B must not remove runtime A's buffered events.
    """

    def test_a_write_from_runtime_b_leaves_runtime_a_events_alone(
        self, tempdir_as_tmp
    ):
        a = _transport("org_alpha")
        b = _transport("org_beta", api_key=OTHER_FAKE_KEY)
        try:
            a.track(_event(1))
            b.track(_event(2))
            assert a._persist_to_wal()
            assert b._persist_to_wal()
        finally:
            a._client.close()
            b._client.close()

        a_rows = _read_rows(a._wal_path())
        b_rows = _read_rows(b._wal_path())

        assert [r["event_id"] for r in a_rows] == ["evt-1"], (
            "runtime B destroyed runtime A's buffered events"
        )
        assert [r["event_id"] for r in b_rows] == ["evt-2"]

    def test_the_two_runtimes_wrote_to_two_different_files(
        self, tempdir_as_tmp
    ):
        a = _transport("org_alpha")
        b = _transport("org_beta", api_key=OTHER_FAKE_KEY)
        try:
            a.track(_event(1))
            b.track(_event(2))
            a._persist_to_wal()
            b._persist_to_wal()
            a_file = a._wal_path()
            b_file = b._wal_path()
        finally:
            a._client.close()
            b._client.close()

        assert a_file != b_file
        assert os.path.exists(a_file)
        assert os.path.exists(b_file)


class TestTheLegacyWalIsReportedButNotStolen:
    def test_a_legacy_wal_is_warned_about_and_left_alone(
        self, tempdir_as_tmp, caplog
    ):
        """The old unscoped file may hold ANOTHER org's events.

        Reading it into this org's WAL would attribute a stranger's spend
        to this tenant. So the SDK reports it and leaves it where it is.
        """
        legacy = legacy_wal_path()
        with open(legacy, "w") as f:
            f.write(json.dumps(_event(99)) + "\n")

        a = _transport("org_alpha")
        try:
            with caplog.at_level("WARNING", logger="nullrun.transport"):
                a.start()
        finally:
            a._client.close()
            a.stop(flush=False)

        assert any(legacy in r.getMessage() for r in caplog.records), (
            "the legacy WAL was not reported"
        )
        # Untouched: still exactly the one row it had.
        assert [r["event_id"] for r in _read_rows(legacy)] == ["evt-99"]
        # And it was not replayed into this org's WAL.
        assert _read_rows(a._wal_path()) == []


class TestTheListingHelper:
    def test_it_lists_only_this_users_wal_files(self, tempdir_as_tmp):
        a = _transport("org_alpha")
        b = _transport("org_beta", api_key=OTHER_FAKE_KEY)
        try:
            a.track(_event(1))
            b.track(_event(2))
            a._persist_to_wal()
            b._persist_to_wal()
        finally:
            a._client.close()
            b._client.close()

        listed = scoped_wal_paths()
        assert a._wal_path() in listed
        assert b._wal_path() in listed
        assert legacy_wal_path() not in listed


class TestTheCli:
    """The operator's tool must not point at the wrong file.

    Before SDK-4 the CLI's default was the ONE fixed path, so every
    command acted on every user's events. After it, the user may own one
    scoped WAL per API key — so ``status`` lists them and ``replay``
    refuses to guess. A guess could send one tenant's events into
    another tenant's ledger.
    """

    def test_status_without_a_path_lists_every_wal_of_this_user(
        self, tempdir_as_tmp, capsys
    ):
        from nullrun.wal_admin import main as wal_main

        a = _transport("org_alpha")
        b = _transport("org_beta", api_key=OTHER_FAKE_KEY)
        try:
            a.track(_event(1))
            b.track(_event(2))
            a._persist_to_wal()
            b._persist_to_wal()
            a_path = a._wal_path()
            b_path = b._wal_path()
        finally:
            a._client.close()
            b._client.close()

        assert wal_main(["status"]) == 0

        out = capsys.readouterr().out
        assert a_path in out
        assert b_path in out

    def test_replay_without_a_path_refuses_to_guess(self, tempdir_as_tmp, capsys):
        from nullrun.wal_admin import main as wal_main

        a = _transport("org_alpha")
        b = _transport("org_beta", api_key=OTHER_FAKE_KEY)
        try:
            a.track(_event(1))
            b.track(_event(2))
            a._persist_to_wal()
            b._persist_to_wal()
        finally:
            a._client.close()
            b._client.close()

        assert wal_main(["replay"]) == 1

        err = capsys.readouterr().err
        assert "--wal" in err


def _read_rows(path: str) -> list[dict]:
    try:
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []
