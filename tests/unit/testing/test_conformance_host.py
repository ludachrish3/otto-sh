"""``assert_host_conforms`` and ``assert_transfer_backend_conforms``.

The interesting half is the DECLARATION-against-BEHAVIOUR probe: every shipped
host family is built here the way otto's own host tests build it, and each of
its four ``user=``-taking verbs is called under a dry run and checked against
what :class:`~otto.host.capability_grid.HostCapabilities` promises for it. That
is the only place in the tree where the promise and the code meet, so a family
whose refusal drifts below its dry-run arm fails here rather than in a user's
``--dry-run``.

Every host built below is inert: no connection is opened, and the helper
installs a dry-run context around each probe. The container's parent is a mock
for the same reason otto's own docker tests use one.
"""

import contextlib
import inspect
from abc import abstractmethod
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.context import try_get_context
from otto.host.capability_grid import UserSupport, shipped_host_families
from otto.host.docker_host import DockerContainerHost
from otto.host.element import Element
from otto.host.embedded_host import ZephyrHost
from otto.host.host import is_dry_run
from otto.host.local_host import LocalHost
from otto.host.login_proxy import Cred
from otto.host.transfer import TRANSFER_BACKENDS
from otto.host.transfer.base import BaseFileTransfer, ProgressGranularity
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.result import CommandNotRunError, Result
from otto.testing import assert_host_conforms, assert_transfer_backend_conforms
from otto.testing.conformance_host import _probe_identity
from otto.utils import Status


def _mock_parent():
    """A stand-in for the lab host running the docker daemon.

    ``DockerContainerHost`` reaches its container THROUGH this object, so a
    mock is what keeps the container probes off the wire; the dry-run context
    stops the host before it calls any of these anyway.
    """
    parent = MagicMock()
    parent.id = "conformance-parent"
    parent.name = "conformance-parent"
    parent.term = "ssh"
    parent.exec = AsyncMock(return_value=None)
    parent.put = AsyncMock(return_value=Result(Status.Success, value={}))
    parent.get = AsyncMock(return_value=Result(Status.Success, value={}))
    return parent


def _unix(term: str = "ssh") -> UnixHost:
    """The default `term` is the one the `unix` row's declaration speaks for.

    See `TestTheDeclarationIsPerClassNotPerInstance` for the telnet half.
    """
    return UnixHost(
        ip="192.0.2.1",
        element=Element("conformance-unix"),
        creds=[Cred(login="u", password="p")],
        term=term,
        log=LogMode.QUIET,
    )


def _embedded(cls: "type[ZephyrHost]" = ZephyrHost) -> ZephyrHost:
    """``ZephyrHost``, not a bare ``EmbeddedHost``.

    ``EmbeddedHost`` requires a command frame; ``ZephyrHost`` supplies the
    built-in one and redeclares no capabilities, so it answers for the
    ``embedded`` row exactly as ``tests/unit/host`` builds it.

    *cls* lets a test build one of its own subclasses on the same inert
    arguments, so there is a single spelling of "an embedded host that never
    connects".
    """
    return cls(ip="192.0.2.2", element=Element("conformance-embedded"), log=LogMode.QUIET)


def _container() -> DockerContainerHost:
    return DockerContainerHost(
        parent=_mock_parent(),
        container_id="conformanceabc",
        project="conformance",
        service="api",
        compose_project="otto-conformance",
    )


INSTANCE_BUILDERS = {
    "unix": _unix,
    "embedded": _embedded,
    "zephyr": _embedded,
    "local": LocalHost,
    "container": _container,
}
"""How each shipped family is constructed for the behavioural probe.

Keyed by row name, so a family added to ``shipped_host_families`` with no
builder here fails the coverage test below rather than quietly going
structural-only.
"""


