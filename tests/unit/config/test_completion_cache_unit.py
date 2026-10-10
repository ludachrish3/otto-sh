"""Pure-unit tests for :mod:`otto.config.completion_cache`.

Focus on the small guards and the option-serialization code path — the
subprocess coverage in ``test_completion_cache.py`` exercises the full stack
but is heavy; these tests run in milliseconds and pinpoint regressions.

Note: this module intentionally does NOT use ``from __future__ import
annotations`` — ``_serialize_options`` introspects ``Annotated[...]`` forms
at runtime, and PEP 563 would stringify them, making the serializer skip the
option entirely.
"""

import inspect
import json
import time
from pathlib import Path
from typing import Annotated
from unittest import mock
from unittest.mock import MagicMock

import pytest
import typer

from otto.config import completion_cache as cc
from otto.host.os_profile import ProfileContext
from otto.labs.json_repository import LAB_FILENAME
from otto.labs.sources import PendingLabSource
from otto.registry import FrozenMap
from tests._fixtures.labdata import json_lab_sources, write_lab_json
from tests._fixtures.sutrepo import touch_settings
from tests.unit.config.test_completion_cache_inventory import (
    _registered,
    _Uncacheable,
)
from tests.unit.config.test_completion_cache_inventory import (
    _repo as _inventory_repo,
)


def _sections_file(
    repos: list,
    *,
    generated_at: int | None = None,
    names_payload: dict | None = None,
) -> dict:
    """An on-disk sections file with REAL digests for *repos*.

    The hand-built counterpart of ``write_cache``, for tests that need to
    plant an entry with a doctored timestamp/schema/payload and then watch
    the reader's verdict. Each entry stores what the writer stores: the TTL
    class and, for a lab-keyed section, the lab key paths.
    """
    from otto.config.cache_sections import SECTIONS, lab_key_paths, section_digest

    lab = [str(path) for path in lab_key_paths(repos)]

    at = int(time.time()) if generated_at is None else generated_at
    payloads = {
        "names": ({"instructions": [], "hosts": []} if names_payload is None else names_payload),
    }
    return {
        "schema": cc.SCHEMA_VERSION,
        "sections": {
            s.name: {
                "fingerprint": section_digest(s, repos),
                "generated_at": at,
                "tainted": False,
                "ttl_seconds": cc._cache_ttl_seconds(repos),
                **({"lab_key_paths": lab} if s.lab_keyed else {}),
                "payload": payloads.get(s.name, {}),
            }
            for s in SECTIONS
        },
    }


def _names_digest(repos: list) -> str:
    """The ``names`` section's digest: what a lab-file or init edit must move."""
    from otto.config.cache_sections import section_by_name, section_digest

    return section_digest(section_by_name("names"), repos)


def test_read_cache_returns_none_for_empty_repos(tmp_path: Path, monkeypatch) -> None:
    """Empty-repo digests poison the cache if allowed; read must skip them."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    # Plant a fully valid-shaped sections file computed FOR empty repos.
    cache_file = cc._cache_path()
    assert cache_file is not None
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    poisoned = _sections_file(
        [],
        names_payload={
            "instructions": [{"name": "poisoned", "options": []}],
            "hosts": [],
        },
    )
    cache_file.write_text(json.dumps(poisoned))

    assert cc.read_cache([]) is None


# ── cache_rebuild_is_worthwhile: skip a collect that write_cache would drop ──


def test_cache_rebuild_is_worthwhile_is_false_for_empty_repos(tmp_path: Path, monkeypatch) -> None:
    """Empty repos must never start the collect-and-write dance at all.

    write_cache refuses an empty-repos fingerprint (it would poison the cache
    with the shared empty-sha256 key), so a caller that only checked
    read_cache would collect on EVERY invocation with no repos configured
    and throw the result away every time.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    assert cc.cache_rebuild_is_worthwhile([]) is False


def test_cache_rebuild_is_worthwhile_is_false_for_an_ephemeral_fingerprint(
    tmp_path: Path, monkeypatch
) -> None:
    """An uncacheable inventory must refuse the collect even with a fresh-looking entry on disk.

    ``write_cache`` never persists an entry keyed on an ephemeral fingerprint
    (see ``test_completion_cache_inventory.test_an_uncacheable_inventory_writes_nothing_at_all``),
    and the clock text baked into that fingerprint means an entry keyed on
    "the fingerprint a moment ago" can never be read back as a hit either —
    the very next digest computation produces a different key.
    ``cache_rebuild_is_worthwhile`` must refuse the O(corpus) collect for this
    class BEFORE it ever asks ``read_cache``, because whatever it computed
    would be thrown away by ``write_cache`` regardless.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    with _registered("unit-ephemeral-worthwhile", _Uncacheable) as table:
        repo = _inventory_repo(tmp_path, table)
        cache_file = cc._cache_path()
        assert cache_file is not None
        cache_file.parent.mkdir(parents=True, exist_ok=True)

        # As close to "a matching entry on disk" as an ephemeral digest can
        # ever get: sections stamped with THIS moment's digests, which the
        # clock text guarantees the next computation will not reproduce.
        cache_file.write_text(json.dumps(_sections_file([repo])))

        assert cc.cache_rebuild_is_worthwhile([repo]) is False, (
            "an ephemeral fingerprint must never be judged worth an O(corpus) collect"
        )


# ── Effective TTL: a non-file host source has no invalidation signal ─────────


def _pending(label: str, backend: str, sut_dir: Path, **raw: object) -> PendingLabSource:
    """One declared ``[[lab.sources]]`` entry, as settings parsing leaves it."""
    return PendingLabSource(
        backend=backend,
        label=label,
        raw=FrozenMap.freeze_json(raw),
        origin=str(sut_dir / ".otto" / "settings.toml"),
        repo_dir=sut_dir,
    )


def _ttl_repo(tmp_path: Path, backend: str | None = None) -> MagicMock:
    """A repo declaring one [[lab.sources]] entry on *backend* (None = none)."""
    repo = MagicMock()
    repo.sut_dir = tmp_path / "sut"
    repo.init = []
    repo.libs = []
    repo.tests = []
    options = {"paths": ["lab"]} if backend == "json" else {}
    repo.lab_sources = (
        [] if backend is None else [_pending(f"ttl/{backend}#1", backend, repo.sut_dir, **options)]
    )
    repo.reservation_settings = {}
    return repo


def test_json_backends_keep_the_long_ttl(tmp_path: Path) -> None:
    """No source at all, or an explicit json one, means a lab.json fingerprint."""
    assert cc._cache_ttl_seconds([]) == cc.CACHE_TTL_SECONDS
    assert cc._cache_ttl_seconds([_ttl_repo(tmp_path)]) == cc.CACHE_TTL_SECONDS
    assert cc._cache_ttl_seconds([_ttl_repo(tmp_path, "json")]) == cc.CACHE_TTL_SECONDS


def test_a_reservation_backend_also_shortens_the_ttl(tmp_path: Path) -> None:
    """The `usernames` field has the identical constant-digest problem.

    The built-in json reservation backend implements no username completion,
    so that field is populated exclusively by custom — typically networked —
    backends, and the fingerprint tracks only settings.toml for them.
    """
    repo = _ttl_repo(tmp_path)
    repo.reservation_settings = {"backend": "acme"}
    assert cc._cache_ttl_seconds([repo]) == cc.UNFINGERPRINTED_CACHE_TTL_SECONDS


def test_a_custom_backend_shortens_the_ttl(tmp_path: Path) -> None:
    """A non-file host source's digest never moves, so the TTL is the only bound."""
    repos = [_ttl_repo(tmp_path, "cmdb")]
    assert cc._cache_ttl_seconds(repos) == cc.UNFINGERPRINTED_CACHE_TTL_SECONDS
    assert cc.UNFINGERPRINTED_CACHE_TTL_SECONDS < cc.CACHE_TTL_SECONDS

    # One custom repo among json ones is enough — the cache entry is shared.
    mixed = [_ttl_repo(tmp_path), _ttl_repo(tmp_path, "cmdb")]
    assert cc._cache_ttl_seconds(mixed) == cc.UNFINGERPRINTED_CACHE_TTL_SECONDS


