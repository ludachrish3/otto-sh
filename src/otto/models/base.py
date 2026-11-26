"""The leaf both ``otto.host`` and ``otto.models`` build on.

It holds what the host layer and the specs layer both need, so neither imports
the other for it: :class:`OttoModel`, the base of every otto pydantic boundary
spec, with :func:`compact_validation_error`; the credential pair,
:class:`CredSpec` (one ``creds`` entry as lab data spells it) and the runtime
:class:`Cred` dataclass it builds, with :func:`cred_identity`, the key every
duplicate check uses, and :func:`cred_layer_by_identity`, the one keying of a
cred layer; and :func:`resources_nonempty`, the one rule for a usable
reservation identifier. It imports no other otto module at import time (spec
2026-10-11 placement-and-phases §3.3); :meth:`JsonFileSource.fingerprint`
classifies a stat through ``otto.config.corpus_snapshot`` when called, and a
refused cred entry is described through ``otto.errors`` when refused.
``Cred`` keeps its public path,
``otto.host.login_proxy.Cred``, and ``CredSpec`` its, ``otto.models.CredSpec``.
"""

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator
from typing_extensions import final

version_re = re.compile(
    r"(?P<major>\d+)\."
    r"(?P<minor>\d+)\."
    r"(?P<patch>\d+)"
    r"(?P<extra>[-+.][0-9A-Za-z.+-]+)?"
)
"""The one pattern for a product version string, ``MAJOR.MINOR.PATCH`` plus an optional tag.

``otto.models.settings`` validates ``version`` with it at load and
``otto.config.version.Version`` parses with it; both call ``fullmatch`` so a
trailing newline is refused. The optional extra tag begins with ``-``, ``+``
or ``.``. It lives here, not in a module of its own, because this module is
on every command's startup path already and a leaf of its own cost every
surface eight file operations (the import budget).
"""


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
    ``[inventory]``, ``[[inventories]]`` and ``[creds]`` table readers, the
    NetBox backend, the creds store, ``otto init``'s item validator, the
    lab repository's lab, element and host-entry wraps, the reservation
    file reader, and the user settings file's loader.
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or '<entry>'}: "
        f"{item['msg']} [type={item['type']}]"
        for item in error.errors(include_input=False)
    )


def validation_message(error: BaseException) -> "str | None":
    """Return the compact form of *error* when it is a ``ValidationError``, else ``None``.

    For a frame that catches any failure of a user file and says something
    of its own for the rest (a type name, a ``repr``): the one test of "is
    this the error whose ``str()`` repeats its input", with
    :func:`compact_validation_error` as the answer.
    """
    if isinstance(error, ValidationError):
        return compact_validation_error(error)
    return None


def failure_reason(error: BaseException) -> str:
    """Why *error* happened, as one line a user reads: its compact form, or its text.

    :func:`validation_message` for a ``ValidationError``, ``str(error)`` for
    anything else. Read by the frames that otherwise print the error's text:
    a skipped host record, a refused lab entry, a refused declared link.
    """
    message = validation_message(error)
    return message if message is not None else str(error)


@final
@dataclass(frozen=True, kw_only=True)
class Cred:
    """One credential entry: a login plus how to become it."""

    login: str
    """The account name."""

    password: SecretStr | None = None
    """Password, or None (key/agent auth on SSH; empty line on telnet; no
    password exchange in the built-in su proxy).

    A ``SecretStr``, so neither the cred's repr nor a traceback's locals show
    it; read it with ``get_secret_value()``. A plain ``str`` given here is
    wrapped on construction, so after construction the attribute is always
    ``SecretStr | None``. The annotation states that read type; a static type
    checker therefore flags ``Cred(password="...")``, which runs correctly
    (pass ``SecretStr("...")`` to satisfy it)."""

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

    def __post_init__(self) -> None:
        # frozen: the one sanctioned write, before anyone can read the field
        object.__setattr__(self, "password", as_secret(self.password))


def as_secret(password: "str | SecretStr | None") -> "SecretStr | None":
    """Wrap a plain-``str`` *password* in ``SecretStr``; a ``SecretStr`` or ``None`` passes through.

    What every holder of a password field does on construction (:class:`Cred`,
    the telnet and console clients), so a caller's plain string never stays
    plain past the constructor.
    """
    return SecretStr(password) if isinstance(password, str) else password


