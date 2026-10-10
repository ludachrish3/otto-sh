"""The run's policy, variant and peer-host resolver, read through the leaf (spec 2 §3.1-§3.4)."""

import ast
import contextvars
import logging
from pathlib import Path

import pytest

from otto import context
from otto.config.lab import Lab
from otto.context import LIBRARY_LAB_NAME, OttoContext, reset_context, set_context
from otto.host.host import SuppressCommandOutput, get_logging_command_output_enabled, is_dry_run
from otto.invocation import RunPolicy, installed_policy, installed_resolver
from tests._fixtures.bootstrap_seam import patch_bootstrap
from tests._fixtures.labdata import make_host
from tests._fixtures.paths import PROJECT_ROOT


def test_with_no_policy_the_readers_see_the_defaults_and_suppression_is_a_no_op():
    assert (is_dry_run(), get_logging_command_output_enabled()) == (False, True)
    with SuppressCommandOutput():
        assert installed_policy() is None
        assert get_logging_command_output_enabled() is True


def test_the_manual_pattern_runs_under_the_variant_set_before_the_context():
    variant_binding = context.set_variant("field")
    try:
        binding = set_context(OttoContext(lab=Lab(name="rig")))
        try:
            assert context.variant() == "field"
        finally:
            reset_context(binding)
    finally:
        context.reset_variant(variant_binding)
    assert context.variant() == "debug"


@pytest.mark.parametrize(("carried", "expected"), [({}, "debug"), ({"variant": "field"}, "field")])
def test_the_manual_pattern_with_a_policy_runs_under_the_variant_that_policy_carries(
    carried, expected
):
    """``policy=`` is used as given: the variant ``set_variant`` chose is not copied onto it."""
    variant_binding = context.set_variant("field")
    try:
        policy = RunPolicy(dry_run=True, **carried)
        binding = set_context(OttoContext(lab=Lab(name="rig"), policy=policy))
        try:
            assert context.variant() == expected
        finally:
            reset_context(binding)
    finally:
        context.reset_variant(variant_binding)


def test_the_library_run_tests_sentinel_inherits_the_variant(tmp_path):
    from otto.suite.run import _session_context

    variant_binding = context.set_variant("field")
    try:
        with _session_context(tmp_path) as ctx:
            assert ctx.lab.name == LIBRARY_LAB_NAME
            assert context.variant() == "field"
            assert ctx.policy.output_dir == tmp_path
    finally:
        context.reset_variant(variant_binding)


@pytest.mark.parametrize(
    ("flag", "value"),
    [("dry_run", True), ("log_command_output", False), ("output_dir", Path("/tmp/out"))],
)
@pytest.mark.parametrize("with_policy", [False, True])
def test_the_run_flags_are_not_constructor_keywords(flag, value, with_policy):  # Review Focus 1
    extra = {"policy": RunPolicy()} if with_policy else {}
    with pytest.raises(TypeError, match=f"unexpected keyword argument '{flag}'"):
        OttoContext(lab=Lab(name="rig"), **extra, **{flag: value})


def test_a_positional_flag_cannot_bind_another_parameter():
    with pytest.raises(TypeError, match="positional argument"):
        OttoContext(Lab(name="rig"), True)  # type: ignore[misc]  # the old order meant dry_run=True


def test_policy_is_the_only_setter_and_the_flags_read_it_live():
    policy = RunPolicy(dry_run=True, log_command_output=False, output_dir=Path("/o"))
    ctx = OttoContext(lab=Lab(name="rig"), policy=policy)
    assert ctx.policy is policy
    assert (ctx.dry_run, ctx.log_command_output, ctx.output_dir) == (True, False, Path("/o"))
    policy.output_dir = Path("/later")
    assert ctx.output_dir == Path("/later")  # read live, never a snapshot
    for flag in ("dry_run", "log_command_output", "output_dir"):
        with pytest.raises(AttributeError):
            setattr(ctx, flag, None)
    plain = OttoContext(lab=Lab(name="rig"))
    assert (plain.dry_run, plain.log_command_output, plain.output_dir) == (False, True, None)


def test_a_context_built_before_set_variant_keeps_the_variant_it_copied():
    ctx = OttoContext(lab=Lab(name="rig"))
    variant_binding = context.set_variant("field")
    try:
        assert ctx.policy.variant == "debug"
    finally:
        context.reset_variant(variant_binding)


def test_set_variant_refusal_names_the_nested_open_context():  # Review Focus 2
    binding = set_context(OttoContext(lab=Lab(name="rig")))
    try:
        with pytest.raises(RuntimeError, match=r"open_context\(variant="):
            context.set_variant("field")
    finally:
        reset_context(binding)


