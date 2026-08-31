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

from ..registry import Registry, get_registering_repo


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

COMPOSE_ADAPTERS: "Registry[ComposeAdapter]" = Registry(
    "compose adapter",
    register_hint="otto.docker.register_compose_adapter()",
    collision_hint="One adapter per (repo, use-case); merge the logic into it.",
)


def register_compose_adapter(use_case: str) -> "Callable[[ComposeAdapter], ComposeAdapter]":
    """Register the decorated callable as the calling repo's adapter for *use_case*.

    Call from an init module listed in ``.otto/settings.toml`` ``[init]`` —
    that import is what attributes the adapter to its repo, exactly like
    :func:`otto.project.actions.register_project_actions`.

    Raises:
        ValueError: If called outside a repo's init import, if *use_case*
            contains ``':'`` (the registry key separator), or if this repo
            already registered an adapter for *use_case*.
    """
    if ":" in use_case:
        raise ValueError(
            f"register_compose_adapter(): use_case {use_case!r} must not contain "
            "':' — it is joined with the repo name as 'repo_name:use_case' to key "
            "the adapter registry, and a colon in either half would let two "
            "distinct (repo, use_case) pairs collide on one key."
        )
    repo_name = get_registering_repo()
    if repo_name is None:
        raise ValueError(
            "register_compose_adapter() must be called from a repo init module "
            "(listed in .otto/settings.toml [init]) — that import is what "
            "attributes the adapter to its repo."
        )

    def _register(fn: ComposeAdapter) -> ComposeAdapter:
        COMPOSE_ADAPTERS.register(f"{repo_name}:{use_case}", fn, origin=fn.__module__)
        return fn

    return _register


def adapter_for(repo_name: str, use_case: str) -> "ComposeAdapter | None":
    """Return the adapter *repo_name* registered for *use_case*, or None."""
    key = f"{repo_name}:{use_case}"
    return COMPOSE_ADAPTERS.get(key) if key in COMPOSE_ADAPTERS else None
