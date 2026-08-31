"""Pydantic boundary specs for ``.otto/settings.toml`` and the ``OTTO_*`` env.

These validate the settings dict (``extra='forbid'``) and build the **unchanged**
runtime objects (``DockerSettings``/``DockerImage``/``DockerCompose`` frozen
dataclasses, ``OsProfile``, the reservation backend) via ``to_runtime()`` — the
same two-type split the option/host specs use.

Leaf isolation: this module must NOT import from ``otto.config`` at module
top — doing so triggers ``config/__init__``'s app bootstrap. Runtime types
from ``config.repo`` are imported lazily inside ``to_runtime()`` and under
``TYPE_CHECKING`` for annotations only.
"""

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# anchor_path lives in ..utils (stdlib-only, imports nothing from otto) so
# the runtime readers that need it (coverage, reservations) never have to
# import this pydantic-heavy module just to anchor a path — see the
# import-budget guard.
# otto.logger.levels is stdlib-only and is imported eagerly by otto.logger's
# package __init__ anyway; it costs this module nothing (no rich — `management`
# is a lazy PEP 562 export) and it is what keeps the accepted level names below
# from drifting out of the module that registers them.
from ..logger.levels import LEVEL_ALIASES
from ..utils import anchor_path
from .base import OttoModel
from .color import validate_color
from .dependencies import clauses_satisfiable, normalize_name, parse_dependency_entry
from .inventory import parse_cache_ttl
from .options import (
    FtpOptionsSpec,
    NcOptionsSpec,
    ScpOptionsSpec,
    SftpOptionsSpec,
    SshOptionsSpec,
    TelnetOptionsSpec,
    UserlandOptionsSpec,
)

if TYPE_CHECKING:
    from ..config.repo import (
        DockerCompose,
        DockerImage,
        DockerSettings,
        DockerUseCase,
        MonitorSettings,
    )


def anchor_to_repo(v: Path, info: ValidationInfo) -> Path:
    """Expand ``~``, then anchor a still-relative path to the repo root.

    ``settings.toml`` is committed and shared team-wide, so a CWD-relative
    value in it can never resolve stably. Absolute paths (including
    ``~``-rooted ones, already expanded here) pass through untouched.

    The repo root arrives via pydantic's validation context, which
    ``Repo.parse_settings`` supplies as ``{"sut_dir": ...}``. With no
    context the path is expanded but left relative so ``SettingsModel`` stays
    independently validatable.

    Deliberately does not ``resolve()``: that would collapse symlinks and
    change path identity for repos reached through symlinked checkouts.
    """
    sut_dir = (info.context or {}).get("sut_dir")
    if sut_dir is None:
        return v.expanduser()
    return anchor_path(v, Path(sut_dir))


RepoPath = Annotated[Path, AfterValidator(anchor_to_repo)]
"""A ``settings.toml`` path: ``~``-expanded, then anchored to the repo root."""


class DockerImageSpec(OttoModel):
    """Boundary spec for a ``[[docker.images]]`` entry in ``settings.toml``.

    Validates the image name, Dockerfile path, build context, optional build stage target,
    and ``build_args`` dict (scalar TOML values are accepted and stringified). Builds a
    ``DockerImage`` runtime dataclass via ``to_runtime()``, with ``build_args`` normalised
    to a sorted, frozen tuple-of-pairs for hashability.
    """

    name: str
    dockerfile: RepoPath
    context: RepoPath
    target: str | None = None
    # dict[str, Any] (not dict[str, str]) for parity with the old TOML parser:
    # a build arg written as a bare scalar (``PORT = 8080``) stays accepted and
    # is stringified below, rather than rejected at validation.
    build_args: dict[str, Any] = Field(default_factory=dict)

    def to_runtime(self) -> "DockerImage":
        """Build the ``DockerImage`` runtime dataclass from the validated spec fields."""
        from ..config.repo import DockerImage

        return DockerImage(
            name=self.name,
            dockerfile=self.dockerfile,
            context=self.context,
            target=self.target,
            # frozen, sorted, all-string tuple-of-tuples so the runtime object
            # stays hashable and order-stable for the docker context hash;
            # ``str(v)`` coerces TOML scalars (ints/bools) like the old parser.
            build_args=tuple((k, str(v)) for k, v in sorted(self.build_args.items())),
        )