def test_set_variant_validates_before_it_refuses():
    binding = set_context(OttoContext(lab=Lab(name="rig")))
    try:
        with pytest.raises(ValueError, match="variant must be"):
            context.set_variant("release")  # type: ignore[arg-type]
    finally:
        reset_context(binding)


def test_set_context_installs_the_contexts_own_policy_and_a_live_resolver():
    ctx = OttoContext(lab=Lab(name="rig"))
    binding = set_context(ctx)
    try:
        assert installed_policy() is ctx.policy
        ctx.policy.dry_run = True  # the context's policy IS the installed object
        assert is_dry_run() is True
        # read live, never a snapshot: a replaced lab, and then a changed mapping
        orphan = make_host("test3")  # no back-reference: it resolves through the resolver
        peer = make_host("test2")
        replacement = Lab(name="other")
        replacement.add_host(peer)
        ctx.lab = replacement
        assert installed_resolver().name == "other"
        assert orphan._lab_host(peer.id, role="hop") is peer
        later = make_host("test1")
        replacement.add_host(later)
        assert orphan._lab_host(later.id, role="hop") is later
    finally:
        reset_context(binding)
    assert (context.try_get_context(), installed_policy(), installed_resolver()) == (
        None,
        None,
        None,
    )
    with pytest.raises(RuntimeError, match="already reset"):
        reset_context(binding)


def test_set_context_refuses_a_non_context_before_installing_anything():
    outer = OttoContext(lab=Lab(name="outer"))
    binding = set_context(outer)
    try:
        with pytest.raises(AttributeError, match="policy"):
            set_context(object())  # type: ignore[arg-type]  # a duck with no policy
        assert context.try_get_context() is outer
        assert installed_policy() is outer.policy
        assert installed_resolver().name == "outer"
    finally:
        reset_context(binding)


def _without_the_root_callback(monkeypatch, build_lab):
    """Stub ``ensure_lab_context``'s inputs for a caller that drives the group directly."""
    from types import SimpleNamespace

    patch_bootstrap(monkeypatch, [])  # ensure_lab_context reads the composition root
    monkeypatch.setattr("otto.session.lab.build_lab", build_lab)
    monkeypatch.setattr(
        "otto.reservations.factory.build_reservation_gate",
        lambda *args, **kwargs: SimpleNamespace(identity=None),
    )
    assert installed_policy() is None  # no root callback ran


def test_a_cli_context_built_without_the_root_callback_takes_the_root_options_variant(
    monkeypatch,
):
    import typer
    from typer.core import TyperGroup

    from otto.cli.invoke import ensure_lab_context
    from tests._fixtures.rootoptions import make_root_options

    _without_the_root_callback(monkeypatch, lambda repos, labs: Lab(name="rig"))
    with typer.Context(TyperGroup(name="otto")) as ctx:
        ctx.meta["_otto_root_options"] = make_root_options(labs=["rig"], field=True, dry_run=True)
        built = ensure_lab_context(ctx)
        assert (built.policy.variant, built.policy.dry_run) == ("field", True)


def test_a_cli_context_built_without_the_root_callback_takes_the_discovered_deadline(
    monkeypatch,
):
    """The fallback policy is prepared as the root callback's is: ``OTTO_TEARDOWN_DEADLINE``."""
    import typer
    from typer.core import TyperGroup

    from otto.cli.invoke import ensure_lab_context
    from tests._fixtures.rootoptions import make_root_options

    _without_the_root_callback(monkeypatch, lambda repos, labs: Lab(name="rig"))
    monkeypatch.setattr("otto.bootstrap.discovered_teardown_deadline", lambda: 60.0)
    with typer.Context(TyperGroup(name="otto")) as ctx:
        ctx.meta["_otto_root_options"] = make_root_options(labs=["rig"])
        built = ensure_lab_context(ctx)
        assert built.policy.teardown_deadline == 60.0


def test_without_the_root_callback_the_lab_build_sees_the_policy_the_context_carries(
    monkeypatch,
):
    """Ingest picks each product's variant entry by the policy, so it is installed first."""
    import typer
    from typer.core import TyperGroup

    from otto.cli.invoke import ensure_lab_context
    from tests._fixtures.rootoptions import make_root_options

    seen: list[RunPolicy | None] = []

    def build_lab(repos, labs):
        seen.append(installed_policy())
        return Lab(name="rig")

    _without_the_root_callback(monkeypatch, build_lab)
    with typer.Context(TyperGroup(name="otto")) as ctx:
        ctx.meta["_otto_root_options"] = make_root_options(labs=["rig"], field=True)
        built = ensure_lab_context(ctx)
        assert seen == [built.policy]
        assert built.policy.variant == "field"
    assert installed_policy() is None  # Click's close reset the context and its policy


