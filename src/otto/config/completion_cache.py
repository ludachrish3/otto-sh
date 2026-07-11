"""Shell-completion cache (Phase B).

Tab completion invokes ``otto`` just far enough to walk the Typer command
tree. The expensive step during that walk is not parsing CLI args — it's the
side effects in :mod:`otto.config` that populate dynamic subcommands:

- :meth:`Repo.import_init_modules` — imports every user-defined instruction
  module so ``@instruction()`` decorators can register into ``INSTRUCTIONS``.
- :meth:`Repo.import_test_files` — exec's every ``test_*.py`` so
  ``OttoSuite.__init_subclass__`` can auto-register ``Test*``-named classes
  into the ``SUITES`` registry.

Both execute arbitrary user code. For completion all we actually need is the
*names* those decorators would register and the *option schemas* the user can
tab-complete against. This module captures both in a small JSON file and,
when the cache is valid, lets the caller skip the user code entirely.

Cache location
--------------

``$OTTO_XDIR/.otto/completion_cache.json``. If ``OTTO_XDIR`` is not set,
caching is disabled and completion always falls through to the slow path.

Cache schema (version 9)
------------------------

Single flat map, keyed by fingerprint hex digest. Each entry records both the
schema version and the wall-clock time it was generated so a reader can drop
stale entries without trusting the mtimes on-disk::

    {
        "<fingerprint>": {
            "schema_version": 8,
            "generated_at": 1745000000,
            "instructions": [
                {
                    "name": "install",
                    "options": [
                        {
                            "name": "debug",
                            "flags": ["--field/--debug"],
                            "kind": "bool",
                            "default": false,
                            "help": "...",
                        },
                        ...,
                    ],
                },
                ...,
            ],
            "suites": [{"name": "TestDevice", "options": [...]}, ...],
            "hosts": ["carrot_seed", "tomato_seed", ...],
            "hosts_by_lab": {"veggies": ["carrot_seed", "tomato_seed"], ...},
            "docker_hosts": ["carrot_seed", ...],
            "term_backends": ["ssh", "telnet", ...],
            "transfer_backends": [{"name": "scp", "host_families": ["unix"]}, ...],
            "labs": ["tech1", "tech2", ...],
            "tests": ["test_smoke", "TestDevice::test_reachable", ...],
            "commands": [
                {"name": "flash", "help": "...", "lab_free": false},
                # a third-party GROUP also carries recursive child metadata;
                # a flattening single-command app carries "options" instead
                # (both keys omitted when empty):
                {
                    "name": "e2etool",
                    "help": "...",
                    "lab_free": true,
                    "commands": [
                        {"name": "ping", "help": "...", "options": [...]},
                        {"name": "nested", "help": "...", "commands": [...]},
                    ],
                },
                ...,
            ],
        }
    }

Collected test-name namespace
-----------------------------

Alongside the fingerprint entries, a single reserved key
``"__collected_tests__"`` holds the *pytest-collected* ``--tests`` names
(dynamically generated tests included), keyed by the same fingerprint::

    {
        "__collected_tests__": {
            "<fingerprint>": {
                "schema_version": 1,
                "generated_at": 1745000000,
                "names": ["test_x", "TestX::test_x", ...],
            }
        }
    }

It is written only by a deliberate collection (a real ``otto test --list-tests``
run, or the bounded subprocess the ``--tests`` completer spawns at tab time) —
never by the slow-path writer, which must not run a collection pass. Keeping it
in its own key means the two writers touch disjoint data and can't clobber.

Fingerprint
-----------

sha256 over ``(path, mtime_ns, size)`` triples for every file whose change
would alter the registered name sets: each SUT's ``settings.toml``, every
``.py`` file under any ``init`` module, every ``test_*.py`` in a configured
``tests`` directory, and every ``lab.json`` under a configured ``labs``
search path. File contents are never read, so the fingerprint is cheap to
compute even when SUTs are large.

A stale fingerprint is always safe: the fast path is skipped, the slow path
runs as normal and rewrites the cache afterward.
"""

import contextlib
import hashlib
import inspect
import json
import logging
import os
import tempfile
import time
import types
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Union, get_args, get_origin

if TYPE_CHECKING:
    from .repo import Repo


COMPLETION_ENV_VAR = "_OTTO_COMPLETE"
CACHE_FILENAME = "completion_cache.json"

# Bump when the on-disk schema changes in a way older readers can't parse.
# v9: added "labs" and "tests" (sources for --lab / --tests completion).
# v10: added "hosts_by_lab" (lab-scoped `otto host <TAB>` fast path).
# v11: host-ID sources now hash lab.json (renamed from hosts.json), so cached
#      fingerprints reference a different filename.
SCHEMA_VERSION = 11

LAB_FILENAME = "lab.json"

# Cache entries older than this (seconds) are treated as a miss. Forces the
# slow path to run periodically so annotation / option changes that don't
# move any tracked file's mtime still eventually refresh.
CACHE_TTL_SECONDS = 24 * 60 * 60


# --- Collected (pytest-accurate) test-name cache, for --tests completion -----
#
# The ``tests`` field above is an ``ast``-only *floor* — every statically
# written ``def test_*`` / ``Test*`` method, discovered without importing a
# thing. The *collected* set below comes from a real pytest collection, so it
# also covers dynamically generated tests (``pytest_generate_tests`` /
# fixture-driven parametrization) and matches the repo's actual pytest config.
# It selects by *base* name — ``otto test --tests`` matches a bare name against
# every parametrization — so per-parametrization ids are deliberately not part
# of it.
#
# It lives under its own reserved top-level key (never a real fingerprint), so
# writing it never disturbs the main fingerprint entries. That separation is
# load-bearing: the slow-path writer rewrites a whole main entry on every real
# command and must NEVER run a collection pass, while this set is warmed only
# by a deliberate collection (a real ``otto test`` run, or a bounded subprocess
# spawned at tab time). The two writers touch disjoint keys and can't clobber.
COLLECTED_TESTS_KEY = "__collected_tests__"
COLLECTED_SCHEMA_VERSION = 1

# Env var that flips ``otto`` into the one-shot "collect and print test names"
# subprocess the completer spawns to warm the collected cache. Handled as an
# early exit in :func:`otto.cli.main.entry`, before the normal CLI runs.
DUMP_TESTS_ENV_VAR = "_OTTO_DUMP_TEST_NAMES"

