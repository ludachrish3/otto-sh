"""
Product lifecycle strategy for hosts.

A :class:`Product` is a unit of software-under-test deployed to a host — the
lifecycle analog of :class:`~otto.host.binary_loader.BinaryLoader`. It is a
**behavior contract** (an ``ABC``): projects subclass it and inject instances
via :attr:`~otto.host.host.BaseHost.products`. The host orchestrates; the
product knows how to stage/install/uninstall/check itself.

It is intentionally **not** a pydantic model — that would force every project
product into pydantic and diverge from the sibling host strategies
(:class:`~otto.host.command_frame.CommandFrame`,
:class:`~otto.host.binary_loader.BinaryLoader`,
:class:`~otto.host.embedded_filesystem.EmbeddedFileSystem`).
Concrete subclasses pick their own data representation (``@dataclass`` or an
``OttoModel``).

Products are customized in repo config or code, never lab data: a
``[[products]]`` entry in ``.otto/settings.toml`` declares the common cases
(see :mod:`otto.declared` and :func:`register_product_kind`), and a
:func:`register_product_provider` callback from a ``.otto`` init module
remains the code fallback for whatever the match table cannot express —
declared entries apply first at ingest, so a provider product whose name a
declared entry claimed stands down. Lab data stays product-agnostic and
evolves independently of product code; declaring products *in* lab data is
deliberately **not** supported.
"""

import logging
import shlex
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from typing_extensions import override

from .. import layout
from ..declared import KindRegistry, declared_for_host
from ..registry import caller_module, get_registering_repo, refuse_during_test_load
from ..result import Result
from ..utils import Status
from .log_haul import haul_globs

if TYPE_CHECKING:
    from ..declared import DeclaredEntry
    from .host import Host

logger = logging.getLogger(__name__)


LOGIN_HOME = "the host's login home"
"""How an undiscovered login home is named in a lab-load message.

:func:`stage_dir_key` runs at lab ingest, where nothing is connected and the
real home cannot be asked for. It stands in for the answer
:func:`resolve_stage_dir` gets at run time."""


def _refuse_relative(stage_dir: Path, who: str) -> None:
    """Refuse a staging directory that is not absolute, naming *who* declared it."""
    raise ValueError(
        f"{who}: 'stage_dir' must be an absolute path on the host, got {str(stage_dir)!r}. "
        "A relative path (or a '~' the transfer layer never expands) means a different "
        "directory to the transfer that puts the artifact and to the command that names it "
        "afterwards. Leave it empty to stage in the host's default_dest_dir, or in the login "
        "user's home when the host declares none."
    )


def validate_stage_dir(stage_dir: Path, who: str) -> Path:
    """Return *stage_dir* if it is empty or absolute; refuse anything else.

    Called by every built-in kind's factory, so a bad value is a lab-load
    error naming the entry rather than a wrong path discovered on the device.
    """
    if str(stage_dir) in ("", "."):
        return stage_dir
    if not stage_dir.is_absolute():
        _refuse_relative(stage_dir, who)
    return stage_dir


def _declared_or_default(stage_dir: Path, host: "Host", who: str) -> Path | None:
    """Return the staging directory as far as it is knowable without touching *host*.

    An absolute declared value, else the host's ``default_dest_dir`` when it
    declares one, else ``None`` — meaning "the login user's home", which only
    a connected host can answer.
    """
    if str(stage_dir) not in ("", "."):
        if not stage_dir.is_absolute():
            _refuse_relative(stage_dir, who)
        return stage_dir
    default = getattr(host, "default_dest_dir", None) or Path()
    if not isinstance(default, Path):
        default = Path(default)
    if str(default) in ("", "."):
        return None
    if not default.is_absolute():
        raise ValueError(
            f"host {getattr(host, 'id', '?')}: 'default_dest_dir' is {str(default)!r}, which is "
            "not absolute, so it cannot be used as a staging directory — the transfer joins a "
            "relative value onto the login directory while a command resolves it against the "
            "shell's own. Make it absolute, or give the product an absolute 'stage_dir'."
        )
    return default


def stage_dir_key(stage_dir: Path, host: "Host", who: str) -> str:
    """Return the staging directory as a comparable KEY, without connecting to *host*.

    The lab-load half of :func:`resolve_stage_dir`, for the per-host collision
    check: same inputs, same precedence, but an undiscovered login home comes
    back as :data:`LOGIN_HOME` rather than a path. Two entries that both fall
    through to the home therefore share a key — which is the collision that
    matters — and a host whose home IS already known keys on the real path.
    """
    known = _declared_or_default(stage_dir, host, who)
    if known is not None:
        return str(known)
    cached = getattr(host, "cached_login_home", None)
    return str(cached) if cached is not None else LOGIN_HOME