def test_a_context_binding_reset_elsewhere_raises():
    binding = set_context(OttoContext(lab=Lab(name="rig")))
    try:
        with pytest.raises(ValueError, match="different Context"):
            contextvars.copy_context().run(reset_context, binding)
    finally:
        reset_context(binding)


class _ConsoleSide(logging.Handler):
    """Keeps what the console side would show: the records HostFilter passes AT EMIT time."""

    def __init__(self) -> None:
        from otto.host.host import HostFilter

        super().__init__(level=logging.INFO)
        self.kept: list[str] = []
        self.addFilter(HostFilter())

    def emit(self, record: logging.LogRecord) -> None:
        self.kept.append(record.getMessage())


def test_a_shared_host_reads_log_command_output_live():
    host = make_host("test1")  # built before any context: nothing captured
    console = _ConsoleSide()
    host_logger = logging.getLogger("otto.host.host")
    host_logger.addHandler(console)
    previous = host_logger.level
    host_logger.setLevel(logging.INFO)
    binding = set_context(OttoContext(lab=Lab(name="rig")))
    try:
        with SuppressCommandOutput():
            host._log_command("hidden-command")
        host._log_command("shown-command")
    finally:
        reset_context(binding)
        host_logger.removeHandler(console)
        host_logger.setLevel(previous)
    assert [m for m in console.kept if "command" in m] == [
        m for m in console.kept if "shown-command" in m
    ]
    assert any("shown-command" in m for m in console.kept)


def test_peer_lookup_prefers_the_back_reference_then_the_installed_resolver():
    attached, own_jump = make_host("test1"), make_host("test2")
    own = Lab(name="own")
    own.add_host(attached)  # sets attached._lab: the back-reference
    own.add_host(own_jump)
    other_jump = make_host("test2")  # same id, another object, in another lab
    other = Lab(name="other")
    other.add_host(other_jump)
    orphan = make_host("test3")  # no back-reference
    binding = set_context(OttoContext(lab=other))
    try:
        # a nested context's lab never re-points an attached host
        assert attached._lab_host(own_jump.id, role="hop") is own_jump
        assert orphan._lab_host(other_jump.id, role="hop") is other_jump
    finally:
        reset_context(binding)
    with pytest.raises(RuntimeError, match="cannot resolve hop"):
        orphan._lab_host(other_jump.id, role="hop")


@pytest.mark.asyncio
async def test_a_power_controller_resolves_through_the_installed_resolver():
    from otto.host.power import CommandPowerConfig, CommandPowerController

    controller_host = make_host("test2")
    lab = Lab(name="rig")
    lab.add_host(controller_host)
    target = make_host("test1")  # no back-reference
    power = CommandPowerController(
        CommandPowerConfig(on_cmd="on", off_cmd="off", controller=controller_host.id)
    )
    binding = set_context(OttoContext(lab=lab))
    try:
        assert await power._runner(target) is controller_host
    finally:
        reset_context(binding)
    with pytest.raises(ValueError, match="not found in the lab"):
        await power._runner(target)


@pytest.mark.asyncio
async def test_a_nested_open_context_with_a_variant_is_not_refused_and_restores_the_outer_policy():
    outer = OttoContext(lab=Lab(name="rig"))
    binding = set_context(outer)
    try:
        async with context.open_context(lab=Lab(name="rig"), variant="field"):
            assert context.variant() == "field"
        assert installed_policy() is outer.policy
    finally:
        reset_context(binding)


@pytest.mark.asyncio
async def test_a_failed_open_context_setup_restores_the_outer_policy(monkeypatch):
    def _refuse(ctx):
        raise RuntimeError("dependency refused")

    monkeypatch.setattr("otto.session.dependencies.check_dependencies", _refuse)
    before = installed_policy()
    with pytest.raises(RuntimeError, match="dependency refused"):
        async with context.open_context(lab=Lab(name="rig"), variant="field"):
            pass
    assert installed_policy() is before


def test_declared_reads_the_variant_through_the_leaf_and_never_imports_the_context():
    tree = ast.parse((PROJECT_ROOT / "src" / "otto" / "declared.py").read_text())
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = {a.name for a in node.names}
            if (node.module or "").endswith("context") or (node.level and "context" in names):
                bad.append(ast.unparse(node))
    leaf_at_top = [
        ast.unparse(node)
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("invocation")
    ]
    assert bad == []
    assert leaf_at_top == []
    assert "invocation" in (PROJECT_ROOT / "src" / "otto" / "declared.py").read_text()
