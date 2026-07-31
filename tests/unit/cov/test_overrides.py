"""load_override_config: the override file's full validation surface."""

import subprocess
from pathlib import Path

import pytest

from otto.coverage.overrides import (
    DEFAULT_OVERRIDES_RELPATH,
    OverrideConfigError,
    load_override_config,
)
from otto.coverage.tiers import TierConfig

_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": "/usr/bin:/bin",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env={**_ENV, "HOME": str(root)},
    ).stdout


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "sut"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.c").write_text("line1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "c1 #1")
    return root, _git(root, "rev-parse", "HEAD").strip()


def _manual_tier(name: str = "bench") -> TierConfig:
    return TierConfig(name=name, kind="manual", precedence=5, color="purple")


_TICKETS_CFG = {"tickets": {"pattern": "#(?P<num>[0-9]+)"}}


def _write(root: Path, text: str) -> None:
    path = root / DEFAULT_OVERRIDES_RELPATH
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)


def test_absent_default_file_and_absent_key_is_none(tmp_path):
    root, _ = _repo(tmp_path)
    assert load_override_config(_TICKETS_CFG, root, [_manual_tier()]) is None


def test_explicit_key_with_missing_file_fails_loud(tmp_path):
    root, _ = _repo(tmp_path)
    cfg = {**_TICKETS_CFG, "overrides": {"file": "nope.toml"}}
    with pytest.raises(OverrideConfigError, match=r"nope\.toml"):
        load_override_config(cfg, root, [_manual_tier()])


