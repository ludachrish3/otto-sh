"""Shell-completion cache (Phase B).

Tab completion invokes ``otto`` just far enough to walk the Typer command
tree. The expensive step during that walk is not parsing CLI args — it's the
side effects in :mod:`otto.configmodule` that populate dynamic subcommands:

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

Cache schema (version 8)
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
            "docker_hosts": ["carrot_seed", ...],
            "term_backends": ["ssh", "telnet", ...],
            "transfer_backends": [{"name": "scp", "host_families": ["unix"]}, ...],
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

Fingerprint
-----------

sha256 over ``(path, mtime_ns, size)`` triples for every file whose change
would alter the registered name sets: each SUT's ``settings.toml``, every
``.py`` file under any ``init`` module, every ``test_*.py`` in a configured
``tests`` directory, and every ``hosts.json`` under a configured ``labs``
search path. File contents are never read, so the fingerprint is cheap to
compute even when SUTs are large.

A stale fingerprint is always safe: the fast path is skipped, the slow path
runs as normal and rewrites the cache afterward.
"""

import contextlib
import hashlib
import inspect
import json
import os
import tempfile
import time
import types
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Union, get_args, get_origin

from ..logger import get_logger

if TYPE_CHECKING:
    from .repo import Repo


COMPLETION_ENV_VAR = "_OTTO_COMPLETE"
CACHE_FILENAME = "completion_cache.json"

# Bump when the on-disk schema changes in a way older readers can't parse.
SCHEMA_VERSION = 8

HOSTS_FILENAME = "hosts.json"

# Cache entries older than this (seconds) are treated as a miss. Forces the
# slow path to run periodically so annotation / option changes that don't
# move any tracked file's mtime still eventually refresh.
CACHE_TTL_SECONDS = 24 * 60 * 60


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
    # Function-local import: this module is loaded early during configmodule
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

        # Host-ID sources: hosts.json under each configured lab search path.
        # Adding these to the fingerprint lets the cache self-invalidate on
        # edits. (Future DB-backed sources will need a different staleness
        # signal — likely a pure TTL or DB revision token.)
        for lab_path in repo.labs:
            _hash_file(h, lab_path / HOSTS_FILENAME)

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
    log = get_logger()
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
    docker_hosts = entry.get("docker_hosts", [])
    term_backends = entry.get("term_backends", [])
    transfer_backends = entry.get("transfer_backends", [])
    usernames = entry.get("usernames", [])
    commands = entry.get("commands", [])
    if (
        not isinstance(instructions, list)
        or not isinstance(suites, list)
        or not isinstance(hosts, list)
        or not isinstance(docker_hosts, list)
        or not isinstance(term_backends, list)
        or not isinstance(transfer_backends, list)
        or not isinstance(usernames, list)
        or not isinstance(commands, list)
    ):
        return None
    return {
        "instructions": instructions,
        "suites": suites,
        "hosts": hosts,
        "docker_hosts": docker_hosts,
        "term_backends": term_backends,
        "transfer_backends": transfer_backends,
        "usernames": usernames,
        "commands": commands,
    }


def write_cache(
    repos: list["Repo"],
    instructions: list[dict[str, Any]],
    suites: list[dict[str, Any]],
    hosts: list[str],
    docker_hosts: list[str] | None = None,
    term_backends: list[str] | None = None,
    transfer_backends: list[dict[str, Any]] | None = None,
    usernames: list[str] | None = None,
    commands: list[dict[str, Any]] | None = None,
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
        "docker_hosts": docker_hosts or [],
        "term_backends": term_backends or [],
        "transfer_backends": transfer_backends or [],
        "usernames": usernames or [],
        "commands": commands or [],
    }

    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=cache_path.parent,
        delete=False,
        prefix=".completion_cache_",
        suffix=".tmp",
    ) as tmp:
        tmp_name = tmp.name
        json.dump(existing, tmp)
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
    :func:`otto.configmodule.completion_stubs.build_stub_command` on the fast
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

    log = get_logger()
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


def collect_docker_capable_host_ids(repos: list["Repo"]) -> list[str]:
    """Enumerate host IDs whose ``hosts.json`` entry has ``docker_capable: true``.

    Used as the completion source for ``otto docker --on <TAB>`` and any
    other surface that should be limited to docker-capable parents.
    Mirrors :func:`collect_host_ids` (no ConfigModule needed; safe in the
    completion fast path).
    """
    from ..storage.factory import create_host_from_dict, validate_host_dict

    ids: set[str] = set()
    for repo in repos:
        for lab_path in repo.labs:
            hosts_file = lab_path / HOSTS_FILENAME
            if not hosts_file.is_file():
                continue
            try:
                data = json.loads(hosts_file.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, list):
                continue
            for host_data in data:
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


def collect_host_ids(repos: list["Repo"]) -> list[str]:
    """Enumerate every host ID reachable via the configured lab search paths.

    Reads each repo's ``labs`` directories for a ``hosts.json`` file and
    builds :class:`UnixHost` objects via the existing factory so the
    resulting IDs match what ``get_host`` will look up at runtime. Also
    synthesizes container host IDs of the form ``<parent>.<project>.<service>``
    from each repo's ``[docker]`` settings so declared container hosts
    are tab-completable before they're actually brought up.

    Runs without an initialized ConfigModule, so it's safe to call from
    the completion fast path as well as the cache writer on the slow path.

    Returns a sorted, de-duplicated list. Malformed files / entries are
    silently skipped — completion must never crash on bad user data.
    """
    from ..host.builtin_hosts import builtin_host_ids
    from ..storage.factory import create_host_from_dict, validate_host_dict

    # Seed with the built-in hosts otto injects into every lab (e.g. `local`) so
    # they are tab-completable in every repo, mirroring load_lab's injection.
    ids: set[str] = set(builtin_host_ids())
    for repo in repos:
        # Map of host_id -> docker_capable flag, scoped to this repo's labs.
        # Populated as we walk hosts.json so we can synthesize container
        # ids in the same pass.
        docker_capable_ids: list[str] = []
        for lab_path in repo.labs:
            hosts_file = lab_path / HOSTS_FILENAME
            if not hosts_file.is_file():
                continue
            try:
                data = json.loads(hosts_file.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, list):
                continue
            for host_data in data:
                if not isinstance(host_data, dict):
                    continue
                try:
                    validate_host_dict(host_data)
                    host = create_host_from_dict(host_data)
                except (ValueError, TypeError):
                    continue
                ids.add(host.id)
                if getattr(host, "docker_capable", False):
                    docker_capable_ids.append(host.id)

        docker = getattr(repo, "docker_settings", None)
        if docker is None or not docker.composes:
            continue
        for compose in docker.composes:
            # Pick parents to enumerate against. Prefer an explicit
            # default_host; otherwise enumerate every docker-capable host
            # in this repo's labs (pessimistic but stable; the actual
            # bring-up picks one).
            parents = [compose.default_host] if compose.default_host else list(docker_capable_ids)
            for parent in parents:
                for service in compose.services:
                    ids.add(f"{parent}.{repo.name}.{service}".lower())
    return sorted(ids)
