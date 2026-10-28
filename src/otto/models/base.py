"""The leaf both ``otto.host`` and ``otto.models`` build on.

It holds what the host layer and the specs layer both need, so neither imports
the other for it: :class:`OttoModel`, the base of every otto pydantic boundary
spec, with :func:`compact_validation_error`; the credential pair,
:class:`CredSpec` (one ``creds`` entry as lab data spells it) and the runtime
:class:`Cred` dataclass it builds, with :func:`cred_identity`, the key every
duplicate check uses; and :func:`resources_nonempty`, the one rule for a usable
reservation identifier. It imports no other otto module (spec 2026-10-11
placement-and-phases §3.3). ``Cred`` keeps its public path,
``otto.host.login_proxy.Cred``, and ``CredSpec`` its, ``otto.models.CredSpec``.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from pydantic import ValidationError


class OttoModel(BaseModel):
    """Base for every otto boundary model.

    ``extra='forbid'`` turns a typo'd or unknown config field into a
    validation error that names the offending key (instead of silently
    dropping it, as the old hand-rolled merge did).

    Descendants that read *historical* data are the sanctioned exception:
    the lenient ``*Record`` spec variants in :mod:`otto.models.monitor`
    override ``extra`` to ``'ignore'`` so older otto builds can read exports
    written by newer ones.

    ``defer_build=True`` means pydantic-core's schema/validator for a
    subclass is not compiled at class-definition time (i.e. at import); it
    builds on first use (validation, ``model_dump``, ``model_json_schema``,
    …) instead. A subclass that overrides ``model_config`` still inherits
    this, because pydantic merges config across the MRO rather than
    replacing it wholesale. Not every ``OttoModel`` defers, though:
    :class:`~otto.models.monitor.EventCreateBody` and
    :class:`~otto.models.monitor.EventUpdateBody` opt back into an eager
    build — see their own docstrings for why. ``tests/unit/models/test_defer_build.py``
    proves both halves for everything else: every other ``OttoModel``
    defers, and every one still builds.
    """

    model_config = ConfigDict(extra="forbid", defer_build=True)


def compact_validation_error(error: "ValidationError") -> str:
    """One-line rendering of *error*: ``field: message [type=…]`` per problem, ``;``-joined.

    ``str(ValidationError)`` spans several lines and carries pydantic's
    ``input_value=`` repr of the rejected value — for a boundary reader
    validating a whole dict (a host entry, a merged cred), that repr can be
    the tail of a password. This calls ``errors(include_input=False)`` and
    reads only ``loc``/``msg``/``type`` — never ``input`` — so the input
    itself cannot reach the message, while ``type=…`` (pydantic's stable
    error code, e.g. ``extra_forbidden``) still names *why* precisely enough
    to search a fix by. Shared by every boundary reader that re-raises a
    pydantic failure as its own error: the inventory's stage-1 parser, the
    NetBox backend, the creds store, ``otto init``'s item validator, and the
    lab repository's host-entry wrap.
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or '<entry>'}: "
        f"{item['msg']} [type={item['type']}]"
        for item in error.errors(include_input=False)
    )


@dataclass(frozen=True)
class Cred:
    """One credential entry: a login plus how to become it."""

    login: str
    """The account name."""

    password: str | None = None
    """Password, or None (key/agent auth on SSH; empty line on telnet; no
    password exchange in the built-in su proxy)."""

    proxy: str | None = None
    """Login-proxy registry key; None means directly loginable (switch via
    the built-in ``"su"``)."""

    via: str | None = None
    """Login of the account the proxy starts from. None defaults to the
    first proxy-less cred entry."""

    params: dict[str, Any] = field(default_factory=dict)
    """Free-form data handed to the proxy callable (host-specific knobs).

    Opaque to otto except for the one key the built-in ``"su"`` proxy reads:
    ``login_shell`` (default True) chooses between ``su - <login>`` and the
    environment-inheriting ``su <login>``. A custom proxy is free to give that
    key its own meaning; nothing outside the proxy that receives these params
    interprets them.
    """

    protocols: list[str] = field(default_factory=list)
    """Protocols this cred is FOR (spec 2026-09-13 cred-scope §3.1).

    Empty means unscoped: the cred applies to every protocol, which is every
    cred written before scope existed. Non-empty names self-authenticating
    backends (``ssh``, ``telnet``, ``ftp``); the host spec validates the names.
    A scoped cred wins its protocol's default pick
    (:func:`~otto.host.login_proxy.default_login`)
    and takes part in the cred's identity (:func:`cred_identity`).
    """