SECRET_DATA_KEYS = frozenset({"password", "passphrase"})
"""The keys whose value is a secret in parsed lab, creds and inventory data.

Exactly the fields :func:`otto.params.sensitive_field_names` reports across
otto's models (``CredSpec.password``, ``SshOptionsSpec.passphrase``); the
secret-fields guard (``tests/unit/models/test_secret_fields_guard.py``) fails
when the two disagree. :func:`secret_locations` reads it."""


@final
@dataclass(frozen=True, kw_only=True, eq=False)
class SecretLocation:
    """Where one secret value sits in a parsed document: what names it, never the value."""

    path: str
    """The value's place in the document, such as ``elements[0].creds[1].password``."""

    parent: "dict[Any, Any]" = field(repr=False)
    """The table that holds the value; left out of the repr, which would show the value."""

    key: str
    """The value's key in :attr:`parent`, one of :data:`SECRET_DATA_KEYS`."""


def secret_locations(data: object) -> "Iterator[SecretLocation]":
    """Yield each value under a :data:`SECRET_DATA_KEYS` key in *data* that is not ``None``.

    In document order. *data* is a parsed JSON document (or record), walked
    at every depth (``_secret_slots``, the one walk): a ``creds`` entry's
    ``password`` and an ``ssh_options`` table's ``passphrase`` alike. Any
    value under a secret key is yielded, a string or not (``1234``, a list, a
    table), and a table under a secret key is then walked into as well.
    """
    for slot in _secret_slots(data):
        yield SecretLocation(path=_path(slot), parent=slot.parent, key=slot.key)


@final
class _SecretSlot:
    """The walk's one cursor: the secret it is at, and the containers that lead there.

    :func:`_secret_slots` yields the same instance every time, moved, so the
    walk allocates nothing per secret either; a reader takes what it needs
    before asking for the next.
    """

    __slots__ = ("key", "parent", "parents")

    def __init__(self, parents: "list[Any]") -> None:
        self.parent: dict[Any, Any] = {}
        """The table holding the value."""
        self.key: str = ""
        """The value's key in :attr:`parent`."""
        self.parents = parents
        """The open containers, from the document's root to :attr:`parent` (the walk's stack)."""


def _secret_slots(data: object) -> "Iterator[_SecretSlot]":
    """Walk *data* once, stopping at each non-``None`` value under a :data:`SECRET_DATA_KEYS` key.

    THE one walk of a parsed document's secrets, which :func:`secret_locations`
    and :func:`wrap_secret_values` both read. It keeps its own stack, so a
    document deeper than the recursion limit is still walked, and it
    allocates nothing per key or item: one cursor for the walk, and one
    iterator per container. No path is built here; :func:`_path` builds one
    from the cursor only for a location that is reported. A reader may
    replace the value in the cursor's ``parent`` before asking for the next.
    """
    if not isinstance(data, (dict, list)):
        return
    parents: list[Any] = [data]
    slot = _SecretSlot(parents)
    walks: list[Iterator[tuple[Any, Any]]] = [
        iter(data.items()) if isinstance(data, dict) else enumerate(data)
    ]
    while walks:
        for key, value in walks[-1]:
            if key in SECRET_DATA_KEYS and value is not None:  # never a list index
                slot.parent, slot.key = parents[-1], key
                yield slot
            if isinstance(value, (dict, list)):
                parents.append(value)
                walks.append(iter(value.items()) if isinstance(value, dict) else enumerate(value))
                break
        else:
            walks.pop()
            parents.pop()


def _path(slot: _SecretSlot) -> str:
    """Return how a location names *slot*'s place: ``elements[0].creds[1].password``.

    Each open container's key in its parent is found by identity, so the
    walk itself never tracks keys.
    """
    from itertools import pairwise

    keys: list[object] = []
    for outer, inner in pairwise(slot.parents):
        pairs = outer.items() if isinstance(outer, dict) else enumerate(outer)
        keys.append(next(k for k, v in pairs if v is inner))
    keys.append(slot.key)
    parts: list[str] = []
    for key in keys:
        if isinstance(key, int):
            parts.append(f"[{key}]")
        else:
            parts.append(f".{key}" if parts else str(key))
    return "".join(parts)


