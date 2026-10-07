"""The public-API golden: the API dump of every namespace ``api/public.toml`` declares.

``scripts/api_snapshot.py`` writes it (``make api-snapshot``) and
``scripts/api_regen.py`` generates it. This pins the committed golden
(``tests/unit/api_snapshot/public_api.txt``) to the working tree's dump byte
for byte, proves that a real change to the surface moves the dump, and keeps
the guarantee that every recorded binding resolves. The format and the rules
are the dump spec's (``docs/superpowers/specs/2026-10-05-api-dump-design.md``).

No ``interpreter_agnostic`` marker: the golden must be the dump on every
interpreter of the matrix (dump spec §6).
"""

import importlib
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import api_compat, api_records, api_regen
from scripts.api_agreement import namespace_reports
from scripts.api_manifest import load_manifest
from tests._fixtures.gitrepo import git_env
from tests._fixtures.paths import PROJECT_ROOT

_MODULE_PATH = PROJECT_ROOT / "scripts" / "api_snapshot.py"
MANIFEST = PROJECT_ROOT / "api" / "public.toml"


def _load():
    spec = importlib.util.spec_from_file_location("api_snapshot", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load()


@pytest.fixture(scope="module")
def live_dump() -> str:
    """The working tree's API dump, generated once for the module."""
    generated = api_regen.generate_worktree(PROJECT_ROOT, MANIFEST)
    assert generated.text is not None, generated.refusals
    return generated.text


def test_the_golden_is_the_dump_of_the_working_tree(live_dump):
    """A declared name, signature, member or format that changed changes this dump.

    The failure says how (breaking, grown, or re-sorted); the fix is usually
    ``make api-snapshot`` and a review of the diff.
    """
    golden = mod.GOLDEN_PATH.read_bytes()
    assert golden == live_dump.encode("utf-8"), (
        "tests/unit/api_snapshot/public_api.txt is not the API dump of this tree "
        "-- run `make api-snapshot` and review the diff.\n"
        + mod.describe_dump_drift(golden.decode("utf-8", errors="replace"), live_dump)
    )


def test_the_dump_records_every_name_the_declaration_exports(live_dump):
    """The dump's size check: every declared ``__all__`` name has its ``name`` record.

    It replaces v1's binding-count floor: the dump's coverage is exact, so a
    producer that dropped declared names is caught by name, not by a count.
    """
    names = sorted(load_manifest(MANIFEST))
    assert mod.undumped_names(live_dump, namespace_reports(names, PROJECT_ROOT)) == []


def _resolve(module: str, name: str) -> "str | None":
    """Import *module*, then ``getattr`` the dotted *name* off it; return an error, or None."""
    try:
        obj = importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001 -- a dead golden entry is a finding, not a crash
        return f"cannot import {module!r}: {exc}"
    try:
        for part in name.split("."):
            obj = getattr(obj, part)
    except AttributeError as exc:
        return f"{module}:{name} does not exist: {exc}"
    return None


def _unresolved(text: str) -> "list[str]":
    """Return one message per ``name`` record of dump *text* whose key does not resolve.

    Every ``ns:dotted`` key must resolve by ``getattr`` along the dots. A dump
    that does not parse is one message.
    """
    try:
        dump = api_records.parse_dump(text)
    except api_records.DumpError as exc:
        return [f"the dump does not parse: {exc}"]
    unresolved = []
    for key in sorted(dump.bindings):
        module_name, _, name = key.partition(":")
        error = _resolve(module_name, name)
        if error is not None:
            unresolved.append(f"{key}: {error}")
    return unresolved


def test_every_golden_line_resolves():
    """Each committed binding must still import -- a golden that can go stale is worthless."""
    unresolved = _unresolved(mod.GOLDEN_PATH.read_text(encoding="utf-8"))
    assert not unresolved, unresolved


V2_FIXTURE = (
    "# api-snapshot v2\n"
    "# producer-schema 1\n"
    "name\totto.tls:os_trust_session\tfunction\n"
    "call\totto.tls:os_trust_session\tsync\tKO:timeout:F:0x1.e000000000000p+4\n"
)


def test_v2_name_records_resolve_through_their_namespace():
    """Spec §6: the every-entry-resolves guarantee carries over to the dump's ``name`` records."""
    assert _unresolved(V2_FIXTURE) == []


def test_v2_resolution_reports_a_dead_name_and_an_unparseable_dump():
    dead = V2_FIXTURE + "name\totto.tls:NoSuchThingAtAll\tvalue\n"
    (message,) = _unresolved(dead)
    assert message.startswith("otto.tls:NoSuchThingAtAll: ")
    (broken,) = _unresolved(V2_FIXTURE + "bogus\n")
    assert broken.startswith("the dump does not parse: ")


def _dump_of_an_edited_copy(tmp_path: Path, rel: str, anchors: "list[str]", old: str, new: str):
    """Return the dump of a copy of the tracked ``src`` with one edit in *rel*.

    The copy holds exactly the files git tracks under ``src/``, as the
    checker's archive does (dump spec §5.1). In *rel*, the first *old* after
    each of *anchors* in turn is replaced by *new*.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "src"],
        cwd=PROJECT_ROOT,
        env=git_env(tmp_path / "home"),  # hermetic per tests/_fixtures/gitrepo.py
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    for path in filter(None, listed.split("\0")):
        source = PROJECT_ROOT / path
        if source.is_file():
            (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, tmp_path / path)
    target = tmp_path / "src" / rel
    text = target.read_text(encoding="utf-8")
    at = 0
    for anchor in anchors:
        at = text.index(anchor, at)
    at = text.index(old, at)
    target.write_text(text[:at] + new + text[at + len(old) :], encoding="utf-8")
    generated = api_regen.generate_worktree(tmp_path, MANIFEST)
    assert generated.text is not None, generated.refusals
    return generated.text


def _findings(old: str, new: str) -> "list[str]":
    return api_compat.compare_dumps(api_records.parse_dump(old), api_records.parse_dump(new))


def test_red_a_name_dropped_from_a_declared_all_moves_the_dump(tmp_path, live_dump):
    """RED-proof: drop a name from ``otto.__all__`` and the dump loses its record."""
    mutated = _dump_of_an_edited_copy(
        tmp_path, "otto/__init__.py", ["__all__ = ["], '    "CommandResult",\n', ""
    )
    assert mutated != live_dump
    assert "otto:CommandResult: removed" in _findings(live_dump, mutated)


def test_red_a_renamed_host_parameter_moves_the_dump(tmp_path, live_dump):
    """RED-proof: rename a ``Host.put`` parameter and the dump records a breaking change.

    ``Host`` is a structural protocol: a family that renamed a keyword otto's
    own call sites pass would still type-check, so the dump reads the live
    signature (``member otto.host:Host.put``).
    """
    mutated = _dump_of_an_edited_copy(
        tmp_path,
        "otto/host/host.py",
        ["class Host(Protocol):", "    async def put("],
        "        user: str | None = None,\n",
        "        renamed_user: str | None = None,\n",
    )
    findings = _findings(live_dump, mutated)
    assert any(f.startswith("otto.host:Host.put: ") and "user" in f for f in findings), findings


def _tls_manifest(tmp_path):
    manifest = tmp_path / "public.toml"
    manifest.write_text(
        '[namespaces."otto.tls"]\ntier = 1\nstability = "provisional"\n', encoding="utf-8"
    )
    return manifest


def test_dump_mode_prints_the_dump_of_a_manifest(tmp_path, capsys):
    exit_code = mod.main(["--manifest", str(_tls_manifest(tmp_path))])
    out = capsys.readouterr().out.splitlines()
    assert exit_code == 0
    assert out[:2] == ["# api-snapshot v2", "# producer-schema 1"]
    assert any(line.startswith("name\totto.tls:os_trust_session\t") for line in out)
    assert not any(line.startswith("otto.host.host:Host.") for line in out)


def test_dump_check_compares_against_the_given_golden(tmp_path, capsys):
    manifest, golden = _tls_manifest(tmp_path), tmp_path / "golden.txt"
    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--update"]) == 0
    assert golden.read_text(encoding="utf-8").startswith("# api-snapshot v2\n# producer-schema 1\n")
    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--check"]) == 0
    golden.write_text(golden.read_text(encoding="utf-8") + "name\totto.tls:Ghost\tvalue\n")
    capsys.readouterr()
    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--check"]) == 1
    out = capsys.readouterr().out
    assert "public API CHANGED" in out
    assert "Ghost: removed [otto.tls]" in out


def test_dump_check_calls_a_resorted_golden_stale_but_not_a_change(tmp_path, capsys):
    manifest, golden = _tls_manifest(tmp_path), tmp_path / "golden.txt"
    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--update"]) == 0
    header, schema, body = golden.read_text(encoding="utf-8").split("\n", 2)
    records = body.splitlines(keepends=True)
    assert len(records) > 1, (
        "otto.tls must dump more than one record for a re-sort to mean anything"
    )
    golden.write_text(f"{header}\n{schema}\n" + "".join(reversed(records)), encoding="utf-8")
    capsys.readouterr()
    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--check"]) == 1
    out = capsys.readouterr().out
    assert "not in canonical order" in out
    assert "CHANGED" not in out


def test_dump_update_refuses_to_write_when_the_producer_refuses(tmp_path, capsys, monkeypatch):
    from scripts import api_regen

    monkeypatch.setattr(
        api_regen,
        "generate_worktree",
        lambda repo, manifest, assume_dir=False, seed="0": api_regen.Generated(
            None, ["otto.x: has no __all__"]
        ),
    )
    golden = tmp_path / "golden.txt"
    argv = ["--manifest", str(_tls_manifest(tmp_path)), "--golden", str(golden), "--update"]
    assert mod.main(argv) == 1
    assert not golden.exists()
    assert "FAIL otto.x: has no __all__" in capsys.readouterr().out


def test_dump_report_always_exits_zero_and_counts_refusals(tmp_path, capsys, monkeypatch):
    from scripts import api_regen

    monkeypatch.setattr(
        api_regen,
        "generate_worktree",
        lambda repo, manifest, assume_dir=False, seed="0": api_regen.Generated(None, ["a", "b"]),
    )
    assert mod.main(["--manifest", str(_tls_manifest(tmp_path)), "--report", "--assume-dir"]) == 0
    assert "api-dump-report: 2 producer refusal(s)" in capsys.readouterr().out


def test_v2_check_says_ok_when_the_golden_matches(tmp_path, capsys):
    manifest, golden = _tls_manifest(tmp_path), tmp_path / "golden.txt"
    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--update"]) == 0
    capsys.readouterr()

    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--check"]) == 0
    assert capsys.readouterr().out == "api snapshot: OK\n"


def test_v2_check_calls_a_crlf_copy_of_a_fresh_golden_stale(tmp_path, capsys):
    """``--check`` compares bytes, as check-breaking's freshness check does (dump spec §5.1)."""
    manifest, golden = _tls_manifest(tmp_path), tmp_path / "golden.txt"
    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--update"]) == 0
    golden.write_bytes(golden.read_bytes().replace(b"\n", chr(13).encode() + b"\n"))
    capsys.readouterr()

    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--check"]) == 1
    out = capsys.readouterr().out
    assert "api snapshot: OK" not in out
    assert "bytes differ" in out


def test_v2_check_of_a_missing_golden_is_a_fail_line_not_a_traceback(tmp_path, capsys):
    manifest, golden = _tls_manifest(tmp_path), tmp_path / "absent.txt"

    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--check"]) == 1
    (line,) = capsys.readouterr().out.splitlines()
    assert line.startswith(f"FAIL cannot read the golden {golden}: ")


def test_v2_a_failed_namespace_report_is_a_fail_line_not_a_traceback(tmp_path, capsys, monkeypatch):
    from scripts import api_agreement

    def broken(namespaces, repo):
        raise api_agreement.AgreementError("the reporting child died")

    monkeypatch.setattr(api_agreement, "namespace_reports", broken)
    manifest = _tls_manifest(tmp_path)
    for mode in ([], ["--check"], ["--update"]):
        argv = ["--manifest", str(manifest), "--golden", str(tmp_path / "g.txt"), *mode]
        assert mod.main(argv) == 1
        assert capsys.readouterr().out == (
            "FAIL cannot report the declared namespaces: the reporting child died\n"
        )


def test_v2_check_refuses_a_golden_without_the_v2_header(tmp_path, capsys):
    """The checker reads a header-less golden as v1, so ``--check`` must not call it v2."""
    manifest = tmp_path / "public.toml"
    manifest.write_text(
        '[namespaces."otto.tls"]\ntier = 1\nstability = "provisional"\n', encoding="utf-8"
    )
    golden = tmp_path / "golden.txt"
    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--update"]) == 0
    golden.write_text(
        golden.read_text(encoding="utf-8").replace("# api-snapshot v2\n", ""), encoding="utf-8"
    )
    capsys.readouterr()

    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--check"]) == 1
    assert "not an api-snapshot v2 golden" in capsys.readouterr().out


def test_dump_report_prints_producer_refusals_not_agreement_failures(tmp_path, capsys, monkeypatch):
    from scripts import api_agreement, api_regen

    monkeypatch.setattr(
        api_regen,
        "generate_worktree",
        lambda repo, manifest, assume_dir=False, seed="0": api_regen.Generated(
            None, ["producer broke"]
        ),
    )
    monkeypatch.setattr(api_agreement, "agreement_failures", lambda names, reports: ["not agreed"])
    assert mod.main(["--manifest", str(_tls_manifest(tmp_path)), "--report"]) == 0
    out = capsys.readouterr().out
    assert "refusal: producer broke" in out
    assert "not agreed" not in out
    assert "api-dump-report: 1 producer refusal(s)" in out


def _without(text: str, binding: str) -> str:
    """Return dump *text* with every record of *binding* dropped."""
    from scripts import api_records

    dump = api_records.parse_dump(text)
    kept = [r for r in dump.records.values() if api_records.binding_of(r) != binding]
    return api_records.render_dump(kept)


def test_dump_update_refuses_a_dump_that_misses_a_declared_name(tmp_path, capsys, monkeypatch):
    """The dump's size check: a declared ``__all__`` name with no record is a FAIL line."""
    from scripts import api_regen

    manifest, golden = _tls_manifest(tmp_path), tmp_path / "golden.txt"
    real = api_regen.generate_worktree(PROJECT_ROOT, manifest)
    short = _without(real.text, "otto.tls:os_trust_session")
    monkeypatch.setattr(
        api_regen,
        "generate_worktree",
        lambda repo, manifest, assume_dir=False, seed="0": api_regen.Generated(short),
    )
    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--update"]) == 1
    assert not golden.exists()
    assert (
        "FAIL the dump has no name record for the declared otto.tls:os_trust_session"
        in capsys.readouterr().out
    )


def test_a_complete_dump_misses_no_declared_name(tmp_path):
    from scripts import api_regen
    from scripts.api_agreement import namespace_reports

    real = api_regen.generate_worktree(PROJECT_ROOT, _tls_manifest(tmp_path))
    assert mod.undumped_names(real.text, namespace_reports(["otto.tls"], PROJECT_ROOT)) == []


def test_the_hash_seed_reaches_the_dump_child(tmp_path, monkeypatch):
    from scripts import api_regen

    seen = []

    def spy(repo, manifest, assume_dir=False, seed=api_regen.GATE_HASH_SEED):
        seen.append(seed)
        return api_regen.Generated(None, ["stop here"])

    monkeypatch.setattr(api_regen, "generate_worktree", spy)
    manifest = str(_tls_manifest(tmp_path))
    assert mod.main(["--manifest", manifest]) == 1
    assert mod.main(["--manifest", manifest, "--hash-seed", "312"]) == 1
    assert seen == [api_regen.GATE_HASH_SEED, "312"]


@pytest.mark.parametrize(
    "seed",
    ["-1", "4294967296", " 1", "1.0", "x", chr(0xFF11)],
    ids=["negative", "too-big", "space", "float", "word", "fullwidth-digit"],
)
def test_a_hash_seed_python_would_refuse_is_a_usage_error(tmp_path, capsys, seed):
    with pytest.raises(SystemExit) as exc:
        mod.main(["--manifest", str(_tls_manifest(tmp_path)), "--hash-seed", seed])
    assert exc.value.code == 2
    assert "is not a PYTHONHASHSEED" in capsys.readouterr().err


def test_the_dump_is_the_same_under_another_hash_seed(tmp_path, capsys):
    manifest, golden = _tls_manifest(tmp_path), tmp_path / "golden.txt"
    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--update"]) == 0
    capsys.readouterr()
    check = ["--manifest", str(manifest), "--golden", str(golden), "--check"]
    assert mod.main([*check, "--hash-seed", "4294967295"]) == 0
    assert capsys.readouterr().out == "api snapshot: OK\n"