class TestEveryShippedFamilyConforms:
    @pytest.mark.parametrize("family", shipped_host_families(), ids=lambda f: f.name)
    def test_family_behaves_as_it_declares(self, family):
        """Structural rules plus the declaration-against-behaviour probe."""
        assert_host_conforms(family.cls, instance=INSTANCE_BUILDERS[family.name]())

    def test_every_family_has_a_builder(self):
        """A family with no builder would silently lose its behavioural probe."""
        missing = [f.name for f in shipped_host_families() if f.name not in INSTANCE_BUILDERS]
        assert not missing, f"no instance builder for {missing}; the probe would not run"

    def test_at_least_one_family_declares_each_answer_the_probe_distinguishes(self):
        """The probe has two arms; a suite exercising one of them proves half a rule."""
        declared = {
            getattr(family.capabilities, field)
            for family in shipped_host_families()
            for field in ("run_user", "exec_user", "put_user", "get_user")
        }
        assert UserSupport.refused in declared
        assert declared - {UserSupport.refused}


class TestTheStructuralRules:
    def test_a_class_that_is_not_a_host_is_refused(self):
        class NotAHost:
            pass

        with pytest.raises(
            AssertionError, match=r"must be a subclass of otto\.host\.host\.BaseHost"
        ):
            assert_host_conforms(NotAHost)

    def test_a_class_declaring_no_capabilities_is_refused(self):
        class Undeclared(UnixHost):
            capabilities = "everything"

        with pytest.raises(AssertionError, match="must declare a HostCapabilities"):
            assert_host_conforms(Undeclared)

    def test_a_verb_missing_a_keyword_production_passes_is_refused(self):
        """The expected set is read off the ``Host`` protocol, never retyped."""

        class NoUserOnPut(UnixHost):
            async def put(
                self, src_files, dest_dir, mode=None, show_progress=True, recursive=False
            ):
                return None

        with pytest.raises(AssertionError, match=r"Host\.put: does not accept user by keyword"):
            assert_host_conforms(NoUserOnPut)

    def test_a_verb_that_is_not_async_is_refused(self):
        class SyncLogin(UnixHost):
            def login(self, user=None):
                return None

        with pytest.raises(AssertionError, match=r"Host\.login: must be an async def"):
            assert_host_conforms(SyncLogin)

    def test_a_verb_whose_signature_cannot_be_read_is_refused(self):
        """A third party can bind anything to the name; the rule must survive it."""

        class PutIsNotCallable(UnixHost):
            put = 42

        with pytest.raises(AssertionError, match=r"Host\.put: signature is unreadable"):
            assert_host_conforms(PutIsNotCallable)

    def test_the_expected_keywords_come_from_the_protocol_itself(self):
        """Pin the derivation, not the list: a protocol parameter added later is asked for."""
        from otto.host.host import Host
        from otto.testing.conformance_host import _keyword_names

        assert _keyword_names(Host.put) == list(inspect.signature(Host.put).parameters)[1:]


class TestTheBehaviouralRule:
    def test_a_verb_declared_refused_that_honours_the_call_is_refused(self, monkeypatch):
        """Flip ``local``'s ``put_user`` promise; the class keeps refusing, so it drifts."""
        monkeypatch.setattr(
            LocalHost,
            "capabilities",
            type(LocalHost.capabilities)(
                run_user=UserSupport.refused,
                exec_user=UserSupport.refused,
                put_user=UserSupport.chown,
                get_user=UserSupport.refused,
                show_progress=False,
                session_identity=LocalHost.capabilities.session_identity,
                transfer="none",
            ),
        )
        with pytest.raises(AssertionError, match=r"Host\.put: declared put_user='chown'"):
            assert_host_conforms(LocalHost, instance=LocalHost())

    def test_a_verb_declared_refused_that_declines_instead_is_refused(self, monkeypatch):
        """A refusal that sits BELOW the dry-run arm is the failure this rule exists for.

        Under ``--dry-run`` such a verb reports a decline for a call the family
        could never honour, which reads to a user as "it would have worked".
        """
        monkeypatch.setattr(LocalHost, "_refuse_exec_user", lambda self, user: None)
        with pytest.raises(
            AssertionError,
            match=r"Host\.exec: declared exec_user='refused'.*above the dry-run arm",
        ):
            assert_host_conforms(LocalHost, instance=LocalHost())

    def test_the_probe_reports_an_unexpected_exception_rather_than_swallowing_it(self, monkeypatch):
        def boom(self, user):
            raise RuntimeError("probe blew up")

        monkeypatch.setattr(LocalHost, "_refuse_exec_user", boom)
        with pytest.raises(AssertionError, match=r"RuntimeError: probe blew up"):
            assert_host_conforms(LocalHost, instance=LocalHost())

    def test_a_keyboard_interrupt_during_a_probe_propagates_rather_than_being_reported(
        self, monkeypatch
    ):
        """A probe's outcome is the measurement, but Ctrl-C is the caller asking

        the process to stop -- it must reach the caller as a KeyboardInterrupt,
        not be folded into an AssertionError violation string as if the class
        under test had done something wrong.
        """

        def stopped(self, user):
            raise KeyboardInterrupt

        monkeypatch.setattr(LocalHost, "_refuse_exec_user", stopped)
        with pytest.raises(KeyboardInterrupt):
            assert_host_conforms(LocalHost, instance=LocalHost())

    def test_an_honourable_verb_raising_something_else_is_reported_too(self):
        """The other arm of the same rule: a probe blowing up is a finding, not noise."""

        class PutBlowsUp(LocalHost):
            capabilities = replace(LocalHost.capabilities, put_user=UserSupport.chown)

            async def put(self, src_files, dest_dir, mode=None, user=None, show_progress=True):
                raise ValueError("probe blew up")

        with pytest.raises(
            AssertionError,
            match=r"but a dry-run put\(user=\.\.\.\) raised ValueError: probe blew up",
        ):
            assert_host_conforms(PutBlowsUp, instance=PutBlowsUp())

    def test_without_an_instance_only_the_structural_rules_run(self, monkeypatch):
        """The same drifted declaration passes when nothing is there to probe."""
        monkeypatch.setattr(LocalHost, "_refuse_exec_user", lambda self, user: None)
        assert_host_conforms(LocalHost)