# Hard cap on the tab-time collection subprocess: a cold ``--tests`` TAB blocks
# at most this long before falling back to the static floor. "Slow on the first
# attempt is better than no completion" — but bounded, never a wedged shell.
COLLECT_TIMEOUT_SECONDS = 15

# After a failed / timed-out tab-time collection, skip re-collecting at tab time
# for this long. Keeps a repo that can't collect within the timeout from costing
# a slow TAB on *every* keystroke — at most one per cooldown window.
COLLECT_COOLDOWN_SECONDS = 60

COLLECT_LOCK_FILENAME = ".completion_collect.lock"
# A lock older than this is treated as orphaned (its holder died) and stolen,
# so a crashed collector can't block warming forever.
COLLECT_LOCK_STALE_SECONDS = COLLECT_TIMEOUT_SECONDS + 30

# Frame the dumped payload so the parent can recover the names even if repo
# discovery emits stray stdout before them.
_DUMP_BEGIN = "__OTTO_TESTS_BEGIN__"
_DUMP_END = "__OTTO_TESTS_END__"


# Python type <-> serialized kind. Kept intentionally small: these are the
# only types whose tab-completion shape (value vs. flag, how many args) we
# need to recreate. Anything not in this map is "unsupported" for caching
# purposes — the option is logged at DEBUG and dropped from the cached
# schema; completion still works on the slow path.
_TYPE_TO_KIND: dict[Any, str] = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
    Path: "path",
}
_KIND_TO_TYPE: dict[str, Any] = {v: k for k, v in _TYPE_TO_KIND.items()}


def is_completion_mode() -> bool:
    """Return True when otto is being invoked by shell completion."""
    return bool(os.environ.get(COMPLETION_ENV_VAR))


def _cache_path() -> Path | None:
    """Return the cache file path, or ``None`` when caching is disabled.

    Caching requires ``OTTO_XDIR`` to be set. Without it we can't pick a
    stable per-user location, so we skip caching entirely and fall back to
    the slow path every time.
    """
    # Function-local import: this module is loaded early during config
    # bootstrap, so defer the models import to call time. A fresh
    # OttoEnvSettings() re-reads OTTO_XDIR each call (tests monkeypatch it).
    from ..models.settings import OttoEnvSettings

    xdir = OttoEnvSettings().xdir  # Path | None ("" normalized to None)
    if xdir is None:
        return None
    return xdir / ".otto" / CACHE_FILENAME


def clear_cache() -> bool:
    """Delete the completion cache file if it exists.

    Returns True if a file was removed, False otherwise. Surface for the
    ``--clear-autocomplete-cache`` CLI escape hatch.
    """
    cache_path = _cache_path()
    if cache_path is None or not cache_path.is_file():
        return False
    try:
        cache_path.unlink()
    except OSError:
        return False
    else:
        return True


def _hash_file(h: "hashlib._Hash", path: Path) -> None:
    try:
        st = path.stat()
    except OSError:
        h.update(f"missing:{path}\n".encode())
        return
    h.update(f"{path}|{st.st_mtime_ns}|{st.st_size}\n".encode())


def compute_fingerprint(repos: list["Repo"]) -> str:
    """Stat-based sha256 of every file that contributes instruction/suite names."""
    h = hashlib.sha256()
    for repo in sorted(repos, key=lambda r: str(r.sut_dir)):
        _hash_file(h, repo.sut_dir / ".otto" / "settings.toml")

        # Init-module files: resolve each `init` name under the configured
        # `libs` directories. Either a package directory or a plain .py file.
        for init_mod in repo.init:
            mod_base = init_mod.split(".")[0]
            resolved = False
            for lib in repo.libs:
                mod_dir = lib / mod_base
                mod_file = lib / f"{mod_base}.py"
                if mod_dir.is_dir():
                    for py in sorted(mod_dir.rglob("*.py")):
                        _hash_file(h, py)
                    resolved = True
                elif mod_file.is_file():
                    _hash_file(h, mod_file)
                    resolved = True
            if not resolved:
                h.update(f"unresolved:{init_mod}\n".encode())

        for test_dir in repo.tests:
            if test_dir.is_dir():
                for t in sorted(test_dir.glob("test_*.py")):
                    _hash_file(h, t)

        # Host-ID sources: lab.json under each configured lab search path.
        # Adding these to the fingerprint lets the cache self-invalidate on
        # edits. (Future DB-backed sources will need a different staleness
        # signal — likely a pure TTL or DB revision token.)
        for lab_path in repo.labs:
            _hash_file(h, lab_path / LAB_FILENAME)

    return h.hexdigest()


# ---------------------------------------------------------------------------
# Option serialization — convert a live Typer command callback's signature
# into a JSON-safe list of {name, flags, kind, default, help} dicts.
# ---------------------------------------------------------------------------


def _unwrap_optional(t: Any) -> Any:
    """Strip a single ``Optional[...]`` wrapper, leaving other types intact."""
    origin = get_origin(t)
    is_union = origin is Union or isinstance(t, types.UnionType)
    if not is_union:
        return t
    non_none = [a for a in get_args(t) if a is not type(None)]
    if len(non_none) == 1:
        return non_none[0]
    return t


def _type_to_kind(base: Any) -> str | None:
    """Map a Python type to the cache's ``kind`` tag, or ``None`` if unsupported."""
    base = _unwrap_optional(base)
    if base in _TYPE_TO_KIND:
        return _TYPE_TO_KIND[base]
    if get_origin(base) is list and get_args(base) == (str,):
        return "str_list"
    return None


def _extract_flags(option_info: Any) -> list[str]:
    """Return the user-authored flag strings from a ``typer.Option`` instance.

    Typer stores the first positional flag as the info's ``default`` attribute
    and the rest in ``param_decls``; concatenate them in declaration order so
    the rebuilder reproduces the original call.
    """
    flags: list[str] = []
    primary = getattr(option_info, "default", None)
    if isinstance(primary, str) and (primary.startswith("-") or "/" in primary):
        flags.append(primary)
    flags.extend(getattr(option_info, "param_decls", ()) or ())
    return flags


