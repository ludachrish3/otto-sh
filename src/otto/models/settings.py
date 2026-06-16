"""Pydantic boundary specs for ``.otto/settings.toml`` and the ``OTTO_*`` env.

These validate the settings dict (``extra='forbid'``) and build the **unchanged**
runtime objects (``DockerSettings``/``DockerImage``/``DockerCompose`` frozen
dataclasses, ``OsProfile``, the reservation backend) via ``to_runtime()`` — the
same two-type split the option/host specs use.

Leaf isolation: this module must NOT import from ``otto.configmodule`` at module
top — doing so triggers ``configmodule/__init__``'s app bootstrap. Runtime types
from ``configmodule.repo`` are imported lazily inside ``to_runtime()`` and under
``TYPE_CHECKING`` for annotations only.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import ConfigDict, field_validator
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
    from ..configmodule.repo import DockerCompose, DockerImage, DockerSettings


class DockerImageSpec(OttoModel):
    name: str
    dockerfile: Path
    context: Path
    target: str | None = None
    # dict[str, Any] (not dict[str, str]) for parity with the old TOML parser:
    # a build arg written as a bare scalar (``PORT = 8080``) stays accepted and
    # is stringified below, rather than rejected at validation.
    build_args: dict[str, Any] = {}

    def to_runtime(self) -> DockerImage:
        from ..configmodule.repo import DockerImage
        return DockerImage(
            name=self.name,
            dockerfile=self.dockerfile,
            context=self.context,
            target=self.target,
            # frozen, sorted, all-string tuple-of-tuples so the runtime object
            # stays hashable and order-stable for the docker context hash;
            # ``str(v)`` coerces TOML scalars (ints/bools) like the old parser.
            build_args=tuple(
                (k, str(v)) for k, v in sorted(self.build_args.items())
            ),
        )


class DockerComposeSpec(OttoModel):
    path: Path
    default_host: str | None = None
    services: tuple[str, ...] = ()

    def to_runtime(self) -> DockerCompose:
        from ..configmodule.repo import DockerCompose
        return DockerCompose(
            path=self.path,
            default_host=self.default_host,
            services=self.services,
        )


class DockerSettingsSpec(OttoModel):
    registry_url: str = "docker.io"
    images: list[DockerImageSpec] = []
    composes: list[DockerComposeSpec] = []

    def to_runtime(self) -> DockerSettings:
        from ..configmodule.repo import DockerSettings
        return DockerSettings(
            registry_url=self.registry_url,
            images=tuple(i.to_runtime() for i in self.images),
            composes=tuple(c.to_runtime() for c in self.composes),
        )


class OsProfileSpec(OttoModel):
    """A named ``[os_profiles.<name>]`` bundle: a required ``base`` host-class
    name plus an open bag of raw default field values merged beneath each host.

    ``extra='allow'`` collects the non-``base`` keys; the per-field typo guard
    runs later, in ``register_os_profile`` (against the base class's slots), so
    the bundle stays raw here exactly as a ``hosts.json`` entry would be.
    """

    model_config = ConfigDict(extra="allow")

    base: str

    @property
    def defaults(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class ReservationConfigSpec(OttoModel):
    """The otto-owned ``[reservations]`` envelope: ``backend`` + optional ``url``.

    ``extra='allow'`` keeps the backend-specific ``[reservations.<backend>]``
    sub-table open — otto-core cannot type a third-party backend's kwargs.
    """

    model_config = ConfigDict(extra="allow")

    backend: str = "none"
    url: str | None = None


def _iso8601_utc(value: object) -> object:
    """Normalize an ISO-8601 ``expires`` string: trailing ``Z`` → ``+00:00`` and
    a naive timestamp is treated as UTC. Non-strings pass through unchanged so
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
    reservations: list[ReservationEntry] = []


# ---------------------------------------------------------------------------
# SettingsModel — boundary model for .otto/settings.toml
# ---------------------------------------------------------------------------

# settings.toml version floor: X.Y.Z. Mirrors configmodule.version.version_re;
# duplicated (not imported) so models/ stays free of the configmodule bootstrap.
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+")

# The six per-protocol option tables accepted under [host_defaults], each mapped
# to the spec that validates it. Keys mirror storage.factory.OPTIONS_KEYS (a
# drift test keeps them in lockstep).
_HOST_DEFAULT_OPTION_SPECS: dict[str, type[OttoModel]] = {
    "ssh_options": SshOptionsSpec,
    "telnet_options": TelnetOptionsSpec,
    "sftp_options": SftpOptionsSpec,
    "scp_options": ScpOptionsSpec,
    "ftp_options": FtpOptionsSpec,
    "nc_options": NcOptionsSpec,
}


class SettingsModel(OttoModel):
    """Boundary model for a repo's ``.otto/settings.toml`` (post ``${sut_dir}``
    expansion). ``extra='forbid'`` turns a typo'd top-level key into an error.
    """

    # required identity
    name: str
    version: str

    # legacy / passthrough — present in every fixture, consumed by nobody in
    # parse_settings, but must be tolerated under extra='forbid'.
    lab_data_type: str = "json"
    coverage: dict[str, Any] = {}

    # paths + module/name lists
    labs: list[Path] = []
    valid_labs: list[str] = []
    libs: list[Path] = []
    tests: list[Path] = []
    init: list[str] = []

    # structured sub-tables
    host_defaults: dict[str, dict[str, Any]] = {}
    os_profiles: dict[str, OsProfileSpec] = {}
    docker: DockerSettingsSpec = DockerSettingsSpec()
    reservations: ReservationConfigSpec = ReservationConfigSpec()

    @field_validator("version")
    @classmethod
    def _validate_version_format(cls, v: str) -> str:
        if _VERSION_RE.match(v) is None:
            # Prefix match (no ``$``) to stay consistent with the runtime
            # ``configmodule.version.Version`` parser, which Repo builds from
            # this same string — a trailing SemVer suffix (``1.2.3-rc1``) is
            # accepted by both, so the message says "start with".
            raise ValueError(
                f"version {v!r} must start with MAJOR.MINOR.PATCH (e.g. 1.2.3)"
            )
        return v

    @field_validator("host_defaults")
    @classmethod
    def _validate_host_defaults_partial(
        cls, v: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Validate each ``*_options`` sub-table against its spec (catching
        typos + type errors) but keep only the user-set keys, so the downstream
        per-key precedence merge in storage.factory still applies its defaults.
        """
        result: dict[str, dict[str, Any]] = {}
        for key, table in v.items():
            spec_cls = _HOST_DEFAULT_OPTION_SPECS.get(key)
            if spec_cls is None:
                known = ", ".join(sorted(_HOST_DEFAULT_OPTION_SPECS))
                raise ValueError(
                    f"unknown [host_defaults] sub-table {key!r}. Valid: {known}"
                )
            validated = spec_cls.model_validate(table)
            result[key] = validated.model_dump(exclude_unset=True)
        return result


# ---------------------------------------------------------------------------
# OttoEnvSettings — typed view of the OTTO_* environment surface
# ---------------------------------------------------------------------------

# Split OTTO_SUT_DIRS on comma OR the OS path separator (':' on Linux), matching
# the historical configmodule.env behavior.
_PATH_LIST_SEP = re.compile(rf"[,{re.escape(os.pathsep)}]")


class OttoEnvSettings(BaseSettings):
    """Typed view of the ``OTTO_*`` environment surface. Single source of truth
    for the variables otto reads programmatically. (The six CLI-option vars are
    read by Typer's ``envvar=`` at parse time; this model documents the whole
    surface and is the reader for the non-CLI reads: sut_dirs, field_default,
    compose_suffix, and the completion-cache xdir.)

    sut_dirs existence-checking is done by ``configmodule.env.load_otto_env`` so a
    missing dir raises ``FileNotFoundError`` (not a wrapped ValidationError).
    """

    # env_ignore_empty: an empty env var (e.g. ``OTTO_LOG_DAYS=``, a common
    # "cleared in my shell profile" case) means "unset" → use the field default,
    # rather than failing to parse "" as int/bool. Matches the historical reads
    # (os.environ.get(...) falsiness / Typer's envvar handling).
    model_config = SettingsConfigDict(
        env_prefix="OTTO_", extra="ignore", env_ignore_empty=True
    )

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