class DockerComposeSpec(OttoModel):
    """Boundary spec for a ``[[docker.composes]]`` entry in ``settings.toml``.

    Validates the Compose file path and the list of services within the
    Compose project — a pure file inventory (spec §14): placement lives on
    ``[[docker.use_cases]]`` fragments, never here. Builds a ``DockerCompose``
    runtime dataclass via ``to_runtime()``. ``name`` is the handle
    ``[[docker.use_cases]]`` entries reference; defaults to the path stem.
    """

    name: str | None = None
    path: RepoPath
    services: tuple[str, ...] = ()

    @property
    def effective_name(self) -> str:
        """The handle this compose is known by -- ``name``, or the path stem when unset."""
        return self.name or self.path.stem

    def to_runtime(self) -> "DockerCompose":
        """Build the ``DockerCompose`` runtime dataclass from the validated spec fields."""
        from ..config.repo import DockerCompose

        return DockerCompose(
            name=self.effective_name,
            path=self.path,
            services=self.services,
        )


class DockerUseCaseSpec(OttoModel):
    """Boundary spec for a ``[[docker.use_cases]]`` fragment (spec §3.1).

    A fragment is the atomic unit of participation and placement. Same-named
    fragments across repos form one use-case; ``provides``/``priority`` enter
    the provider competition (spec §4). ``env`` accepts scalar TOML values and
    stringifies them, like ``build_args``.
    """

    name: str
    composes: list[str] = Field(min_length=1)
    role: str | None = None
    placement: dict[str, str] = Field(default_factory=dict)
    provides: str | None = None
    priority: int = 0
    env: dict[str, Any] = Field(default_factory=dict)
    pass_env: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _priority_requires_provides(self) -> "DockerUseCaseSpec":
        if self.priority != 0 and self.provides is None:
            raise ValueError(
                f"use_case {self.name!r}: priority is meaningful only on provider "
                f"fragments — set provides, or drop priority."
            )
        return self

    def to_runtime(self) -> "DockerUseCase":
        """Build the ``DockerUseCase`` runtime dataclass from the validated spec fields."""
        from ..config.repo import DockerUseCase

        return DockerUseCase(
            name=self.name,
            composes=tuple(self.composes),
            role=self.role,
            placement=dict(self.placement),
            provides=self.provides,
            priority=self.priority,
            env={k: str(v) for k, v in self.env.items()},
            pass_env=tuple(self.pass_env),
        )


class DockerSettingsSpec(OttoModel):
    """Boundary spec for the ``[docker]`` section of ``settings.toml``.

    Validates the Docker registry URL and the lists of image and Compose specs.
    Builds a ``DockerSettings`` runtime dataclass (with images and composes as
    frozen tuples) via ``to_runtime()``.
    """

    registry_url: str = "docker.io"
    images: list[DockerImageSpec] = Field(default_factory=list)
    composes: list[DockerComposeSpec] = Field(default_factory=list)
    use_cases: list[DockerUseCaseSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _handles_resolve(self) -> "DockerSettingsSpec":
        names = [c.effective_name for c in self.composes]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"[[docker.composes]] handles must be unique; duplicated: {dupes}")
        known = set(names)
        for uc in self.use_cases:
            missing = [h for h in uc.composes if h not in known]
            if missing:
                raise ValueError(
                    f"use_case {uc.name!r} references unknown compose handle(s) "
                    f"{missing}; declared: {sorted(known)}"
                )
        return self

    def to_runtime(self) -> "DockerSettings":
        """Build the ``DockerSettings`` runtime dataclass from the validated spec fields."""
        from ..config.repo import DockerSettings

        return DockerSettings(
            registry_url=self.registry_url,
            images=tuple(i.to_runtime() for i in self.images),
            composes=tuple(c.to_runtime() for c in self.composes),
            use_cases=tuple(u.to_runtime() for u in self.use_cases),
        )


class MonitorSettingsSpec(OttoModel):
    """Boundary spec for the ``[monitor]`` section of ``settings.toml``.

    TLS for the dashboard server. Paths follow the settings-wide convention
    (``RepoPath``): ``~``-expanded, then anchored to the repo root if still
    relative. The committed value is shared by the whole team, so it
    conventionally points under ``~/.otto/tls/`` — identical text,
    per-user resolution. ``tls_key`` without ``tls_cert`` is rejected;
    ``tls_cert`` alone is fine (bundled PEM).
    """

    tls_cert: RepoPath | None = None
    tls_key: RepoPath | None = None

    @model_validator(mode="after")
    def _key_requires_cert(self) -> "MonitorSettingsSpec":
        if self.tls_key is not None and self.tls_cert is None:
            raise ValueError(
                "[monitor] tls_key is set but tls_cert is not — set tls_cert "
                "(it may be a combined PEM, making tls_key unnecessary)."
            )
        return self

    def to_runtime(self) -> "MonitorSettings":
        """Build the ``MonitorSettings`` runtime dataclass from the validated spec fields."""
        from ..config.repo import MonitorSettings

        return MonitorSettings(tls_cert=self.tls_cert, tls_key=self.tls_key)