def _json_safe_default(default: Any) -> Any:
    """Coerce a parameter default to a JSON-serializable form."""
    if default is inspect.Parameter.empty or default is Ellipsis:
        return None
    if isinstance(default, Path):
        return str(default)
    if isinstance(default, (str, int, float, bool)) or default is None:
        return default
    # Lists of scalars are the only composite we care to round-trip (str_list).
    if isinstance(default, list):
        try:
            json.dumps(default)
        except TypeError:
            return None
        else:
            return default
    return None


def _serialize_options(
    callback: Any,
    *,
    command_name: str,
) -> list[dict[str, Any]] | None:
    """Convert a Typer command callback's signature into cache-shape dicts.

    Returns ``None`` (not an empty list) when any parameter uses an
    annotation form we don't know how to round-trip — that causes the
    command to be skipped entirely rather than cached with a half-signature.
    """
    log = logging.getLogger(__name__)
    try:
        sig = inspect.signature(callback)
    except (TypeError, ValueError) as e:  # pragma: no cover — paranoia
        log.debug(
            f"completion-cache: skipping {command_name!r}, signature inspection failed: {e!r}",
        )
        return None

    import typer  # lazy: this runs at cache-seed time, not at module import

    options: list[dict[str, Any]] = []
    for pname, param in sig.parameters.items():
        ann = param.annotation
        # The suite runner carries a Typer-injected ``ctx: typer.Context``
        # parameter (used to read run options from ``ctx.meta``). It is not a CLI
        # option and has no ``Annotated[...]`` metadata, so skip it rather than
        # treating the whole command as un-cacheable.
        if ann is typer.Context:
            continue
        if get_origin(ann) is not Annotated:
            log.debug(
                f"completion-cache: skipping option {command_name}.{pname!r} — "
                f"annotation {ann!r} is not Annotated[...]",
            )
            return None
        args = get_args(ann)
        base = args[0]
        # OptionInfo lives at module path typer.models.OptionInfo; match on
        # attribute shape to avoid importing typer at module load.
        meta = next(
            (a for a in args[1:] if hasattr(a, "param_decls")),
            None,
        )
        if meta is None:
            log.debug(
                f"completion-cache: skipping option {command_name}.{pname!r} — "
                f"no typer.Option metadata in annotation",
            )
            return None

        kind = _type_to_kind(base)
        if kind is None:
            log.debug(
                f"completion-cache: skipping option {command_name}.{pname!r} — "
                f"unsupported annotation type {base!r}",
            )
            return None

        options.append(
            {
                "name": pname,
                "flags": _extract_flags(meta),
                "kind": kind,
                "default": _json_safe_default(param.default),
                "help": getattr(meta, "help", None) or "",
            }
        )
    return options


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def read_cache(repos: list["Repo"]) -> dict[str, Any] | None:
    """Return the cached command lists for the current fingerprint, or ``None``.

    ``None`` means any of: caching disabled, empty repos (would produce the
    empty-sha256 fingerprint and poison the cache for other shells), cache
    file missing, cache file corrupt, fingerprint mismatch, schema mismatch,
    or TTL expired. In every case the caller should fall back to the slow
    path.

    On success returns a dict with ``instructions``, ``suites``, ``hosts``,
    ``docker_hosts``, ``term_backends``, ``transfer_backends``, and
    ``commands`` keys. The first two are lists of
    ``{"name": str, "options": [...]}`` dicts; ``hosts`` and ``docker_hosts``
    are plain lists of host-ID strings; ``term_backends`` is a list of
    backend-name strings; ``transfer_backends`` is a list of
    ``{"name": str, "host_families": [str, ...]}`` dicts; ``commands`` is a
    list of ``{"name": str, "help": str | None, "lab_free": bool}`` dicts for
    third-party top-level CLI commands (default ``[]`` when absent).
    """
    if not repos:
        return None

    cache_path = _cache_path()
    if cache_path is None or not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    fingerprint = compute_fingerprint(repos)
    entry = data.get(fingerprint)
    if not isinstance(entry, dict):
        return None
    if entry.get("schema_version") != SCHEMA_VERSION:
        return None
    generated_at = entry.get("generated_at")
    if not isinstance(generated_at, (int, float)):
        return None
    if time.time() - generated_at > CACHE_TTL_SECONDS:
        return None
    instructions = entry.get("instructions")
    suites = entry.get("suites")
    hosts = entry.get("hosts")
    hosts_by_lab = entry.get("hosts_by_lab", {})
    docker_hosts = entry.get("docker_hosts", [])
    term_backends = entry.get("term_backends", [])
    transfer_backends = entry.get("transfer_backends", [])
    usernames = entry.get("usernames", [])
    commands = entry.get("commands", [])
    labs = entry.get("labs", [])
    tests = entry.get("tests", [])
    if (
        not isinstance(instructions, list)
        or not isinstance(suites, list)
        or not isinstance(hosts, list)
        or not isinstance(hosts_by_lab, dict)
        or not isinstance(docker_hosts, list)
        or not isinstance(term_backends, list)
        or not isinstance(transfer_backends, list)
        or not isinstance(usernames, list)
        or not isinstance(commands, list)
        or not isinstance(labs, list)
        or not isinstance(tests, list)
    ):
        return None
    return {
        "instructions": instructions,
        "suites": suites,
        "hosts": hosts,
        "hosts_by_lab": hosts_by_lab,
        "docker_hosts": docker_hosts,
        "term_backends": term_backends,
        "transfer_backends": transfer_backends,
        "usernames": usernames,
        "commands": commands,
        "labs": labs,
        "tests": tests,
    }


