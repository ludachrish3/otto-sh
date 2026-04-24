"""Shell-completion cache (Phase B).

Tab completion invokes ``otto`` just far enough to walk the Typer command
tree. The expensive step during that walk is not parsing CLI args — it's the
side effects in :mod:`otto.configmodule` that populate dynamic subcommands:

- :meth:`Repo.importInitModules` — imports every user-defined instruction
  module so ``@run_app.command`` decorators can attach to ``run_app``.
- :meth:`Repo.importTestFiles` — exec's every ``test_*.py`` so
  ``@register_suite()`` decorators can populate ``_SUITE_REGISTRY``.

Both execute arbitrary user code. For completion all we actually need is the
*names* those decorators would register and the *option schemas* the user can
tab-complete against. This module captures both in a small JSON file and,
when the cache is valid, lets the caller skip the user code entirely.

Cache location
--------------

``$OTTO_XDIR/.otto/completion_cache.json``. If ``OTTO_XDIR`` is not set,
caching is disabled and completion always falls through to the slow path.

Cache schema (version 3)
------------------------

Single flat map, keyed by fingerprint hex digest. Each entry records both the
schema version and the wall-clock time it was generated so a reader can drop
stale entries without trusting the mtimes on-disk::

    {
        "<fingerprint>": {
            "schema_version": 3,
            "generated_at": 1745000000,
            "instructions": [
                {"name": "install",
                 "options": [{"name": "debug", "flags": ["--field/--debug"],
                              "kind": "bool", "default": false, "help": "..."},
                             ...]},
                ...
            ],
            "suites": [
                {"name": "TestDevice", "options": [...]},
                ...
            ],
            "hosts": ["carrot_seed", "tomato_seed", ...]
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
from __future__ import annotations

import hashlib
import inspect
import json
import os
import tempfile
import time
import types
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Union, get_args, get_origin

from ..logger import getOttoLogger

if TYPE_CHECKING:
    from .repo import Repo


COMPLETION_ENV_VAR = '_OTTO_COMPLETE'
XDIR_ENV_VAR = 'OTTO_XDIR'
CACHE_FILENAME = 'completion_cache.json'

# Bump when the on-disk schema changes in a way older readers can't parse.
SCHEMA_VERSION = 3

HOSTS_FILENAME = 'hosts.json'

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
    str: 'str',
    int: 'int',
    float: 'float',
    bool: 'bool',
    Path: 'path',
}
_KIND_TO_TYPE: dict[str, Any] = {v: k for k, v in _TYPE_TO_KIND.items()}


def is_completion_mode() -> bool:
    """True when otto is being invoked by shell completion."""
    return bool(os.environ.get(COMPLETION_ENV_VAR))


def _cache_path() -> Path | None:
    """Return the cache file path, or ``None`` when caching is disabled.

    Caching requires ``OTTO_XDIR`` to be set. Without it we can't pick a
    stable per-user location, so we skip caching entirely and fall back to
    the slow path every time.
    """
    xdir = os.environ.get(XDIR_ENV_VAR)
    if not xdir:
        return None
    return Path(xdir) / '.otto' / CACHE_FILENAME


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
        return True
    except OSError:
        return False


def _hash_file(h: 'hashlib._Hash', path: Path) -> None:
    try:
        st = path.stat()
    except OSError:
        h.update(f'missing:{path}\n'.encode())
        return
    h.update(f'{path}|{st.st_mtime_ns}|{st.st_size}\n'.encode())


def compute_fingerprint(repos: list['Repo']) -> str:
    """Stat-based sha256 of every file that contributes instruction/suite names."""
    h = hashlib.sha256()
    for repo in sorted(repos, key=lambda r: str(r.sutDir)):
        _hash_file(h, repo.sutDir / '.otto' / 'settings.toml')

        # Init-module files: resolve each `init` name under the configured
        # `libs` directories. Either a package directory or a plain .py file.
        for init_mod in repo.init:
            mod_base = init_mod.split('.')[0]
            resolved = False
            for lib in repo.libs:
                mod_dir = lib / mod_base
                mod_file = lib / f'{mod_base}.py'
                if mod_dir.is_dir():
                    for py in sorted(mod_dir.rglob('*.py')):
                        _hash_file(h, py)
                    resolved = True
                elif mod_file.is_file():
                    _hash_file(h, mod_file)
                    resolved = True
            if not resolved:
                h.update(f'unresolved:{init_mod}\n'.encode())

        for test_dir in repo.tests:
            if test_dir.is_dir():
                for t in sorted(test_dir.glob('test_*.py')):
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
        return 'str_list'
    return None


def _extract_flags(option_info: Any) -> list[str]:
    """Return the user-authored flag strings from a ``typer.Option`` instance.

    Typer stores the first positional flag as the info's ``default`` attribute
    and the rest in ``param_decls``; concatenate them in declaration order so
    the rebuilder reproduces the original call.
    """
    flags: list[str] = []
    primary = getattr(option_info, 'default', None)
    if isinstance(primary, str) and (primary.startswith('-') or '/' in primary):
        flags.append(primary)
    flags.extend(getattr(option_info, 'param_decls', ()) or ())
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
            return default
        except TypeError:
            return None
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
    log = getOttoLogger()
    try:
        sig = inspect.signature(callback)
    except (TypeError, ValueError) as e:  # pragma: no cover — paranoia
        log.debug(
            f'completion-cache: skipping {command_name!r}, '
            f'signature inspection failed: {e!r}',
        )
        return None

    options: list[dict[str, Any]] = []
    for pname, param in sig.parameters.items():
        ann = param.annotation
        if get_origin(ann) is not Annotated:
            log.debug(
                f'completion-cache: skipping option {command_name}.{pname!r} — '
                f'annotation {ann!r} is not Annotated[...]',
            )
            return None
        args = get_args(ann)
        base = args[0]
        # OptionInfo lives at module path typer.models.OptionInfo; match on
        # attribute shape to avoid importing typer at module load.
        meta = next(
            (a for a in args[1:] if hasattr(a, 'param_decls')),
            None,
        )
        if meta is None:
            log.debug(
                f'completion-cache: skipping option {command_name}.{pname!r} — '
                f'no typer.Option metadata in annotation',
            )
            return None

        kind = _type_to_kind(base)
        if kind is None:
            log.debug(
                f'completion-cache: skipping option {command_name}.{pname!r} — '
                f'unsupported annotation type {base!r}',
            )
            return None

        options.append({
            'name': pname,
            'flags': _extract_flags(meta),
            'kind': kind,
            'default': _json_safe_default(param.default),
            'help': getattr(meta, 'help', None) or '',
        })
    return options


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

def read_cache(repos: list['Repo']) -> dict[str, Any] | None:
    """Return the cached command lists for the current fingerprint, or ``None``.

    ``None`` means any of: caching disabled, empty repos (would produce the
    empty-sha256 fingerprint and poison the cache for other shells), cache
    file missing, cache file corrupt, fingerprint mismatch, schema mismatch,
    or TTL expired. In every case the caller should fall back to the slow
    path.

    On success returns a dict with ``instructions``, ``suites``, and
    ``hosts`` keys. The first two are lists of ``{"name": str,
    "options": [...]}`` dicts; ``hosts`` is a plain list of host-ID strings.
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
    if entry.get('schema_version') != SCHEMA_VERSION:
        return None
    generated_at = entry.get('generated_at')
    if not isinstance(generated_at, (int, float)):
        return None
    if time.time() - generated_at > CACHE_TTL_SECONDS:
        return None
    instructions = entry.get('instructions')
    suites = entry.get('suites')
    hosts = entry.get('hosts')
    if (not isinstance(instructions, list)
            or not isinstance(suites, list)
            or not isinstance(hosts, list)):
        return None
    return {'instructions': instructions, 'suites': suites, 'hosts': hosts}