def test_a_repo_double_without_lab_sources_reads_as_json(tmp_path: Path) -> None:
    """A double that declares no source must not be mistaken for a custom backend.

    Guards the getattr(...) default and the reservations isinstance(...) check in
    _has_unfingerprinted_source: without them, every mock-based repo double in
    the suite would silently take the short TTL.
    """
    # Each double pins `os_profiles`, which every repo has: the profile
    # context reads it unguarded.
    bare = MagicMock(spec=["sut_dir", "os_profiles"])
    bare.os_profiles = {}
    assert cc._cache_ttl_seconds([bare]) == cc.CACHE_TTL_SECONDS

    automocked = MagicMock()  # .lab_sources iterates empty; .reservation_settings is a Mock
    automocked.os_profiles = {}
    assert cc._cache_ttl_seconds([automocked]) == cc.CACHE_TTL_SECONDS


def test_read_cache_applies_the_short_ttl_to_a_custom_backend(tmp_path: Path, monkeypatch) -> None:
    """An entry inside the long TTL but past the short one is served or not, by its stored class.

    The writer decides the TTL class from the prepared sources and stores it
    with the entry; the reader enforces the stored class and never asks again.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    json_repo = _ttl_repo(tmp_path)
    cache_file = cc._cache_path()
    assert cache_file is not None
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    six_hours_ago = int(time.time()) - 6 * 60 * 60
    cache_file.write_text(json.dumps(_sections_file([json_repo], generated_at=six_hours_ago)))
    assert cc.read_cache([json_repo]) is not None, "6h is well inside the 24h TTL"

    custom = _ttl_repo(tmp_path, "cmdb")
    from otto.config.cache_sections import SECTIONS, section_digest

    assert [section_digest(s, [custom]) for s in SECTIONS] == [
        section_digest(s, [json_repo]) for s in SECTIONS
    ], "positive control: the digests are identical — only the TTL differs"
    cache_file.write_text(json.dumps(_sections_file([custom], generated_at=six_hours_ago)))
    assert cc.read_cache([custom]) is None, "6h is past the short TTL"

    # ...and a FRESH entry is still served to that same custom repo, so the
    # short TTL bounds staleness rather than disabling the cache (which would
    # put a full bootstrap behind every TAB).
    cache_file.write_text(json.dumps(_sections_file([custom], generated_at=int(time.time()) - 60)))
    assert cc.read_cache([custom]) is not None, "a 1-minute-old entry must still serve"


# ── Fingerprint coverage of the [[lab.sources]] host data ────────────────────


@pytest.mark.parametrize(
    ("spelling", "path_of"),
    [
        pytest.param("directory", lambda lab_dir: lab_dir, id="directory"),
        pytest.param("json file", lambda lab_dir: lab_dir / "lab.json", id="json-file"),
    ],
)
def test_fingerprint_moves_when_a_source_lab_file_is_edited(
    tmp_path: Path, spelling: str, path_of
) -> None:
    """Editing a json source's lab file must move the digest.

    The host ids behind ``otto host <TAB>`` come from these files, so a digest
    that ignores them serves a stale host list until the 24h TTL expires — the
    exact staleness the file-backed source is entitled to escape (a backend
    with no file signal falls back to the short TTL instead).

    Parametrized over BOTH ``paths`` spellings because the digest reads them
    through the prepared source's ``lab_files()``: a digest
    wired to the directory form alone would silently stop tracking a source
    that names its ``.json`` file directly.
    """
    sut_dir = tmp_path / "sut"
    sut_dir.mkdir(parents=True)
    touch_settings(sut_dir)
    lab_dir = tmp_path / "lab"
    lab_dir.mkdir(parents=True)
    lab_file = write_lab_json(lab_dir / "lab.json", [{"ip": "10.0.0.1", "element": "test1"}])

    repo = MagicMock()
    repo.sut_dir = sut_dir
    repo.init = []
    repo.libs = []
    repo.tests = []
    repo.lab_sources = [_pending(f"fp/{spelling}", "json", sut_dir, paths=[str(path_of(lab_dir))])]

    before = _names_digest([repo])
    write_lab_json(
        lab_file,
        [
            {"ip": "10.0.0.1", "element": "test1"},
            {"ip": "10.0.0.2", "element": "test2"},
        ],
    )

    assert _names_digest([repo]) != before, (
        f"editing the lab file of a {spelling} source left the digest unchanged — "
        "the cache cannot self-invalidate on host-data edits"
    )


def test_fingerprint_ignores_a_custom_source_with_no_files(tmp_path: Path) -> None:
    """A source that reads no file contributes nothing — the TTL fallback covers it.

    Pins the other half of the rule: an unregistered backend (unknown) and a
    backend that is not file-backed contribute no lab key path, so the digest
    is constant and ``_has_unfingerprinted_source`` is what shortens the TTL.
    Hashing the repo_dir here would give such a source a false invalidation
    signal.
    """
    sut_dir = tmp_path / "sut"
    sut_dir.mkdir(parents=True)
    touch_settings(sut_dir)

    repo = MagicMock()
    repo.sut_dir = sut_dir
    repo.init = []
    repo.libs = []
    repo.tests = []
    repo.lab_sources = [_pending("fp/cmdb#1", "cmdb", sut_dir)]

    before = _names_digest([repo])
    (sut_dir / "anything.json").write_text("{}")
    assert _names_digest([repo]) == before
    assert cc._has_unfingerprinted_source([repo]) is True


def test_write_cache_skips_empty_repos(tmp_path: Path, monkeypatch) -> None:
    """Writing for empty repos must be a no-op — no file, no poisoned entry."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    cc.write_cache([], instructions=[{"name": "x", "options": []}], hosts=[])
    assert not cc._cache_path().exists()  # type: ignore[union-attr]


