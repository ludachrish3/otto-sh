"""The ``[coverage]`` table in ``.otto/settings.toml``: which repo declares it, and its selector.

Pure helpers over the already-parsed repo list: which repo (if any) declared
a ``[coverage]`` section, and what that section's raw settings dict looks
like. Every coverage code path — ``otto test --cov``, ``otto cov``, and the
coverage awareness behind :attr:`otto.context.OttoContext.cov` — resolves
its coverage settings through :func:`has_cov_config`, :func:`get_cov_repo`
and :func:`get_cov_config`, so a lab with multiple SUT repos always picks the
same one. :func:`load_hosts_pattern` compiles that section's optional
``hosts`` host-id selector, refusing a malformed value by name.

Lives in ``otto.config``, beneath ``otto.coverage``, because reading a
settings table is configuration, not collection: the context layer needs the
answer too, and it may not depend on the coverage pipeline.
"""

import re
from typing import TYPE_CHECKING, Any

from ..errors import OttoError

if TYPE_CHECKING:
    from .repo import Repo


class CoverageConfigError(OttoError, ValueError):
    """No ``[coverage]`` section is configured for the resolved repo(s).

    Raised by ``otto.coverage.collect.collect_coverage`` before any fetch is
    attempted: with no ``[coverage]`` section there is nothing to resolve a
    host selector, a product's ``cov_dir``, or a tier against.

    Also raised at capture and report time by
    ``otto.coverage.tree.iter_product_dirs`` for a cov directory whose shape
    is wrong — a host directory holding counters or a ``capture.json``
    directly instead of the ``cov/<host>/<product>/`` tree this version of
    otto stages — since that too is a configuration the pipeline cannot run
    against, and there is no migration shim for it.
    """


def has_cov_config(cov: dict[str, Any]) -> bool:
    """Return True when the repo actually declared coverage settings."""
    return bool(cov.get("embedded") or cov.get("tiers") or cov.get("hosts"))


def get_cov_repo(repos: "list[Repo]") -> "Repo | None":
    """Return the first repo with a ``[coverage]`` section in its settings."""
    for repo in repos:
        if has_cov_config(repo.settings.get("coverage") or {}):
            return repo
    return None


def get_cov_config(repos: "list[Repo]") -> dict[str, Any]:
    """Extract the ``[coverage]`` config from the first repo that has one."""
    repo = get_cov_repo(repos)
    return repo.settings["coverage"] if repo else {}


def load_hosts_pattern(cov_config: dict[str, Any]) -> "re.Pattern[str] | None":
    """Compile the optional ``[coverage].hosts`` host-id selector.

    ``None`` when the key is absent — every lab host is a coverage target.
    The value comes straight out of settings.toml, so a wrong shape is refused
    by name rather than with ``re.compile``'s bare TypeError. The empty string
    is refused too: it *looks* like a selector but would fall through as "no
    selector", silently fanning coverage (and ``otto cov clean``'s deletes)
    out to every host — including the SSH hop the selector exists to exclude.

    Raises:
        CoverageConfigError: On a non-string or empty ``hosts`` value.
    """
    raw = cov_config.get("hosts")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise CoverageConfigError(
            f"[coverage] hosts must be a string (a host-id regex), got {type(raw).__name__}"
        )
    if not raw:
        raise CoverageConfigError(
            "[coverage] hosts must not be empty — omit the key to select every host"
        )
    return re.compile(raw)