async def resolve_stage_dir(stage_dir: Path, host: "Host", who: str = "product") -> Path:
    """Resolve a declared staging directory to an ABSOLUTE path on *host*.

    One rule, every kind, in precedence order:

    1. a declared ``stage_dir`` — which must be absolute (:func:`validate_stage_dir`);
    2. the host's ``default_dest_dir``, when it declares one (absolute too);
    3. the login user's home, discovered once per host object
       (``host.login_home()``).

    The answer is always absolute, deliberately. A relative destination is
    resolved by whoever reads it, and the readers disagree: a transfer lands
    it in the login/SFTP directory while a command resolves it against the
    SHELL's current directory — the same place only on a direct SSH exec
    channel, and a different one the moment the host takes the pooled-shell
    route (telnet, a proxied login, a ``session_setup`` hook that changes
    directory). An absolute path removes the question instead of encoding an
    answer that two layers would read differently.

    Raises
    ------
    ValueError
        If a declared value is relative, if ``default_dest_dir`` is relative,
        or if the host has no login-home concept at all (an embedded target
        with no filesystem) and declares no ``default_dest_dir`` — there is
        then nothing this could honestly answer. ``ValueError`` because every
        one of those is a statement the LAB or the entry made and the reader
        has to go and edit; the host-side failure of step 3 is a command that
        did not answer, and ``login_home`` raises
        :class:`~otto.host.errors.HostCommandError` for it — the taxonomy's
        own split between "your config is wrong" and "the device said no".
    """
    known = _declared_or_default(stage_dir, host, who)
    if known is not None:
        return known
    discover = getattr(host, "login_home", None)
    if discover is None:
        raise ValueError(
            f"host {getattr(host, 'id', '?')}: cannot stage an artifact — this host has no "
            "login home to fall back on. Declare an absolute 'default_dest_dir' on the host, "
            "or an absolute 'stage_dir' on the entry."
        )
    return await discover()