def test_read_cache_rejects_schema_mismatch(tmp_path: Path, monkeypatch) -> None:
    """A cache whose top-level schema stamp is older is not consulted."""
    from unittest.mock import MagicMock

    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir()
    touch_settings(fake_repo.sut_dir)
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    # A MagicMock auto-attribute is TRUTHY and `dict()`s to `{}`, which reads
    # as a present-but-empty [inventory] — a shape no real Repo produces, and
    # one `build_inventory` rejects, taking the cache write down with it.
    fake_repo.inventory_settings = {}

    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    cache_file = cc._cache_path()
    cache_file.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    stale = _sections_file([fake_repo])
    stale["schema"] = cc.SCHEMA_VERSION - 1  # digests valid, stamp old
    cache_file.write_text(json.dumps(stale))  # type: ignore[union-attr]

    assert cc.read_cache([fake_repo]) is None


def test_serialize_options_handles_supported_kinds() -> None:
    """Every kind in the type-map should produce a non-None schema."""

    def source(
        s: Annotated[str, typer.Option("--s")] = "",
        i: Annotated[int, typer.Option("--i")] = 0,
        f: Annotated[float, typer.Option("--f")] = 0.0,
        b: Annotated[bool, typer.Option("--b/--no-b")] = False,
        p: Annotated[Path, typer.Option("--p")] = Path(),
        l: Annotated[list[str] | None, typer.Option("--l")] = None,  # noqa: E741 — deliberate single-char CLI option name in type-map test
    ) -> None: ...

    schema = cc._serialize_options(source, command_name="source")
    assert schema is not None
    kinds = [entry["kind"] for entry in schema]
    assert kinds == ["str", "int", "float", "bool", "path", "str_list"]


def test_serialize_options_returns_none_on_unsupported() -> None:
    """An unsupported annotation drops the entire command schema."""
    from decimal import Decimal

    def source(
        ok: Annotated[str, typer.Option("--ok")] = "",
        bad: Annotated[Decimal, typer.Option("--bad")] = Decimal(0),
    ) -> None: ...

    assert cc._serialize_options(source, command_name="source") is None


def test_clear_cache_returns_false_when_missing(tmp_path: Path, monkeypatch) -> None:
    """clear_cache reports False when there's nothing to remove."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    assert cc.clear_cache() is False


# ---------------------------------------------------------------------------
# collect_current_commands — reads otto.instructions.INSTRUCTIONS
# ---------------------------------------------------------------------------


class TestCollectCurrentCommands:
    """collect_current_commands() reads the live INSTRUCTIONS registry."""

    def test_nothing_registered_yields_empty_instructions(self, monkeypatch) -> None:
        """A run where no init module registered anything reports [], never an error.

        THE EMPTINESS IS INJECTED, not inherited: otto's six project
        instructions derive from a constant, so the live table is never empty.

        Replacing the registry with an empty one states the condition the test
        is actually about and holds under every seed.
        """
        import sys

        from otto.registry import ClassEntry, Registry

        # A module first imported under this patch would keep the empty table
        # for the rest of the process if it binds INSTRUCTIONS by name
        # (`otto.cli.run` does), and every later test that builds an
        # instruction app would find no instruction at all. So the call runs
        # once unpatched first, importing everything it needs, and the patched
        # call must import nothing new.
        cc.collect_current_commands()
        before = set(sys.modules)
        monkeypatch.setattr(
            "otto.instructions.INSTRUCTIONS",
            Registry(
                "instruction", entry=ClassEntry, register_hint="@otto.instructions.instruction()"
            ),
        )
        assert cc.collect_current_commands() == []
        first_imported = sorted(set(sys.modules) - before)
        assert first_imported == [], (
            f"{first_imported} first imported under the patched INSTRUCTIONS; a module "
            "that binds the table by name would keep the empty one for the whole process"
        )

    def test_collects_registered_instruction_with_options(self) -> None:
        from otto.instructions import STANDALONE_INSTRUCTIONS, InstructionEntry

        async def _probe_instr(name: Annotated[str, typer.Option("--name")] = "x") -> None: ...

        STANDALONE_INSTRUCTIONS.register(
            "_cc_probe_instr",
            InstructionEntry(name="_cc_probe_instr", module=__name__, handler=_probe_instr),
        )
        instructions = cc.collect_current_commands()

        entry = next(e for e in instructions if e["name"] == "_cc_probe_instr")
        assert entry["options"]
        assert entry["options"][0]["kind"] == "str"

    def test_unserializable_options_cache_with_empty_options_list(self) -> None:
        """A command whose options can't be serialized still completes by name."""
        from decimal import Decimal

        from otto.instructions import STANDALONE_INSTRUCTIONS, InstructionEntry

        async def _probe_bad(
            bad: Annotated[Decimal, typer.Option("--bad")] = Decimal(0),
        ) -> None: ...

        STANDALONE_INSTRUCTIONS.register(
            "_cc_probe_bad",
            InstructionEntry(name="_cc_probe_bad", module=__name__, handler=_probe_bad),
        )
        instructions = cc.collect_current_commands()

        entry = next(e for e in instructions if e["name"] == "_cc_probe_bad")
        assert entry["options"] == []