def wrap_secret_values(data: object) -> None:
    """Wrap, in place, each ``str`` under a :data:`SECRET_DATA_KEYS` key in *data* as ``SecretStr``.

    *data* is a parsed JSON document (or record), walked at every depth
    (``_secret_slots``, the walk :func:`secret_locations` reads): a
    ``creds`` entry's ``password`` and an ``ssh_options`` table's
    ``passphrase`` alike. A value that is not a string is left as it is, for
    the models to refuse. Every loader of creds-bearing data calls this on
    what it parsed before any other otto frame holds it, so a traceback's
    locals (or in the log files) never show the plain value; the models then
    take the ``SecretStr`` as it is.
    """
    for slot in _secret_slots(data):
        value = slot.parent[slot.key]
        if isinstance(value, str):
            slot.parent[slot.key] = SecretStr(value)


def load_toml_settings(text: str) -> dict[str, Any]:
    """Parse a settings TOML document, its secret values wrapped by :func:`wrap_secret_values`.

    The one parse of every otto settings file (a project's
    ``.otto/settings.toml``, the user settings file), so a ``passphrase`` or
    ``password`` key at any depth — ``[host_preferences."x".ssh_options]
    passphrase`` — is a ``SecretStr`` before any otto frame holds the parsed
    table. A decode error's traceback starts at this frame, with *text*
    already dropped from its locals: the parser's frames, and this one's
    argument, hold the whole file text, secrets included, and a logged
    traceback shows every shown frame's locals. (``tomli`` is also in
    :func:`otto.logger.formatters.suppressed_traceback_packages`.) The error's
    message still names the line and column.

    Raises
    ------
    tomli.TOMLDecodeError
        When *text* is not valid TOML.
    """
    import tomli

    try:
        data = tomli.loads(text)
    except tomli.TOMLDecodeError as e:
        del text
        raise e.with_traceback(None) from None
    wrap_secret_values(data)
    return data


def secret_safe_json_default(secret_key: "Callable[[], bytes]") -> "Callable[[object], object]":
    """Return the ``default=`` hook of every ``json.dumps`` over parsed settings data.

    The hook turns a ``SecretStr`` (see :func:`wrap_secret_values`) into
    ``"secret:hmac-sha256:<hex>"``, the HMAC-SHA256 of its value under
    ``secret_key()`` (the install's key,
    :func:`otto.config.home.secret_digest_key`): stable across processes and
    changed by any change to the secret, so a fingerprint built from the dump
    still tells two configurations apart, while neither the value nor an
    unkeyed hash a guess could be checked against is ever dumped or stored.
    ``secret_key`` is asked only when a secret is met, and at most once per
    hook, so a dump without one never reads (or creates) the key. Anything
    else becomes its ``str()``.
    """
    import functools

    key = functools.cache(secret_key)

    def default(value: object) -> object:
        if isinstance(value, SecretStr):
            import hashlib
            import hmac

            secret = value.get_secret_value().encode()
            mac = hmac.new(key(), secret, hashlib.sha256).hexdigest()
            return f"secret:hmac-sha256:{mac}"
        return str(value)

    return default


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


def cred_layer_by_identity(
    layer: list[dict[str, Any]], *, name: str, key: "str | None" = None
) -> dict[str, dict[str, Any]]:
    """Return each entry of the cred *layer* by identity: THE one keying of a cred layer.

    The identity is ``login`` plus scope (:func:`cred_identity` over
    ``protocols``, an absent or ``None`` one being ``[]``), and each entry is
    kept as the fields it states (present and not ``None``).
    :func:`otto.inventory.merge_creds` composes two layers keyed this way, and
    :mod:`otto.host.provenance` attributes each merged field to the identity
    it keys, so ``otto lab explain`` names exactly the identity the merge
    formed. It lives here, below both, so neither imports the other for it.

    Raises :class:`ValueError` for an entry with no non-empty string
    ``login``, for a non-``list``-of-``str`` ``protocols``, and for an
    identity repeated within the layer; the message names the layer (*name*)
    and *key*, and lists an entry's field NAMES only (or the identity, for a
    duplicate), because a value here may be a password. ``merge_creds``
    raises it as an :class:`~otto.inventory.errors.InventoryError`.
    """
    where = f" (inventory key {key!r})" if key is not None else ""
    out: dict[str, dict[str, Any]] = {}
    for entry in layer:
        login = entry.get("login") if isinstance(entry, dict) else None
        if not isinstance(login, str) or not login:
            from ..errors import describe_refused  # lazy: refusal path only

            raise ValueError(
                f"cred entry without a 'login' string in the {name} layer{where}: "
                f"{describe_refused(entry, secret=True)}"
            )
        protocols = entry.get("protocols")
        if protocols is None:
            protocols = []
        if not isinstance(protocols, list) or not all(isinstance(p, str) for p in protocols):
            raise ValueError(
                f"cred {login!r}: 'protocols' must be a list of strings in the {name} layer{where}"
            )
        identity = cred_identity(login, protocols)
        if identity in out:
            raise ValueError(f"duplicate cred entry {identity!r} in the {name} layer{where}")
        out[identity] = {k: v for k, v in entry.items() if v is not None}
    return out