class TestTheSessionIdentityRule:
    """The same declaration-against-behaviour rule, over ``session_identity``.

    ``as_user``/``switch_user`` are the two verbs that change the persistent
    session's identity, so the column that says where that identity comes from
    is the one they answer to (spec 2026-09-09 host-field-single-home §3.8).
    """

    def test_a_family_declaring_none_must_refuse_both_identity_verbs(self):
        """``embedded`` promises nothing switches identity; the probe holds it to that."""
        assert ZephyrHost.capabilities.session_identity.value == "none"
        assert_host_conforms(ZephyrHost, instance=_embedded())

    def test_a_none_family_whose_as_user_works_is_reported(self):
        """Mutate-and-observe-red: a ``none`` family whose ``as_user`` does NOT refuse."""

        @contextlib.asynccontextmanager
        async def permissive(self, user="root", password=None):
            yield self

        class Lying(ZephyrHost):
            pass

        Lying.as_user = permissive  # type: ignore[method-assign]
        with pytest.raises(
            AssertionError, match=r"Host\.as_user: declared session_identity='none'"
        ):
            assert_host_conforms(Lying, instance=_embedded(Lying))

    def test_a_none_family_whose_switch_user_works_is_reported(self):
        """The other identity verb, so neither arm of the loop rests on the other."""

        class AlsoLying(ZephyrHost):
            async def switch_user(self, user="", password=None):
                return None

        with pytest.raises(
            AssertionError, match=r"Host\.switch_user: declared session_identity='none'"
        ):
            assert_host_conforms(AlsoLying, instance=_embedded(AlsoLying))

    def test_a_family_declaring_as_user_scoped_must_not_refuse(self):
        """The other arm: ``local`` declines the dry run rather than refusing outright."""
        assert LocalHost.capabilities.session_identity.value == "as_user scoped"
        assert_host_conforms(LocalHost, instance=LocalHost())

    def test_a_scoped_family_that_refuses_is_reported(self):
        """Mutate-and-observe-red for the scoped arm."""

        class Refusing(LocalHost):
            async def switch_user(self, user="", password=None):
                raise NotImplementedError("nope")

        with pytest.raises(
            AssertionError,
            match=r"Host\.switch_user: declared session_identity='as_user scoped'",
        ):
            assert_host_conforms(Refusing, instance=Refusing())

    def test_a_scoped_family_whose_identity_verbs_are_the_wrong_shape_is_reported(self):
        """The scoped arm has positive content: only completion or the decline passes.

        Neither method refuses here, so the ``NotImplementedError`` arm above
        would let both through; what is wrong is the SHAPE -- an ``as_user``
        that is not a context manager, a ``switch_user`` that is not awaitable.
        A family declaring it can change identity is the one family for which
        these verbs are meant to work, so this is where shape matters most.
        """

        class BrokenScoped(LocalHost):
            def as_user(self, user="root", password=None):
                return object()  # not a context manager

            def switch_user(self, user="", password=None):
                return None  # not awaitable

        with pytest.raises(AssertionError) as caught:
            assert_host_conforms(BrokenScoped, instance=BrokenScoped())
        reported = str(caught.value)
        assert "Host.as_user: declared session_identity='as_user scoped'" in reported
        # The EXCEPTION CLASS is the interpreter's business and it changed:
        # ``async with object()`` raises ``AttributeError: __aenter__`` up to
        # 3.10 and ``TypeError: 'object' object does not support the
        # asynchronous context manager protocol (missed __aexit__ method)``
        # from 3.11 on. What this test owns is that the shape failure is
        # REPORTED and names the missing protocol, not which class carried it.
        assert "__aenter__" in reported or "asynchronous context manager" in reported, reported
        assert "Host.switch_user: declared session_identity='as_user scoped'" in reported
        assert "TypeError" in reported, reported

    def test_a_scoped_family_that_declines_the_dry_run_is_accepted(self):
        """The control for the test above: the decline is the expected outcome.

        Without it, an arm that accepted nothing at all would look just as green.
        """
        raised = _probe_identity(LocalHost(), "as_user")
        assert isinstance(raised, CommandNotRunError), raised

    def test_bound_at_open_is_not_probed_at_all(self):
        """The container exemption (spec §3.8), proven by a container that refuses.

        ``bound at open`` speaks about ``run``'s channel, not about ``as_user``;
        a container refusing both identity verbs is its own business, so the
        probe must not report it.
        """

        class RefusingContainer(DockerContainerHost):
            async def switch_user(self, user="", password=None):
                raise NotImplementedError("nope")

        assert DockerContainerHost.capabilities.session_identity.value == "bound at open"
        assert_host_conforms(
            RefusingContainer,
            instance=RefusingContainer(
                parent=_mock_parent(),
                container_id="conformanceabc",
                project="conformance",
                service="api",
                compose_project="otto-conformance",
            ),
        )