class OsProfileSpec(OttoModel):
    """A named ``[os_profiles.<name>]`` bundle: a ``base`` host-class plus raw default field values.

    ``extra='allow'`` collects the non-``base`` keys; the per-field typo guard
    runs later, in ``register_os_profile`` (against the base class's slots), so
    the bundle stays raw here exactly as a ``lab.json`` entry would be.
    """

    model_config = ConfigDict(extra="allow")

    base: str

    @property
    def defaults(self) -> dict[str, Any]:
        """Return the non-``base`` extra fields as a plain dict of host field defaults."""
        return dict(self.model_extra or {})


class ReservationConfigSpec(OttoModel):
    """The otto-owned ``[reservations]`` envelope: ``backend`` + optional ``url``.

    ``extra='allow'`` keeps the backend-specific ``[reservations.<backend>]``
    sub-table open — otto-core cannot type a third-party backend's kwargs.
    """

    model_config = ConfigDict(extra="allow")

    backend: str = "none"
    url: str | None = None


class InventoryConfigSpec(OttoModel):
    """The otto-owned ``[inventory]`` envelope (spec 2026-08-28 host-inventory §8).

    ``backend`` selects a registered inventory backend; ``creds_file`` and
    ``cache_ttl`` are core, backend-independent (§9.4, §9.5). ``extra='allow'``
    keeps the backend's own kwargs (``path``, ``url``, ``filter``…) open here —
    :func:`otto.inventory.compile_inventory` validates them knowing the backend.
    """

    model_config = ConfigDict(extra="allow")

    backend: str
    creds_file: str | None = None
    cache_ttl: str = "24h"

    @field_validator("cache_ttl")
    @classmethod
    def _cache_ttl_grammar(cls, v: str) -> str:
        parse_cache_ttl(v)  # raises ValueError with the grammar
        return v


#: Level names ``[logging.levels]`` accepts: the five stdlib names, plus otto's
#: aliases taken FROM the module that registers them rather than hand-copied —
#: a third alias in ``otto.logger.levels`` becomes configurable here with no
#: second edit, and a removed one stops validating instead of being accepted and
#: then crashing ``setLevel`` downstream. ``NOTSET`` is excluded: "inherit root"
#: is what an ABSENT entry already means.
_LOG_LEVEL_NAMES = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} | set(LEVEL_ALIASES))


class LoggingConfigSpec(OttoModel):
    """Boundary spec for the ``[logging]`` section of ``settings.toml``.

    ``levels`` maps a logger name to the minimum level that ENTERS otto's
    funnel (design 2026-08-30 §4): otto's own defaults quiet known-noisy
    libraries, and an entry here overrides or extends them per logger. Sink
    levels (``--log-level``) still apply downstream, so the two knobs stay
    independent — ``asyncssh = "DEBUG"`` admits the records, and only
    ``--log-level DEBUG`` puts them on the console.

    ``capture`` is GONE — capture is automatic now that otto configures the
    root logger — and a config still carrying it is rejected with a pointer at
    the successor rather than silently ignored (``extra='forbid'`` alone would
    name the dead key without saying what replaced it).
    """

    levels: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_capture(cls, data: Any, info: ValidationInfo) -> Any:
        if isinstance(data, dict) and "capture" in data:
            sut_dir = (info.context or {}).get("sut_dir")
            where = ""
            if sut_dir is not None:
                # Lazy, and only on this error path: otto.models is a leaf and
                # must not import otto.config at module top (module docstring).
                from ..config.repo import TOML_SETTINGS_PATH

                where = f"{Path(sut_dir) / TOML_SETTINGS_PATH}: "
            raise ValueError(
                f"{where}[logging] `capture` was removed: capture is automatic "
                "(otto logs through the root logger — every library's records "
                "are routed without registration). See the [logging.levels] "
                "section of the settings reference for per-library levels."
            )
        return data

    @field_validator("levels")
    @classmethod
    def _validate_levels(cls, v: dict[str, str]) -> dict[str, str]:
        for name, level in v.items():
            if name == "otto" or name.startswith("otto."):
                raise ValueError(
                    f"[logging.levels] {name!r}: otto's own verbosity is --log-level's job"
                )
            if level.upper() not in _LOG_LEVEL_NAMES:
                raise ValueError(
                    f"[logging.levels] {name} = {level!r}: not a level "
                    f"({', '.join(sorted(_LOG_LEVEL_NAMES))})"
                )
        return {name: level.upper() for name, level in v.items()}


