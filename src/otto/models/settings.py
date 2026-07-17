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

from pydantic import ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .base import OttoModel
from .options import (
    FtpOptionsSpec,
    NcOptionsSpec,
    ScpOptionsSpec,
    SftpOptionsSpec,
    SshOptionsSpec,
    TelnetOptionsSpec,
)

if TYPE_CHECKING:
    from ..config.repo import DockerCompose, DockerImage, DockerSettings, MonitorSettings


class DockerImageSpec(OttoModel):
    """Boundary spec for a ``[[docker.images]]`` entry in ``settings.toml``.

    Validates the image name, Dockerfile path, build context, optional build stage target,
    and ``build_args`` dict (scalar TOML values are accepted and stringified). Builds a
    ``DockerImage`` runtime dataclass via ``to_runtime()``, with ``build_args`` normalised
    to a sorted, frozen tuple-of-pairs for hashability.
    """

    name: str
    dockerfile: Path
    context: Path
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

    Validates the Compose file path, an optional default service host name, and the list
    of services within the Compose project. Builds a ``DockerCompose`` runtime dataclass
    via ``to_runtime()``.
    """

    path: Path
    default_host: str | None = None
    services: tuple[str, ...] = ()

    def to_runtime(self) -> "DockerCompose":
        """Build the ``DockerCompose`` runtime dataclass from the validated spec fields."""
        from ..config.repo import DockerCompose

        return DockerCompose(
            path=self.path,
            default_host=self.default_host,
            services=self.services,
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

    def to_runtime(self) -> "DockerSettings":
        """Build the ``DockerSettings`` runtime dataclass from the validated spec fields."""
        from ..config.repo import DockerSettings

        return DockerSettings(
            registry_url=self.registry_url,
            images=tuple(i.to_runtime() for i in self.images),
            composes=tuple(c.to_runtime() for c in self.composes),
        )


class MonitorSettingsSpec(OttoModel):
    """Boundary spec for the ``[monitor]`` section of ``settings.toml``.

    TLS for the dashboard server. Paths are ``expanduser()``-expanded here
    (settings expansion only handles ``${sut_dir}``): the committed value is
    shared by the whole team, so it conventionally points under
    ``~/.config/otto/tls/`` — identical text, per-user resolution. ``tls_key``
    without ``tls_cert`` is rejected; ``tls_cert`` alone is fine (bundled PEM).
    """

    tls_cert: Path | None = None
    tls_key: Path | None = None

    @field_validator("tls_cert", "tls_key")
    @classmethod
    def _expand_user(cls, v: Path | None) -> Path | None:
        return v.expanduser() if v is not None else v

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


class LoggingConfigSpec(OttoModel):
    """Boundary spec for the ``[logging]`` section of ``settings.toml``.

    ``capture`` lists top-level logger prefixes whose ``logging.getLogger(__name__)``
    records otto should route into its sinks (in addition to the package prefixes
    auto-derived from a repo's ``init``/``libs``). Defaults to an empty list.
    """

    capture: list[str] = Field(default_factory=list)


class LabConfigSpec(OttoModel):
    """The otto-owned ``[lab]`` envelope: which host-source ``backend`` to use.

    ``extra='allow'`` keeps the backend-specific ``[lab.<backend>]`` sub-table
    open — otto-core cannot type a third-party backend's kwargs. Defaults to the
    built-in ``"json"`` backend so repos with no ``[lab]`` block behave exactly
    as before.
    """

    model_config = ConfigDict(extra="allow")

    backend: str = "json"


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

# settings.toml version floor: X.Y.Z. Mirrors config.version.version_re;
# duplicated (not imported) so models/ stays free of the config bootstrap.
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+")

# The six per-protocol option tables accepted under [host_preferences."<selector>"],
# each mapped to the spec that validates it. Keys mirror host.factory.OPTIONS_KEYS
# (a drift test keeps them in lockstep).
_HOST_DEFAULT_OPTION_SPECS: dict[str, type[OttoModel]] = {
    "ssh_options": SshOptionsSpec,
    "telnet_options": TelnetOptionsSpec,
    "sftp_options": SftpOptionsSpec,
    "scp_options": ScpOptionsSpec,
    "ftp_options": FtpOptionsSpec,
    "nc_options": NcOptionsSpec,
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
        from ..coverage.colors import validate_color

        return validate_color(v)

    @field_validator("max_age")
    @classmethod
    def _validate_max_age(cls, v: str | None) -> str | None:
        if v is not None and _MAX_AGE_RE.match(v) is None:
            raise ValueError(f"max_age {v!r} must be '<days>d', e.g. '180d'")
        return v


class CoverageExclusionsSpec(OttoModel):
    """``[coverage.exclusions]`` — extra exclusion-marker strings."""

    markers: list[str] = Field(default_factory=list)


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


class SettingsModel(OttoModel):
    """Boundary model for a repo's ``.otto/settings.toml`` (post ``${sut_dir}`` expansion).

    ``extra='forbid'`` turns a typo'd top-level key into an error.
    """

    # required identity
    name: str
    version: str

    # legacy / passthrough — present in every fixture, consumed by nobody in
    # parse_settings, but must be tolerated under extra='forbid'.
    lab_data_type: str = "json"
    coverage: CoverageSettingsSpec = CoverageSettingsSpec()

    # paths + module/name lists
    labs: list[Path] = Field(default_factory=list)
    valid_labs: list[str] = Field(default_factory=list)
    libs: list[Path] = Field(default_factory=list)
    tests: list[Path] = Field(default_factory=list)
    init: list[str] = Field(default_factory=list)

    # structured sub-tables
    host_preferences: dict[str, dict[str, Any]] = Field(default_factory=dict)
    os_profiles: dict[str, OsProfileSpec] = Field(default_factory=dict)
    docker: DockerSettingsSpec = DockerSettingsSpec()
    monitor: MonitorSettingsSpec = MonitorSettingsSpec()
    lab: LabConfigSpec = LabConfigSpec()
    logging: LoggingConfigSpec = LoggingConfigSpec()
    reservations: ReservationConfigSpec = ReservationConfigSpec()

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_host_defaults(cls, data: Any) -> Any:
        if isinstance(data, dict) and "host_defaults" in data:
            raise ValueError(
                "[host_defaults] was removed; declare option values under "
                '[host_preferences."<selector>".<opt>], e.g. '
                '[host_preferences.".*".ssh_options].'
            )
        return data

    @field_validator("version")
    @classmethod
    def _validate_version_format(cls, v: str) -> str:
        if _VERSION_RE.match(v) is None:
            # Prefix match (no ``$``) to stay consistent with the runtime
            # ``config.version.Version`` parser, which Repo builds from
            # this same string — a trailing SemVer suffix (``1.2.3-rc1``) is
            # accepted by both, so the message says "start with".
            raise ValueError(f"version {v!r} must start with MAJOR.MINOR.PATCH (e.g. 1.2.3)")
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
    log_days: int = 30
    log_level: str = "INFO"
    log_rich: bool = False
    field_default: str | None = None
    field_products: str | None = None
    compose_suffix: str | None = None

    @field_validator("sut_dirs", mode="before")
    @classmethod
    def _split_path_list(cls, v: object) -> object:
        if isinstance(v, str):
            return [p for p in _PATH_LIST_SEP.split(v) if p]
        return v
