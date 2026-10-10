"""Repo-registered compose adapters (spec §7).

The registration line is the ONLY otto touchpoint: the registered callable
receives a plain-data facts dict and returns an :class:`AdapterResult`, so
everything beneath it can be the product's own, otto-free templating code.
Adapters must be pure with respect to devices — no host access; they run
under ``--dry-run`` (that is what makes the full plan printable). They may
write inside ``facts["scratch_dir"]``.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from ..registry import (
    Justified,
    Registry,
    RequireRepo,
    get_registering_repo,
    registration_boundary,
)


@dataclass
class AdapterResult:
    """What a compose adapter hands back to otto (spec §7)."""

    files: "dict[str, str]" = field(default_factory=dict)
    """Compose handle -> replacement text. Omitted handles ship verbatim."""

    env: "dict[str, str]" = field(default_factory=dict)
    """Channel-2 env; merges over the fragment static tables."""

    extra_files: "dict[str, str]" = field(default_factory=dict)
    """Relative name -> text, staged beside the compose files.

    These are the ``env_file:`` sidecars an adapter generates rather than the
    repo committing them.
    """


ComposeAdapter = Callable[[dict[str, object]], AdapterResult]


@dataclass(frozen=True)
class ComposeAdapterEntry:
    """One repo's adapter for one use case, keyed ``"<repo>:<use_case>"``."""

    use_case: str
    """The use case the adapter serves; never contains ``':'``."""

    fn: ComposeAdapter
    """The adapter, which takes plain-data facts and returns an :class:`AdapterResult`."""


def _check_adapter_entry(name: str, entry: ComposeAdapterEntry, _proposed: object) -> None:
    """Refuse a key that is not the registering repo joined with the record's use case."""
    if ":" in entry.use_case or name != f"{get_registering_repo()}:{entry.use_case}":
        raise ValueError(
            f"compose adapter key {name!r} does not match its repo and use case {entry.use_case!r}"
        )


COMPOSE_ADAPTERS: "Registry[ComposeAdapterEntry]" = Registry(
    "compose adapter",
    entry=ComposeAdapterEntry,
    register_hint="otto.docker.register_compose_adapter()",
    validate=_check_adapter_entry,
    capabilities=[
        Justified(
            RequireRepo(), reason="one adapter per (repo, use case); the repo is half the key"
        ),
    ],
)
"""Each repo's compose adapters, keyed ``"<repo>:<use_case>"``."""


def register_compose_adapter(
    use_case: str, *, overwrite: bool = False
) -> "Callable[[ComposeAdapter], ComposeAdapter]":
    """Register the decorated callable as the calling repo's adapter for *use_case*.

    Call from an init module listed in ``.otto/settings.toml`` ``[init]`` —
    that import is what attributes the adapter to its repo, exactly like
    :func:`otto.project.actions.register_project_actions`.

    *overwrite* replaces this repo's existing adapter for *use_case*
    deliberately; by default a second one raises.

    Raises:
        ValueError: If *use_case* contains ``':'`` (the registry key
            separator).
        otto.registry.RegistrationRefused: If called outside a repo's init
            import.
        otto.registry.DuplicateRegistration: If this repo already registered
            an adapter for *use_case* and *overwrite* is false.
    """
    if ":" in use_case:
        raise ValueError(
            f"register_compose_adapter(): use_case {use_case!r} must not contain "
            "':' — it is joined with the repo name as 'repo_name:use_case' to key "
            "the adapter registry, and a colon in either half would let two "
            "distinct (repo, use_case) pairs collide on one key."
        )

    @registration_boundary
    def _register(fn: ComposeAdapter) -> ComposeAdapter:
        repo = get_registering_repo()
        # Outside an init import the engine refuses before the key is checked,
        # and its message names the bare use case rather than a "None:" key.
        key = f"{repo}:{use_case}" if repo is not None else use_case
        COMPOSE_ADAPTERS.register(key, ComposeAdapterEntry(use_case, fn), overwrite=overwrite)
        return fn

    return _register


def adapter_for(repo_name: str, use_case: str) -> "ComposeAdapter | None":
    """Return the adapter *repo_name* registered for *use_case*, or None."""
    key = f"{repo_name}:{use_case}"
    return COMPOSE_ADAPTERS.get(key).fn if key in COMPOSE_ADAPTERS else None