class LabSourceSpec(OttoModel):
    """One ``[[lab.sources]]`` entry: a host-data source declaration.

    ``extra='allow'`` because everything beyond ``backend``/``name`` is the
    selected backend's constructor kwargs (the built-in ``json`` backend's
    ``paths``, a custom backend's connection settings). Structural validation
    of those kwargs happens in :func:`otto.labs.sources.compile_lab_sources`,
    which knows which backend the entry selected.
    """

    model_config = ConfigDict(extra="allow")

    backend: str
    name: str | None = None


class LabConfigSpec(OttoModel):
    """The otto-owned ``[lab]`` envelope: the ordered ``sources`` list, nothing else.

    Backend selection and kwargs live inline in each ``[[lab.sources]]``
    entry; there is no per-process ``backend`` key and no ``[lab.<backend>]``
    kwarg tables (spec 2026-08-19 §4). ``extra='forbid'`` (inherited) is what
    turns a leftover ``[lab.cmdb]`` table into an error that names the key.
    """

    sources: list[LabSourceSpec]

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_shape(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "backend" in data:
                raise ValueError(
                    "[lab] `backend` was removed; select backends per source: "
                    '[[lab.sources]] backend = "<name>", with its kwargs inline '
                    "in the entry."
                )
            if not data.get("sources"):
                raise ValueError(
                    "the [lab] table declares no sources; add [[lab.sources]] "
                    "entries or delete the table."
                )
        return data


def _iso8601_utc(value: object) -> object:
    """Normalize an ISO-8601 ``expires`` string: trailing ``Z`` → ``+00:00``; naive → UTC.

    Non-strings pass through unchanged so
    pydantic handles them (a ``datetime``/``None`` is valid; anything else fails
    the ``datetime | None`` type check) — this validator never swallows.
    """
    if not isinstance(value, str):
        return value
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class ReservationEntry(OttoModel):
    """A single reservation record: the holder, the reserved resource names, and an optional expiry.

    The ``expires`` field accepts an ISO-8601 string from JSON (including trailing ``Z``)
    and normalises it to a timezone-aware ``datetime`` via ``_normalize_expires``.
    """

    user: str
    resources: list[str]
    expires: datetime | None = None

    @field_validator("expires", mode="before")
    @classmethod
    def _normalize_expires(cls, v: object) -> object:
        return _iso8601_utc(v)


class ReservationFile(OttoModel):
    """The ``version: 1`` JSON reservation file the built-in JSON backend reads."""

    version: Literal[1]
    reservations: list[ReservationEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# SettingsModel — boundary model for .otto/settings.toml
# ---------------------------------------------------------------------------

# settings.toml version format: X.Y.Z with an optional extra tag beginning
# with '-', '+' or '.'. Mirrors config.version.version_re; duplicated (not
# imported) so models/ stays free of the config bootstrap. A drift test in
# tests/unit/config/test_version.py keeps the two in behavioral lockstep.
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+.][0-9A-Za-z.+-]+)?$")

# The option tables accepted under [host_preferences."<selector>"], each mapped
# to the spec that validates it. Keys mirror host.factory.OPTIONS_KEYS (a drift
# test keeps them in lockstep). All but the last are per-protocol;
# userland_options describes the device itself, and is settable here so a
# product can answer for a whole class of hosts at once.
_HOST_DEFAULT_OPTION_SPECS: dict[str, type[OttoModel]] = {
    "ssh_options": SshOptionsSpec,
    "telnet_options": TelnetOptionsSpec,
    "sftp_options": SftpOptionsSpec,
    "scp_options": ScpOptionsSpec,
    "ftp_options": FtpOptionsSpec,
    "nc_options": NcOptionsSpec,
    "userland_options": UserlandOptionsSpec,
}

# Capability names accepted inside a [host_preferences."<selector>"] table. Each
# names a menu-style host field (term/transfer/impairer) whose value is an
# ordered list of preferred backends; the resolver intersects the list with
# each host's menu at build time. Extend this set when a new menu-style
# capability gains a resolver.
_HOST_PREFERENCE_CAPABILITIES: frozenset[str] = frozenset({"term", "transfer", "impairer"})