def test_clear_cache_removes_existing(tmp_path: Path, monkeypatch) -> None:
    """clear_cache unlinks a present cache file and reports True."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    path = cc._cache_path()
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    assert cc.clear_cache() is True
    assert not path.exists()


def test_collect_backend_names_includes_builtins():
    from otto.config import completion_cache as cc

    snap = cc.collect_backend_names()
    assert "ssh" in snap["term_backends"]
    assert "telnet" in snap["term_backends"]
    by_name = {e["name"]: e["host_families"] for e in snap["transfer_backends"]}
    assert by_name["scp"] == ["unix"]
    assert by_name["console"] == ["embedded"]


def test_write_read_cache_round_trips_backend_names(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import MagicMock

    from otto.config import completion_cache as cc

    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir()
    touch_settings(fake_repo.sut_dir)
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    # A MagicMock auto-attribute is TRUTHY and `dict()`s to `{}`, which reads
    # as a present-but-empty [inventory] — a shape no real Repo produces, and
    # one `build_inventory` rejects, taking the cache write down with it.
    fake_repo.inventory_settings = {}

    cc.write_cache(
        [fake_repo],
        instructions=[],
        hosts=[],
        term_backends=["ssh", "telnet"],
        transfer_backends=[{"name": "scp", "host_families": ["unix"]}],
    )
    out = cc.read_cache([fake_repo])
    assert out is not None
    assert out["term_backends"] == ["ssh", "telnet"]
    assert out["transfer_backends"] == [{"name": "scp", "host_families": ["unix"]}]


# ---------------------------------------------------------------------------
# _json_safe_default — pure function table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (inspect.Parameter.empty, None),
        ([1, 2, "x"], [1, 2, "x"]),
        (object(), None),
        ([{1, 2}], None),  # non-serializable list → json.dumps TypeError → None
    ],
)
def test_json_safe_default(value: object, expected: object) -> None:
    """_json_safe_default coerces each supported form correctly."""
    assert cc._json_safe_default(value) == expected


# ---------------------------------------------------------------------------
# _serialize_options — skip-gate tests
# ---------------------------------------------------------------------------


def test_serialize_options_non_annotated_returns_none() -> None:
    """A plain (non-Annotated) param annotation causes the whole callback to be skipped."""

    def cb(x: int) -> None: ...

    assert cc._serialize_options(cb, command_name="cb") is None


def test_serialize_options_annotated_without_option_returns_none() -> None:
    """Annotated param without a typer.Option in metadata causes the callback to be skipped."""

    def cb(x: Annotated[int, "meta-but-not-typer-Option"]) -> None: ...

    assert cc._serialize_options(cb, command_name="cb") is None


# ---------------------------------------------------------------------------
# collect_cli_commands — CLI_COMMANDS registry snapshot (third-party only)
# ---------------------------------------------------------------------------


def test_cache_round_trips_third_party_commands(tmp_path: Path, monkeypatch) -> None:
    """collect_cli_commands surfaces third-party specs in the cache shape."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    from otto.cli.registry import CLI_COMMANDS, register_cli_command

    register_cli_command("e2etool", typer.Typer(name="e2etool"), help="Tool.")
    try:
        from otto.config.completion_cache import collect_cli_commands

        commands = collect_cli_commands()
        assert {"name": "e2etool", "help": "Tool.", "lab_free": False} in commands
    finally:
        CLI_COMMANDS.unregister("e2etool")


def test_collect_cli_commands_includes_decorator_registered_leaves() -> None:
    """A third-party ``@cli_command`` leaf is cached like a direct registration.

    The decorator registers by CALLING ``register_cli_command`` from inside
    ``otto.cli.registry``, so a caller-frame origin capture attributes the
    leaf to otto itself — and the built-in filter then drops it from the
    ``commands`` payload. That is how warm root help silently lost every
    decorated plugin leaf while groups registered by direct call survived.
    The registered origin must therefore be the module that APPLIED the
    decorator, and the leaf must survive collection.
    """
    from otto.cli.registry import CLI_COMMANDS, cli_command
    from otto.config.completion_cache import collect_cli_commands

    @cli_command(name="_cc_probe_leaf", help="Probe leaf.", lab_free=True)
    async def _probe() -> None: ...

    try:
        assert CLI_COMMANDS.origin("_cc_probe_leaf") == __name__
        commands = {c["name"]: c for c in collect_cli_commands()}
        assert "_cc_probe_leaf" in commands
        assert commands["_cc_probe_leaf"]["help"] == "Probe leaf."
        assert commands["_cc_probe_leaf"]["lab_free"] is True
    finally:
        CLI_COMMANDS.unregister("_cc_probe_leaf")


def test_a_decorated_third_party_command_is_not_classified_built_in() -> None:
    """Built-in means otto REGISTERED it, whatever the command's callable is.

    ``acme_cli`` re-exposes one of otto's own functions as its own command
    through ``@cli_command``. The engine credits the module that applied the
    decorator, so the command is acme's and is cached for warm root help; a
    classifier that read the module of ``spec.loader`` would call it otto's
    and drop it.
    """
    import types

    from otto.cli.registry import CLI_COMMANDS
    from otto.config.completion_cache import collect_cli_commands

    acme = types.ModuleType("acme_cli")
    source = (
        "from otto.cli.registry import cli_command\n"
        "from otto.version import get_version\n"
        "cli_command(name='acme-version', help='Acme version.', lab_free=True)(get_version)\n"
    )
    exec(compile(source, "<acme_cli>", "exec"), acme.__dict__)  # noqa: S102 — a synthetic plugin

    assert CLI_COMMANDS.origin("acme-version") == "acme_cli"
    commands = {c["name"]: c for c in collect_cli_commands()}
    assert commands["acme-version"] == {
        "name": "acme-version",
        "help": "Acme version.",
        "lab_free": True,
    }


def test_collect_cli_commands_skips_otto_builtins() -> None:
    """Builtin commands (origin starting with 'otto.') are never cached."""
    from otto.config.completion_cache import collect_cli_commands

    names = {c["name"] for c in collect_cli_commands()}
    # 'run' is a builtin top-level command registered from otto.* — must be
    # excluded since builtins re-register on every real invocation anyway.
    assert "run" not in names


class TestCollectCliCommandChildren:
    """Third-party GROUP children serialize into the cache (fast-path tab
    completion of `otto <plugin-group> <TAB>` rebuilds stubs from them)."""

    def _collect_entry(self, name: str) -> dict:
        from otto.config.completion_cache import collect_cli_commands

        return next(c for c in collect_cli_commands() if c["name"] == name)

    def test_group_children_serialize_names_help_options(self) -> None:
        from otto.cli.registry import CLI_COMMANDS, register_cli_command

        grp = typer.Typer(name="grptool")

        @grp.command()
        def ping() -> None:
            """Pong."""

        @grp.command(name="re-set")
        def reset_cmd(
            force: Annotated[bool, typer.Option("--force", help="Force it.")] = False,
        ) -> None:
            """Reset."""

        register_cli_command("grptool", grp, help="Group tool.")
        try:
            children = {c["name"]: c for c in self._collect_entry("grptool")["commands"]}
            assert set(children) == {"ping", "re-set"}
            assert children["ping"]["help"] == "Pong."
            assert ["--force"] in [o["flags"] for o in children["re-set"]["options"]]
        finally:
            CLI_COMMANDS.unregister("grptool")

    def test_nested_group_recurses(self) -> None:
        from otto.cli.registry import CLI_COMMANDS, register_cli_command

        inner = typer.Typer(name="inner", help="Inner group.")

        @inner.command()
        def alpha() -> None: ...

        @inner.command()
        def beta() -> None: ...

        outer = typer.Typer(name="outer")
        outer.add_typer(inner)

        @outer.command()
        def top() -> None: ...

        register_cli_command("outer", outer, help="Outer.")
        try:
            children = {c["name"]: c for c in self._collect_entry("outer")["commands"]}
            assert set(children) == {"top", "inner"}
            inner_children = {c["name"] for c in children["inner"]["commands"]}
            assert inner_children == {"alpha", "beta"}
        finally:
            CLI_COMMANDS.unregister("outer")

    def test_string_loader_group_imports_at_cache_write(self, monkeypatch) -> None:
        import sys
        import types

        from otto.cli.registry import CLI_COMMANDS, register_cli_command

        app = typer.Typer(name="fptool")

        @app.command()
        def x() -> None: ...

        @app.command()
        def y() -> None: ...

        mod = types.ModuleType("fake_plugin_mod")
        mod.app = app  # ty: ignore[unresolved-attribute]
        monkeypatch.setitem(sys.modules, "fake_plugin_mod", mod)
        register_cli_command("fptool", "fake_plugin_mod:app", help="FP.")
        try:
            names = {c["name"] for c in self._collect_entry("fptool")["commands"]}
            assert names == {"x", "y"}
        finally:
            CLI_COMMANDS.unregister("fptool")

    def test_broken_loader_degrades_to_name_only(self) -> None:
        from otto.cli.registry import CLI_COMMANDS, register_cli_command

        register_cli_command("brokentool", "nonexistent_module_xyz:app", help="Broken.")
        try:
            entry = self._collect_entry("brokentool")
            assert entry["help"] == "Broken."
            assert "commands" not in entry
            assert "options" not in entry
        finally:
            CLI_COMMANDS.unregister("brokentool")

    def test_flattened_leaf_app_serializes_options(self) -> None:
        from otto.cli.registry import CLI_COMMANDS, register_cli_command

        solo = typer.Typer(name="solo")

        @solo.command()
        def solo_cmd(
            count: Annotated[int, typer.Option("--count", help="How many.")] = 1,
        ) -> None:
            """Solo."""

        register_cli_command("solotool", solo)
        try:
            entry = self._collect_entry("solotool")
            assert "commands" not in entry  # flattens to a leaf, not a group
            assert ["--count"] in [o["flags"] for o in entry["options"]]
        finally:
            CLI_COMMANDS.unregister("solotool")


