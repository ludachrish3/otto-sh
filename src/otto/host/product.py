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

Products are customized in project config or code, never lab data: a
``[[products]]`` entry in ``.otto/settings.toml`` declares the common cases
(see :mod:`otto.declared` and :func:`register_product_kind`), and a
:func:`register_product_provider` callback from a ``.otto`` init module
remains the code route for whatever the match table cannot express. A product
is defined in data OR in code: a provider product whose name a declared entry
holds is refused at ingest, naming both (:func:`defined_twice`). Lab data stays product-agnostic and
evolves independently of product code; declaring products *in* lab data is
deliberately **not** supported.
"""

import logging
import posixpath
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, ClassVar, Literal, NoReturn

from typing_extensions import TypeIs, final, override

from .. import layout
from ..declared import (
    DeclaredEntry,
    KindBuilder,
    KindEntry,
    KindFactory,
    _check_resolved_kind_entry,
    _validate_kind_entry,
    declared_for_host,
)
from ..registry import Ref, Registry, Subscription, registration_boundary
from ..result import Result
from ..utils import Status
from .log_haul import haul_globs

if TYPE_CHECKING:
    from ..result import CommandResult
    from .file_ops import PosixFileOps
    from .host import Host

__all__ = [
    "DeclaredEntry",
    "Product",
    "ProductPlan",
    "ShellProduct",
    "ensure_stage_dir",
    "gcov_prefix_strip",
    "mkdir_line",
    "planned_stage_dir",
    "put_line",
    "register_product_kind",
    "register_product_provider",
    "stage_lines",
    "sudo_line",
    "unplanned",
]

logger = logging.getLogger(__name__)


LOGIN_HOME = "the host's login home"
"""How an undiscovered login home is named in a lab-load message.

:func:`stage_dir_key` runs at lab ingest, where nothing is connected and the
real home cannot be asked for. It stands in for the answer
:func:`resolve_stage_dir` gets at run time."""


LOGIN_HOME_PLACEHOLDER = "<login home>"
"""How a plan names a staging directory that only a connected host can answer."""


@final
@dataclass
class ProductPlan:
    """What a product's hooks would do on one host, from configuration alone."""

    stage: list[str] = field(default_factory=list)
    """One line per transfer or command ``stage`` makes, in order."""

    install: list[str] = field(default_factory=list)
    """One line per transfer or command ``install`` makes, in order."""

    uninstall: list[str] = field(default_factory=list)
    """One line per transfer or command ``uninstall`` makes, in order."""

    unchecked: list[str] = field(default_factory=list)
    """One line per fact a real run reads off the host that this preview could
    not; each says what the missing read decides."""


@dataclass
class StageDirPlan:
    """A staging directory as a plan names it, with the gap when it is the login home."""

    directory: str
    """The staging directory as plan lines name it, a path or ``LOGIN_HOME_PLACEHOLDER``."""

    unchecked: str | None = None
    """The gap line when *directory* is the placeholder; ``None`` when the directory is known."""

    refusal: str | None = None
    """Why a real run refuses to stage here, or ``None`` when it does not.

    Set when the host has no login home to fall back on. A plan whose staging
    is refused has no transfer and no command to show: it returns this line
    alone as its ``unchecked``, as the embedded kind does for a host without a
    loader."""


def put_line(*, local: Path, remote_dir: str) -> str:
    """Return the plan's line for ``host.put(local, remote_dir)``."""
    return f"PUT {local} -> {remote_dir}"


def mkdir_line(*, directory: str) -> str:
    """Return the plan's line for :func:`ensure_stage_dir` making *directory*.

    Quoted as the host's ``mkdir`` quotes it; :data:`LOGIN_HOME_PLACEHOLDER`
    is shown as is, since it names a directory rather than spelling one.
    """
    from .shell_quote import path_operand  # function-local: off the host import path

    shown = directory if directory == LOGIN_HOME_PLACEHOLDER else path_operand(directory)
    return f"mkdir -p {shown}"


def stage_lines(*, host: "Host", local: Path, directory: str) -> list[str]:
    """Return the plan's lines for staging *local* into *directory* on *host*.

    The ``mkdir -p`` :func:`ensure_stage_dir` runs first, on a host that has a
    shell ``mkdir``, then the transfer.
    """
    made = [mkdir_line(directory=directory)] if _creates_stage_dir(host) else []
    return [*made, put_line(local=local, remote_dir=directory)]


def sudo_line(*, command: str) -> str:
    """Return the plan's line for ``host.run(command, sudo=True)``."""
    return f"sudo {command}"


def planned_stage_dir(*, stage_dir: Path, host: "Host", who: str) -> StageDirPlan:
    """Name the staging directory a plan can know, and the gap when it cannot.

    The same precedence as :func:`resolve_stage_dir`, never contacting *host*:
    a declared value, the host's ``default_dest_dir``, a login home the host
    has already cached. An undiscovered login home becomes
    :data:`LOGIN_HOME_PLACEHOLDER` plus one ``unchecked`` line; a host with no
    login home at all (an embedded target) is the refusal
    :func:`resolve_stage_dir` raises, so it comes back with a ``refusal``.
    """
    known = _declared_or_default(stage_dir, host, who)
    if known is not None:
        return StageDirPlan(str(known))
    cached = getattr(host, "cached_login_home", None)
    if cached is not None:
        return StageDirPlan(str(cached))
    host_id = getattr(host, "id", "?")
    if getattr(host, "login_home", None) is None:
        return StageDirPlan(
            LOGIN_HOME_PLACEHOLDER,
            refusal=(
                f"{who} declares no stage_dir and {host_id} no default_dest_dir, and {host_id} "
                "has no login home to fall back on: the install is refused"
            ),
        )
    return StageDirPlan(
        LOGIN_HOME_PLACEHOLDER,
        f"the login home — {who} declares no stage_dir and {host_id} no default_dest_dir",
    )