# max_age format: "<days>d", e.g. "180d". No months/weeks — keep the unit
# unambiguous for the staleness calculation in the collection model.
_MAX_AGE_RE = re.compile(r"^\d+d$")


class CoverageTierSpec(OttoModel):
    """One ``[coverage.tiers.<name>]`` block: a declared coverage tier."""

    kind: Literal["e2e", "unit", "manual"]
    precedence: int
    color: str | None = None
    harvest_dirs: list[Path] = Field(default_factory=list)
    max_age: str | None = None

    @field_validator("color")
    @classmethod
    def _validate_color(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return validate_color(v)

    @field_validator("max_age")
    @classmethod
    def _validate_max_age(cls, v: str | None) -> str | None:
        if v is not None and _MAX_AGE_RE.match(v) is None:
            raise ValueError(f"max_age {v!r} must be '<days>d', e.g. '180d'")
        return v


def _compilable_pattern(v: str) -> str:
    """Compile *v* here, at parse, so an unusable regex is a settings error.

    Same reason ``config.repo`` compiles lab-source regexes at parse: the
    alternative is a coverage run that dies (or silently matches nothing)
    long after the config was accepted. ``load_exclusion_rules`` compiles
    again for its own use — the duplication is inherent to this repo's
    validate-then-re-read-raw split, which already duplicates the
    exactly-one-matcher rule.
    """
    try:
        re.compile(v)
    except re.error as e:
        raise ValueError(f"invalid regex {v!r} ({e})") from e
    return v


class MarkerRuleSpec(OttoModel):
    """``kind = "marker"`` — a marker family named by its base."""

    kind: Literal["marker"]
    name: str
    stat: Literal["line", "branch"] = "line"

    @field_validator("name")
    @classmethod
    def _base_is_a_token(cls, v: str) -> str:
        """Refuse a base that is empty or holds whitespace.

        The derived members (``{base}_LINE`` and friends) are searched as
        bare substrings, so an empty base yields ``_LINE`` and matches
        inside ordinary identifiers like ``MAX_LINE_LEN``. Exclusion has no
        per-rule accounting by design, so the damage would be silent.
        Duplicated in ``coverage.exclusions.rules`` for the same reason
        every other rule check is: the coverage package re-reads the raw
        dict rather than this model.
        """
        if not v or any(c.isspace() for c in v):
            raise ValueError(f"marker name must be a non-empty token with no whitespace, got {v!r}")
        return v


class PreprocessorRuleSpec(OttoModel):
    """``kind = "preprocessor"`` — exactly one of ``pattern`` or ``macros``."""

    kind: Literal["preprocessor"]
    pattern: str | None = None
    macros: list[str] = Field(default_factory=list)
    stat: Literal["line", "branch"] = "line"

    @field_validator("pattern")
    @classmethod
    def _pattern_compiles(cls, v: str | None) -> str | None:
        return v if v is None else _compilable_pattern(v)

    @model_validator(mode="after")
    def _exactly_one_matcher(self) -> "PreprocessorRuleSpec":
        if bool(self.pattern) == bool(self.macros):
            raise ValueError("a preprocessor rule must set exactly one of 'pattern' or 'macros'")
        return self


class PathRuleSpec(OttoModel):
    """``kind = "path"`` — whole-file exclusion by glob."""

    kind: Literal["path"]
    patterns: list[str]
    stat: Literal["line", "branch"] = "line"


class RegexRuleSpec(OttoModel):
    """``kind = "regex"`` — exclude any source line matching the pattern."""

    kind: Literal["regex"]
    pattern: str
    stat: Literal["line", "branch"] = "line"

    @field_validator("pattern")
    @classmethod
    def _pattern_compiles(cls, v: str) -> str:
        return _compilable_pattern(v)


ExclusionRuleSpec = Annotated[
    MarkerRuleSpec | PreprocessorRuleSpec | PathRuleSpec | RegexRuleSpec,
    Field(discriminator="kind"),
]


class CoverageExclusionsSpec(OttoModel):
    """``[coverage.exclusions]`` — rules that remove lines from the data."""

    rules: list[ExclusionRuleSpec] = Field(default_factory=list)


class CoverageReportSpec(OttoModel):
    """``[coverage.report]`` — report rendering thresholds (design §4/§11).

    gcovr-style percentage cutoffs: ``pct >= high`` renders green,
    ``pct >= medium`` yellow, below red.  Validation-only, like the other
    coverage specs — the runtime value is re-read from the raw settings
    dict by ``otto.coverage.report_config.load_report_thresholds``.
    """

    # int bounds, float defaults: autodoc's annotation stringifier treats a
    # float literal inside Annotated metadata (``Le(100.0)``) as a dotted
    # py:class target and nitpicky -W fails on it; int literals never
    # xref (see docs/conf.py _EXTERNAL_DOC_LINKS notes). Same constraint.
    high: float = Field(default=80.0, ge=0, le=100)
    medium: float = Field(default=70.0, ge=0, le=100)

    @model_validator(mode="after")
    def _medium_not_above_high(self) -> "CoverageReportSpec":
        if self.medium > self.high:
            raise ValueError(
                f"[coverage.report] medium ({self.medium}) must not exceed high ({self.high})"
            )
        return self


class CoverageTicketsSpec(OttoModel):
    """``[coverage.tickets]`` — commit-message ticket attribution.

    Validation-only, like the other coverage specs — the runtime value is
    re-read from the raw settings dict by
    ``otto.coverage.tickets.load_ticket_spec``, which compiles ``pattern``
    and cross-checks ``url``'s named groups against it.
    """

    pattern: str
    url: str | None = None


class CoverageOverridesSpec(OttoModel):
    """``[coverage.overrides]`` — manual-testing override file location.

    Validation-only, like the other coverage specs — the runtime value is
    re-read from the raw settings dict by
    ``otto.coverage.overrides.load_override_config``, which parses and
    validates the override file itself.
    """

    file: str | None = None


class CoverageSettingsSpec(OttoModel):
    """Typed ``[coverage]`` table (was a free-form dict).

    ``embedded`` stays a passthrough dict because its ``builds.<version>``
    sub-tables carry dynamic version keys.
    """

    hosts: str | None = None
    gcda_remote_dir: str = ""
    embedded: dict[str, Any] = Field(default_factory=dict)
    tiers: dict[str, CoverageTierSpec] = Field(default_factory=dict)
    exclusions: CoverageExclusionsSpec = CoverageExclusionsSpec()
    report: CoverageReportSpec = CoverageReportSpec()
    tickets: CoverageTicketsSpec | None = None
    overrides: CoverageOverridesSpec | None = None


class EnvSettingsSpec(OttoModel):
    """``[env]`` — this repo's standing preference for the orchestration venv.

    One key today. It is a PREFERENCE, not a requirement: ``--backend`` on the
    command line outranks it, because the operator in front of the terminal
    knows things the file does not (uv not installed on this host, say).
    """

    backend: str | None = None
    """``"uv"`` or ``"pip"``; None means auto-detect."""


class DependenciesSpec(OttoModel):
    """``[dependencies]`` — inter-project dependencies on other ``OTTO_SUT_DIRS`` repos.

    Entries are ``"name"`` or ``"name <op> N[.N[.N]], ..."``; names match other
    repos' ``name`` fields PEP-503-normalized. Parsed here only to validate —
    the resolution pass re-parses via ``Repo.declared_dependencies``.
    """

    required: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)

    @field_validator("required", "optional")
    @classmethod
    def _validate_entries(cls, v: list[str], info: ValidationInfo) -> list[str]:
        for entry in v:
            parsed = parse_dependency_entry(entry, required=info.field_name == "required")
            if not clauses_satisfiable(parsed.clauses):
                raise ValueError(
                    f"dependency {entry!r} can never be satisfied: "
                    "its clauses are mutually exclusive"
                )
        return v