def write_cache(  # noqa: PLR0913 — one keyword arg per cached name-set, by design
    repos: list["Repo"],
    instructions: list[dict[str, Any]],
    suites: list[dict[str, Any]],
    hosts: list[str],
    docker_hosts: list[str] | None = None,
    term_backends: list[str] | None = None,
    transfer_backends: list[dict[str, Any]] | None = None,
    usernames: list[str] | None = None,
    commands: list[dict[str, Any]] | None = None,
    labs: list[str] | None = None,
    tests: list[str] | None = None,
    hosts_by_lab: dict[str, list[str]] | None = None,
) -> None:
    """Write (or update) the entry for the current fingerprint.

    Skipped silently when repos is empty — an empty-repo fingerprint is the
    empty-string sha256, which any shell without ``OTTO_SUT_DIRS`` would
    also compute, and that would wrongly override a real entry's meaning.

    Atomic via ``tempfile`` + :func:`os.replace` so a concurrent otto
    invocation can't observe a half-written file. Stale entries from past
    SUT_DIRS combinations are left in place and ignored.
    """
    if not repos:
        return

    cache_path = _cache_path()
    if cache_path is None:
        return

    cache_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] = {}
    if cache_path.is_file():
        try:
            loaded = json.loads(cache_path.read_text())
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            pass

    fingerprint = compute_fingerprint(repos)
    existing[fingerprint] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": int(time.time()),
        "instructions": instructions,
        "suites": suites,
        "hosts": hosts,
        "hosts_by_lab": hosts_by_lab or {},
        "docker_hosts": docker_hosts or [],
        "term_backends": term_backends or [],
        "transfer_backends": transfer_backends or [],
        "usernames": usernames or [],
        "commands": commands or [],
        "labs": labs or [],
        "tests": tests or [],
    }

    _atomic_write_json(cache_path, existing)


def _atomic_write_json(cache_path: Path, obj: dict[str, Any]) -> None:
    """Write *obj* as JSON to *cache_path* atomically (tempfile + ``os.replace``).

    A concurrent reader always sees either the old file or the complete new
    one, never a half-written mix.
    """
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=cache_path.parent,
        delete=False,
        prefix=".completion_cache_",
        suffix=".tmp",
    ) as tmp:
        tmp_name = tmp.name
        json.dump(obj, tmp)
    try:
        Path(tmp_name).replace(cache_path)
    except Exception:
        with contextlib.suppress(OSError):
            Path(tmp_name).unlink()
        raise


# ---------------------------------------------------------------------------
# Live-registry introspection (writer side)
# ---------------------------------------------------------------------------