def cred_identity(login: str, protocols: list[str]) -> str:
    """Build the display identity of a cred: ``login``, or ``login [p1, p2]`` when scoped.

    The key every duplicate check and the three-layer merge use (spec
    2026-09-13 cred-scope §3.2), so an error names exactly the string the key
    was built from. Scope is sorted: ``["telnet", "ssh"]`` and ``["ssh",
    "telnet"]`` are one identity.
    """
    if not protocols:
        return login
    return f"{login} [{', '.join(sorted(protocols))}]"


class CredSpec(OttoModel):
    """One ``creds`` entry: a login plus (optionally) how to become it."""

    login: str
    password: str | None = None
    proxy: str | None = None
    via: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    protocols: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _proxy_field_rules(self) -> "CredSpec":
        if self.proxy is None and (self.via is not None or self.params):
            raise ValueError(f"cred {self.login!r}: 'via' and 'params' require 'proxy'")
        if self.via is not None and self.via == self.login:
            raise ValueError(f"cred {self.login!r}: 'via' cannot reference itself")
        return self

    @property
    def identity(self) -> str:
        """``login``, or ``login [scope]`` when scoped.

        See :func:`cred_identity`.
        """
        return cred_identity(self.login, self.protocols)

    def to_cred(self) -> Cred:
        """Build the runtime ``Cred`` dataclass from the validated fields."""
        return Cred(
            login=self.login,
            password=self.password,
            proxy=self.proxy,
            via=self.via,
            params=dict(self.params),
            protocols=list(self.protocols),
        )


class JsonFileSource:
    """One JSON file a source reads once, on first use.

    What the json creds store and the json inventory share: the label, the
    change fingerprint (path, mtime and size, or ``|missing``), the one path
    that fingerprint stats, and the read that refuses an unreadable or
    malformed file as ``<path>: <reason>`` in the source's own error class.
    The parse of the document, and its cache, are the subclass's.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.label = f"json:{self.path}"

    def read_document(self, refuse: "type[Exception]") -> Any:
        """Parse the file, raising *refuse* as ``<path>: <reason>`` when it cannot be read.

        ``<path>: …`` is the one prefix the source's errors use: a reader
        scanning a log for the file should not have to know which layer
        refused it.
        """
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise refuse(f"{self.path}: {e}") from e

    def fingerprint(self) -> "str | None":
        """Return the file's path, mtime and size, or ``|missing`` if it does not exist."""
        try:
            st = self.path.stat()
        except OSError:
            return f"{self.path}|missing"
        return f"{self.path}|{st.st_mtime_ns}|{st.st_size}"

    def stat_paths(self) -> "list[Path] | None":
        """Return the one file the fingerprint is derived from."""
        return [self.path]


def strip_comment_keys(data: object) -> object:
    """Drop ``_``-prefixed keys before validation — the JSON comment idiom.

    A lab, host, link or inventory entry cannot carry real comments, so keys
    like ``_comment`` are sanctioned annotation space. Only the
    leading-underscore form is exempt from ``extra='forbid'``; any other
    unknown key still errors. Every spec that accepts the idiom strips
    through this from a ``mode="before"`` model validator, and
    ``otto.models.jsonschema`` mirrors it in the emitted schema.
    """
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if not (isinstance(k, str) and k.startswith("_"))}
    return data


def nonnegative(v: int | None) -> int | None:
    """Refuse a negative position (an element id, a slot, a shelf); ``None`` passes.

    One rule for every spec level that carries one:
    :class:`~otto.models.lab.ElementSpec`, :class:`~otto.host.specs.HostSpec`
    and :class:`~otto.models.inventory.InventoryRecord`.
    """
    if v is not None and v < 0:
        raise ValueError(f"must be >= 0, got {v}")
    return v


def resources_nonempty(v: set[str]) -> set[str]:
    """Reservation identifiers are opaque, non-empty strings.

    Spec 2026-08-28 three-level-reservations §2, §10. Shared by all three
    levels — :class:`~otto.models.lab.LabEntrySpec`,
    :class:`~otto.models.lab.ElementSpec`, and :class:`~otto.host.specs.HostSpec`,
    and by the runtime :class:`~otto.host.element.Element` — so they cannot
    drift into three different notions of a usable identifier. Public
    precisely because it is imported across modules: a leading underscore on a
    name another module depends on says "private" while meaning the opposite.
    """
    if any(not r.strip() for r in v):
        raise ValueError("resources must be non-empty strings")
    return v