def test_write_read_cache_round_trips_commands(tmp_path: Path, monkeypatch) -> None:
    """write_cache/read_cache carry the 'commands' key through a round trip."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir()
    touch_settings(fake_repo.sut_dir)
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    # A MagicMock auto-attribute is TRUTHY and `dict()`s to `{}`, which reads
    # as a present-but-empty [inventory] — a shape no real Repo produces, and
    # one `build_inventory` rejects, taking the cache write down with it.
    fake_repo.inventory_settings = {}

    cc.write_cache(
        [fake_repo],
        instructions=[],
        hosts=[],
        commands=[{"name": "e2etool", "help": "Tool.", "lab_free": False}],
    )
    out = cc.read_cache([fake_repo])
    assert out is not None
    assert out["commands"] == [{"name": "e2etool", "help": "Tool.", "lab_free": False}]


def test_read_cache_defaults_commands_to_empty_list(tmp_path: Path, monkeypatch) -> None:
    """A cache entry written without 'commands' reads back as an empty list."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir()
    touch_settings(fake_repo.sut_dir)
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    # A MagicMock auto-attribute is TRUTHY and `dict()`s to `{}`, which reads
    # as a present-but-empty [inventory] — a shape no real Repo produces, and
    # one `build_inventory` rejects, taking the cache write down with it.
    fake_repo.inventory_settings = {}

    cc.write_cache([fake_repo], instructions=[], hosts=[])
    out = cc.read_cache([fake_repo])
    assert out is not None
    assert out["commands"] == []


def test_write_read_cache_round_trips_hosts_by_lab(tmp_path: Path, monkeypatch) -> None:
    """write_cache/read_cache carry the 'hosts_by_lab' map through a round trip."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir()
    touch_settings(fake_repo.sut_dir)
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    # A MagicMock auto-attribute is TRUTHY and `dict()`s to `{}`, which reads
    # as a present-but-empty [inventory] — a shape no real Repo produces, and
    # one `build_inventory` rejects, taking the cache write down with it.
    fake_repo.inventory_settings = {}

    cc.write_cache(
        [fake_repo],
        instructions=[],
        hosts=["test1", "alt2"],
        hosts_by_lab={"unix": ["test1"], "unix_alt": ["alt2"]},
    )
    out = cc.read_cache([fake_repo])
    assert out is not None
    assert out["hosts_by_lab"] == {"unix": ["test1"], "unix_alt": ["alt2"]}


def test_read_cache_defaults_hosts_by_lab_to_empty_dict(tmp_path: Path, monkeypatch) -> None:
    """A cache entry written without 'hosts_by_lab' reads back as an empty dict."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir()
    touch_settings(fake_repo.sut_dir)
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    # A MagicMock auto-attribute is TRUTHY and `dict()`s to `{}`, which reads
    # as a present-but-empty [inventory] — a shape no real Repo produces, and
    # one `build_inventory` rejects, taking the cache write down with it.
    fake_repo.inventory_settings = {}

    cc.write_cache([fake_repo], instructions=[], hosts=[])
    out = cc.read_cache([fake_repo])
    assert out is not None
    assert out["hosts_by_lab"] == {}