class Product(ABC):
    """A unit of software-under-test deployed to a host (behavior contract)."""

    stage_dir: Path = Path()
    """Directory on the host this product's artifact is staged into, in the
    HOST's path domain. Empty (the default) means the host's own
    ``default_dest_dir`` -- see :meth:`resolved_stage_dir`. Every kind reads
    the same field: a ``shell`` product's artifact STAYS there (it is the
    product), while the transient kinds (``kmod``, ``docker_image``, the
    kernel-module dev tools) delete their staged copy once it is consumed."""

    name: str
    """Logical identity — used for logging, ``is_installed`` lookups, dedup,
    and as the ``<product>`` segment of the run tree (see :mod:`otto.layout`):
    a single path segment, never ``debug``."""

    owner: str | None = None
    """Owning repo's name, stamped at lab ingest from the registering-repo
    marker (see :func:`otto.registry.registering_repo`). ``None`` = attached
    outside any repo's init import. Default per-repo actions filter on this."""

    cov_dir: str | None = None
    """Host-side directory this product writes its coverage counters under —
    the value it hands to ``GCOV_PREFIX``. A string in the HOST's path domain
    (it only ever appears in shell commands), distinct from the CLI's local
    ``--cov-dir`` staging root. ``None`` means the default ``/tmp/<name>``,
    which ingest stamps (:func:`stamp_cov_dir`) so a code product can read
    ``self.cov_dir`` when composing its install or run command."""

    debug_log_globs: Sequence[str] = ()
    """Host paths (literal or glob) of this product's own debug logs, hauled
    into ``logs/<host>/<name>/debug/`` by :meth:`get_debug_logs`. An immutable
    empty default on the ABC; subclasses assign their own list."""

    @property
    def stages_artifact(self) -> bool:
        """Whether this product puts a FILE at ``<stage_dir>/<artifact basename>``.

        False on the base: a product is free to install itself from a package
        feed, a container registry or the device's own loader and place no
        file at all. The per-host staging-collision check
        (:func:`otto.host.factory.apply_providers`) keys on this -- two
        products that stage nothing cannot overwrite each other.
        """
        return False

    async def resolved_stage_dir(self, host: "Host") -> Path:
        """Return :attr:`stage_dir` resolved against *host* (:func:`resolve_stage_dir`).

        The single seam every kind goes through, so a kind's ``install`` can
        never name a different directory than its ``stage`` put the artifact
        in. Async because the fallback is the host's login home, which only
        the host can answer (read once per host object and cached; see
        :meth:`~otto.host.unix_host.UnixHost.login_home`).

        THE ONE OWNER of resolution: what this returns is absolute and is
        handed to ``put`` / ``host.load`` / a command line as-is. Nothing
        downstream resolves it again — applying the rule twice is how a
        relative answer became ``<home>/<home>/x.ko``.
        """
        return await resolve_stage_dir(self.stage_dir, host, who=f"product {self.name!r}")

    def stage_key(self, host: "Host") -> str:
        """:attr:`stage_dir` as a comparable key at lab load (:func:`stage_dir_key`)."""
        return stage_dir_key(self.stage_dir, host, who=f"product {self.name!r}")

    @abstractmethod
    async def stage(self, host: "Host") -> Result:
        """Transfer/place this product's artifacts onto *host* (no install).

        Return any :class:`~otto.result.Result` — a bare one, or the
        :class:`~otto.result.CommandResult` of the command that did the work,
        whose retcode and output then reach the CLI's exit code untouched.
        """
        ...

    @abstractmethod
    async def install(self, host: "Host") -> Result:
        """Install this product's already-staged artifacts on *host*."""
        ...

    @abstractmethod
    async def uninstall(self, host: "Host") -> Result:
        """Remove this product from *host*."""
        ...

    @abstractmethod
    async def is_installed(self, host: "Host") -> bool:
        """Return True when this product is currently installed on *host*."""
        ...

    async def get_logs(self, host: "Host", dest: Path) -> Result:  # noqa: ARG002 — required by the Product.get_logs hook signature; overrides use host/dest, this retrieves-nothing default does not
        """Retrieve this product's log files into local directory *dest*.

        Default: retrieves nothing, successfully — zero logs is not a
        failure. Override when the product produces logs; retrieval need
        not run on *host* (an external mechanism is fine). Write files
        under *dest*; the caller owns the directory layout above it.
        """
        return Result(Status.Success)

    async def get_debug_logs(self, host: "Host", dest: Path) -> Result:
        """Fetch :attr:`debug_log_globs` matches into local directory *dest*.

        Same rules as the host-level haul: literal entries fetched as declared,
        a glob needs the host's ``glob`` and fails loud without it, zero logs
        is success. Override for a product whose debug logs come another way.
        """
        return await haul_globs(host, self.debug_log_globs, dest, who=f"product {self.name!r}")

    def instrumented(self) -> bool | None:
        """Whether this product's build carries coverage instrumentation.

        ``True``/``False`` when known, ``None`` when this product cannot tell
        (the ABC default — a code product that is not a :class:`ShellProduct`
        overrides this, typically by scanning its artifact with
        :func:`scan_for_instrumentation`). Local and synchronous: it runs
        before anything executes, over the artifact on the otto machine.
        """
        return None

    async def prepare_coverage(self, host: "Host") -> Result:  # noqa: ARG002 — hook signature; the default touches nothing on the host
        """Put this product's counters on disk under :attr:`cov_dir` before a fetch.

        Default: nothing to do, success — a user-space build writes its
        ``.gcda`` files as it runs. A kind whose counters live somewhere else
        first (a kernel module's live in the kernel) overrides this to
        materialise them; a failure skips the product for that run and is
        logged with host and product.
        """
        return Result(Status.Success)

    async def reset_coverage(self, host: "Host") -> Result:
        """Zero this product's counters on *host*.

        Default: delete every ``.gcda`` under :attr:`cov_dir` — the pre-run
        ``--cov-clean``, the post-fetch clean and ``otto cov clean`` all come
        through here. A kind whose counters also live elsewhere overrides
        this to zero them first, then awaits this default to finish.

        Raises:
            ValueError: :attr:`name` is not a single safe path segment;
                checked before any command is issued.
        """
        layout.validate_product_name(self.name)
        return await host.exec(f"{gcda_find_cmd(cov_dir_of(self))} -delete", timeout=60)


def cov_dir_of_name(name: str) -> str:
    """Return the default :attr:`Product.cov_dir` for a product called *name*."""
    return f"/tmp/{name}"  # noqa: S108 — the documented default


