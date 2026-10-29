"""The known-hosts store: keys recorded and checked by otto host ID, in the user's file.

Every test plants the hostile condition in a real file under ``tmp_path``:
an address-keyed entry with another key, a different key recorded by
another process, a revoked key under the other form, an unwritable file,
a hashed entry, an X.509 subject line. Parsing and matching are asyncssh's;
these tests pin what otto layers on top (name-only lookup, the bare and
``[alias]:port`` combination, the lock, the forget filter).
"""

import base64
import contextlib
import hashlib
import hmac
import multiprocessing
import os
import re
import stat
from collections.abc import Generator
from pathlib import Path
from typing import Any

import asyncssh
import pytest

from otto.host.errors import (
    HostKeyChangedError,
    HostKeyRevokedError,
    KnownHostsError,
    KnownHostsUnwritableError,
)
from otto.host.known_hosts import ForgetResult, HostKeyVerdict, KnownHosts, known_hosts_path
from otto.host.options import SshOptions


def _key() -> asyncssh.SSHKey:
    return asyncssh.generate_private_key("ssh-ed25519").convert_to_public()


def _pub(key: asyncssh.SSHKey) -> str:
    return key.export_public_key("openssh").decode().strip()


def _hashed(name: str) -> str:
    """An OpenSSH ``HashKnownHosts`` host field for *name*."""
    salt = os.urandom(20)
    digest = hmac.new(salt, name.encode(), hashlib.sha1).digest()
    return f"|1|{base64.b64encode(salt).decode()}|{base64.b64encode(digest).decode()}"


def _lines(path: Path) -> list[str]:
    return [line for line in path.read_text().splitlines() if line.strip()]


def _cannot_enforce_permissions() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


# ---------------------------------------------------------------------------
# Matching, by name only
# ---------------------------------------------------------------------------


def test_match_ignores_address_entries(tmp_path):
    """An entry the user's own ``ssh`` wrote under the address never matches the host ID."""
    f = tmp_path / "known_hosts"
    f.write_text(f"10.0.0.5 {_pub(_key())}\n")
    assert KnownHosts(f).judge("router1", 22, _key()) is HostKeyVerdict.FIRST_USE
    assert list(KnownHosts(f).match("router1", None).host_keys) == []


def test_match_finds_the_host_id_entry_and_none_means_port_22(tmp_path):
    key = _key()
    f = tmp_path / "known_hosts"
    f.write_text(f"10.0.0.5 {_pub(_key())}\nrouter1 {_pub(key)}\n")
    assert list(KnownHosts(f).match("router1", None).host_keys) == [key]
    assert list(KnownHosts(f).match("router1", 22).host_keys) == [key]
    assert KnownHosts(f).judge("router1", 22, _key()) is HostKeyVerdict.CHANGED


def test_port_form_trusted_entry_wins_over_the_bare_form(tmp_path):
    bare, ported = _key(), _key()
    f = tmp_path / "known_hosts"
    f.write_text(f"router1 {_pub(bare)}\n[router1]:2222 {_pub(ported)}\n")
    assert list(KnownHosts(f).match("router1", 2222).host_keys) == [ported]
    assert list(KnownHosts(f).match("router1", 22).host_keys) == [bare]


def test_a_bare_entry_does_not_decide_another_port(tmp_path):
    """OpenSSH: on a non-22 port only ``[alias]:port`` decides, so a bare entry
    neither trusts a key there nor makes it a changed key: first use is accepted."""
    bare, ported = _key(), _key()
    f = tmp_path / "known_hosts"
    f.write_text(f"router1 {_pub(bare)}\n")
    store = KnownHosts(f)
    assert list(store.match("router1", 2222).host_keys) == []
    assert store.judge("router1", 2222, _key()) is HostKeyVerdict.FIRST_USE
    store.record("router1", 2222, ported)
    assert _lines(f) == [f"router1 {_pub(bare)}", f"[router1]:2222 {_pub(ported)}"]
    assert list(store.match("router1", 2222).host_keys) == [ported]


def test_missing_file_reads_as_empty_and_is_not_created(tmp_path):
    f = tmp_path / "nope" / "known_hosts"
    assert KnownHosts(f).judge("router1", 22, _key()) is HostKeyVerdict.FIRST_USE
    assert not f.parent.exists()