def write_cache(
    repos: list['Repo'],
    instructions: list[dict[str, Any]],
    suites: list[dict[str, Any]],
    hosts: list[str],
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

    existing: dict = {}
    if cache_path.is_file():
        try:
            loaded = json.loads(cache_path.read_text())
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            pass

    fingerprint = compute_fingerprint(repos)
    existing[fingerprint] = {
        'schema_version': SCHEMA_VERSION,
        'generated_at': int(time.time()),
        'instructions': instructions,
        'suites': suites,
        'hosts': hosts,
    }

    tmp = tempfile.NamedTemporaryFile(
        mode='w',
        dir=cache_path.parent,
        delete=False,
        prefix='.completion_cache_',
        suffix='.tmp',
    )
    try:
        json.dump(existing, tmp)
        tmp.close()
        os.replace(tmp.name, cache_path)
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Live-registry introspection (writer side)
# ---------------------------------------------------------------------------

def collect_current_commands() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read the currently-registered instructions and suites with options.

    Must be called after :func:`applyRepoSettings` has finished populating
    ``run_app`` and ``_SUITE_REGISTRY``. Returns empty lists for any source
    that hasn't been loaded (e.g. no init modules → ``otto.cli.run`` never
    imported → no instructions).

    Each item is ``{"name": str, "options": list[dict]}``; a command whose
    options can't be fully serialized is cached with ``options: []`` so
    the name still completes even though the per-option flags don't.
    """
    import sys

    instructions: list[dict[str, Any]] = []
    run_mod = sys.modules.get('otto.cli.run')
    if run_mod is not None:
        for group in run_mod.run_app.registered_groups:
            for cmd in group.typer_instance.registered_commands:
                name = cmd.name
                if name is None and cmd.callback is not None:
                    name = cmd.callback.__name__.replace('_', '-')
                if not name:
                    continue
                options = _serialize_options(cmd.callback, command_name=name)
                instructions.append({
                    'name': name,
                    'options': options if options is not None else [],
                })

    suites: list[dict[str, Any]] = []
    try:
        from ..suite.register import _SUITE_REGISTRY
    except ImportError:
        _SUITE_REGISTRY = []  # type: ignore[assignment]
    for name, sub_app in _SUITE_REGISTRY:
        callback = None
        if sub_app.registered_commands:
            callback = sub_app.registered_commands[0].callback
        options = (
            _serialize_options(callback, command_name=name)
            if callback is not None
            else None
        )
        suites.append({
            'name': name,
            'options': options if options is not None else [],
        })

    return instructions, suites


def collect_host_ids(repos: list['Repo']) -> list[str]:
    """Enumerate every host ID reachable via the configured lab search paths.

    Reads each repo's ``labs`` directories for a ``hosts.json`` file and
    builds :class:`RemoteHost` objects via the existing factory so the
    resulting IDs match what ``get_host`` will look up at runtime. Runs
    without an initialized ConfigModule, so it's safe to call from the
    completion fast path as well as the cache writer on the slow path.

    Returns a sorted, de-duplicated list. Malformed files / entries are
    silently skipped — completion must never crash on bad user data.
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
                try:
                    validate_host_dict(host_data)
                    host = create_host_from_dict(host_data)
                except (ValueError, TypeError):
                    continue
                ids.add(host.id)
    return sorted(ids)