def unplanned(*, obj: object) -> ProductPlan:
    """Return the plan of a product or tool that does not describe itself.

    Empty step lists and one ``unchecked`` line naming *obj*'s class: the
    shared body of :meth:`Product.plan` and
    :meth:`~otto.host.dev_tool.DevTool.plan`, so the two bases cannot drift.
    """
    cls = type(obj)
    return ProductPlan(
        unchecked=[
            (
                f"`{cls.__module__}.{cls.__qualname__}`: stage, install and uninstall are "
                "Python code and are not previewed; implement plan() to describe them"
            )
        ]
    )


_STAGE_DIR_REMEDY = (
    "A relative path (or a '~' the transfer layer never expands) means a different "
    "directory to the transfer that puts the artifact and to the command that names it "
    "afterwards. Leave it empty to stage in the host's default_dest_dir, or in the login "
    "user's home when the host declares none."
)


def _refuse_relative(
    value: "Path | str", who: str, *, field: str = "stage_dir", remedy: str = _STAGE_DIR_REMEDY
) -> NoReturn:
    """Refuse a host directory *field* that is not absolute, naming *who* declared it."""
    raise ValueError(
        f"{who}: {field!r} must be an absolute path on the host, got {str(value)!r}. {remedy}"
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


def _creates_stage_dir(host: object) -> "TypeIs[PosixFileOps]":
    """Return whether staging on *host* makes a missing directory first (a shell ``mkdir``).

    The one predicate :func:`ensure_stage_dir` and :func:`stage_lines` share, so
    the plan names the ``mkdir -p`` exactly when the hook runs it.
    """
    from .file_ops import PosixFileOps  # local: keeps the host graph off the product import path

    return isinstance(host, PosixFileOps)


async def ensure_stage_dir(*, host: "Host", directory: Path, who: str) -> Result:
    """Create the resolved staging *directory* on *host* (``mkdir -p``) before a transfer.

    A remote transfer to a directory that does not exist writes the artifact
    ONTO that path as a file, and the next step (``chmod``, ``insmod``,
    ``docker load``) then fails far from the cause. Every staging kind calls
    this with the directory it is about to hand ``put`` or ``load``, so a
    missing ``stage_dir`` is made rather than clobbered; only a failed
    creation is an error, and its ``msg`` carries the shell's reason.

    Hosts without a shell ``mkdir`` (embedded targets) are left alone, as is a
    dry run: the transfer that follows announces the staging itself.
    """
    from .host import is_dry_run  # local: keeps the host graph off the product import path

    if not _creates_stage_dir(host) or is_dry_run():
        return Result(status=Status.Success)
    made = await host.mkdir(directory)
    if made.is_ok:
        return Result(status=Status.Success)
    reason = made.msg.strip() or f"mkdir exited {made.status.name}"
    return Result(
        status=made.status,
        msg=(
            f"{who}: cannot create stage_dir {str(directory)!r} on host "
            f"{getattr(host, 'id', '?')}: {reason}"
        ),
    )


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
    """Owning project's name, stamped at lab ingest from the registering-project
    marker (see :func:`otto.registry.registering_project`). ``None`` = attached
    outside any project's init import. Default per-project actions filter on this."""

    cov_dir: str | None = None
    """Host-side directory this product writes its coverage counters under —
    the value it hands to ``GCOV_PREFIX``. A string in the HOST's path domain
    (it only ever appears in shell commands), distinct from the local
    staged ``<run>/cov`` root. ``None`` means the default ``/tmp/<name>``,
    which ingest stamps (:func:`stamp_cov_dir`) so a code product can read
    ``self.cov_dir`` when composing its install or run command.

    It must be absolute, normalized and at least two components deep
    (``/tmp/x``, never ``/tmp``), and no two products on one host may share
    it or nest one inside the other: ingest refuses the lab otherwise
    (:func:`validate_cov_dir`, :func:`~otto.host.factory.check_cov_dir_overlaps`).
    Uninstall leaves it alone; cleanup removes the ``.gcda`` counters under
    it and the directories they leave empty (:mod:`otto.host.cov_sweep`)."""

    build_root: PurePosixPath | None = None
    """The directory this product's build ran in, as the build machine saw it.

    The one input to :attr:`gcov_prefix_strip`: an instrumented binary bakes
    each ``.gcda`` path in under this directory, and stripping its components
    leaves the counters at their build-relative paths under :attr:`cov_dir`.
    A declared product always has one (the ``build_root`` param, else the
    declaring project's root); ``None`` (the default) is a code product that
    did not say, and reading :attr:`gcov_prefix_strip` on it raises."""

    cov_dir_root_owned: ClassVar[bool] = False
    """Whether this kind's :attr:`cov_dir` is written as root, so it is cleared elevated.

    A property of the kind, never of one product: ``docker_image`` (docker
    creates the bind-mount source as root) and ``kmod`` (the kernel writes
    the counters) set it. The counter reset (:meth:`reset_coverage`) and the
    cleanup sweep both read it, so the dir has one answer to "who may delete
    here". The sweep's probe still asks unelevated first: a readable
    root-owned dir needs no sudo to look into."""

    kind: str = "code"
    """The declared kind that built this product (``"shell"``, ``"kmod"``, ...),
    stamped by :meth:`~otto.declared.KindBuilder.build`; ``"code"`` for a
    product a provider constructed. Read by ``otto --list-products``."""

    origin: str = "provider"
    """The origin of this product, ``"declared"`` (a ``[[products]]`` entry) or
    ``"provider"`` (a :func:`register_product_provider` callback). Stamped at
    ingest; read by ``otto --list-products``."""

    source_entry: "DeclaredEntry | None" = None
    """The ``[[products]]`` entry that built this product, stamped by
    :meth:`~otto.declared.KindBuilder.build`; ``None`` for a provider product.
    ``otto --list-products`` uses it to tell which entries built somewhere."""

    debug_log_globs: Sequence[str] = ()
    """Host paths (literal or glob) of this product's own debug logs, hauled
    into ``logs/<host>/<name>/debug/`` by :meth:`get_debug_logs`. An immutable
    empty default on the ABC; subclasses assign their own list."""

    @property
    def gcov_prefix_strip(self) -> int:
        """The ``GCOV_PREFIX_STRIP`` that pairs with :attr:`build_root` (:func:`gcov_prefix_strip`).

        Raises:
            ValueError: :attr:`build_root` is ``None`` -- the strip is never a
                silent 0 -- or is not absolute, or contains ``..``.
        """
        if self.build_root is None:
            raise ValueError(
                f"product {self.name!r} has no build_root, so otto cannot compute "
                "GCOV_PREFIX_STRIP; set Product.build_root to the directory its build ran in"
            )
        try:
            return gcov_prefix_strip(build_root=self.build_root)
        except ValueError as e:
            raise ValueError(f"product {self.name!r}: {e} (got {str(self.build_root)!r})") from e

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

    def stage_key(self, *, host: "Host") -> str:
        """:attr:`stage_dir` as a comparable key at lab load (:func:`stage_dir_key`)."""
        return stage_dir_key(self.stage_dir, host, who=f"product {self.name!r}")

    def plan(self, host: "Host") -> ProductPlan:
        """Describe what ``stage``, ``install`` and ``uninstall`` would do on *host*.

        Pure and synchronous: built from configuration and *host*'s declared
        attributes, never from a command. The base does not guess: empty step
        lists and one ``unchecked`` line naming this class, so a code-defined
        product shows as an honest gap until its author describes it. The
        built-in kinds override this, and a unit test runs their real hooks
        against a recording host to keep the description equal to the deed.
        """
        del host  # the base describes the class, not the host
        return unplanned(obj=self)

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

    async def get_logs(self, host: "Host", *, dest: Path) -> Result:  # noqa: ARG002 — required by the Product.get_logs hook signature; overrides use host/dest, this retrieves-nothing default does not
        """Retrieve this product's log files into local directory *dest*.

        Default: retrieves nothing, successfully — zero logs is not a
        failure. Override when the product produces logs; retrieval need
        not run on *host* (an external mechanism is fine). Write files
        under *dest*; the caller owns the directory layout above it.
        """
        return Result(status=Status.Success)

    async def get_debug_logs(self, host: "Host", *, dest: Path) -> Result:
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
        return Result(status=Status.Success)

    async def reset_coverage(self, host: "Host") -> Result:
        """Zero this product's counters on *host*.

        Default: delete every ``.gcda`` under :attr:`cov_dir` and the dirs
        below it those deletions left empty
        (:func:`~otto.host.product.gcda_prune_files`), then check that none
        is left — the pre-run ``--cov-clean``, the post-fetch clean and
        ``otto cov clean`` all come through here. A :attr:`cov_dir` that does
        not exist yet has nothing to clear and succeeds (see
        :func:`~otto.host.product.gcda_delete_cmd`); a counter that survives
        the removal, or a check that fails, is an Error naming it
        (:func:`~otto.host.product.gcda_check_cmd`). A kind whose counters
        also live elsewhere overrides this to zero them first, then awaits
        this default to finish.

        Raises:
            ValueError: :attr:`name` is not a single safe path segment;
                checked before any command is issued.
        """
        return await _clear_gcda(self, host)


def cov_dir_of_name(name: str) -> str:
    """Return the default :attr:`Product.cov_dir` for a product called *name*."""
    return f"/tmp/{name}"  # noqa: S108 — the documented default


def cov_dir_of(product: Product) -> str:
    """*product*'s effective :attr:`Product.cov_dir`: the explicit value, else ``/tmp/<name>``."""
    return product.cov_dir or cov_dir_of_name(product.name)


def gcov_prefix_strip(*, build_root: PurePosixPath) -> int:
    """Return the ``GCOV_PREFIX_STRIP`` for a build that ran in *build_root*.

    THE ONE RESOLVER: every component of *build_root* is stripped, so a
    counter's baked path lands under ``GCOV_PREFIX`` at its path relative to
    the build root. Pure: the path is never resolved and no filesystem is
    read, because it names a directory on the build machine, which need not
    be this one.

    Raises:
        ValueError: *build_root* is relative (there is no count to take) or
            contains ``..`` (its depth is not the depth the compiler baked).
            The message names the rule, not the value: each caller quotes the
            value as its user wrote it.
    """
    if not build_root.is_absolute():
        raise ValueError("'build_root' must be absolute")
    if ".." in build_root.parts:
        raise ValueError(
            "'build_root' must not contain '..': otto never resolves it, so write the "
            "directory the build ran in"
        )
    return len(build_root.parts) - 1


def is_instrumented(product: Product) -> bool:
    """Whether *product*'s build is known to be instrumented: ``instrumented() is True``.

    A product that cannot tell (``None``) is not. The one predicate behind
    :func:`instrumented_products`, and behind every other place that acts on
    a product's coverage only when otto will also clean and fetch it.
    """
    return product.instrumented() is True


def instrumented_products(host: Any, products: Iterable[Product] | None = None) -> list[Product]:
    """*host*'s products whose build is known to be instrumented (``is_instrumented``).

    The counter reset, the fetch and the cleanup sweep all ask this one
    predicate. *products* narrows the question to a subset of the host's
    products (one owner's, say); ``None`` means all of them.
    """
    return [p for p in (host.products if products is None else products) if is_instrumented(p)]


def cov_dir_products(host: Any, products: Iterable[Product]) -> list[Product]:
    """Those of *products* that have a ``cov_dir`` on *host* for coverage to probe or sweep.

    :func:`instrumented_products` first, then the two exclusions the counter
    reset does not need: an :class:`~otto.host.embedded_host.EmbeddedHost`
    has no host directory at all (its counters live in the firmware), and
    neither does a ``kind = "embedded"`` product. The presence probe, the
    leftover probe, the cleanup sweep and its preview all ask this one
    predicate, so a shell command never reaches a Zephyr console.
    """
    from .embedded_host import EmbeddedHost  # function-local: off the host import path

    if isinstance(host, EmbeddedHost):
        return []
    return [p for p in instrumented_products(host, products) if p.kind != "embedded"]


def stamp_cov_dir(product: Product) -> None:
    """Make :attr:`Product.cov_dir` concrete (ingest does this once per product)."""
    if product.cov_dir is None:
        product.cov_dir = cov_dir_of(product)


_COV_DIR_MIN_PARTS = 3
"""``/`` plus two components: ``PurePosixPath("/tmp/x").parts`` is ``("/", "tmp", "x")``."""


def validate_cov_dir(cov_dir: str, who: str) -> None:
    """Refuse a :attr:`Product.cov_dir` that is not absolute, normalized and two components deep.

    Ingest calls it right after :func:`stamp_cov_dir`, for every product.
    The rule keeps the coverage sweep's start path the directory the product
    meant: no ``..``, ``.``, ``//`` or trailing slash for ``find`` to read
    differently, and never ``/`` or a top-level directory such as ``/tmp``,
    whose counters could belong to anything on the host.
    """
    if not cov_dir.startswith("/"):
        _refuse_relative(
            cov_dir,
            who,
            field="cov_dir",
            remedy=(
                "Give the absolute host directory the product writes its counters under, "
                "for example /tmp/<name>."
            ),
        )
    if (
        cov_dir != posixpath.normpath(cov_dir)
        or cov_dir.startswith("//")
        or len(PurePosixPath(cov_dir).parts) < _COV_DIR_MIN_PARTS
    ):
        raise ValueError(
            f"{who}: 'cov_dir' must be absolute, normalized, at least two components "
            f"(e.g. /tmp/x), got {cov_dir!r}: no '..', '.', '//' or trailing '/'"
        )


def gcda_find_cmd(cov_dir: str) -> str:
    """``find <cov_dir> -name '*.gcda' -type f``, with *cov_dir* shell-quoted.

    The one spelling of "the counters under *cov_dir*": the removal
    (:func:`gcda_remove_files`) appends ``-exec rm``, which the reset's
    removal (:func:`gcda_prune_files`) extends to prune the dirs it emptied,
    and the listing (:func:`gcda_list_cmd`) appends ``-print``. The quoting is load-bearing:
    an unquoted ``/opt/My App/cov`` would reach ``find`` as TWO start points,
    the second of them relative to the shell's cwd.
    """
    from .shell_quote import path_operand  # function-local: off the host import path

    return f"find {path_operand(cov_dir)} -name '*.gcda' -type f"


def gcda_remove_files(cov_dir: str) -> str:
    r"""Remove every ``.gcda`` regular file under *cov_dir*, one ``rm`` per file.

    ``find <cov_dir> -name '*.gcda' -type f -exec rm -f {} \;``. POSIX
    ``-exec``, never ``-delete``: that is a GNU/BusyBox extension a BusyBox
    build may compile out (the distro's 1.36.1 does, #842). ``{} \;``, never
    ``{} +``: BusyBox 1.36.1's ``+`` ignores ``ARG_MAX``, passes a fraction of
    a large tree's paths and still exits 0 (measured; see the 2026-11-04
    coverage and docker bed batch spec). ``\;`` does not carry ``rm``'s
    status, so a zero exit proves nothing: every caller verifies afterwards
    (:func:`gcda_check_cmd`).
    """
    return f"{gcda_find_cmd(cov_dir)} -exec rm -f {{}} \\;"


_NEWLINE_GLOB = "'*\n*'"
"""``find -path``'s pattern for "a path holding a newline", single-quoted with a literal newline.

A literal newline, never ``[[:cntrl:]]``: BusyBox 1.16.1 and 1.21.1 accept
the class and never match it (measured). Single quotes carry it through
:func:`~otto.host.privilege.run_sh_elevated`'s :func:`shlex.quote` intact."""


def gcda_prune_files(cov_dir: str) -> str:
    r"""Remove every ``.gcda`` under *cov_dir*, then the dirs those removals left empty.

    The counter reset's removal: :func:`gcda_remove_files` with
    ``! -path '*<newline>*' -print`` appended, which prints a counter only
    once its ``rm`` succeeded, and never one whose path holds a newline.
    Each printed path's ancestor dirs strictly below *cov_dir* are listed by
    shell builtins alone (``case`` against the quoted *cov_dir*, the test
    :func:`path_under` makes, which relies on :func:`validate_cov_dir`
    refusing a trailing ``/`` that ``path_under`` strips and ``case`` does not, then
    ``${d%/*}``), sorted once with ``LC_ALL=C sort -ru`` so a child comes
    before its parent, and ``rmdir`` is tried on each:

    ``{ __otto_gcda_rc=$( { { <gcda_remove_files> ! -path '*<newline>*'
    -print; echo "$?" >&3; } | <ancestors> | { LC_ALL=C sort -ru 2>/dev/null || <drain>; }
    | while IFS= read -r d; do rmdir "$d" 2>/dev/null; done; } 3>&1 );
    test "$__otto_gcda_rc" = 0; }``

    One ``rm`` per counter, as cleanup's removal; one ``sort``, and one
    ``rmdir`` per distinct dir, per reset. ``rmdir`` refuses a dir that
    still holds anything, so a dir with a file of its own stays, and so do
    its parents. Only an ancestor of a removed counter is tried, so a dir
    that was already empty is not removed, and the ``case`` bound keeps
    *cov_dir* itself and everything above it out of the list for every
    path ``find`` printed: *cov_dir* stays for the next run to write under,
    and is what the pre-run presence probe
    (:func:`~otto.host.cov_sweep.probe_presence`) asked about. Cleanup,
    which owns every trace *cov_dir* included, sweeps every empty dir with
    :func:`gcda_remove_dirs` instead.

    The bound is textual: it holds only because each line read is one path
    ``find`` printed, and ``find`` never follows a symlink below its start.
    A counter whose path holds a newline would be read as two lines, the
    second a forged path that could lead ``rmdir`` through a symlink to an
    empty dir anywhere on the host (as root, for an elevated kind). So a
    counter whose path holds a newline (``_NEWLINE_GLOB``) is removed but
    never printed, and its dirs are left for cleanup.

    Removing a dir is best effort and never stops the counter removal: an
    ``rmdir`` that fails (a permission error, a dir something wrote into
    meanwhile) is silent, and a ``sort`` that fails is silent too and
    followed by a builtin-only drain of its input, so nothing upstream is killed by
    ``SIGPIPE`` with counters left to remove. A POSIX ``sh`` pipeline exits
    with its last stage's status, so ``find``'s own status is carried out
    on fd 3 and is the line's status: a ``find`` that cannot start or walk
    *cov_dir* still fails the removal. The variable that carries it keeps
    an ``__otto_`` prefix because it lands in the host's session shell.
    """
    from .shell_quote import path_operand  # function-local: off the host import path

    q = path_operand(cov_dir)
    find = f"{gcda_remove_files(cov_dir)} ! -path {_NEWLINE_GLOB} -print"
    ancestors = (
        "while IFS= read -r f; do d=${f%/*}; "
        f"while case $d in {q}/*) true;; *) false;; esac; "
        "do printf '%s\\n' \"$d\"; d=${d%/*}; done; done"
    )
    order = "{ LC_ALL=C sort -ru 2>/dev/null || while IFS= read -r _; do :; done; }"
    prune = 'while IFS= read -r d; do rmdir "$d" 2>/dev/null; done'
    return (
        f'{{ __otto_gcda_rc=$( {{ {{ {find}; echo "$?" >&3; }} | '
        f"{ancestors} | {order} | {prune}; }} 3>&1 ); "
        'test "$__otto_gcda_rc" = 0; }'
    )


def gcda_delete_cmd(cov_dir: str) -> str:
    r"""Build the counter reset's removal line: :func:`gcda_prune_files`, guarded.

    ``! test -d <cov_dir> || <gcda_prune_files>``: a directory that does not
    exist yet has nothing to clear and exits 0 — the default ``/tmp/<name>``
    is gone after a reboot, and gcov only creates it when the product first
    exits. A ``find`` that cannot start or walk *cov_dir* (a permission
    error) exits non-zero, and :func:`gcda_prune_files` makes that the
    line's status; a counter that ``rm`` could not remove does not, which is
    what the reset's check (:func:`gcda_check_cmd`) is for. The line uses
    ``!``, ``||`` and pipes, so it needs a shell; an elevated caller must
    wrap it whole (:func:`~otto.host.privilege.run_sh_elevated`), and the
    emptied dirs are then removed under the same elevation as the counters.
    """
    from .shell_quote import path_operand  # function-local: off the host import path

    return f"! test -d {path_operand(cov_dir)} || {gcda_prune_files(cov_dir)}"


def gcda_list_cmd(cov_dir: str) -> str:
    """``find <cov_dir> -name '*.gcda' -type f -print``: list the counters under *cov_dir*.

    The one spelling of the listing, unguarded: the reset's check
    (:func:`gcda_check_cmd`) and the cleanup sweep's probe
    (:mod:`otto.host.cov_sweep`) both run it.
    """
    return f"{gcda_find_cmd(cov_dir)} -print"


def gcda_remove_dirs(cov_dir: str) -> str:
    r"""Remove every directory under *cov_dir*, *cov_dir* included, that is empty.

    ``find <cov_dir> -depth -type d -exec rmdir {} \;``: children first
    (``-depth``), so a tree that held only counters goes up to and including
    *cov_dir*. ``rmdir`` refuses a non-empty directory, which is the point:
    a dir that still holds anything stays, and so do its parents. Cleanup
    only (:func:`~otto.host.cov_sweep.remove_coverage`), after
    :func:`gcda_remove_files`; the counter reset never runs it (it removes
    only the dirs it emptied, below *cov_dir*: :func:`gcda_prune_files`). Its status
    is meaningless (every refusal fails it) and is never read.
    """
    from .shell_quote import path_operand  # function-local: off the host import path

    return f"find {path_operand(cov_dir)} -depth -type d -exec rmdir {{}} \\;"


def marker_cmd(mark: str) -> str:
    r"""Build ``printf '\n%s\n' <mark>``: the line that opens a framed answer.

    A command's output on a host session is one merged stream, and whatever
    runs before the script (sudo's warnings, its answered prompt, PAM, a
    login shell) lands in it first. A framed script prints *mark* before its
    real answer, and :func:`after_marker` discards everything up to it. The
    leading newline puts a prompt that has no newline of its own (sudo on a
    LocalHost, whose stdin is a pipe) on a line apart from the answer.
    """
    return f"printf '\\n%s\\n' {mark}"


def after_marker(value: str, mark: str) -> str | None:
    """Return what follows the FIRST occurrence of *mark* in *value*; ``None`` if it never appears.

    The occurrence is matched anywhere, never as a whole line: a prompt
    without a trailing newline can share the marker's line. What precedes it
    was printed before the script ran, so it is never the script's answer.
    The reader of what follows classifies it fail-closed.
    """
    index = value.find(mark)
    return None if index < 0 else value[index + len(mark) :]


def failure_detail(output: str, retcode: int) -> str:
    """Render a failed command for a message: its output, or ``exit <rc>, no output``."""
    return output.strip() or f"exit {retcode}, no output"


_GCDA_CHECK_MARK = "otto-gcda-check"
"""The marker :func:`gcda_check_cmd` prints before ``find``'s answer."""


def gcda_check_cmd(cov_dir: str) -> str:
    r"""Build the reset's postcondition: list the ``.gcda`` files left under *cov_dir*.

    ``printf '\n%s\n' otto-gcda-check; LC_ALL=C find <cov_dir> -name '*.gcda'
    -type f -print 2>&1``. The marker (:func:`marker_cmd`) frames the answer
    so what the session printed first is never read as ``find``'s; the exit
    status is still ``find``'s, the last command. There is no ``! test -d …
    ||`` guard: ``test -d`` is false for a dir behind an unreadable ancestor
    too, so a guarded check would report "clean" for a dir nobody could look
    into. ``find`` itself answers, and :func:`classify_gcda_check` reads it.
    ``LC_ALL=C`` keeps the "No such file or directory" text that
    :func:`enoent_for_start` matches.
    """
    return f"{marker_cmd(_GCDA_CHECK_MARK)}; LC_ALL=C {gcda_list_cmd(cov_dir)} 2>&1"


_FIND_ENOENT = re.compile(r"^(?P<prog>\S+): (?P<path>.*): No such file or directory$")


def _gnu_c_quoted(path: str) -> str:
    r"""*path* as GNU findutils quotes it in a C-locale diagnostic: ``'it\'s'``, ``'a\\b'``."""
    return "'" + path.replace("\\", "\\\\").replace("'", "\\'") + "'"


def enoent_for_start(line: str, cov_dir: str) -> bool:
    """Whether *line* is ``find`` saying the start path *cov_dir* itself does not exist.

    The one matcher for "absent", shared by every probe of a ``cov_dir``. It
    accepts ``<prog>: <path>: No such file or directory`` where ``<prog>``
    is ``find`` or a path ending ``/find`` (GNU prints its ``argv[0]``), and
    ``<path>`` is *cov_dir* rendered the way a measured ``find`` renders it:
    bare (BusyBox) or GNU findutils' C-locale quoting (``_gnu_c_quoted``).
    A descendant's path, another path, or a path that merely contains the
    text does not match, and neither does a rendering no measured ``find``
    produces — that reads as an error, never as absent.
    """
    match = _FIND_ENOENT.match(line)
    if match is None:
        return False
    prog = match["prog"]
    if prog != "find" and not prog.endswith("/find"):
        return False
    return match["path"] in (cov_dir, _gnu_c_quoted(cov_dir))


@dataclass(frozen=True)
class GcdaFindError:
    """A ``find`` over a ``cov_dir`` failed: the tagged answer, never a bare string.

    Tagged so no command output — not even one that prints exactly
    ``absent`` — can collide with :func:`classify_gcda_check`'s ``"absent"``.
    """

    detail: str
    """What ``find`` printed, or ``exit <rc>, no output`` when silent (:func:`failure_detail`)."""


def path_under(line: str, cov_dir: str) -> bool:
    """Whether *line* is a path strictly below *cov_dir*, by path component."""
    return line.startswith(cov_dir.rstrip("/") + "/")


def classify_gcda_listing(
    cov_dir: str, lines: "list[str]", retcode: int
) -> "list[str] | Literal['absent'] | GcdaFindError":
    """Read ``find <cov_dir> -name '*.gcda' -type f -print``'s answer, fail-closed.

    The one reading of a counter listing (:func:`gcda_list_cmd`), shared by
    the reset's postcondition (:func:`classify_gcda_check`) and cleanup's
    probe (:mod:`otto.host.cov_sweep`). *lines* are ``find``'s non-empty
    output lines, *retcode* its exit status:

    - exit 0: the counters left (empty means clean), provided EVERY line is a
      path under *cov_dir* (:func:`path_under`) ending ``.gcda``; any other
      line is a :class:`GcdaFindError`, never a phantom counter;
    - non-zero, and exactly one line for which :func:`enoent_for_start`
      holds: ``"absent"``, nothing to check;
    - anything else: a :class:`GcdaFindError` carrying ``find``'s output.
    """
    text = "\n".join(lines)
    if retcode == 0:
        if all(path_under(line, cov_dir) and line.endswith(".gcda") for line in lines):
            return lines
        return GcdaFindError(text)
    if len(lines) == 1 and enoent_for_start(lines[0], cov_dir):
        return "absent"
    return GcdaFindError(failure_detail(text, retcode))


def classify_gcda_check(
    cov_dir: str, cmd: "CommandResult"
) -> "list[str] | Literal['absent'] | GcdaFindError":
    """Read the answer to :func:`gcda_check_cmd` for *cov_dir*, fail-closed.

    Only what follows the check's marker (:func:`after_marker`) is read; no
    marker at all is a :class:`GcdaFindError` carrying the whole output, and
    so is a zero exit otto marked failed. What follows the marker is
    :func:`classify_gcda_listing`'s to read.

    A :class:`~otto.result.NotRunResult` has no output to read: the caller
    handles a decline before asking.
    """
    body = after_marker(cmd.value, _GCDA_CHECK_MARK)
    if body is None:
        return GcdaFindError(failure_detail(cmd.value, cmd.retcode))
    if cmd.retcode == 0 and not cmd.is_ok:
        return GcdaFindError(failure_detail(body, cmd.retcode))
    return classify_gcda_listing(cov_dir, [line for line in body.splitlines() if line], cmd.retcode)


LEFTOVERS_SHOWN = 3
"""How many leftovers a failed reset or cleanup sweep names per product."""


# Spec: docs/superpowers/specs/2026-08-15-dry-run-contract-design.md
async def _clear_gcda(product: Product, host: "Host") -> Result:
    """Reset the counters: remove every ``.gcda`` under the product's ``cov_dir``, then check.

    Validates :attr:`Product.name` first. Runs :func:`gcda_delete_cmd`, then
    :func:`gcda_check_cmd`, both through the same runner — ``host.exec``, or
    for a kind whose :attr:`~Product.cov_dir_root_owned` is set,
    :func:`~otto.host.privilege.run_sh_elevated`, which wraps each line whole
    in ``sh -c`` (sudo takes an argv tail, so unwrapped it would run only
    ``!`` and the ``find`` after ``||`` would run unelevated) — so the check
    sees what the removal could. A ``--dry-run`` decline of either answers
    :attr:`~otto.utils.Status.NotRun`, never Error
    (:doc:`/extend/dry-run-contract`). Success means the removal ran **and**
    the check found no counter (or no ``cov_dir``); a counter left behind,
    or a check that failed, is an Error naming it, so stale counts never
    reach the next collection.
    """
    layout.validate_product_name(product.name)
    cov_dir = cov_dir_of(product)
    sudo = type(product).cov_dir_root_owned

    async def _run(script: str) -> "CommandResult | None":
        """*script*'s one command result, or ``None`` when the session declined it."""
        if sudo:
            from .privilege import run_sh_elevated  # function-local: off the host import path

            results = await run_sh_elevated(host, script, timeout=60)
            return None if results.status is Status.NotRun else results.only
        result = await host.exec(script, timeout=60)
        return None if result.status is Status.NotRun else result

    removal = await _run(gcda_delete_cmd(cov_dir))
    if removal is None:
        return Result(status=Status.NotRun)
    # No product name in the messages: every consumer prefixes `<host>/<product>: `.
    if not removal.is_ok:
        detail = failure_detail(removal.value, removal.retcode)
        return Result(status=Status.Error, msg=f"deleting .gcda under {cov_dir} failed: {detail}")
    check = await _run(gcda_check_cmd(cov_dir))
    if check is None:
        return Result(status=Status.NotRun)
    answer = classify_gcda_check(cov_dir, check)
    if isinstance(answer, GcdaFindError):
        return Result(
            status=Status.Error, msg=f"checking .gcda under {cov_dir} failed: {answer.detail}"
        )
    if answer == "absent" or not answer:
        return Result(status=Status.Success)
    shown = ", ".join(answer[:LEFTOVERS_SHOWN])
    return Result(
        status=Status.Error,
        msg=f"deleting .gcda under {cov_dir} left {len(answer)} file(s): {shown}",
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


@dataclass(slots=True, kw_only=True)
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

    build_root: PurePosixPath | None = None
    """See :attr:`Product.build_root <otto.host.product.Product.build_root>`; a
    dataclass field so kinds can pass it."""

    debug_log_globs: Sequence[str] = field(default_factory=list)
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
        somewhere the installing command does not name. A missing directory
        is created first (:func:`ensure_stage_dir`).
        """
        directory = await self.resolved_stage_dir(host)
        made = await ensure_stage_dir(host=host, directory=directory, who=f"product {self.name!r}")
        if not made.is_ok:
            return made
        return await host.put(self.artifact, directory)

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
and run once per lab-ingested host. All product knowledge stays in project
code; lab data never names a product."""

PRODUCT_PROVIDERS: "Subscription[ProductProvider]" = Subscription(
    "product provider", register_hint="otto.host.register_product_provider()"
)
"""The registered product providers in registration order, each with its registering project."""


def registered_product_providers() -> list[tuple[ProductProvider, str | None]]:
    """Return a snapshot of the registered product providers, in registration order.

    Each item pairs the provider with the name of the project that registered it
    (``None`` for a registration made outside any project's init import). A
    snapshot list built from the subscription.
    """
    return [(s.value, s.project) for s in PRODUCT_PROVIDERS.items()]


@registration_boundary
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

    The registering project is captured **here**, not at ingest: this call runs
    inside that project's init import, whereas the provider runs long after,
    when the marker is gone.

    A returned instance must accept ``kind``, ``origin`` and ``owner``
    attribute assignment: ingest stamps all three on it.
    """
    PRODUCT_PROVIDERS.subscribe(value=provider)


PRODUCT_KINDS: "Registry[KindEntry[Product]]" = Registry(
    "product kind",
    entry=KindEntry,
    register_hint="otto.host.product.register_product_kind()",
    validate=_validate_kind_entry,
    check_resolved=_check_resolved_kind_entry,
)
"""Named factories for settings-declared products (spec 2026-09-01 §5-§6).

Separate from :data:`otto.host.dev_tool.DEV_TOOL_KINDS` on purpose — each
seam owns its registry, the same two-list reasoning as the providers.

The built-in kinds are registered below by :class:`~otto.registry.Ref`, so no
kind module is imported until a declared entry names it.
:data:`PRODUCT_KIND_BUILDER` builds the declared entries from it."""

PRODUCT_KIND_BUILDER: "KindBuilder[Product]" = KindBuilder(
    PRODUCT_KINDS,
    class_factory=Ref(target="otto.host.shell_kind:class_entry"),
    class_resolver=Ref(target="otto.host.shell_kind:resolve_class"),
)
"""Builds the ``[[products]]`` entries a host matches, from :data:`PRODUCT_KINDS`."""


@registration_boundary
def register_product_kind(
    name: str,
    factory: "KindFactory[Product] | Ref",
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

    *factory* may be a :class:`~otto.registry.Ref` (``"module:function"``),
    imported and checked when a declared entry first names the kind.

    Raises
    ------
    TypeError
        If *factory* is not callable.
    """
    PRODUCT_KINDS.register(name=name, entry=KindEntry(factory), overwrite=overwrite)


def apply_declared_products(host: "Host") -> None:
    """Attach the settings-declared products admitted for *host*.

    Called at the ingest chokepoint BEFORE :func:`apply_product_providers`:
    running first is what lets the provider loop enforce the one-definition
    rule: a provider instance whose name a declared entry holds is refused,
    naming both. Entry collection and the §5
    ``[project]`` gate live in :func:`otto.declared.declared_for_host`;
    matching, first-match-wins and owner stamping in
    :meth:`~otto.declared.KindBuilder.build`. A product whose name the host
    already carries is skipped, the provider loop's identical guard.
    """
    seen = {p.name for p in host.products}
    for product in PRODUCT_KIND_BUILDER.build(declared_for_host(host, "declared_products"), host):
        if product.name in seen:
            logger.debug(
                "declared product: skipping duplicate %r on host %s", product.name, host.id
            )
            continue
        host.products.append(product)
        seen.add(product.name)


def defined_twice(
    seam: str, holder: Any, provider: Callable[..., Any], provider_owner: str | None
) -> str:
    """Return the one-definition refusal: a name a declared entry holds, a provider returns too.

    Names both sites — the entry by seam, name and declaring project; the provider
    by ``module:qualname`` and registering project — and says what to do instead.
    Shared by both seams so the sentence is written once.
    """
    where = (
        f"{getattr(provider, '__module__', '<unknown>')}:"
        f"{getattr(provider, '__qualname__', repr(provider))}"
    )
    noun = "dev tool" if seam == "dev_tools" else "product"
    registered = f"project {provider_owner}" if provider_owner else "registered outside any project"
    return (
        f"[[{seam}]] {holder.name!r} (project {holder.owner}) is also defined by provider "
        f"{where} ({registered}) — a {noun} is defined in data OR in code; to give "
        'a declared entry custom behaviour, set `class = "pkg.mod:Class"` on it'
    )


def apply_product_providers(host: "Host") -> None:
    """Run every registered provider against *host*, attaching their products.

    Called at the single lab-ingest chokepoint
    (:func:`otto.host.factory.create_host_from_dict`). Providers run in
    registration order and their results are concatenated onto
    ``host.products``. A product whose :attr:`Product.name` already appears on
    the host is skipped (deduplication guards two overlapping providers) —
    unless the holder is a declared entry, which is a refusal: a name is
    defined in data OR in code (:func:`defined_twice`). A
    provider that raises propagates — a misconfigured provider fails ingest
    loudly.

    Each attached product is stamped with :attr:`Product.owner` — the project that
    registered the provider — unless the product already names an owner, which
    lets one project hand a product to another's ownership deliberately. It is
    also stamped ``kind = "code"`` and ``origin = "provider"``. A product
    dropped because its name was taken is appended, with the host's id, to
    ``host.shadowed_products`` (stamped the same way) so the listing can say so.

    A provider is SKIPPED — not called — when its registering project's
    ``[project]`` declaration does not target ``(host.source_lab, host.id)``
    (spec §5). This is the admission half of scoping: the fleet walks bound
    which hosts a project may reach, and without this a project declared for one lab
    still hangs its products on every host of every other lab the run happens
    to load. Skipping before the call rather than filtering the return is the
    point — a provider that ran has already been handed a machine its project
    never declared, and providers inspect hosts and keep their own state.

    Two carve-outs admit, both because a gate that cannot compute a narrowing
    must narrow nothing. An UNSTAMPED host (``source_lab == ""``) is not
    judged at all: hosts built outside the loader — direct
    :func:`~otto.host.factory.create_host_from_dict` use, container hosts, the
    built-in ``local`` — predate scoping and behave exactly as before. And an
    owner whose declaration cannot be resolved admits, which
    :func:`~otto.declared.scope_for_project` decides and documents.
    """
    from ..config.scope import project_targets  # function-scope: import-light seam
    from ..declared import (
        scope_for_project,
    )  # at call time: a patch of the owner reaches both seams

    seen = {p.name for p in host.products}
    for provider, provider_owner in registered_product_providers():
        if host.source_lab and not project_targets(
            scope_for_project(provider_owner), host.source_lab, host.id
        ):
            logger.debug(
                "product provider: project %r does not target host %s of lab %r — not run",
                provider_owner,
                host.id,
                host.source_lab,
            )
            continue
        for product in provider(host) or ():
            if product.owner is None:
                product.owner = provider_owner
            product.kind = "code"
            product.origin = "provider"
            if product.name in seen:
                holder = next((p for p in host.products if p.name == product.name), None)
                if holder is not None and getattr(holder, "origin", "provider") == "declared":
                    raise ValueError(defined_twice("products", holder, provider, provider_owner))
                logger.debug(
                    "product provider: skipping duplicate %r on host %s",
                    product.name,
                    host.id,
                )
                # Kept, not just logged: `otto --list-products` reports a
                # product that was supplied and then dropped for a taken name.
                host.shadowed_products.append((host.id, product))
                continue
            host.products.append(product)
            seen.add(product.name)


def _register_builtin_kinds() -> None:
    """Register otto's built-in kinds by reference; no kind module is imported here."""
    for kind, module, factory in [
        ("shell", "otto.host.shell_kind", "_shell_kind"),
        ("docker_image", "otto.host.docker_image_kind", "_docker_image_kind"),
        ("kmod", "otto.host.kmod_kind", "_kmod_kind"),
        ("embedded", "otto.host.embedded_kind", "_embedded_kind"),
    ]:
        PRODUCT_KINDS.register(name=kind, entry=KindEntry(Ref(target=f"{module}:{factory}")))


_register_builtin_kinds()