def test_file_without_coverage_tickets_fails_loud(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[[bench]]\ncommit = "{sha}"\nreason = "r"\n')
    with pytest.raises(OverrideConfigError, match=r"\[coverage.tickets\]"):
        load_override_config({}, root, [_manual_tier()])


def test_commit_entry_loads_with_resolved_full_sha(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[[bench]]\ncommit = "{sha[:8]}"\nreason = "hand-tested"\n')
    cfg = load_override_config(_TICKETS_CFG, root, [_manual_tier()])
    assert cfg is not None
    (entry,) = cfg.asserted
    assert (entry.id, entry.tier, entry.commit, entry.as_of) == (0, "bench", sha, None)
    assert entry.key == f"commit:{sha}"


def test_ticket_entry_requires_as_of(tmp_path):
    root, _ = _repo(tmp_path)
    _write(root, '[[bench]]\nticket = "#1"\nreason = "r"\n')
    with pytest.raises(OverrideConfigError, match="as_of"):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


def test_ticket_entry_loads_with_resolved_as_of(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[[bench]]\nticket = "#1"\nas_of = "{sha[:8]}"\nreason = "r"\n')
    cfg = load_override_config(_TICKETS_CFG, root, [_manual_tier()])
    (entry,) = cfg.asserted
    assert (entry.ticket, entry.as_of, entry.key) == ("#1", sha, "ticket:#1")


def test_commit_entry_with_as_of_fails(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[[bench]]\ncommit = "{sha}"\nas_of = "{sha}"\nreason = "r"\n')
    with pytest.raises(OverrideConfigError, match="as_of"):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


@pytest.mark.parametrize(
    "body",
    [
        '[[bench]]\nreason = "r"\n',  # neither key
        '[[bench]]\nticket = "#1"\ncommit = "HEAD"\nreason = "r"\n',  # both keys
        '[[bench]]\nticket = "#1"\nas_of = "HEAD"\n',  # missing reason
        '[[bench]]\nticket = "#1"\nas_of = "HEAD"\nreason = ""\n',  # empty reason
        '[[bench]]\nticket = "#1"\nas_of = "HEAD"\nreason = "r"\nbogus = 1\n',  # unknown key
    ],
)
def test_malformed_asserted_entries_fail_loud(tmp_path, body):
    root, _ = _repo(tmp_path)
    _write(root, body)
    with pytest.raises(OverrideConfigError):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


def test_unknown_table_name_fails_loud(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[[bnech]]\ncommit = "{sha}"\nreason = "r"\n')  # typo'd tier
    with pytest.raises(OverrideConfigError, match="bnech"):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


def test_non_manual_tier_table_fails_loud(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[[system]]\ncommit = "{sha}"\nreason = "r"\n')
    tiers = [_manual_tier(), TierConfig(name="system", kind="e2e", precedence=1, color="green")]
    with pytest.raises(OverrideConfigError, match="manual"):
        load_override_config(_TICKETS_CFG, root, tiers)


def test_manual_tier_named_reattribute_is_reserved(tmp_path):
    root, _ = _repo(tmp_path)
    _write(root, "")
    with pytest.raises(OverrideConfigError, match="reserved"):
        load_override_config(_TICKETS_CFG, root, [_manual_tier("reattribute")])


def test_unresolvable_sha_fails_loud(tmp_path):
    root, _ = _repo(tmp_path)
    _write(root, '[[bench]]\ncommit = "deadbeef"\nreason = "r"\n')
    with pytest.raises(OverrideConfigError, match="deadbeef"):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


def test_reattribute_entry_loads(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[[reattribute]]\ncommit = "{sha[:8]}"\ntickets = ["#9"]\nreason = "wrong id"\n')
    cfg = load_override_config(_TICKETS_CFG, root, [_manual_tier()])
    assert cfg.reattributions == {sha: ["#9"]}


def test_reattribute_empty_tickets_is_legal(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[[reattribute]]\ncommit = "{sha}"\ntickets = []\nreason = "none"\n')
    cfg = load_override_config(_TICKETS_CFG, root, [_manual_tier()])
    assert cfg.reattributions == {sha: []}


def test_reattribute_duplicate_commit_fails(tmp_path):
    root, sha = _repo(tmp_path)
    _write(
        root,
        f'[[reattribute]]\ncommit = "{sha}"\ntickets = ["#9"]\nreason = "a"\n'
        f'[[reattribute]]\ncommit = "{sha[:8]}"\ntickets = ["#8"]\nreason = "b"\n',
    )
    with pytest.raises(OverrideConfigError, match="duplicate"):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


def test_reserved_sentinel_ticket_id_fails(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[[reattribute]]\ncommit = "{sha}"\ntickets = ["(no ticket)"]\nreason = "r"\n')
    with pytest.raises(OverrideConfigError, match="reserved"):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


def test_entry_ids_are_file_order_across_tiers(tmp_path):
    root, sha = _repo(tmp_path)
    _write(
        root,
        f'[[bench]]\ncommit = "{sha}"\nreason = "a"\n'
        f'[[field]]\ncommit = "{sha}"\nreason = "b"\n'
        f'[[bench]]\nticket = "#1"\nas_of = "{sha}"\nreason = "c"\n',
    )
    cfg = load_override_config(_TICKETS_CFG, root, [_manual_tier(), _manual_tier("field")])
    assert [(e.id, e.tier) for e in cfg.asserted] == [(0, "bench"), (1, "bench"), (2, "field")]


def test_toml_syntax_error_fails_loud(tmp_path):
    root, _ = _repo(tmp_path)
    _write(root, "[[bench]\ncommit = \n")
    with pytest.raises(OverrideConfigError):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


def test_top_level_table_not_a_list_fails_loud(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[bench]\ncommit = "{sha}"\nreason = "r"\n')
    with pytest.raises(OverrideConfigError, match="array of tables"):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


def test_non_table_entry_in_array_fails_loud(tmp_path):
    root, _ = _repo(tmp_path)
    _write(root, 'bench = ["x"]\n')
    with pytest.raises(OverrideConfigError, match="table"):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


def test_reattribute_entry_unknown_key_fails_loud(tmp_path):
    root, sha = _repo(tmp_path)
    _write(
        root,
        f'[[reattribute]]\ncommit = "{sha}"\ntickets = ["#1"]\nreason = "r"\nbogus = 1\n',
    )
    with pytest.raises(OverrideConfigError, match="unknown key"):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


def test_reattribute_entry_missing_key_fails_loud(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[[reattribute]]\ncommit = "{sha}"\ntickets = ["#1"]\n')
    with pytest.raises(OverrideConfigError, match="missing required key"):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


@pytest.mark.parametrize(
    "tickets_toml",
    [
        "tickets = [1]\n",
        'tickets = [""]\n',
    ],
)
def test_reattribute_malformed_tickets_fails_loud(tmp_path, tickets_toml):
    root, sha = _repo(tmp_path)
    _write(root, f'[[reattribute]]\ncommit = "{sha}"\n{tickets_toml}reason = "r"\n')
    with pytest.raises(OverrideConfigError, match="tickets"):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


def test_asserted_reserved_sentinel_ticket_id_fails(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[[bench]]\nticket = "(no ticket)"\nas_of = "{sha}"\nreason = "r"\n')
    with pytest.raises(OverrideConfigError, match="reserved"):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


def test_non_ascii_reason_loads_regardless_of_locale(tmp_path, monkeypatch):
    """F5 (final review): TOML mandates UTF-8 regardless of locale, but the
    loader used to read the file with ``Path.read_text()`` (no explicit
    encoding) — the platform locale-default encoding, which on a
    C/POSIX-locale process can be plain ASCII and would mis-decode or raise
    on a non-ASCII ``reason``. Pin the fix two ways: (1) it must not touch
    ``Path.read_text`` at all (the locale-dependent call) — patched here to
    blow up if invoked — and (2) a non-ASCII ``reason`` (accented Latin +
    CJK) must load correctly via the binary + explicit-UTF-8 path.
    """

    def _boom(*_a, **_kw):
        raise AssertionError("read_text (locale-dependent decode) must not be used")

    monkeypatch.setattr(Path, "read_text", _boom)
    root, sha = _repo(tmp_path)
    reason = "Vérifié à la main — 手動確認済み"
    path = root / DEFAULT_OVERRIDES_RELPATH
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(f'[[bench]]\nticket = "#1"\nas_of = "{sha}"\nreason = "{reason}"\n'.encode())
    cfg = load_override_config(_TICKETS_CFG, root, [_manual_tier()])
    (entry,) = cfg.asserted
    assert entry.reason == reason