def collect_current_commands() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read the currently-registered instructions and suites with options.

    Must be called after :func:`otto.bootstrap.bootstrap` has finished
    populating ``otto.cli.run.INSTRUCTIONS`` and ``otto.suite.register.SUITES``.
    Returns empty lists for any source that hasn't been loaded (e.g. no init
    modules → ``otto.cli.run`` never imported → no instructions).

    Each item is ``{"name": str, "options": list[dict]}``; a command whose
    options can't be fully serialized is cached with ``options: []`` so
    the name still completes even though the per-option flags don't.
    """
    import sys

    def _entries_to_dicts(entries: list[tuple[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name, entry in entries:
            callback = None
            if entry.sub_app.registered_commands:
                callback = entry.sub_app.registered_commands[0].callback
            options = _serialize_options(callback, command_name=name) if callback else None
            out.append({"name": name, "options": options if options is not None else []})
        return out

    instructions: list[dict[str, Any]] = []
    run_mod = sys.modules.get("otto.cli.run")
    if run_mod is not None:
        instructions = _entries_to_dicts(run_mod.INSTRUCTIONS.items())

    # Unlike otto.cli.run (guarded above via sys.modules to sidestep a real
    # circular-import hazard at bootstrap time), otto.suite.register has no
    # such hazard — it's safe to import directly here.
    from ..suite.register import SUITES

    suites: list[dict[str, Any]] = _entries_to_dicts(SUITES.items())

    return instructions, suites


def collect_backend_names() -> dict[str, Any]:
    """Snapshot the registered term + transfer backend names for completion.

    Call after :func:`otto.bootstrap.bootstrap` (or ``import_init_modules``) so
    custom per-repo backends are present. Built-ins are always present
    (registered at module import). Each transfer backend carries its
    ``host_families`` so the completer can filter by family (e.g. unix-only
    for ``otto host --transfer``).
    """
    from ..host.connections import TERM_BACKENDS
    from ..host.transfer import TRANSFER_BACKENDS

    return {
        "term_backends": sorted(TERM_BACKENDS.names()),
        "transfer_backends": [
            {"name": name, "host_families": sorted(cls.host_families)}
            for name, cls in sorted(TRANSFER_BACKENDS.items())
        ],
    }


def _serialize_cli_children(app: Any) -> list[dict[str, Any]]:
    """Serialize a third-party Typer group's children for the cache.

    Children reuse the instruction/suite option schema (rebuilt by
    :func:`otto.config.completion_stubs.build_stub_command` on the fast
    path). A child whose options don't round-trip degrades to name+help —
    the name still tab-completes, only ``--<TAB>`` falls back. Nested groups
    recurse; a nested single-command app serializes as the flattened leaf it
    would natively become (see ``_typer_app_flattens``).
    """
    from typer.main import get_command_name

    from ..cli.registry import _typer_app_flattens

    children: list[dict[str, Any]] = []
    for cmd_info in app.registered_commands:
        cname = cmd_info.name or get_command_name(cmd_info.callback.__name__)
        children.append(
            {
                "name": cname,
                "help": cmd_info.help or inspect.getdoc(cmd_info.callback) or "",
                "options": _serialize_options(cmd_info.callback, command_name=cname) or [],
            }
        )
    for grp_info in app.registered_groups:
        sub = grp_info.typer_instance
        if sub is None:
            continue
        if _typer_app_flattens(sub):
            children.extend(_serialize_cli_children(sub))
            continue
        gname = next(
            (n for n in (grp_info.name, sub.info.name) if isinstance(n, str) and n),
            None,
        )
        if gname is None:
            continue
        ghelp = next((h for h in (grp_info.help, sub.info.help) if isinstance(h, str)), "")
        children.append({"name": gname, "help": ghelp, "commands": _serialize_cli_children(sub)})
    return children


def collect_cli_commands() -> list[dict[str, Any]]:
    """Snapshot third-party top-level CLI commands for the completion cache.

    Reads the live :data:`otto.cli.registry.CLI_COMMANDS` registry and
    returns one ``{"name", "help", "lab_free"}`` dict per entry whose
    ``origin`` module is *not* under ``otto.`` — built-in commands re-register
    on every real invocation (bootstrap always runs), so caching them would
    be redundant and risks masking a genuine removal. Third-party commands,
    by contrast, only exist in the registry after a plugin's init module has
    executed, which the completion fast path deliberately skips; caching
    their name/help/``lab_free`` here is what lets them still tab-complete.

    A GROUP entry additionally carries ``"commands"`` (recursive child
    metadata) and a flattening single-command app carries ``"options"`` —
    both omitted when empty. Serializing children may import a lazy
    ``"pkg.mod:attr"`` loader's module: a slow-path-only, once-per-cache-
    refresh cost, contained per command (a broken loader degrades that entry
    to name+help and real dispatch still reports the import error loudly).
    """
    import importlib

    import typer

    from ..cli.registry import CLI_COMMANDS, _typer_app_flattens

    log = logging.getLogger(__name__)
    out: list[dict[str, Any]] = []
    for name, spec in CLI_COMMANDS.items():
        if spec.origin.startswith("otto."):
            continue
        entry: dict[str, Any] = {"name": name, "help": spec.help, "lab_free": spec.lab_free}
        try:
            loader = spec.loader
            if isinstance(loader, str):
                mod_name, _, attr = loader.partition(":")
                loader = getattr(importlib.import_module(mod_name), attr)
            if isinstance(loader, typer.Typer):
                if _typer_app_flattens(loader):
                    cmd_info = loader.registered_commands[0]
                    options = _serialize_options(cmd_info.callback, command_name=spec.name)
                    if options:
                        entry["options"] = options
                else:
                    commands = _serialize_cli_children(loader)
                    if commands:
                        entry["commands"] = commands
        except Exception as e:  # noqa: BLE001 — containment seam: cache stays name-only, dispatch reports loudly
            log.debug(f"completion-cache: no child metadata for {spec.name!r}: {e!r}")
        out.append(entry)
    return out


def collect_reservation_usernames(repos: list["Repo"]) -> list[str]:
    """Best-effort usernames for ``--as-user`` completion (cached).

    Builds the selected reservation backend (first repo with a
    ``[reservations]`` section) and, when it implements
    :class:`~otto.reservations.protocol.SupportsUsernameCompletion`, returns
    ``list_usernames()`` sorted. Runs on the slow path; any failure (no backend
    configured, build error, enumeration error, missing capability) yields
    ``[]`` so completion degrades gracefully and never blocks real work.
    """
    from ..reservations import build_backend
    from ..reservations.protocol import SupportsUsernameCompletion

    for repo in repos:
        settings = getattr(repo, "reservation_settings", None)
        if not settings:
            continue
        try:
            backend = build_backend(settings, repo.sut_dir)
            if isinstance(backend, SupportsUsernameCompletion):
                return sorted(backend.list_usernames())
        except Exception:  # noqa: BLE001 — completion fallback, best-effort username list; return empty on any error
            return []
        return []
    return []


def _read_lab_hosts(lab_file: Path) -> list[dict[str, Any]]:
    """Best-effort read of a lab.json's ``hosts`` array ([] on any problem).

    Completion must never crash on bad user data, so malformed shapes are
    silently empty here (the real loader raises with full diagnostics). Kept
    stdlib-only (json) so the completion fast path stays import-light.
    """
    try:
        data = json.loads(lab_file.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    hosts = data.get("hosts", [])
    return hosts if isinstance(hosts, list) else []


def collect_docker_capable_host_ids(repos: list["Repo"]) -> list[str]:
    """Enumerate host IDs whose ``lab.json`` host entry has ``docker_capable: true``.

    Used as the completion source for ``otto docker --on <TAB>`` and any
    other surface that should be limited to docker-capable parents.
    Mirrors :func:`collect_host_ids` (no :func:`otto.bootstrap.bootstrap` call
    needed; safe in the completion fast path).
    """
    from ..host.factory import create_host_from_dict, validate_host_dict

    ids: set[str] = set()
    for repo in repos:
        for lab_path in repo.labs:
            for host_data in _read_lab_hosts(lab_path / LAB_FILENAME):
                if not isinstance(host_data, dict):
                    continue
                if not host_data.get("docker_capable"):
                    continue
                try:
                    validate_host_dict(host_data)
                    host = create_host_from_dict(host_data)
                except (ValueError, TypeError):
                    continue
                ids.add(host.id)
    return sorted(ids)


def collect_host_ids(repos: list["Repo"], lab_names: list[str] | None = None) -> list[str]:
    """Enumerate every host ID reachable via the configured lab search paths.

    Reads each repo's ``labs`` directories for a ``lab.json`` file and
    builds :class:`UnixHost` objects via the existing factory so the
    resulting IDs match what ``get_host`` will look up at runtime. Also
    synthesizes container host IDs of the form ``<parent>.<project>.<service>``
    from each repo's ``[docker]`` settings so declared container hosts
    are tab-completable before they're actually brought up.

    When *lab_names* is given, only hosts whose ``labs`` array names one of
    those labs are enumerated — the completion source for ``otto host <TAB>``
    once a lab is selected via ``-l``/``--lab``/``OTTO_LAB``. Container IDs are
    scoped the same way (only docker-capable parents in the selected lab).
    The built-in hosts are always seeded regardless of the filter, mirroring
    ``load_lab`` injecting ``local`` into every lab.

    Also emits positional logical handles (``<element-slug><N>``, e.g.
    ``server1``) for every host in a repeated-element group, computed via
    :func:`otto.config.lab.logical_indices` — the same single source
    ``Lab._assign_logical_indices`` stamps from — so a completed handle always
    matches what ``Lab.resolve_handle`` resolves at runtime. Added alongside
    canonical ids, never in place of them.

    Runs without :func:`otto.bootstrap.bootstrap` having been called, so it's
    safe to call from the completion fast path as well as the cache writer
    on the slow path.

    Returns a sorted, de-duplicated list. Malformed files / entries are
    silently skipped — completion must never crash on bad user data.
    """
    from ..host.builtin_hosts import builtin_host_ids
    from ..host.factory import create_host_from_dict, validate_host_dict
    from ..host.remote_host import slug
    from .lab import logical_indices

    wanted = set(lab_names) if lab_names is not None else None

    # Seed with the built-in hosts otto injects into every lab (e.g. `local`) so
    # they are tab-completable in every repo, mirroring load_lab's injection.
    ids: set[str] = set(builtin_host_ids())
    # Every constructed host across all repos, keyed by id (dedup). Logical
    # positions are derived from this combined set (once, below) so a group
    # split across repos' lab.json files is still numbered as one group —
    # matching how a real Lab merges hosts from multiple sources before
    # stamping.
    built: dict[str, Any] = {}
    for repo in repos:
        # Map of host_id -> docker_capable flag, scoped to this repo's labs.
        # Populated as we walk lab.json so we can synthesize container
        # ids in the same pass.
        docker_capable_ids: list[str] = []
        for lab_path in repo.labs:
            for host_data in _read_lab_hosts(lab_path / LAB_FILENAME):
                if not isinstance(host_data, dict):
                    continue
                # Lab filter: keep only hosts tagged with a requested lab.
                if wanted is not None and wanted.isdisjoint(host_data.get("labs", [])):
                    continue
                try:
                    validate_host_dict(host_data)
                    host = create_host_from_dict(host_data)
                except (ValueError, TypeError):
                    continue
                ids.add(host.id)
                built[host.id] = host
                if getattr(host, "docker_capable", False):
                    docker_capable_ids.append(host.id)

        docker = getattr(repo, "docker_settings", None)
        if docker is None or not docker.composes:
            continue
        for compose in docker.composes:
            # Pick parents to enumerate against. Prefer an explicit
            # default_host; otherwise enumerate every docker-capable host
            # in this repo's labs (pessimistic but stable; the actual
            # bring-up picks one). Under a lab filter, an explicit
            # default_host only counts if it survived the filter (i.e. it is a
            # docker-capable host in the selected lab).
            if compose.default_host:
                parents = (
                    [compose.default_host]
                    if wanted is None or compose.default_host in docker_capable_ids
                    else []
                )
            else:
                parents = list(docker_capable_ids)
            for parent in parents:
                for service in compose.services:
                    ids.add(f"{parent}.{repo.name}.{service}".lower())

    # Logical handles (<slug(element)><position>) alongside canonical ids, so
    # `otto host <TAB>` offers exactly what Lab.resolve_handle would resolve at
    # runtime — logical_indices is the single shared source (see lab.py).
    positions = logical_indices(built.values())
    for host in built.values():
        pos = positions.get(host.id)
        if pos is not None:
            ids.add(f"{slug(host.element)}{pos}")

    return sorted(ids)


def _read_lab_links(lab_file: Path) -> list[dict[str, Any]]:
    """Best-effort read of a lab.json's ``links`` array ([] on any problem).

    Mirrors :func:`_read_lab_hosts`: completion must never crash on bad user
    data, so malformed shapes are silently empty here.
    """
    try:
        data = json.loads(lab_file.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    links = data.get("links", [])
    return links if isinstance(links, list) else []


def collect_link_ids(repos: list["Repo"]) -> list[str]:
    """Enumerate static link ids/names for ``otto link`` completion.

    Each id is the declared ``name`` if set, else the ``lo--hi`` static id.
    Pure lab-data derivation (sync, no live scan).

    Reuses :func:`collect_host_ids`'s repo/lab-file iteration, but reads raw
    ``links`` entries with no host construction/validation: an entry's
    completion id is either its declared ``name`` or the sorted ``a--b`` pair
    of its two endpoint host ids — exactly
    :func:`~otto.link.model.make_static_link_id`'s no-name form (the ids here
    are already the resolved host ids a raw ``links`` entry names, so no
    element/board resolution is needed). Malformed entries (missing/short
    endpoints, non-dict shapes) are silently skipped — completion must never
    crash on bad user data.
    """
    ids: set[str] = set()
    for repo in repos:
        for lab_path in repo.labs:
            for entry in _read_lab_links(lab_path / LAB_FILENAME):
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                if isinstance(name, str) and name:
                    ids.add(name)
                    continue
                endpoints = entry.get("endpoints")
                if not isinstance(endpoints, list) or len(endpoints) != 2:  # noqa: PLR2004
                    continue
                hosts = [ep.get("host") for ep in endpoints if isinstance(ep, dict)]
                if len(hosts) != 2 or not all(isinstance(h, str) and h for h in hosts):  # noqa: PLR2004
                    continue
                lo, hi = sorted(hosts)
                ids.add(f"{lo}--{hi}")
    return sorted(ids)


def collect_lab_names(repos: list["Repo"]) -> list[str]:
    """Enumerate every lab name referenced across the configured lab.json files.

    A lab is a *tag* on hosts (each host's ``labs`` array), not a directory,
    so the names come straight from the built-in json backend's
    :meth:`~otto.labs.json_repository.JsonFileLabRepository.list_labs` over
    the aggregated ``labs`` search paths — the same source ``otto --list-labs``
    uses. Data-only (no host construction, no user code), so it is safe in the
    completion fast path as well as the cache writer. Malformed files are
    skipped by ``list_labs`` itself; any unexpected error yields ``[]`` so
    completion never crashes.
    """
    from ..labs.json_repository import JsonFileLabRepository

    search_paths: list[Path] = []
    for repo in repos:
        search_paths.extend(repo.labs)
    try:
        return JsonFileLabRepository(search_paths=search_paths).list_labs()
    except Exception:  # noqa: BLE001 — completion must degrade, never crash
        return []


def collect_host_ids_by_lab(repos: list["Repo"]) -> dict[str, list[str]]:
    """Map each lab name to the host IDs that belong to it (pure membership).

    Powers lab-scoped ``otto host <TAB>`` completion from the fast cache path:
    the completer unions the buckets for the selected lab(s) and adds the
    always-present built-in hosts. The buckets therefore deliberately EXCLUDE
    built-ins — the "``local`` is in every lab" policy lives in the completer,
    in one place, shared with the live fallback (:func:`collect_host_ids` with
    ``lab_names``). Keeping buckets to true membership also means a bogus lab
    name resolves to exactly the built-ins on both the warm and cold paths.

    Written by the slow-path cache writer only, so the per-lab rescan of
    ``lab.json`` is not on any latency-sensitive path.
    """
    from ..host.builtin_hosts import builtin_host_ids

    builtins = set(builtin_host_ids())
    return {
        lab: [h for h in collect_host_ids(repos, lab_names=[lab]) if h not in builtins]
        for lab in collect_lab_names(repos)
    }


def collect_test_names(repos: list["Repo"]) -> list[str]:
    """Statically discover test names for ``otto test --tests`` completion.

    Parses every ``test_*.py`` / ``*_test.py`` under each repo's test dirs with
    :mod:`ast` — no import, no collection, no user code — and returns the base
    names of top-level ``def test_*`` / ``async def test_*`` functions plus, for
    each ``Test*`` class, its ``test_*`` methods (emitted both bare and as
    ``ClassName::method`` to match ``--tests``'s disambiguation form).

    This is deliberately static: real pytest collection (which ``--tests``
    resolves against, and which ``otto test --list-tests`` runs) expands
    parametrization and honors ``conftest`` / ``pytest_generate_tests``, none
    of which are visible to a source scan. So a *parametrized-only* id or a
    dynamically generated test will not appear here — those still need
    ``--list-tests`` — but every statically-defined test name does, without
    ever executing test code at tab time. Kept in lockstep with pytest's
    default ``python_files`` / ``python_classes`` / ``python_functions``.
    """
    import ast

    names: set[str] = set()
    for repo in repos:
        for test_dir in repo.tests:
            if not test_dir.exists():
                continue
            for path in (*test_dir.rglob("test_*.py"), *test_dir.rglob("*_test.py")):
                try:
                    tree = ast.parse(path.read_text(), filename=str(path))
                except (OSError, SyntaxError):
                    continue  # unreadable / unparseable file: skip, never crash
                for node in tree.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if node.name.startswith("test"):
                            names.add(node.name)
                    elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                        for method in node.body:
                            if isinstance(
                                method, (ast.FunctionDef, ast.AsyncFunctionDef)
                            ) and method.name.startswith("test"):
                                names.add(method.name)
                                names.add(f"{node.name}::{method.name}")
    return sorted(names)


# ---------------------------------------------------------------------------
# Collected (pytest-accurate) test names — real collection, cached separately
# ---------------------------------------------------------------------------


def _test_names_from_items(items: list[Any]) -> list[str]:
    """Completion candidates from collected pytest items: base + ``Class::base``.

    Collapses parametrizations to the base name (``test_x[a]`` → ``test_x``):
    ``otto test --tests`` selects by base name (a bare name runs every
    parametrization and per-parametrization ids are rejected), so this mirrors
    :func:`collect_test_names`'s shape — only the *source* differs (real
    collection vs. an ``ast`` scan). Duck-typed on ``.name`` / ``.cls_name`` so
    it needn't import :class:`~otto.config.repo.CollectedTest`.
    """
    names: set[str] = set()
    for item in items:
        base = str(item.name).partition("[")[0]
        names.add(base)
        cls_name = getattr(item, "cls_name", None)
        if cls_name:
            names.add(f"{cls_name}::{base}")
    return sorted(names)


def dump_collected_test_names(repos: list["Repo"]) -> None:
    """Collect every repo's tests and print a framed name list to stdout.

    The child side of the tab-time warm: run by :func:`otto.cli.main.entry`
    when :data:`DUMP_TESTS_ENV_VAR` is set. Collection runs here — in a
    disposable, timeout-bounded subprocess — never inside the completer itself
    (whose stdout is the shell's completion channel). ``Repo.collect_tests``
    already redirects the inner pytest run's stdout/stderr, so only the framed
    payload below reaches the parent.
    """
    import sys

    items: list[Any] = []
    for repo in repos:
        items.extend(repo.collect_tests())
    names = _test_names_from_items(items)
    sys.stdout.write("\n".join([_DUMP_BEGIN, *names, _DUMP_END]) + "\n")
    sys.stdout.flush()


def _parse_dumped_names(stdout: str) -> list[str] | None:
    """Recover the framed name list from the dump subprocess's stdout."""
    lines = stdout.splitlines()
    try:
        start = lines.index(_DUMP_BEGIN)
        end = lines.index(_DUMP_END)
    except ValueError:
        return None
    if end < start:
        return None
    return [ln for ln in lines[start + 1 : end] if ln.strip()]


def _collected_cache_entry(repos: list["Repo"]) -> dict[str, Any] | None:
    """Raw collected-cache entry (names + timestamp) for the current fingerprint."""
    if not repos:
        return None
    cache_path = _cache_path()
    if cache_path is None or not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    namespace = data.get(COLLECTED_TESTS_KEY)
    if not isinstance(namespace, dict):
        return None
    entry = namespace.get(compute_fingerprint(repos))
    return entry if isinstance(entry, dict) else None


def read_collected_tests(repos: list["Repo"]) -> list[str] | None:
    """Return the fresh pytest-collected test names for ``--tests``, or ``None``.

    ``None`` means the collected set is cold for the completer: caching
    disabled, no entry for this fingerprint, wrong schema, TTL-expired, a
    recorded *failed* attempt (``names`` is ``null``), or malformed data. The
    completer then falls back to the static floor and may warm the cache via
    :func:`maybe_warm_collected_tests`. Fingerprint keying means any test-file
    edit invalidates this automatically, exactly like the main cache.
    """
    entry = _collected_cache_entry(repos)
    if entry is None:
        return None
    if entry.get("schema_version") != COLLECTED_SCHEMA_VERSION:
        return None
    generated_at = entry.get("generated_at")
    if not isinstance(generated_at, (int, float)):
        return None
    if time.time() - generated_at > CACHE_TTL_SECONDS:
        return None
    names = entry.get("names")
    if not isinstance(names, list):
        return None
    return names


def _record_collected_tests(repos: list["Repo"], names: list[str] | None) -> None:
    """Merge a collected-cache result for the current fingerprint.

    ``names=None`` records a *failed* attempt; its timestamp drives the
    tab-time retry cooldown. Only the reserved :data:`COLLECTED_TESTS_KEY`
    namespace is touched — every main fingerprint entry is preserved — so this
    warmer and the slow-path writer never clobber each other.
    """
    if not repos:
        return
    cache_path = _cache_path()
    if cache_path is None:
        return
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] = {}
    if cache_path.is_file():
        try:
            loaded = json.loads(cache_path.read_text())
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            pass

    namespace = existing.get(COLLECTED_TESTS_KEY)
    if not isinstance(namespace, dict):
        namespace = {}
    namespace[compute_fingerprint(repos)] = {
        "schema_version": COLLECTED_SCHEMA_VERSION,
        "generated_at": int(time.time()),
        "names": names,  # None => a failed attempt (cooldown marker only)
    }
    existing[COLLECTED_TESTS_KEY] = namespace
    _atomic_write_json(cache_path, existing)


def record_collected_tests_from_items(repos: list["Repo"], items: list[Any]) -> None:
    """Warm the collected cache from an already-run *unfiltered* collection.

    The free "Option B" path: when a real ``otto test --list-tests`` runs with
    no marker/suite narrowing, it has already collected the full test set, so
    cache it here rather than paying a separate collection later. Callers must
    only pass an *unfiltered* item list — a marker/suite-narrowed collection
    would cache an incomplete set.
    """
    _record_collected_tests(repos, _test_names_from_items(items))


def _acquire_collect_lock(lock: Path) -> bool:
    """Try to take the tab-time collection lock (atomic ``O_EXCL`` create).

    Returns ``False`` when another process holds a fresh lock; steals and takes
    a lock older than :data:`COLLECT_LOCK_STALE_SECONDS` (its holder died).
    """
    now = time.time()
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            age = now - lock.stat().st_mtime
        except OSError:
            return False
        if age <= COLLECT_LOCK_STALE_SECONDS:
            return False
        with contextlib.suppress(OSError):
            lock.unlink()
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            return False
    except OSError:
        return False
    with contextlib.suppress(OSError):
        os.write(fd, str(now).encode())
    os.close(fd)
    return True


def _run_collect_subprocess() -> list[str] | None:
    """Spawn the bounded ``DUMP_TESTS_ENV_VAR`` subprocess and parse its names.

    Returns the collected names, or ``None`` on timeout / non-zero exit / spawn
    failure. Runs the *venv* ``otto`` binary (so ``entry`` runs, unlike ``python
    -m otto``) with the completion env vars stripped, so the child dumps names
    instead of recursing into another completion.
    """
    import subprocess
    import sys

    otto_bin = Path(sys.executable).parent / "otto"
    if not otto_bin.exists():
        return None
    env = dict(os.environ)
    for var in (COMPLETION_ENV_VAR, "COMP_WORDS", "COMP_CWORD"):
        env.pop(var, None)
    env[DUMP_TESTS_ENV_VAR] = "1"
    try:
        proc = subprocess.run(  # noqa: S603 — venv otto binary, fixed argv, no shell
            [str(otto_bin)],
            env=env,
            capture_output=True,
            text=True,
            timeout=COLLECT_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return _parse_dumped_names(proc.stdout)


def maybe_warm_collected_tests(repos: list["Repo"]) -> list[str] | None:
    """Best-effort: run one bounded collection to warm the collected cache.

    Returns the collected names on success (so the triggering completion is
    already enriched), else ``None`` — when warming is skipped (caching
    disabled, cooldown active after a recent failure, another process already
    collecting) or the collection times out / fails. Never raises: completion
    must degrade to the static floor, never traceback into the shell.
    """
    if not repos:
        return None
    cache_path = _cache_path()
    if cache_path is None:
        return None
    try:
        return _warm_collected_tests(repos, cache_path)
    except Exception:  # noqa: BLE001 — completion must never raise into the shell
        return None


def _warm_collected_tests(repos: list["Repo"], cache_path: Path) -> list[str] | None:
    """Cooldown-gated, lock-guarded body of :func:`maybe_warm_collected_tests`."""
    entry = _collected_cache_entry(repos)
    if entry is not None:
        at = entry.get("generated_at")
        # read_collected_tests already returned a *fresh success* upstream, so a
        # recent timestamp here means a recent failure → cooldown, skip.
        if isinstance(at, (int, float)) and time.time() - at <= COLLECT_COOLDOWN_SECONDS:
            return None
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    lock = cache_path.parent / COLLECT_LOCK_FILENAME
    if not _acquire_collect_lock(lock):
        return None
    try:
        names = _run_collect_subprocess()
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()
    with contextlib.suppress(OSError):
        _record_collected_tests(repos, names)  # names=None stamps the cooldown
    return names


# ---------------------------------------------------------------------------
# Dynamic tunnel-id namespace, for `otto tunnel remove <id>` completion
# ---------------------------------------------------------------------------
#
# Like COLLECTED_TESTS_KEY above, this lives under its own reserved top-level
# key rather than inside a fingerprint entry: live tunnel state is discovered
# by process/argv inspection, not by anything the fingerprint's file-mtime
# hashing tracks, and it must never clobber (or be clobbered by) the main
# fingerprint entries. The TTL is intentionally short — tunnels come and go
# independently of otto invocations, so a stale id list is wrong far sooner
# than the main cache's config-derived data would be.
DYNAMIC_TUNNELS_KEY = "__dynamic_tunnels__"
DYNAMIC_TUNNELS_SCHEMA_VERSION = 1
DYNAMIC_TUNNELS_TTL_SECONDS = 120  # tunnel state is volatile; short TTL (spec §11.2)


def record_tunnel_ids(repos: list["Repo"], ids: list[str]) -> None:
    """Cache the freshly-discovered tunnel ids for ``remove <id>`` completion."""
    if not repos:
        return
    cache_path = _cache_path()
    if cache_path is None:
        return
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if cache_path.is_file():
        try:
            loaded = json.loads(cache_path.read_text())
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            pass
    namespace = existing.get(DYNAMIC_TUNNELS_KEY)
    if not isinstance(namespace, dict):
        namespace = {}
    namespace[compute_fingerprint(repos)] = {
        "schema_version": DYNAMIC_TUNNELS_SCHEMA_VERSION,
        "generated_at": int(time.time()),
        "ids": list(ids),
    }
    existing[DYNAMIC_TUNNELS_KEY] = namespace
    _atomic_write_json(cache_path, existing)


def read_tunnel_ids(repos: list["Repo"]) -> list[str] | None:
    """Fresh cached tunnel ids, or ``None`` (cold / expired / malformed)."""
    if not repos:
        return None
    cache_path = _cache_path()
    if cache_path is None or not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    namespace = data.get(DYNAMIC_TUNNELS_KEY) if isinstance(data, dict) else None
    entry = namespace.get(compute_fingerprint(repos)) if isinstance(namespace, dict) else None
    if not isinstance(entry, dict) or entry.get("schema_version") != DYNAMIC_TUNNELS_SCHEMA_VERSION:
        return None
    generated_at = entry.get("generated_at")
    if not isinstance(generated_at, (int, float)):
        return None
    if time.time() - generated_at > DYNAMIC_TUNNELS_TTL_SECONDS:
        return None
    ids = entry.get("ids")
    return ids if isinstance(ids, list) else None