class TestTheDeclarationIsPerClassNotPerInstance:
    """A KNOWN, DOCUMENTED divergence, pinned here rather than left to surprise.

    `HostCapabilities` has one row per family and no term dimension, so a class
    whose answer depends on the instance's own configuration conforms on one
    instance and reports a violation on another -- both truthfully. otto ships
    exactly such a class, and the probe above passes only because `_unix()`
    takes the default `term="ssh"`; without this test nothing would say so.
    """

    def test_a_telnet_unix_host_reports_the_divergence_its_row_cannot_carry(self):
        """`unix` declares `exec_user=authenticate`; telnet has no exec channel.

        The violation is a statement about THIS INSTANCE's configuration, not a
        defect in `UnixHost`. `assert_host_conforms`'s docstring and
        `docs/library/custom-host-classes.md` say so where an author reads
        them; this is the executable half.
        """
        with pytest.raises(
            AssertionError,
            match=(
                r"Host\.exec: declared exec_user='authenticate', but "
                r"exec\(user=\.\.\.\) raised NotImplementedError"
            ),
        ):
            assert_host_conforms(UnixHost, instance=_unix(term="telnet"))

    def test_the_ssh_instance_the_row_speaks_for_still_conforms(self):
        """The control: without it the test above would pass on a broken asserter."""
        assert_host_conforms(UnixHost, instance=_unix())


class TestCalledFromTheWrongKindOfTest:
    @pytest.mark.asyncio
    async def test_an_async_test_is_told_what_is_actually_wrong(self):
        """`asyncio.run` cannot nest; the caller's class must not be blamed for it.

        Without the up-front check the four probes each catch the same
        "cannot be called from a running event loop" and report it as four
        violations of a class that is perfectly fine.
        """
        with pytest.raises(RuntimeError, match="must be called from a SYNCHRONOUS test"):
            assert_host_conforms(LocalHost, instance=LocalHost())

    @pytest.mark.asyncio
    async def test_the_structural_rules_still_run_from_an_async_test(self):
        """Only the probes need a loop of their own, so `instance=None` is fine."""
        assert_host_conforms(LocalHost)