def test_write_read_cache_round_trips_the_class_map_projects_and_links(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir()
    touch_settings(fake_repo.sut_dir)
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    fake_repo.inventory_settings = {}
    cc.write_cache(
        [fake_repo],
        instructions=[],
        hosts=["z1"],
        host_classes_by_id={"z1": "zephyr"},
        projects=["b", "a"],
        links=[{"id": "l", "hosts": ["z1", "u1"]}],
    )
    out = cc.read_cache([fake_repo])
    assert out is not None
    assert out["host_classes_by_id"] == {"z1": "zephyr"}
    assert out["projects"] == ["b", "a"]
    assert out["links"] == [{"id": "l", "hosts": ["z1", "u1"]}]


def test_read_cache_rejects_a_malformed_class_map(tmp_path, monkeypatch):
    """A dict is what every consumer indexes; anything else is a miss, not a crash."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir()
    touch_settings(fake_repo.sut_dir)
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    fake_repo.inventory_settings = {}
    cc.write_cache([fake_repo], instructions=[], hosts=[])
    data = json.loads(cc._cache_path().read_text())
    data["sections"]["names"]["payload"]["host_classes_by_id"] = ["not", "a", "dict"]
    cc._cache_path().write_text(json.dumps(data))
    assert cc.read_cache([fake_repo]) is None


# ---------------------------------------------------------------------------
# collect_docker_capable_host_ids — lab.json reading + docker_capable filter
# ---------------------------------------------------------------------------

_DOCKER_HOST = {
    "ip": "10.0.0.1",
    "element": "b",
    "board": "seed",
    "os_type": "unix",
    "docker_capable": True,
    "creds": [{"login": "user", "password": "pass"}],
    "resources": ["b"],
    "labs": ["lab"],
}
_NON_DOCKER_HOST = {
    "ip": "10.0.0.2",
    "element": "a",
    "board": "seed",
    "os_type": "unix",
    "docker_capable": False,
    "creds": [{"login": "user", "password": "pass"}],
    "resources": ["a"],
    "labs": ["lab"],
}


def _make_fake_repo(tmp_path: Path) -> MagicMock:
    """Build a minimal fake Repo whose lab path is tmp_path."""
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir(parents=True, exist_ok=True)
    touch_settings(fake_repo.sut_dir)
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    # Pinned rather than left to the auto-attribute: `build_inventory` reads
    # it, and a MagicMock's is TRUTHY and converts to `{}`, which validates as
    # a broken [inventory] table — that takes out both the host enumeration
    # and (since a broken declaration is ephemeral) every cache WRITE.
    fake_repo.inventory_settings = {}
    # One json source over tmp_path/lab — the built-in backend, which is what
    # these tests exercise.
    fake_repo.lab_sources = json_lab_sources(fake_repo.sut_dir, [tmp_path / "lab"])
    return fake_repo


def test_collect_returns_only_capable_sorted(tmp_path: Path) -> None:
    """Only docker_capable hosts are returned, sorted, and bad entries are skipped."""
    lab_path = tmp_path / "lab"
    lab_path.mkdir(parents=True)
    # docker_capable host "b-seed", non-docker host "a-seed", and a docker_capable
    # entry whose identity cannot resolve. v2 keeps that skip per RECORD; a
    # malformed ELEMENT takes its whole file out of enumeration instead (see
    # tests/unit/labs/test_json_repository.py), which is why the junk entry
    # here is a bad host field rather than a non-dict.
    write_lab_json(
        lab_path / LAB_FILENAME,
        [
            _DOCKER_HOST,
            _NON_DOCKER_HOST,
            {**_DOCKER_HOST, "element": "junk", "slot": "not-an-int"},
        ],
    )
    repo = _make_fake_repo(tmp_path)

    result = cc.collect_docker_capable_host_ids([repo])

    assert result == ["b-seed"]


def test_collect_skips_missing_file(tmp_path: Path) -> None:
    """A repo whose lab path has no lab.json yields an empty list."""
    lab_path = tmp_path / "lab"
    lab_path.mkdir(parents=True)
    # Deliberately do NOT write lab.json
    repo = _make_fake_repo(tmp_path)

    assert cc.collect_docker_capable_host_ids([repo]) == []


def test_collect_skips_non_list_elements_section(tmp_path: Path) -> None:
    """A lab.json whose ``elements`` section is not a JSON array is skipped.

    ``elements``, not the v1 ``hosts``: a document carrying ``hosts`` at all
    is now the migration error, so this test would go on passing on the WRONG
    branch — never reaching the section-type check it exists to cover.
    """
    lab_path = tmp_path / "lab"
    lab_path.mkdir(parents=True)
    (lab_path / LAB_FILENAME).write_text(json.dumps({"elements": {"not": "a list"}}))
    repo = _make_fake_repo(tmp_path)

    assert cc.collect_docker_capable_host_ids([repo]) == []


# ---------------------------------------------------------------------------
# The names digest — init-module resolution branches + determinism
# ---------------------------------------------------------------------------


def _make_fingerprint_repo(
    tmp_path: Path,
    *,
    init: list[str],
    libs: list[Path],
    labs: list[Path] | None = None,
) -> MagicMock:
    """Build a fake Repo suitable for names-digest tests."""
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir(parents=True, exist_ok=True)
    touch_settings(fake_repo.sut_dir)
    fake_repo.init = init
    fake_repo.libs = libs
    fake_repo.tests = []
    # A MagicMock auto-attribute is TRUTHY and `dict()`s to `{}`, which reads
    # as a present-but-empty [inventory] — a shape no real Repo produces, and
    # one `build_inventory` rejects, taking the cache write down with it.
    fake_repo.inventory_settings = {}
    fake_repo.lab_sources = json_lab_sources(fake_repo.sut_dir, labs) if labs else []
    return fake_repo


def test_fingerprint_resolves_single_py_module(tmp_path: Path) -> None:
    """A single-file init module (lib/foo.py) is hashed via the resolved path."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "mymod.py").write_text("# init module")

    repo = _make_fingerprint_repo(
        tmp_path,
        init=["mymod"],
        libs=[lib_dir],
    )
    d1 = _names_digest([repo])
    assert isinstance(d1, str)
    assert len(d1) == 64  # sha256 hex


def test_fingerprint_unresolved_module_token(tmp_path: Path) -> None:
    """An unresolvable init token produces a DISTINCT fingerprint from the resolved case."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "mymod.py").write_text("# init module")

    repo_resolved = _make_fingerprint_repo(
        tmp_path / "resolved",
        init=["mymod"],
        libs=[lib_dir],
    )
    repo_unresolved = _make_fingerprint_repo(
        tmp_path / "unresolved",
        init=["no_such_module.sub.path"],
        libs=[lib_dir],
    )

    d_resolved = _names_digest([repo_resolved])
    d_unresolved = _names_digest([repo_unresolved])

    assert d_resolved != d_unresolved


def test_an_unresolved_init_module_shortens_the_ttl(tmp_path: Path) -> None:
    """An init name that never resolves under ``libs`` hashes as the literal
    string ``unresolved:<name>`` (see ``test_fingerprint_unresolved_module_token``)
    — a plugin upgrade neither moves that string nor invalidates the entry,
    so a repo declaring one needs the same short TTL the other
    unfingerprinted sources get.
    """
    repo = _make_fingerprint_repo(tmp_path, init=["no_such_plugin"], libs=[])
    assert cc._has_unfingerprinted_source([repo]) is True
    assert cc._cache_ttl_seconds([repo]) == cc.UNFINGERPRINTED_CACHE_TTL_SECONDS


def test_a_resolved_init_module_keeps_the_long_ttl(tmp_path: Path) -> None:
    """The positive control: a module that DOES resolve must not be flagged."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "myplugin.py").write_text("# init module")

    repo = _make_fingerprint_repo(tmp_path, init=["myplugin"], libs=[lib_dir])
    assert cc._has_unfingerprinted_source([repo]) is False
    assert cc._cache_ttl_seconds([repo]) == cc.CACHE_TTL_SECONDS


def test_fingerprint_resolves_package_dir_module(tmp_path: Path) -> None:
    """A package-directory init module (lib/mypkg/__init__.py) is hashed via rglob."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    pkg_dir = lib_dir / "mypkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("# package init")
    (pkg_dir / "helpers.py").write_text("# helper")

    repo = _make_fingerprint_repo(
        tmp_path,
        init=["mypkg"],
        libs=[lib_dir],
    )
    digest = _names_digest([repo])
    assert isinstance(digest, str)
    assert len(digest) == 64  # sha256 hex


def test_fingerprint_is_deterministic(tmp_path: Path) -> None:
    """Digesting the same repo set twice returns equal digests."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "mymod.py").write_text("# init module")

    repo = _make_fingerprint_repo(
        tmp_path,
        init=["mymod"],
        libs=[lib_dir],
    )

    d1 = _names_digest([repo])
    d2 = _names_digest([repo])

    assert d1 == d2


def test_collect_skips_corrupt_json(tmp_path: Path) -> None:
    """A lab.json with invalid JSON (JSONDecodeError branch) is silently skipped."""
    lab_path = tmp_path / "lab"
    lab_path.mkdir(parents=True)
    (lab_path / LAB_FILENAME).write_text("not valid json }{")
    repo = _make_fake_repo(tmp_path)

    assert cc.collect_docker_capable_host_ids([repo]) == []


def test_collect_skips_invalid_host_dict(tmp_path: Path) -> None:
    """A docker_capable host dict that fails validation is silently skipped.

    The bad entry sits INSIDE an otherwise valid element, so the document
    parses and the skip under test is the per-host one in
    ``list_host_summaries`` — not a whole-file rejection, which would make the
    empty result prove nothing about host-level resilience.
    """
    lab_path = tmp_path / "lab"
    lab_path.mkdir(parents=True)
    # docker_capable=True but missing required fields (no 'ip', invalid os_type, etc.)
    bad_host = {"docker_capable": True, "os_type": "nonexistent_profile"}
    (lab_path / LAB_FILENAME).write_text(
        json.dumps(
            {"labs": {"x": {}}, "elements": [{"name": "x", "labs": ["x"], "hosts": [bad_host]}]}
        )
    )
    repo = _make_fake_repo(tmp_path)

    assert cc.collect_docker_capable_host_ids([repo]) == []


# ── A host source that STALLS must not wedge the shell ───────────────────────


@pytest.mark.serial_timing
def test_a_hanging_host_source_is_bounded_not_waited_on(monkeypatch, caplog) -> None:
    """Failing was already contained; stalling was not.

    A custom `[lab]` backend is allowed to be a networked CMDB — that is the
    documented reason this cache exists — but on a cold cache the enumeration
    really runs, and an unreachable service would otherwise hang the user's
    TAB with no feedback until they interrupt it.
    """
    import logging
    import threading
    import time

    monkeypatch.setattr(cc, "HOST_SUMMARY_DEADLINE_SECONDS", 0.05)
    entered = threading.Event()

    def _never_returns(_repo, _inventory, _profiles, _abandoned=None):
        entered.set()
        time.sleep(30)  # pragma: no cover — the point is that we do not wait

    monkeypatch.setattr(cc, "_enumerate_host_summaries", _never_returns)
    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    repo = MagicMock()
    repo.sut_dir = Path("/nowhere-hang")

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="otto.config.completion_cache"):
        result = cc.repo_host_summaries(
            repo, cc.InventoryResolution(), profiles=ProfileContext.empty()
        )
    elapsed = time.monotonic() - started

    assert result == []
    assert entered.is_set(), "positive control: the enumeration must actually have started"
    # Tight against the 0.05s patched deadline: a 5s bound would pass even
    # if the deadline were 4.9s, leaving the caplog line to carry the test.
    assert elapsed < 1, f"waited {elapsed:.1f}s on a hanging backend"
    assert any("did not answer within" in r.message for r in caplog.records), caplog.text