class ProjectScopeSpec(OttoModel):
    """``[project]`` — the labs and hosts this repo is about (its "fleet of interest").

    A repo declares its reach as regexes rather than names so a lab family
    (``tech-1``, ``tech-2``, …) is one line that keeps working as the lab set
    grows.  Both axes are matched with ``re.fullmatch`` at runtime, never
    ``re.search``: ``"bench"`` does not admit ``"bench-overflow"``.  Write
    ``".*"`` to mean everything — match-all is a visible choice here, never a
    default that quietly widens a project's fleet.

    ``lab_patterns`` is optional in the schema and required in practice for any
    repo that registers product or dev-tool providers; that check runs at
    bootstrap phase 2, after init imports have registered them, so it can name
    what registered.  Leaving it unset does NOT mean "every lab" — it compiles
    to no patterns at all (see
    :class:`otto.config.scope.ProjectScopeConfig`).

    Patterns are compiled here, at settings parse, so an unclosed group fails
    the parse instead of silently fullmatching nothing on every later fleet
    walk.  The message names the pattern and the ``re`` complaint but not the
    repo — the repo is the caller's frame to add.
    """

    lab_patterns: list[str] | None = None
    """Labs this project applies to; a lab is applicable when ANY entry fullmatches."""

    host_patterns: list[str] = Field(default_factory=lambda: [".*"])
    """Hosts of interest within those labs; ORed, and defaulting to all of them."""

    @field_validator("lab_patterns", "host_patterns")
    @classmethod
    def _validate_patterns(cls, v: list[str] | None) -> list[str] | None:
        """Reject a pattern ``re`` cannot compile, naming it and the complaint.

        One ``try`` around the whole loop (not one per entry): ``re.error``
        carries the offending ``pattern`` itself, so the message loses nothing.
        Pydantic supplies the field name — the caller supplies the repo.
        """
        try:
            for pattern in v or []:
                re.compile(pattern)
        except re.error as e:
            raise ValueError(
                f"[project] pattern {e.pattern!r} is not a valid regular expression: {e}"
            ) from None
        return v