def cov_dir_of(product: Product) -> str:
    """*product*'s effective :attr:`Product.cov_dir`: the explicit value, else ``/tmp/<name>``."""
    return product.cov_dir or cov_dir_of_name(product.name)


def stamp_cov_dir(product: Product) -> None:
    """Make :attr:`Product.cov_dir` concrete (ingest does this once per product)."""
    if product.cov_dir is None:
        product.cov_dir = cov_dir_of(product)


def gcda_find_cmd(cov_dir: str) -> str:
    """``find <cov_dir> -name '*.gcda' -type f``, with *cov_dir* shell-quoted.

    The quoting is load-bearing on the reset path, which appends ``-delete``:
    an unquoted ``/opt/My App/cov`` would reach ``find`` as TWO start points,
    the second of them relative to the shell's cwd.
    """
    return f"find {shlex.quote(cov_dir)} -name '*.gcda' -type f"


async def sudo_gcda_delete(product: Product, host: "Host") -> Result:
    """Delete every ``.gcda`` under *product*'s :attr:`~Product.cov_dir`, elevated.

    Shared by kinds whose counters are written as root — a kernel module's
    are the kernel's own (:mod:`otto.host.kmod_kind`), a container's are
    whatever user its process ran as (:mod:`otto.host.docker_image_kind`).
    Validates :attr:`Product.name` first, exactly as the unelevated default
    (:meth:`Product.reset_coverage`) does, then runs the same find+delete line
    through ``host.run(..., sudo=True)``. A host declining under
    ``--dry-run`` answers :attr:`~otto.utils.Status.NotRun`; that is
    returned as-is, never mapped to :attr:`~otto.utils.Status.Error` — the
    dry-run contract requires a decline to reach the fetcher unchanged
    (``docs/superpowers/specs/2026-08-15-dry-run-contract-design.md``).

    Raises:
        ValueError: :attr:`Product.name` is not a single safe path segment;
            checked before any command is issued.
    """
    layout.validate_product_name(product.name)
    cov_dir = cov_dir_of(product)
    result = await host.run(f"{gcda_find_cmd(cov_dir)} -delete", sudo=True)
    if result.status is Status.NotRun:
        return Result(Status.NotRun)
    if result.is_ok:
        return Result(Status.Success)
    failure = result.first_failure
    detail = f": {failure.value.strip()}" if failure is not None else ""
    return Result(
        Status.Error, msg=f"{product.name}: deleting .gcda under {cov_dir} failed{detail}"
    )


INSTRUMENTATION_MARKERS: tuple[bytes, ...] = (b".gcda", b"__gcov_", b"__llvm_gcov")
"""Byte strings a coverage build leaves in its objects. ``.gcda`` is the
load-bearing one: GCC and clang both embed each translation unit's ``.gcda``
filename, and it survives ``strip``; the symbol prefixes catch unstripped
binaries whose filename strings were relocated."""

_SCAN_CHUNK = 1 << 20

ARCHIVE_SUFFIXES: tuple[str, ...] = (
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.xz",
    ".tar.bz2",
    ".zip",
    ".gz",
    ".xz",
    ".bz2",
    ".zst",
)
"""Artifact names the scan never opens. An archive's bytes say nothing about
the objects inside it — a compressed one hides every marker, an uncompressed
one may show a marker from a file that is not the product — so it answers
``None`` (unknown), which is what puts the ``instrumented = true`` remedy in
front of the reader."""


def _scan_file(path: Path) -> bool | None:
    """Scan one file, or ``None`` when it cannot be read (permissions, races)."""
    overlap = max(len(m) for m in INSTRUMENTATION_MARKERS) - 1
    tail = b""
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(_SCAN_CHUNK)
                if not chunk:
                    return False
                window = tail + chunk
                if any(marker in window for marker in INSTRUMENTATION_MARKERS):
                    return True
                tail = window[-overlap:]
    except OSError:
        return None


def scan_for_instrumentation(path: Path) -> bool | None:
    """Scan *path* for :data:`INSTRUMENTATION_MARKERS`.

    A regular file answers ``True``/``False``, or ``None`` when it cannot be
    read (permissions, a race with deletion) or when its name ends in one of
    :data:`ARCHIVE_SUFFIXES` — the scan cannot see inside archives, so the
    tri-state means "cannot tell", not "clean".
    A directory answers ``True`` when any regular file under it hits, else
    ``None`` — an unreadable member is skipped rather than raised, so a clean
    directory is unknown, not clean.
    A missing path is ``None``.
    """
    if path.is_file():
        if path.name.lower().endswith(ARCHIVE_SUFFIXES):
            return None
        return _scan_file(path)
    if path.is_dir():
        for candidate in sorted(path.rglob("*")):
            if candidate.is_file() and _scan_file(candidate) is True:
                return True
        return None
    return None