def test_a_working_host_source_is_untouched_by_the_deadline(monkeypatch) -> None:
    """The bound must not cost the normal path its answer."""
    from otto.labs import HostSummary

    expected = cc.RepoEnumeration(summaries=[HostSummary(id="test1", labs=["unix"])])
    monkeypatch.setattr(
        cc,
        "_enumerate_host_summaries",
        lambda _repo, _inventory, _profiles, _abandoned=None: expected,
    )
    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    repo = MagicMock()
    repo.sut_dir = Path("/nowhere-ok")
    assert (
        cc.repo_host_summaries(repo, cc.InventoryResolution(), profiles=ProfileContext.empty())
        == expected.summaries
    )


def test_one_enumeration_per_repo_however_many_collectors_ask(monkeypatch) -> None:
    """Three collectors enumerate the same repo on one cache-write pass.

    Un-memoized, a stalled backend cost three deadlines — and worse, could
    time out for one collector and not another, writing a cache where
    `otto host <TAB>` is full and `otto docker --parent <TAB>` is empty, served
    for the whole TTL.
    """
    from otto.labs import HostSummary

    calls = 0

    def _count(_repo, _inventory, _profiles, _abandoned=None):
        nonlocal calls
        calls += 1
        return cc.RepoEnumeration(summaries=[HostSummary(id="test1", labs=["unix"])])

    monkeypatch.setattr(cc, "_enumerate_host_summaries", _count)
    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    repo = MagicMock()
    repo.sut_dir = Path("/memo")

    for _ in range(3):
        assert [
            s.id
            for s in cc.repo_host_summaries(
                repo, cc.InventoryResolution(), profiles=ProfileContext.empty()
            )
        ] == ["test1"]
    assert calls == 1, f"enumerated {calls} times for one repo"


def test_the_deadline_is_overridable_for_a_merely_slow_backend(monkeypatch) -> None:
    """Giving up costs the user ALL host completion until the backend speeds up.

    A module constant would leave an affected team no recourse, so the bound
    is an env var — otherwise the fix for "my CMDB takes 3s" is "patch otto".
    """
    monkeypatch.delenv(cc.HOST_SUMMARY_DEADLINE_ENV_VAR, raising=False)
    assert cc._host_summary_deadline() == cc.HOST_SUMMARY_DEADLINE_SECONDS

    monkeypatch.setenv(cc.HOST_SUMMARY_DEADLINE_ENV_VAR, "7.5")
    assert cc._host_summary_deadline() == 7.5

    # Garbage and non-positive values fall back rather than disabling the bound.
    for bad in ("", "soon", "0", "-1"):
        monkeypatch.setenv(cc.HOST_SUMMARY_DEADLINE_ENV_VAR, bad)
        assert cc._host_summary_deadline() == cc.HOST_SUMMARY_DEADLINE_SECONDS, bad


def test_a_backend_that_explodes_does_not_reach_the_terminal(monkeypatch, capsys) -> None:
    """`_bounded` runs `work` on a thread, so an escape hits threading.excepthook
    and prints a full traceback to the user's terminal mid-TAB — which is
    exactly what this function's "never crashes the shell" contract forbids."""

    def _explodes(_repo, _inventory, _profiles, _abandoned=None):
        raise KeyboardInterrupt  # a BaseException: `except Exception` misses it

    monkeypatch.setattr(cc, "_enumerate_host_summaries", _explodes)
    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    repo = MagicMock()
    repo.sut_dir = Path("/boom")

    assert (
        cc.repo_host_summaries(repo, cc.InventoryResolution(), profiles=ProfileContext.empty())
        == []
    )
    assert "Traceback" not in capsys.readouterr().err