class SettingsModel(OttoModel):
    """Boundary model for a repo's ``.otto/settings.toml``.

    ``extra='forbid'`` turns a typo'd top-level key into an error.
    """

    # required identity
    name: str
    version: str

    coverage: CoverageSettingsSpec = CoverageSettingsSpec()

    # paths + module/name lists
    libs: list[RepoPath] = Field(default_factory=list)
    tests: list[RepoPath] = Field(default_factory=list)
    init: list[str] = Field(default_factory=list)

    # structured sub-tables
    host_preferences: dict[str, dict[str, Any]] = Field(default_factory=dict)
    os_profiles: dict[str, OsProfileSpec] = Field(default_factory=dict)
    docker: DockerSettingsSpec = DockerSettingsSpec()
    monitor: MonitorSettingsSpec = MonitorSettingsSpec()
    lab: LabConfigSpec | None = None
    logging: LoggingConfigSpec = LoggingConfigSpec()
    reservations: ReservationConfigSpec = ReservationConfigSpec()
    inventory: InventoryConfigSpec | None = None
    """Per-project inventory override (spec 2026-08-28 host-inventory §8).

    ``~/.otto/settings.toml`` is the usual home — an inventory is a per-user
    fact; this key exists for the fractured phase where one repo needs its own.
    """
    dependencies: DependenciesSpec = DependenciesSpec()
    env: EnvSettingsSpec = EnvSettingsSpec()
    project: ProjectScopeSpec | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "host_defaults" in data:
                raise ValueError(
                    "[host_defaults] was removed; declare option values under "
                    '[host_preferences."<selector>".<opt>], e.g. '
                    '[host_preferences.".*".ssh_options].'
                )
            if "labs" in data:
                raise ValueError(
                    "`labs = [...]` was removed; declare host-data sources instead:\n"
                    '[[lab.sources]]\nbackend = "json"\npaths = ["lab_data"]'
                )
            if "lab_data_type" in data:
                raise ValueError(
                    "`lab_data_type` was removed; host-data sources are declared "
                    "as [[lab.sources]] entries."
                )
        return data

    @field_validator("version")
    @classmethod
    def _validate_version_format(cls, v: str) -> str:
        if _VERSION_RE.match(v) is None:
            raise ValueError(
                f"version {v!r} must be MAJOR.MINOR.PATCH with an optional "
                "'-', '+' or '.' suffix (e.g. 1.2.3, 1.2.3-rc1)"
            )
        return v

    @field_validator("host_preferences")
    @classmethod
    def _validate_host_preferences(cls, v: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Validate each ``[host_preferences."<selector>"]`` block.

        The selector must be a compilable regex (``re.fullmatch`` against the host ``id``).
        Inner keys partition by name: a capability (``term``/``transfer``) takes
        an ordered ``list[str]``; an option table (``ssh_options`` …) takes a dict
        validated against its spec (only user-set keys kept, so the factory's
        per-key merge still applies stock defaults). Capability *values* are not
        registry-checked here (custom backends register after settings parse —
        an out-of-menu entry is skipped leniently at resolution).
        """
        cap_known = ", ".join(sorted(_HOST_PREFERENCE_CAPABILITIES))
        opt_known = ", ".join(sorted(_HOST_DEFAULT_OPTION_SPECS))
        out: dict[str, dict[str, Any]] = {}
        for selector, entries in v.items():
            try:
                re.compile(selector)
            except re.error as e:
                raise ValueError(
                    f"[host_preferences] selector {selector!r} is not a valid "
                    f"regular expression: {e}"
                ) from None
            validated: dict[str, Any] = {}
            for key, val in entries.items():
                if key in _HOST_PREFERENCE_CAPABILITIES:
                    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                        raise ValueError(
                            f"[host_preferences] capability {key!r} under selector "
                            f"{selector!r} must be a list of backend names"
                        )
                    validated[key] = list(val)
                elif key in _HOST_DEFAULT_OPTION_SPECS:
                    spec_cls = _HOST_DEFAULT_OPTION_SPECS[key]
                    validated[key] = spec_cls.model_validate(val).model_dump(exclude_unset=True)
                else:
                    raise ValueError(
                        f"unknown [host_preferences] key {key!r} under selector "
                        f"{selector!r}. Valid selections: {cap_known}. "
                        f"Valid option tables: {opt_known}."
                    )
            out[selector] = validated
        return out

    @model_validator(mode="after")
    def _validate_dependency_names(self) -> "SettingsModel":
        """Self-dependency and required∩optional are author errors, caught here."""
        req = {
            parse_dependency_entry(e, required=True).normalized for e in self.dependencies.required
        }
        opt = {
            parse_dependency_entry(e, required=False).normalized for e in self.dependencies.optional
        }
        both = sorted(req & opt)
        if both:
            raise ValueError(f"dependencies declared both required and optional: {', '.join(both)}")
        if normalize_name(self.name) in req | opt:
            raise ValueError(f"project {self.name!r} cannot depend on itself")
        return self


class UserSettingsModel(OttoModel):
    """Boundary model for the user-level ``~/.otto/settings.toml``.

    Spec 2026-08-28 host-inventory §8. A general per-user file —
    ``[inventory]`` is its first table, not its purpose. ``extra='forbid'``
    (inherited from :class:`~otto.models.base.OttoModel`) so a repo-only table
    pasted here (``[lab]``, ``[reservations]``) errors naming the key instead
    of being silently ignored.
    """

    inventory: InventoryConfigSpec | None = None


# ---------------------------------------------------------------------------
# OttoEnvSettings — typed view of the OTTO_* environment surface
# ---------------------------------------------------------------------------

# Split OTTO_SUT_DIRS on comma OR the OS path separator (':' on Linux), matching
# the historical config.env behavior.
_PATH_LIST_SEP = re.compile(rf"[,{re.escape(os.pathsep)}]")


class OttoEnvSettings(BaseSettings):
    """Typed view of the ``OTTO_*`` environment surface; single source of truth for otto's env vars.

    The six CLI-option vars are read by Typer's ``envvar=`` at parse time; this model
    documents the whole surface and is the reader for the non-CLI reads: sut_dirs,
    field_default, compose_suffix, and the completion-cache xdir.

    sut_dirs existence-checking is done by ``config.env.load_otto_env`` so a
    missing dir raises ``FileNotFoundError`` (not a wrapped ValidationError).
    """

    # env_ignore_empty: an empty env var (e.g. ``OTTO_LOG_DAYS=``, a common
    # "cleared in my shell profile" case) means "unset" → use the field default,
    # rather than failing to parse "" as int/bool. Matches the historical reads
    # (os.environ.get(...) falsiness / Typer's envvar handling).
    model_config = SettingsConfigDict(env_prefix="OTTO_", extra="ignore", env_ignore_empty=True)

    # NoDecode: stop pydantic-settings from JSON-decoding the env string for this
    # "complex" (list) field, so the raw OTTO_SUT_DIRS value reaches the
    # ``_split_path_list`` validator below (which splits on comma / os.pathsep).
    sut_dirs: Annotated[list[Path], NoDecode] = []
    lab: str | None = None
    xdir: Path | None = None
    home: Path | None = None
    log_days: int = 30
    log_level: str = "INFO"
    log_rich: bool = False
    teardown_deadline: float = 10.0
    """Seconds an interrupted command's graceful cleanup may run before it is
    abandoned (second Ctrl+C / SIGTERM abandons it sooner). OTTO_TEARDOWN_DEADLINE."""
    field_default: str | None = None
    field_products: str | None = None
    compose_suffix: str | None = None

    @field_validator("sut_dirs", mode="before")
    @classmethod
    def _split_path_list(cls, v: object) -> object:
        if isinstance(v, str):
            return [p for p in _PATH_LIST_SEP.split(v) if p]
        return v
