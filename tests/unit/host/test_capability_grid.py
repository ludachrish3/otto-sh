"""Guards on the per-family ``capabilities`` declarations.

THREE JOBS, and they are the three ways a declaration can stop being true.

It can be MISSING: :func:`~otto.host.os_profile.register_host_class` refuses a
class that declares none, so a custom family cannot reach a reader as a blank
row — the guard ``transfer/registry.py`` already applies to
``progress_granularity``, applied to host classes.

It can be UNPUBLISHED: a family can exist in the tree and not on the page,
because two of otto's own families never pass through that registry. The class
walk below finds a host class no row speaks for.

And it can be INCOHERENT: a declaration whose transfer column names both a
registry family and a literal, or neither, renders an empty or doubled cell.
``HostCapabilities`` refuses that at construction.

The page these back is ``docs/cli/host/families.md``, whose byte-for-byte
sync with the tree is pinned in ``tests/unit/test_support_matrix.py``.
"""

import pytest

from otto.host.capability_grid import (
    HostCapabilities,
    OptionSupport,
    SessionIdentity,
    UserSupport,
    shipped_host_families,
)
from otto.host.embedded_host import EmbeddedHost
from otto.host.limitation import Limitation
from otto.host.os_profile import register_host_class
from otto.host.remote_host import RemoteHost
from otto.host.specs import EmbeddedHostSpec
from tests._fixtures.host_classes import host_classes_in_the_tree


def _limitation(kind: str = "device") -> Limitation:
    return Limitation(
        kind=kind, affects="an option", reason="the device cannot", measured_on=["a bench"]
    )


def _capabilities(**overrides) -> HostCapabilities:
    """A valid declaration, so a test can vary exactly one thing about it."""
    fields = {
        "sudo": OptionSupport.honoured(),
        "expects": OptionSupport.honoured(),
        "user_refusal": _limitation(),
        "run_user": UserSupport.refused,
        "exec_user": UserSupport.refused,
        "put_user": UserSupport.refused,
        "get_user": UserSupport.refused,
        "show_progress": False,
        "session_identity": SessionIdentity.none,
        "transfer_family": "unix",
    }
    fields.update(overrides)
    return HostCapabilities(**fields)


class TestRegistrationRefusesAnUndeclaredClass:
    def test_a_class_inheriting_only_the_annotation_is_refused(self):
        """``BaseHost.capabilities`` is a bare ``ClassVar`` annotation.

        It creates no attribute, so a subclass that declares nothing has
        nothing under the name — which is the ordinary shape of the mistake
        and the one the refusal exists for.
        """

        class NoPromise(RemoteHost):
            pass

        assert "capabilities" not in dir(NoPromise)
        with pytest.raises(ValueError, match="capabilities is missing"):
            register_host_class("nopromise", NoPromise, spec=EmbeddedHostSpec)

    def test_a_class_declaring_something_else_under_the_name_is_refused(self):
        """The refusal checks the TYPE, not the name -- a string promises nothing."""

        class BareString(RemoteHost):
            capabilities = "everything"

        with pytest.raises(ValueError, match="capabilities is missing"):
            register_host_class("barestring", BareString, spec=EmbeddedHostSpec)

    def test_a_class_that_declares_one_registers(self):
        """The positive control: without it, a refusal of EVERY class would pass above."""

        class Declared(EmbeddedHost):
            capabilities = _capabilities()

        register_host_class("declared", Declared, spec=EmbeddedHostSpec)
        from otto.host.os_profile import build_host_class

        assert build_host_class(name="declared") is Declared