@pytest.mark.parametrize("revoked_form", ["router1", "[router1]:2222"])
def test_revocation_in_either_form_wins(tmp_path, revoked_form):
    """asyncssh's own lookup lets the port form's result replace the bare form's,
    revocations included, so a bare ``@revoked`` line would vanish on 2222;
    otto unions the revoked keys of both forms."""
    key = _key()
    f = tmp_path / "known_hosts"
    f.write_text(f"[router1]:2222 {_pub(key)}\n@revoked {revoked_form} {_pub(key)}\n")
    found = KnownHosts(f).match("router1", 2222)
    assert key in list(found.host_keys)
    assert key in list(found.revoked_keys)


def test_a_bracketed_port_22_revocation_revokes_on_port_22(tmp_path):
    """``[router1]:22`` is never port 22's deciding entry, but it names the
    same host and port, so its ``@revoked`` line counts there."""
    key = _key()
    f = tmp_path / "known_hosts"
    f.write_text(f"router1 {_pub(key)}\n@revoked [router1]:22 {_pub(key)}\n")
    found = KnownHosts(f).match("router1", 22)
    assert key in list(found.revoked_keys)
    assert list(found.host_keys) == [key]  # the bracketed form still decides nothing on 22
    with pytest.raises(HostKeyRevokedError):
        KnownHosts(f).record("router1", 22, key)


def test_a_bracketed_port_22_entry_does_not_decide_port_22(tmp_path):
    f = tmp_path / "known_hosts"
    f.write_text(f"[router1]:22 {_pub(_key())}\n")
    assert KnownHosts(f).judge("router1", 22, _key()) is HostKeyVerdict.FIRST_USE


def test_a_port_revocation_does_not_reach_port_22(tmp_path):
    key = _key()
    f = tmp_path / "known_hosts"
    f.write_text(f"router1 {_pub(key)}\n@revoked [router1]:2222 {_pub(key)}\n")
    assert list(KnownHosts(f).match("router1", 22).revoked_keys) == []


@pytest.mark.parametrize("subject_host", ["router1", "@revoked router1"])
def test_x509_subject_line_refused_with_line_number(tmp_path, subject_host):
    f = tmp_path / "known_hosts"
    f.write_text(
        f"# comment\n[router1]:2222 {_pub(_key())}\n"
        f"{subject_host} x509v3-sign-rsa subject=CN=router1\n"
    )
    store = KnownHosts(f)
    with pytest.raises(KnownHostsError, match=rf"{re.escape(str(f))}:3:.*X\.509 subject.*router1"):
        store.match("router1", 22)
    if subject_host.startswith("@revoked"):  # a bare revocation applies on 2222 too
        with pytest.raises(KnownHostsError, match=rf"{re.escape(str(f))}:3:"):
            store.judge("router1", 2222, _key())


def test_another_hosts_x509_subject_line_does_not_break_the_check(tmp_path):
    key = _key()
    f = tmp_path / "known_hosts"
    f.write_text(f"router2 x509v3-sign-rsa subject=CN=router2\nrouter1 {_pub(key)}\n")
    store = KnownHosts(f)
    assert list(store.match("router1", 22).host_keys) == [key]
    assert store.judge("router1", 22, _key()) is HostKeyVerdict.CHANGED
    store.record("router1", 22, key)
    store.record("router1", 2222, _key())
    assert len(_lines(f)) == 3


def test_match_result_is_what_asyncssh_accepts_from_a_known_hosts_callable(tmp_path):
    """The result goes back to asyncssh through its ``known_hosts`` callable."""
    from asyncssh.known_hosts import match_known_hosts

    key = _key()
    f = tmp_path / "known_hosts"
    f.write_text(f"[router1]:2222 {_pub(key)}\n@revoked router1 {_pub(_key())}\n")
    store = KnownHosts(f)

    def by_name(host: str, _addr: str, port: int | None) -> Any:
        # asyncssh annotates the callable's result as strings; at runtime it
        # loads key objects as readily, which is what otto hands it.
        return store.match(host, port)

    result = match_known_hosts(by_name, "router1", "", 2222)
    assert list(result[0]) == [key]
    assert len(result[2]) == 1


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def test_record_bare_and_port_forms(tmp_path):
    """22 and 2222 hold different keys: each is recorded and matched on its own."""
    k22, k2222 = _key(), _key()
    f = tmp_path / "known_hosts"
    store = KnownHosts(f)
    store.record("router1", 22, k22)
    store.record("router1", 2222, k2222)
    assert _lines(f) == [f"router1 {_pub(k22)}", f"[router1]:2222 {_pub(k2222)}"]
    assert list(store.match("router1", 22).host_keys) == [k22]
    assert list(store.match("router1", 2222).host_keys) == [k2222]
    store.record("router1", 22, k22)  # already held: nothing written
    store.record("router1", 2222, k2222)
    assert len(_lines(f)) == 2
    with pytest.raises(HostKeyChangedError, match=re.escape("[router1]:2222")):
        store.record("router1", 2222, k22)