@dataclass(slots=True)
class ShellProduct(Product):
    """Convenience base for a product that *is* a single artifact file (the ``shell`` kind's base).

    ``stage()`` transfers the artifact via :meth:`~otto.host.host.Host.put`. ``name`` defaults to
    the artifact's basename. ``install``/``uninstall``/``is_installed`` remain
    abstract — they are inherently project-specific. Once the remote file-ops
    phase lands, the natural ``is_installed`` is
    ``await host.exists(self.resolved_stage_dir(host) / self.artifact.name)``.
    """

    artifact: Path
    """Local path to the artifact file to stage onto the host."""

    name: str = ""
    """Logical name; defaults to ``artifact.name`` when left empty."""

    stage_dir: Path = field(default_factory=Path)
    """See :attr:`Product.stage_dir <otto.host.product.Product.stage_dir>`; a
    dataclass field so kinds can pass it. Empty is resolved against the host's
    ``default_dest_dir`` by :meth:`~otto.host.host.Host.put` and, identically,
    by :meth:`~otto.host.product.Product.resolved_stage_dir`."""

    cov_dir: str | None = None
    """See :attr:`Product.cov_dir <otto.host.product.Product.cov_dir>`; a dataclass
    field so kinds can pass it."""

    debug_log_globs: list[str] = field(default_factory=list)
    """See :attr:`Product.debug_log_globs <otto.host.product.Product.debug_log_globs>`."""

    instrumented_override: bool | None = None
    """Forces :meth:`instrumented() <otto.host.product.Product.instrumented>`'s
    verdict when the scan cannot answer — an archive the scan cannot see
    inside, or a build whose provenance is known out of band. ``None`` = scan
    the artifact. Every declared kind's ``instrumented`` param lands here,
    which is why it lives on the base."""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.artifact.name

    @property
    @override
    def stages_artifact(self) -> bool:
        """Answer True — the artifact IS the product, and it lands under :attr:`stage_dir`."""
        return True

    @override
    async def stage(self, host: "Host") -> Result:
        """Transfer the artifact, returning ``host.put``'s result unchanged.

        ``put`` is handed the RESOLVED, absolute directory: ``put``'s own
        ``_resolve_dest`` knows the host's ``default_dest_dir`` but not the
        login-home fallback, so passing the raw value would land the artifact
        somewhere the installing command does not name.
        """
        return await host.put(self.artifact, await self.resolved_stage_dir(host))

    @override
    def instrumented(self) -> bool | None:
        """Return :attr:`instrumented_override` when set, else scan the artifact.

        The scan is :func:`scan_for_instrumentation` over :attr:`artifact`.
        """
        if self.instrumented_override is not None:
            return self.instrumented_override
        return scan_for_instrumentation(self.artifact)


ProductProvider = Callable[["Host"], Iterable[Product] | None]
"""A function that, given a host, returns the products it should carry.

Registered from a ``.otto`` init module via :func:`register_product_provider`
and run once per lab-ingested host. All product knowledge stays in product-repo
code; lab data never names a product."""

_PRODUCT_PROVIDERS: list[tuple[ProductProvider, str | None]] = []
"""Registered providers paired with the repo that registered each one."""


def register_product_provider(provider: ProductProvider) -> None:
    """Register a function that decides which products a host carries.

    Call from an init module listed in ``.otto/settings.toml`` — the same
    extension hook the other host strategies use. The provider runs once per
    lab-ingested host; inspect the host's product-agnostic attributes
    (``element.name``, ``element.id``, ``element.metadata``,
    ``element.resources``, ``os_type``, ``id``, ``ip``, ``source_lab``,
    ``metadata``)
    and return the products that host should carry (or ``None``/``[]`` for
    none). Behavior lives in code; lab data stays product-agnostic.

    The registering repo is captured **here**, not at ingest: this call runs
    inside that repo's init import, whereas the provider runs long after,
    when the marker is gone.
    """
    refuse_during_test_load(
        "product provider",
        getattr(provider, "__name__", repr(provider)),
        getattr(provider, "__module__", None) or "<unknown>",
    )
    _PRODUCT_PROVIDERS.append((provider, get_registering_repo()))