class TestTheDeclarationItself:
    @pytest.mark.parametrize("family", shipped_host_families(), ids=lambda f: f.name)
    def test_every_shipped_family_declares_its_capabilities(self, family):
        assert isinstance(family.capabilities, HostCapabilities)
        for option in (family.capabilities.sudo, family.capabilities.expects):
            assert isinstance(option, OptionSupport)

    @pytest.mark.parametrize(
        ("name", "sudo_refused"),
        [
            ("unix", False),
            ("local", False),
            ("container", False),
            ("embedded", True),
            ("zephyr", True),
        ],
    )
    def test_each_shipped_family_declares_its_options(self, name, sudo_refused):
        caps = next(f for f in shipped_host_families() if f.name == name).capabilities
        assert (caps.sudo.limitation is not None) is sudo_refused
        assert caps.expects == OptionSupport.honoured()
        assert (caps.user_refusal is not None) is sudo_refused
        for limitation in (caps.sudo.limitation, caps.user_refusal):
            if limitation is not None:
                assert limitation.kind == "device"
                assert all(
                    "tests/measurements/zephyr_privilege/" in m for m in limitation.measured_on
                )

    def test_a_refused_user_with_no_reason_is_refused(self):
        with pytest.raises(ValueError, match="set user_refusal exactly when"):
            _capabilities(user_refusal=None)

    def test_a_reason_for_a_user_nothing_refuses_is_refused(self):
        with pytest.raises(ValueError, match="set user_refusal exactly when"):
            _capabilities(run_user=UserSupport.switch, exec_user=UserSupport.switch)

    @pytest.mark.parametrize("field_name", ["sudo", "expects", "user_refusal"])
    def test_a_refusal_that_is_a_library_s_is_refused(self, field_name):
        """A refused option is the device's; anything else otto could fix."""
        library = _limitation(kind="library")
        value = (
            library if field_name == "user_refusal" else OptionSupport.refused(limitation=library)
        )
        with pytest.raises(ValueError, match=rf"HostCapabilities\.{field_name}: .*kind 'device'"):
            _capabilities(**{field_name: value})

    def test_switch_is_a_member_with_its_own_meaning(self):
        assert UserSupport.switch.value == "switch"
        assert "switched to that user" in (UserSupport.switch.__doc__ or "")

    def test_unix_answers_exec_user_with_switch(self):
        from otto.host.unix_host import UnixHost

        assert UnixHost.capabilities.exec_user is UserSupport.switch

    def test_a_declaration_naming_no_transfer_at_all_is_refused(self):
        with pytest.raises(ValueError, match="exactly one of transfer_family"):
            _capabilities(transfer_family="")

    def test_a_declaration_naming_a_transfer_twice_is_refused(self):
        with pytest.raises(ValueError, match="exactly one of transfer_family"):
            _capabilities(transfer_family="unix", transfer="scp, by hand")

    def test_every_user_support_value_explains_itself(self):
        """The page RENDERS these docstrings, so a member without one renders a blank cell."""
        for member in (*UserSupport, *SessionIdentity):
            assert (member.__doc__ or "").strip(), member


def test_every_host_class_in_the_tree_is_published_by_some_row():
    """A family otto ships and the page does not name is the failure mode here.

    ``shipped_host_families`` is hand-maintained where the registry cannot
    answer (``local`` and ``container`` never pass through
    ``register_host_class``), so nothing but this walk would notice a third one
    arriving. Three shapes are legitimate:

    * the class IS a row;
    * it is a subclass of a row's class that redeclares nothing, so the row's
      promises are literally its own (``ZephyrHost``);
    * it is an intermediate BASE of a row's class (``RemoteHost``), which
      declares nothing and which ``register_host_class`` would refuse.

    Anything else is a family with promises nobody publishes.
    """
    rows = {family.cls: family.name for family in shipped_host_families()}
    for cls in host_classes_in_the_tree():
        if cls in rows:
            continue
        covering = [base for base in rows if issubclass(cls, base)]
        if covering:
            assert "capabilities" not in cls.__dict__, (
                f"{cls.__module__}.{cls.__name__} redeclares capabilities but is "
                f"published under {rows[covering[0]]!r}; give it its own row in "
                f"shipped_host_families()"
            )
            continue
        if any(issubclass(base, cls) for base in rows):
            assert "capabilities" not in cls.__dict__, (
                f"{cls.__module__}.{cls.__name__} is a base of a published family "
                f"and declares capabilities of its own; it needs a row"
            )
            continue
        pytest.fail(
            f"{cls.__module__}.{cls.__name__} is a host class no row speaks for; "
            f"add it to otto.host.capability_grid.shipped_host_families()"
        )


@pytest.mark.parametrize("family", shipped_host_families(), ids=lambda f: f.name)
def test_an_option_s_limitation_is_read_from_its_one_field(family):
    """``option_limitation`` maps each ``run``/``exec`` option to the field declaring it."""
    caps = family.capabilities
    assert caps.option_limitation(option="sudo") is caps.sudo.limitation
    assert caps.option_limitation(option="expects") is caps.expects.limitation
    assert caps.option_limitation(option="user") is caps.user_refusal


def test_an_option_support_s_field_is_keyword_only():
    with pytest.raises(TypeError):
        OptionSupport(None)  # ty: ignore[too-many-positional-arguments] — the refusal under test