def test_declining_loader_module_does_not_escape_collect_cli_commands(monkeypatch, tmp_path):
    """A third-party loader module that DECLINES to load must not brick every command.

    ``collect_cli_commands`` imports each non-otto command's ``"pkg.mod:attr"``
    loader. A module-level ``pytest.importorskip`` there raises ``Skipped`` —
    rooted at ``BaseException``, so an ``except Exception`` seam misses it. It
    escapes ``entry()`` as a call ARGUMENT to ``write_cache``, outside the
    ``suppress(OSError)``, and tracebacks out of EVERY command including
    ``otto --help``, and into the shell mid-TAB.
    """
    import sys

    from otto.cli.registry import CLI_COMMANDS, CommandSpec

    mod = tmp_path / "declining_loader.py"
    mod.write_text("import pytest\npytest.importorskip('otto_no_such_optional_dep')\napp = None\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "declining_loader", raising=False)

    spec = CommandSpec(name="probecmd", loader="declining_loader:app", help="probe")
    # Registered from a synthetic third-party module (attribution is by calling
    # frame), so `collect_cli_commands`, which skips anything whose origin starts
    # with "otto.", sees it; the root registry isolation removes it after the test.
    code = compile("CLI_COMMANDS.register('probecmd', spec)", "<thirdparty.pkg>", "exec")
    exec(code, {"__name__": "thirdparty.pkg", "CLI_COMMANDS": CLI_COMMANDS, "spec": spec})  # noqa: S102
    assert CLI_COMMANDS.origin("probecmd") == "thirdparty.pkg"

    try:
        out = cc.collect_cli_commands()
    except BaseException as exc:  # the escape IS the defect under test
        raise AssertionError(
            f"collect_cli_commands() let {type(exc).__name__} escape: {exc!r}"
        ) from exc
    # Contained: the command still appears, name-only, with no child metadata.
    entry = next(e for e in out if e["name"] == "probecmd")
    assert "commands" not in entry
    assert "options" not in entry


# ── docker use-case names: collect -> write -> read -> completer ─────────────


def _cache_repo(tmp_path: Path, name: str = "sut") -> MagicMock:
    """A repo double `write_cache`/`read_cache` will actually key on.

    ``inventory_settings = {}`` is not decoration: a MagicMock auto-attribute
    is TRUTHY and ``dict()``s to ``{}``, which reads as a present-but-empty
    ``[inventory]`` — a shape no real ``Repo`` produces and one
    ``build_inventory_from_declarations`` SKIPS (no inventory is built), same
    as if ``[inventory]`` were never declared at all.
    """
    repo = MagicMock()
    repo.sut_dir = tmp_path / name
    repo.sut_dir.mkdir()
    touch_settings(repo.sut_dir)
    repo.init = []
    repo.libs = []
    repo.tests = []
    repo.inventory_settings = {}
    return repo


def _use_case(name: str):
    from otto.config.repo import DockerUseCase

    return DockerUseCase(name=name, composes=("core",))


def test_collect_docker_use_case_names_dedupes_across_repos(tmp_path: Path) -> None:
    """One name declared by three repos is ONE use-case — that sharing is the
    whole mechanism (spec §3.1), not three completions of the same word."""
    a = _cache_repo(tmp_path, "a")
    a.docker_settings.use_cases = (_use_case("integration"), _use_case("soak"))
    b = _cache_repo(tmp_path, "b")
    b.docker_settings.use_cases = (_use_case("integration"),)

    assert cc.collect_docker_use_case_names([a, b]) == ["integration", "soak"]


def test_write_read_cache_round_trips_docker_use_cases(tmp_path: Path, monkeypatch) -> None:
    """The REAL write -> read path, unpatched, end to end.

    The completer tests in ``tests/unit/docker/test_cli.py`` hand
    ``get_completion_names`` a dict they built themselves, so they stay green
    against a ``write_cache`` that never writes the key at all. This is the
    half that would notice: collect the names off a repo, write them, read them
    back off DISK, and then feed that exact payload to the completer the way
    ``otto docker compose up <TAB>`` does.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    repo = _cache_repo(tmp_path)
    repo.docker_settings.use_cases = (_use_case("soak"), _use_case("integration"))

    names = cc.collect_docker_use_case_names([repo])
    cc.write_cache([repo], instructions=[], hosts=[], docker_use_cases=names)

    out = cc.read_cache([repo])
    assert out is not None
    assert out["docker_use_cases"] == ["integration", "soak"], (
        "write_cache did not persist docker_use_cases, or read_cache did not return it"
    )

    # ...and the file on disk really carries the key, not just the reader's default.
    data = json.loads(cc._cache_path().read_text())  # type: ignore[union-attr]
    assert data["sections"]["names"]["payload"]["docker_use_cases"] == ["integration", "soak"]
    assert data["schema"] == cc.SCHEMA_VERSION

    # The consumer end: the completer serves exactly what came off disk.
    from otto.cli.docker import _use_case_completer

    with mock.patch("otto.bootstrap.get_completion_names", return_value=out):
        assert _use_case_completer(MagicMock(), "i") == ["integration"]


def test_a_v13_entry_is_not_served_for_docker_use_cases(tmp_path: Path, monkeypatch) -> None:
    """A surviving pre-v14 entry must never be served, as an executable claim.

    ``read_cache`` defaults ``docker_use_cases`` to ``[]``, so an old entry
    would validate as an EMPTY list rather than missing — the completer's
    ``isinstance(..., list)`` guard would then take the cache branch and
    offer nothing instead of falling back to a live scan. Under v14 the
    per-entry schema stamp made it miss; under the v15 sections layout the
    whole fingerprint-keyed file has no ``"schema"``/``"sections"`` keys
    and misses structurally. The planted file stays byte-shaped as the
    genuine artifact an upgrade finds on disk.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    repo = _cache_repo(tmp_path)
    cache_file = cc._cache_path()
    cache_file.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    entry = {
        "schema_version": 13,  # the version that predates docker_use_cases
        "generated_at": int(time.time()),
        "instructions": [],
        "suites": [],
        "hosts": [],
    }
    cache_file.write_text(  # type: ignore[union-attr]
        json.dumps({_names_digest([repo]): entry})
    )

    assert cc.read_cache([repo]) is None, (
        "a pre-v14 entry was served, so it would answer `otto docker compose up <TAB>` "
        "with an empty use-case list"
    )


def test_a_non_list_docker_use_cases_entry_is_rejected(tmp_path: Path, monkeypatch) -> None:
    """The new validation arm: a corrupt value fails the whole entry, not the
    reader. Without it the completer would iterate a string one char at a time."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    repo = _cache_repo(tmp_path)
    cc.write_cache([repo], instructions=[], hosts=[], docker_use_cases=["integration"])

    cache_file = cc._cache_path()
    data = json.loads(cache_file.read_text())  # type: ignore[union-attr]
    payload = data["sections"]["names"]["payload"]
    payload["docker_use_cases"] = "integration"  # a string, not a list
    cache_file.write_text(json.dumps(data))  # type: ignore[union-attr]

    assert cc.read_cache([repo]) is None