def test_record_appends_after_a_file_without_a_final_newline(tmp_path):
    other, key = _key(), _key()
    f = tmp_path / "known_hosts"
    f.write_text(f"router9 {_pub(other)}")
    KnownHosts(f).record("router1", 22, key)
    assert _lines(f) == [f"router9 {_pub(other)}", f"router1 {_pub(key)}"]


def test_record_refuses_when_another_process_recorded_a_different_key(tmp_path, monkeypatch):
    """The lock-free check saw no entry; by the time the lock is held another
    process has recorded a different key. The re-read under the lock refuses,
    stating the fact (the file holds a different key), not a guess at who wrote it."""
    ours, theirs = _key(), _key()
    f = tmp_path / "known_hosts"
    f.write_text("")
    store = KnownHosts(f)
    real_locked = KnownHosts._locked

    def locked_after_another_writer(self: KnownHosts) -> Generator[None, None, None]:
        with real_locked(self):
            with f.open("a") as out:
                out.write(f"router1 {_pub(theirs)}\n")
            yield

    monkeypatch.setattr(
        KnownHosts, "_locked", contextlib.contextmanager(locked_after_another_writer)
    )
    with pytest.raises(HostKeyChangedError, match="holds a different key for router1") as ei:
        store.record("router1", 22, ours)
    assert "another process" not in str(ei.value)
    # The advice is HostKeyRefusedError's, which the connection turns this into.
    assert "forget-key" not in str(ei.value)
    assert _lines(f) == [f"router1 {_pub(theirs)}"]


@pytest.mark.parametrize("revoked_form", ["router1", "[router1]:22"])
def test_record_refuses_a_key_revoked_after_the_lock_free_read(tmp_path, monkeypatch, revoked_form):
    """The handshake saw no entry; by the time the lock is held the key has
    been revoked. The re-read under the lock judges revocations too, so the
    key is refused and never recorded."""
    key = _key()
    f = tmp_path / "known_hosts"
    f.write_text("")
    store = KnownHosts(f)
    real_locked = KnownHosts._locked

    def locked_after_a_revocation(self: KnownHosts) -> Generator[None, None, None]:
        with real_locked(self):
            with f.open("a") as out:
                out.write(f"@revoked {revoked_form} {_pub(key)}\n")
            yield

    monkeypatch.setattr(KnownHosts, "_locked", contextlib.contextmanager(locked_after_a_revocation))
    with pytest.raises(HostKeyRevokedError, match="revokes the key offered for router1"):
        store.record("router1", 22, key)
    assert _lines(f) == [f"@revoked {revoked_form} {_pub(key)}"]


def test_record_refuses_a_revoked_key_even_when_it_is_also_trusted(tmp_path):
    """Revocation wins, as in the handshake: a trusted-and-revoked key is not
    "already recorded", it is refused."""
    key = _key()
    f = tmp_path / "known_hosts"
    f.write_text(f"router1 {_pub(key)}\n@revoked router1 {_pub(key)}\n")
    with pytest.raises(HostKeyRevokedError):
        KnownHosts(f).record("router1", 22, key)


def test_record_writes_the_whole_line_through_short_writes(tmp_path, monkeypatch):
    """``os.write`` may write fewer bytes than it was given; the line still lands whole."""
    key = _key()
    f = tmp_path / "known_hosts"
    real_write = os.write

    def short_write(fd: int, data: bytes) -> int:
        return real_write(fd, data[:5]) if fd > 2 else real_write(fd, data)

    monkeypatch.setattr(os, "write", short_write)
    KnownHosts(f).record("router1", 22, key)
    monkeypatch.undo()
    assert _lines(f) == [f"router1 {_pub(key)}"]