@final
class CredSpec(OttoModel):
    """One ``creds`` entry: a login plus (optionally) how to become it."""

    login: str
    password: SecretStr | None = None
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


def observed_fingerprint(path: Path) -> str:
    """Return *path*, its mtime and its size; ``|missing`` when absent, ``|unreadable``.

    The one freshness formatter for a file a source reads: every source that
    fingerprints the files it depends on spells each one through here.

    The three classes are ``otto.config.corpus_snapshot.classify``'s,
    the one rule the console-script shim mirrors when it stats a source's
    ``stat_paths``: whatever the shim sees change, this must change
    with, or a TAB hands over while the cache digest never moves. Inside
    a rebuild's ``corpus_snapshot()`` scope the stat is the memoized one
    the shim's stored triple for the same path is taken from, so the two
    are one observation; outside a scope it is a plain stat.
    """
    # Function scope: the leaf imports no otto module at import time.
    from ..config.corpus_snapshot import Unreadable, observe

    st = observe(path)
    if st is None:
        return f"{path}|missing"
    if isinstance(st, Unreadable):
        return f"{path}|unreadable"
    return f"{path}|{st.st_mtime_ns}|{st.st_size}"


@final
class JsonFileError(Exception):
    """A JSON file :func:`read_json_file` could not decode; ``__cause__`` is why.

    The message is the underlying error's. :attr:`malformed` tells malformed
    JSON from a file that could not be opened or whose bytes do not decode.
    """

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.malformed = isinstance(cause, json.JSONDecodeError)
        """Whether the text was read and is not JSON; false for an unreadable file."""


def read_json_file(path: Path) -> Any:
    """Return the JSON value of the file *path*: THE one decoding of a JSON file otto reads.

    The lab reader (``otto.labs.documents.read_lab_document``) and
    :class:`JsonFileSource` (the json inventory and the json creds store)
    read through here. The text is decoded in the locale's default encoding
    (``path.open()``), as otto has always read these files: a strict UTF-8
    decode would refuse a locale-encoded file that loads today (on Windows,
    say). Bytes the locale cannot decode make the file unreadable, never a
    bare ``UnicodeDecodeError``.

    Raises:
        JsonFileError: The file cannot be opened, its bytes do not decode, or
            its text is malformed JSON.
    """
    try:
        with path.open() as f:
            return json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise JsonFileError(e) from e


class JsonFileSource:
    """One JSON file a source reads once, on first use.

    What the json creds store and the json inventory share: the label, the
    change fingerprint (path, mtime and size, or ``|missing`` or
    ``|unreadable``), the one path that fingerprint stats, and the read that
    refuses an unreadable or
    malformed file as ``<path>: <reason>`` in the source's own error class.
    The parse of the document, and its cache, are the subclass's.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.label = f"json:{self.path}"

    def read_document(self, *, refuse: "type[Exception]") -> Any:
        """Parse the file, raising *refuse* as ``<path>: <reason>`` when it cannot be read.

        Its secrets are wrapped as it is parsed (:func:`wrap_secret_values`).

        ``<path>: …`` is the one prefix the source's errors use: a reader
        scanning a log for the file should not have to know which layer
        refused it. The file is decoded by :func:`read_json_file`, so bytes
        that do not decode are refused like a file that cannot be opened.
        """
        try:
            data = read_json_file(self.path)
        except JsonFileError as e:
            raise refuse(f"{self.path}: {e}") from e.__cause__
        wrap_secret_values(data)
        return data

    def fingerprint(self) -> "str | None":
        """Return the file's :func:`observed_fingerprint`."""
        return observed_fingerprint(self.path)

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