class TestEveryRegisteredTransferBackendConforms:
    @pytest.mark.parametrize("name", sorted(TRANSFER_BACKENDS.names()))
    def test_backend_conforms(self, name):
        assert_transfer_backend_conforms(TRANSFER_BACKENDS.get(name))


class _Conforming(BaseFileTransfer):
    """The positive control every deficient subclass below varies one thing from."""

    host_families = frozenset({"unix"})
    progress_granularity = ProgressGranularity(put=4096, get=4096)

    @classmethod
    def create(cls, ctx):
        return cls("probe")

    async def _run_put(self, src_files, dest_dir, progress_factory):
        return {}

    async def _run_get(self, src_files, dest_dir, progress_factory):
        return {}


class TestTheTransferBackendRules:
    def test_the_positive_control_conforms(self):
        """Without it, a rule that refuses everything would pass every test below."""
        assert_transfer_backend_conforms(_Conforming)

    def test_a_backend_applicable_to_no_family_is_refused(self):
        class NoFamilies(_Conforming):
            host_families = frozenset()

        with pytest.raises(AssertionError, match="host_families must be a non-empty"):
            assert_transfer_backend_conforms(NoFamilies)

    def test_a_backend_promising_the_progress_bar_nothing_is_refused(self):
        class NoPromise(_Conforming):
            progress_granularity = 4096

        with pytest.raises(AssertionError, match="progress_granularity must be a"):
            assert_transfer_backend_conforms(NoPromise)

    def test_a_backend_missing_a_keyword_the_base_class_passes_is_refused(self):
        class NoProgressFactory(_Conforming):
            async def _run_put(self, src_files, dest_dir):
                return {}

        with pytest.raises(
            AssertionError,
            match=r"BaseFileTransfer\._run_put: does not accept progress_factory by keyword",
        ):
            assert_transfer_backend_conforms(NoProgressFactory)

    def test_a_backend_missing_a_keyword_the_host_passes_is_refused(self):
        class NoMode(_Conforming):
            async def put_files(self, src_files, dest_dir, show_progress=True):
                return None

        with pytest.raises(
            AssertionError,
            match=r"BaseFileTransfer\.put_files: does not accept mode by keyword",
        ):
            assert_transfer_backend_conforms(NoMode)

    def test_a_backend_that_never_overrode_create_is_refused(self):
        class InheritedCreate(_Conforming):
            create = BaseFileTransfer.create

        with pytest.raises(AssertionError, match="create: must be overridden"):
            assert_transfer_backend_conforms(InheritedCreate)

    def test_a_backend_leaving_a_method_abstract_is_refused(self):
        class StillAbstract(_Conforming):
            @abstractmethod
            async def _run_get(self, src_files, dest_dir, progress_factory):
                """Reopened, so the class cannot be instantiated."""

        with pytest.raises(AssertionError, match=r"leaves _run_get abstract"):
            assert_transfer_backend_conforms(StillAbstract)

    def test_a_backend_that_swallows_every_keyword_is_accepted(self):
        """``**kwargs`` genuinely accepts what production passes; the rule must not cry wolf."""

        class Kwargs(_Conforming):
            async def _run_put(self, *args, **kwargs):
                return {}

        assert_transfer_backend_conforms(Kwargs)


_PROBED_UNDER: "list[bool]" = []
"""What ``is_dry_run()`` answered inside the probe, recorded by the host below."""


class _RecordsItsProbe(LocalHost):
    """Declares ``put`` honourable and records the mode its probe ran under.

    Every other verb keeps ``local``'s refusal, so exactly one entry lands.
    """

    capabilities = replace(LocalHost.capabilities, put_user=UserSupport.chown)

    async def put(
        self, src_files, dest_dir, mode=None, user=None, show_progress=True, recursive=False
    ):
        _PROBED_UNDER.append(is_dry_run())


def test_the_probe_runs_under_a_dry_run_and_leaves_the_context_as_it_found_it():
    """Nothing may reach a real target, and the caller's own context must survive."""
    _PROBED_UNDER.clear()
    before = try_get_context()
    assert_host_conforms(_RecordsItsProbe, instance=_RecordsItsProbe())
    assert _PROBED_UNDER == [True]
    assert try_get_context() is before