def test_record_that_cannot_write_the_line_whole_refuses(tmp_path, monkeypatch):
    """A write that makes no progress is a failure, raised as unwritable, so the
    connection that needed the record refuses rather than authenticates."""
    f = tmp_path / "known_hosts"

    def stuck_write(fd: int, data: bytes) -> int:
        return 0

    monkeypatch.setattr(os, "write", stuck_write)
    with pytest.raises(KnownHostsUnwritableError, match="could not be written whole"):
        KnownHosts(f).record("router1", 22, _key())


def _record_in_child(path: str, pub: str, ready, results) -> None:
    """Child process: record *pub*, signalling once the lock-free check has seen no entry."""
    from otto.host import known_hosts as kh

    real_locked = kh.KnownHosts._locked

    def signalling_locked(self: kh.KnownHosts) -> Generator[None, None, None]:
        ready.release()  # the lock-free read is done and found nothing
        with real_locked(self):
            yield

    setattr(kh.KnownHosts, "_locked", contextlib.contextmanager(signalling_locked))  # noqa: B010 — a child process, no monkeypatch
    try:
        kh.KnownHosts(Path(path)).record("router1", 22, asyncssh.import_public_key(pub))
        results.put("ok")
    except (OSError, ValueError) as err:  # reported to the parent, which asserts on it
        results.put(repr(err))


def test_two_processes_record_one_line(tmp_path):
    """Both processes pass the lock-free check before either may take the lock,
    so only the re-read under the lock keeps the second from appending."""
    key = _key()
    f = tmp_path / "known_hosts"
    ctx = multiprocessing.get_context("fork")
    ready = ctx.Semaphore(0)
    results = ctx.Queue()
    store = KnownHosts(f)
    with store._locked():  # held by the parent until both children are past their read
        children = [
            ctx.Process(
                target=_record_in_child,
                args=(str(f), _pub(key), ready, results),
                daemon=True,  # a hung child cannot hold the worker open at exit
            )
            for _ in range(2)
        ]
        for child in children:
            child.start()
        for _ in children:
            assert ready.acquire(timeout=60), "a child never reached the lock"
    for child in children:
        child.join(timeout=60)
        assert child.exitcode == 0
    assert sorted(results.get(timeout=5) for _ in children) == ["ok", "ok"]
    assert _lines(f) == [f"router1 {_pub(key)}"]


@pytest.mark.skipif(_cannot_enforce_permissions(), reason="root ignores file modes")
def test_unwritable_file_refuses_naming_both_fixes(tmp_path):
    f = tmp_path / "known_hosts"
    f.write_text(f"router9 {_pub(_key())}\n")
    f.chmod(0o444)
    with pytest.raises(KnownHostsUnwritableError) as ei:
        KnownHosts(f).record("router1", 22, _key())
    message = str(ei.value)
    assert str(f) in message
    assert 'known_hosts = "<writable path>"' in message
    assert "verify_host_key = false" in message


@pytest.mark.skipif(_cannot_enforce_permissions(), reason="root ignores file modes")
def test_read_only_matching_file_is_never_written(tmp_path):
    """A pre-seeded read-only file (CI) whose entry matches: no lock, no write, no error."""
    key = _key()
    d = tmp_path / "ro"
    d.mkdir()
    f = d / "known_hosts"
    f.write_text(f"router1 {_pub(key)}\n")
    f.chmod(0o444)
    d.chmod(0o555)
    try:
        KnownHosts(f).record("router1", 22, key)
        assert sorted(p.name for p in d.iterdir()) == ["known_hosts"]
    finally:
        d.chmod(0o755)


def test_missing_ssh_dir_created_0700_file_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = known_hosts_path(SshOptions())
    assert path == tmp_path / ".ssh" / "known_hosts"
    key = _key()
    KnownHosts(path).record("router1", 22, key)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _lines(path) == [f"router1 {_pub(key)}"]


def test_known_hosts_path_expands_a_configured_home_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert known_hosts_path(SshOptions(known_hosts="~/lab/kh")) == tmp_path / "lab" / "kh"
    assert known_hosts_path(SshOptions(known_hosts="/etc/kh")) == Path("/etc/kh")


# ---------------------------------------------------------------------------
# Forgetting
# ---------------------------------------------------------------------------