PRODUCT_KINDS: KindRegistry["Product"] = KindRegistry(
    "product kind", register_hint="otto.host.product.register_product_kind()"
)
"""Named factories for settings-declared products (spec 2026-09-01 §5-§6).

Separate from :data:`otto.host.dev_tool.DEV_TOOL_KINDS` on purpose — each
seam owns its registry, the same two-list reasoning as the providers."""


def register_product_kind(
    name: str,
    factory: "Callable[[DeclaredEntry, Host], Product]",
    *,
    overwrite: bool = False,
) -> None:
    """Register a product *kind* — the code a ``[[products]]`` entry binds to.

    Call from an init module listed in ``.otto/settings.toml``, the same
    extension hook :func:`register_product_provider` uses. The factory
    receives the parsed :class:`~otto.declared.DeclaredEntry` (whose
    ``params`` carry every non-reserved TOML key) and the matched host, and
    returns the :class:`Product` to attach; it should validate its params and
    raise ``ValueError`` naming the entry on a bad one — a misdeclared entry
    fails ingest loudly, exactly as a misconfigured provider does.
    """
    PRODUCT_KINDS.register(name, factory, overwrite=overwrite, origin=caller_module())


def apply_declared_products(host: "Host") -> None:
    """Attach the settings-declared products admitted for *host*.

    Called at the ingest chokepoint BEFORE :func:`apply_product_providers`:
    running first is the fallback contract — the provider loop's name-dedup
    then skips any code product whose name a declared entry already claimed,
    so config wins and code fills the gaps. Entry collection and the §5
    ``[project]`` gate live in :func:`otto.declared.declared_for_host`;
    matching, first-match-wins and owner stamping in
    :meth:`~otto.declared.KindRegistry.build`. A product whose name the host
    already carries is skipped, the provider loop's identical guard.
    """
    seen = {p.name for p in host.products}
    for product in PRODUCT_KINDS.build(declared_for_host(host, "declared_products"), host):
        if product.name in seen:
            logger.debug(
                "declared product: skipping duplicate %r on host %s", product.name, host.id
            )
            continue
        host.products.append(product)
        seen.add(product.name)


def apply_product_providers(host: "Host") -> None:
    """Run every registered provider against *host*, attaching their products.

    Called at the single lab-ingest chokepoint
    (:func:`otto.host.factory.create_host_from_dict`). Providers run in
    registration order and their results are concatenated onto
    ``host.products``. A product whose :attr:`Product.name` already appears on
    the host is skipped (deduplication guards two overlapping providers). A
    provider that raises propagates — a misconfigured provider fails ingest
    loudly.

    Each attached product is stamped with :attr:`Product.owner` — the repo that
    registered the provider — unless the product already names an owner, which
    lets one repo hand a product to another's ownership deliberately.

    A provider is SKIPPED — not called — when its registering repo's
    ``[project]`` declaration does not target ``(host.source_lab, host.id)``
    (spec §5). This is the admission half of scoping: the fleet walks bound
    which hosts a repo may reach, and without this a repo declared for one lab
    still hangs its products on every host of every other lab the run happens
    to load. Skipping before the call rather than filtering the return is the
    point — a provider that ran has already been handed a machine its repo
    never declared, and providers inspect hosts and keep their own state.

    Two carve-outs admit, both because a gate that cannot compute a narrowing
    must narrow nothing. An UNSTAMPED host (``source_lab == ""``) is not
    judged at all: hosts built outside the loader — direct
    :func:`~otto.host.factory.create_host_from_dict` use, container hosts, the
    built-in ``local`` — predate scoping and behave exactly as before. And an
    owner whose declaration cannot be resolved admits, which
    :func:`~otto.config.scope.scope_for_repo` decides and documents.
    """
    from ..config.scope import repo_targets, scope_for_repo  # function-scope: import-light seam

    seen = {p.name for p in host.products}
    for provider, provider_owner in _PRODUCT_PROVIDERS:
        if host.source_lab and not repo_targets(
            scope_for_repo(provider_owner), host.source_lab, host.id
        ):
            logger.debug(
                "product provider: repo %r does not target host %s of lab %r — not run",
                provider_owner,
                host.id,
                host.source_lab,
            )
            continue
        for product in provider(host) or ():
            if product.name in seen:
                logger.debug(
                    "product provider: skipping duplicate %r on host %s",
                    product.name,
                    host.id,
                )
                continue
            if product.owner is None:
                product.owner = provider_owner
            host.products.append(product)
            seen.add(product.name)