def test_forget_removes_bare_port_and_hashed_entries(tmp_path):
    keep = f"router9 {_pub(_key())}"
    address = f"10.0.0.5 {_pub(_key())}"
    f = tmp_path / "known_hosts"
    f.write_text(
        "\n".join(
            [
                f"router1 {_pub(_key())}",
                keep,
                f"[router1]:2222 {_pub(_key())}",
                f"[router1]:8022 {_pub(_key())}",  # a plain port form is found by text
                f"{_hashed('router1')} {_pub(_key())}",
                f"{_hashed('[router1]:2222')} {_pub(_key())}",
                address,
            ]
        )
        + "\n"
    )
    result = KnownHosts(f).forget("router1", [2222, 22])
    assert result == ForgetResult(alias="router1", removed=5, kept_shared=[], path=f)
    assert _lines(f) == [keep, address]
    assert str(result) == f"removed 5 entries for router1 from {f}"


def test_forget_keeps_shared_wildcard_ca_and_revoked_lines(tmp_path):
    shared = f"router1,router2 {_pub(_key())}"
    wildcard = f"router* {_pub(_key())}"
    ca = f"@cert-authority router1 {_pub(_key())}"
    revoked = f"@revoked [router1]:2222 {_pub(_key())}"
    # Negates the bare name only, so it still applies to [router1]:2222.
    negated_bare = f"!router1,* {_pub(_key())}"
    unrelated = f"!router1,![router1]:*,* {_pub(_key())}"
    own = f"router1 {_pub(_key())}"
    keep = [shared, wildcard, ca, revoked, negated_bare]
    f = tmp_path / "known_hosts"
    f.write_text("".join(f"{line}\n" for line in [*keep, unrelated, own]))
    result = KnownHosts(f).forget("router1", [2222])
    assert result.removed == 1
    assert result.kept_shared == keep
    assert _lines(f) == [*keep, unrelated]
    assert str(result).splitlines() == [
        f"removed 1 entry for router1 from {f}",
        *(f"kept: {line}" for line in keep),
    ]


def test_forget_with_no_entry_reports_zero_and_the_path(tmp_path):
    other = f"router9 {_pub(_key())}\n"
    f = tmp_path / "known_hosts"
    f.write_text(other)
    result = KnownHosts(f).forget("router1", [22])
    assert result.removed == 0
    assert str(result) == f"no entry for router1 in {f}"
    assert f.read_text() == other
    missing = tmp_path / "absent" / "known_hosts"
    assert str(KnownHosts(missing).forget("router1", [22])) == f"no entry for router1 in {missing}"
    assert not missing.parent.exists()


@pytest.mark.skipif(_cannot_enforce_permissions(), reason="root ignores file modes")
def test_forget_with_no_entry_in_a_read_only_directory_takes_no_lock(tmp_path):
    """Nothing to remove means nothing to write, the lock file included: a
    read-only pre-seeded file (CI) answers "no entry" instead of failing."""
    other = f"router9 {_pub(_key())}\n"
    d = tmp_path / "ro"
    d.mkdir()
    f = d / "known_hosts"
    f.write_text(other)
    f.chmod(0o444)
    d.chmod(0o555)
    try:
        result = KnownHosts(f).forget("router1", [22])
        assert sorted(p.name for p in d.iterdir()) == ["known_hosts"]
    finally:
        d.chmod(0o755)
    assert result.removed == 0
    assert str(result) == f"no entry for router1 in {f}"
    assert f.read_text() == other


def test_forget_re_reads_under_the_lock(tmp_path, monkeypatch):
    """The lock-free read found a line to remove; under the lock another writer
    has added a second one. The rewrite works from the re-read, so both go and
    the other writer's unrelated line stays."""
    other = f"router9 {_pub(_key())}"
    f = tmp_path / "known_hosts"
    f.write_text(f"router1 {_pub(_key())}\n")
    real_locked = KnownHosts._locked

    def locked_after_another_writer(self: KnownHosts) -> Generator[None, None, None]:
        with real_locked(self):
            with f.open("a") as out:
                out.write(f"{other}\nrouter1 {_pub(_key())}\n")
            yield

    monkeypatch.setattr(
        KnownHosts, "_locked", contextlib.contextmanager(locked_after_another_writer)
    )
    assert KnownHosts(f).forget("router1", [22]).removed == 2
    assert _lines(f) == [other]


def test_forget_keeps_the_file_mode(tmp_path):
    f = tmp_path / "known_hosts"
    f.write_text(f"router1 {_pub(_key())}\nrouter9 {_pub(_key())}\n")
    f.chmod(0o640)
    KnownHosts(f).forget("router1", [22])
    assert stat.S_IMODE(f.stat().st_mode) == 0o640


def test_forget_through_a_symlink_edits_the_target_and_keeps_the_link(tmp_path):
    real = tmp_path / "real" / "known_hosts"
    real.parent.mkdir()
    other = f"router9 {_pub(_key())}"
    real.write_text(f"router1 {_pub(_key())}\n{other}\n")
    link = tmp_path / "known_hosts"
    link.symlink_to(real)
    assert KnownHosts(link).forget("router1", [22]).removed == 1
    assert link.is_symlink()
    assert _lines(real) == [other]
    # The lock lives beside the real file, the one every writer resolves to.
    assert (real.parent / "known_hosts.lock").exists()
    assert not (tmp_path / "known_hosts.lock").exists()


@pytest.mark.parametrize("failing", ["write", "fsync"])
def test_a_failed_forget_leaves_the_file_byte_identical(tmp_path, monkeypatch, failing):
    """forget rewrites the user's shared file write-then-rename, like
    ``ssh-keygen -R``: a failure mid-write never truncates it."""
    f = tmp_path / "known_hosts"
    original = f"router1 {_pub(_key())}\nrouter9 {_pub(_key())}\n".encode()
    f.write_bytes(original)

    def broken(*_args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, failing, broken)
    with pytest.raises(KnownHostsUnwritableError, match="No space left"):
        KnownHosts(f).forget("router1", [22])
    monkeypatch.undo()
    assert f.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["known_hosts", "known_hosts.lock"]


# ---------------------------------------------------------------------------
# judge: the one verdict the handshake and record share
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lines", "verdict"),
    [
        ([], HostKeyVerdict.FIRST_USE),
        (["router1 {key}"], HostKeyVerdict.TRUSTED),
        (["router1 {other}"], HostKeyVerdict.CHANGED),
        (["router1 {other}", "router1 {key}"], HostKeyVerdict.TRUSTED),
        (["@revoked router1 {key}"], HostKeyVerdict.REVOKED),
        # Revocation wins over an entry that also holds the key, under either form.
        (["router1 {key}", "@revoked router1 {key}"], HostKeyVerdict.REVOKED),
        (["router1 {key}", "@revoked [router1]:22 {key}"], HostKeyVerdict.REVOKED),
        # Another host's entry decides nothing for router1.
        (["router2 {other}"], HostKeyVerdict.FIRST_USE),
        # A CA line for the host: known by its CA, so a plain key is no first use.
        (["@cert-authority router1 {other}"], HostKeyVerdict.NOT_CERTIFIED),
        # A plain key held as well: the plain entry decides, as a changed key.
        (["@cert-authority router1 {other}", "router1 {other}"], HostKeyVerdict.CHANGED),
        (["@cert-authority router1 {other}", "router1 {key}"], HostKeyVerdict.TRUSTED),
        (["@cert-authority router2 {other}"], HostKeyVerdict.FIRST_USE),
    ],
)
def test_judge_gives_each_verdict(tmp_path, lines, verdict):
    key, other = _key(), _key()
    kh = tmp_path / "known_hosts"
    kh.write_text("".join(f"{line.format(key=_pub(key), other=_pub(other))}\n" for line in lines))
    assert KnownHosts(kh).judge("router1", 22, key) is verdict


def test_judge_on_another_port_reads_the_bracketed_entry_only(tmp_path):
    key = _key()
    kh = tmp_path / "known_hosts"
    kh.write_text(f"router1 {_pub(_key())}\n")
    assert KnownHosts(kh).judge("router1", 2222, key) is HostKeyVerdict.FIRST_USE
    kh.write_text(f"[router1]:2222 {_pub(key)}\n")
    assert KnownHosts(kh).judge("router1", 2222, key) is HostKeyVerdict.TRUSTED


def test_record_refuses_a_plain_key_for_a_host_known_by_its_ca(tmp_path):
    """record judges through the same verdict: a CA line that appeared since the
    handshake's check makes the plain key not certified, and nothing is written."""
    from otto.host.errors import HostKeyNotCertifiedError

    f = tmp_path / "known_hosts"
    ca = f"@cert-authority router1 {_pub(_key())}"
    f.write_text(f"{ca}\n")
    with pytest.raises(HostKeyNotCertifiedError):
        KnownHosts(f).record("router1", 22, _key())
    assert _lines(f) == [ca]
