import pytest

from otto.host.element import Element
from otto.host.embedded_host import EmbeddedHost, ZephyrHost
from otto.host.os_profile import (
    OS_PROFILES,
    OsProfile,
    ProfileFields,
    build_host_class,
    build_os_profile,
    check_os_profile,
    get_host_class,
    get_os_profile,
    register_host_class,
    register_os_profile,
    registered_profile_names,
)
from otto.host.unix_host import UnixHost
from otto.registry import DuplicateRegistration, FrozenMap


class TestBuiltins:
    def test_builtins_registered(self):
        assert set(registered_profile_names()) >= {"unix", "embedded", "zephyr"}

    def test_builtin_profiles_pass_check_os_profile(self):
        """The built-ins are registered without importing their classes, so run the public check.

        ``check_os_profile`` validates ``defaults`` against the base class's
        fields, which imports the host class the built-ins name by reference;
        running it over each built-in holds them to the same check a third
        party's profile gets.
        """
        for name in ["unix", "embedded", "zephyr", "busybox"]:
            profile = build_os_profile(name)
            check_os_profile(
                profile.name,
                profile.base,
                profile.fields.defaults.thaw_json(),
                login_prompt=profile.fields.login_prompt,
                password_prompt=profile.fields.password_prompt,
            )

    def test_unix_and_embedded_have_no_defaults(self):
        assert build_os_profile("unix") == OsProfile(
            "unix",
            "unix",
            ProfileFields(login_prompt=r"login: ?$", password_prompt=r"[Pp]assword: ?$"),
        )
        assert build_os_profile("embedded") == OsProfile("embedded", "embedded")

    def test_zephyr_profile_points_to_zephyr_class(self):
        z = build_os_profile("zephyr")
        assert z.base == "zephyr"
        assert dict(z.fields.defaults) == {}


class TestRegistry:
    def test_unknown_profile_raises_with_known_list(self):
        with pytest.raises(ValueError, match="Unknown os_type") as exc:
            build_os_profile("does-not-exist")
        # the registered names are listed so a typo is diagnosable
        assert "unix" in str(exc.value)

    def test_get_returns_none_for_unknown(self):
        assert get_os_profile("does-not-exist") is None

    def test_register_then_build_round_trips(self):
        register_os_profile("riot", base="embedded", defaults={"os_name": "RIOT"})
        prof = build_os_profile("riot")
        assert prof == OsProfile("riot", "embedded", ProfileFields(FrozenMap({"os_name": "RIOT"})))

    def test_register_defaults_are_optional(self):
        register_os_profile("bare", base="unix")
        assert dict(build_os_profile("bare").fields.defaults) == {}

    def test_register_rejects_bad_base(self):
        with pytest.raises(ValueError, match="base"):
            register_os_profile("weird", base="windows")

    def test_register_rejects_unknown_default_field(self):
        with pytest.raises(ValueError, match="unknown default field"):
            register_os_profile("typo", base="unix", defaults={"osTyp": "unix"})

    def test_register_validates_fields_against_chosen_base(self):
        # ``docker_capable`` is a UnixHost field, not an EmbeddedHost field.
        with pytest.raises(ValueError, match="unknown default field"):
            register_os_profile("bad-embedded", base="embedded", defaults={"docker_capable": True})
        # but it is fine on a unix-base profile
        register_os_profile("ok-unix", base="unix", defaults={"docker_capable": True})

    def test_re_registering_a_name_needs_overwrite(self):
        register_os_profile("dup", base="unix", defaults={"os_name": "First"})
        with pytest.raises(DuplicateRegistration):
            register_os_profile("dup", base="embedded", defaults={"os_name": "Second"})
        register_os_profile("dup", base="embedded", defaults={"os_name": "Second"}, overwrite=True)
        prof = build_os_profile("dup")
        assert prof.base == "embedded"
        assert dict(prof.fields.defaults) == {"os_name": "Second"}

    def test_overriding_builtin_warns(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            register_os_profile("embedded", base="embedded", defaults={"os_name": "Custom"})
        assert any("built-in" in r.message for r in caplog.records)


class TestConsolePrompts:
    def test_unix_and_busybox_carry_the_getty_prompts(self):
        for name in ("unix", "busybox"):
            prof = build_os_profile(name)
            assert prof.fields.login_prompt == r"login: ?$", name
            assert prof.fields.password_prompt == r"[Pp]assword: ?$", name

    def test_embedded_and_zephyr_carry_none(self):
        for name in ("embedded", "zephyr"):
            fields = build_os_profile(name).fields
            assert fields.login_prompt is None and fields.password_prompt is None, name  # noqa: PT018

    def test_register_accepts_prompt_keywords_and_compiles_them(self):
        register_os_profile("vendor", base="unix", login_prompt=r"Username: ?$")
        assert build_os_profile("vendor").fields.login_prompt == r"Username: ?$"
        with pytest.raises(ValueError, match="login_prompt"):
            register_os_profile("bad", base="unix", login_prompt="(")

    def test_resolve_fills_only_the_unset_fields(self):
        from otto.host.options import ConsoleOptions
        from otto.host.os_profile import resolve_console_prompts

        unix = build_os_profile("unix").fields
        resolved = resolve_console_prompts(ConsoleOptions(password_prompt="pw: $"), unix)
        assert resolved.login_prompt == r"login: ?$"
        assert resolved.password_prompt == "pw: $"
        untouched = resolve_console_prompts(ConsoleOptions(), build_os_profile("zephyr").fields)
        assert untouched.login_prompt is None

    def test_resolve_with_no_profile_fields_leaves_the_options_alone(self):
        from otto.host.options import ConsoleOptions
        from otto.host.os_profile import resolve_console_prompts

        assert resolve_console_prompts(ConsoleOptions(), None).login_prompt is None


class TestHostClassRegistry:
    def test_builtin_host_classes_registered(self):
        assert build_host_class("unix") is UnixHost
        assert build_host_class("embedded") is EmbeddedHost
        assert build_host_class("zephyr") is ZephyrHost

    def test_register_host_class_round_trips_and_its_name_resolves_as_a_profile(self):
        class FooHost(EmbeddedHost):
            pass

        register_host_class("foo", FooHost)
        assert build_host_class("foo") is FooHost
        # the class's own name resolves as a profile, with nothing registered
        assert "foo" not in OS_PROFILES
        prof = build_os_profile("foo")
        assert prof.base == "foo"
        assert dict(prof.fields.defaults) == {}

    def test_get_host_class_missing_returns_none(self):
        assert get_host_class("does-not-exist") is None

    def test_register_host_class_rejects_non_remotehost(self):
        with pytest.raises(ValueError, match="RemoteHost"):
            register_host_class("bad", dict)  # type: ignore[arg-type]

    def test_register_os_profile_base_must_be_registered_class(self):
        with pytest.raises(ValueError, match="base"):
            register_os_profile("bogus", base="not-a-class", defaults={})

    def test_profile_defaults_validated_against_subclass_inherited_fields(self):
        # max_filename_len is an EmbeddedHost field; a profile over 'embedded'
        # must accept it (MRO-union slots), not reject it as unknown.
        register_os_profile("emb-variant", base="embedded", defaults={"max_filename_len": 32})
        assert build_os_profile("emb-variant").fields.defaults["max_filename_len"] == 32

    def test_build_host_class_unknown_raises_with_known_list(self):
        with pytest.raises(ValueError, match="Unknown host class") as exc:
            build_host_class("does-not-exist")
        assert "unix" in str(exc.value)


class TestHostSpecRegistry:
    def test_builtins_carry_their_specs(self):
        from otto.host.os_profile import build_host_spec
        from otto.models.host import EmbeddedHostSpec, UnixHostSpec

        assert build_host_spec("unix") is UnixHostSpec
        assert build_host_spec("embedded") is EmbeddedHostSpec
        assert build_host_spec("zephyr") is EmbeddedHostSpec  # adds no fields

    def test_register_with_explicit_spec(self):
        from otto.host.embedded_host import EmbeddedHost
        from otto.host.os_profile import build_host_spec, register_host_class
        from otto.models.host import EmbeddedHostSpec

        class MyHost(EmbeddedHost):
            pass

        register_host_class("myos", MyHost, spec=EmbeddedHostSpec)
        assert build_host_spec("myos") is EmbeddedHostSpec

    def test_register_defaults_spec_via_mro(self):
        from otto.host.embedded_host import EmbeddedHost
        from otto.host.os_profile import build_host_spec, register_host_class
        from otto.models.host import EmbeddedHostSpec

        class MyHost(EmbeddedHost):
            pass

        register_host_class("myos2", MyHost)  # no spec -> nearest base spec
        assert build_host_spec("myos2") is EmbeddedHostSpec

    def test_register_rejects_non_hostspec_spec(self):
        from otto.host.os_profile import register_host_class
        from otto.host.unix_host import UnixHost

        with pytest.raises(ValueError, match="HostSpec"):
            register_host_class("bad", UnixHost, spec=dict)  # type: ignore[arg-type] — not a HostSpec

    def test_register_no_spec_and_no_base_spec_raises(self):
        # A direct RemoteHost subclass: no base in its MRO has a registered
        # spec, and none was passed -> fail loud rather than store None. It
        # declares capabilities, so the class itself passes HOST_CLASSES's
        # validator and the spec lookup is what refuses it.
        from otto.host.os_profile import register_host_class
        from otto.host.remote_host import RemoteHost
        from otto.host.unix_host import UnixHost

        class BareRemoteHost(RemoteHost):
            capabilities = UnixHost.capabilities

        with pytest.raises(ValueError, match="no spec given"):
            register_host_class("bare", BareRemoteHost)

    def test_build_host_spec_unknown_raises(self):
        from otto.host.os_profile import build_host_spec

        with pytest.raises(ValueError, match="No host spec"):
            build_host_spec("nope")


def test_custom_subclass_with_data_bundle_composes():
    """External pattern: register a subclass, then layer a data bundle over it."""
    from otto.host.embedded_host import EmbeddedHost
    from otto.host.factory import create_host_from_dict

    class MyRtosHost(EmbeddedHost):
        pass

    register_host_class("myrtos", MyRtosHost)
    register_os_profile(
        "myrtos-v2",
        base="myrtos",
        defaults={"os_name": "MyRTOS", "command_frame": "zephyr", "max_filename_len": 12},
    )
    host = create_host_from_dict(
        {
            "ip": "192.0.2.9",
            "os_type": "myrtos-v2",
        },
        element=Element("widget"),
    )
    assert isinstance(host, MyRtosHost)
    assert host.os_type == "myrtos-v2"  # selector recorded
    assert host.os_name == "MyRTOS"  # from the data bundle
    assert host.max_filename_len == 12  # from the data bundle


class TestBusyBoxProfile:
    """`os_type: "busybox"` — a bundle of unix defaults, not a new host class.

    A BusyBox box is a unix host whose userland answers differently. The
    capability answers themselves are PROBED at runtime by `Userland`, so this
    profile carries only what probing cannot discover: facts about the host that
    change which code paths otto is allowed to take at all.
    """

    def test_busybox_resolves_to_the_unix_base(self):
        """No new host class — the spec's central decision, asserted."""
        from otto.host.os_profile import build_os_profile

        profile = build_os_profile("busybox")

        assert profile.base == "unix", (
            "busybox must build UnixHost; a separate class would fork every "
            "unix code path for one userland variant"
        )

    def test_busybox_declares_it_has_no_bash(self):
        """`has_bash` gates real behaviour, so its default is load-bearing.

        `otto.tunnel.discovery` scans only `has_bash` hosts, and
        `bash -c 'exec -a ...'` is how commands get tagged. A BusyBox box
        typically ships no bash at all, so leaving the unix default of True
        makes otto emit bash-only commands to a shell that cannot run them.
        """
        from otto.host.os_profile import build_os_profile

        assert build_os_profile("busybox").fields.defaults["has_bash"] is False

    def test_busybox_selects_the_ash_frame_by_its_registered_name(self):
        """Profiles hold RAW lab-data values — a string, coerced by the factory.

        Asserted as the string rather than an instance: a profile holding a
        built object would bypass the factory's own coercion and diverge from
        what a hand-written lab.json produces.
        """
        from otto.host.os_profile import build_os_profile

        assert build_os_profile("busybox").fields.defaults["command_frame"] == "ash"

    def test_the_frame_the_profile_names_is_actually_registered(self):
        """A profile naming an unregistered frame fails at host BUILD time, on a
        real lab, not here. This closes the gap between the two registries."""
        from otto.host.command_frame import FRAME_CLASSES
        from otto.host.os_profile import build_os_profile

        named = build_os_profile("busybox").fields.defaults["command_frame"]
        assert named in FRAME_CLASSES, (
            f"the busybox profile names frame {named!r}, which is not registered"
        )

    def test_busybox_names_the_shell_transfer_backend_and_it_is_registered(self):
        """The deferral is over: `shell` is the default, and both registries agree.

        `register_os_profile` validates default *keys* against the base
        class's fields, never values (see `_builtin_profiles`),
        so a profile naming an unregistered backend registers cleanly
        regardless -- the mismatch only surfaces later, at host-build time,
        not here (measured during this test's own mutation verification:
        pydantic's own field validator raises if the bad name is also
        listed in `valid_transfers`; `CapabilityResolver`'s menu-membership
        check raises if it is not -- neither at registration). Same shape
        as `test_the_frame_the_profile_names_is_actually_registered`:
        assert BOTH that the profile names `shell` and that `shell` is
        actually in `TRANSFER_BACKENDS`, so a naming mistake is caught here
        instead of on a real lab device.
        """
        from otto.host.os_profile import build_os_profile
        from otto.host.transfer import TRANSFER_BACKENDS

        defaults = build_os_profile("busybox").fields.defaults
        assert defaults["transfer"] == "shell"
        assert "shell" in defaults["valid_transfers"]
        assert "shell" in TRANSFER_BACKENDS, (
            f"the busybox profile names transfer {defaults['transfer']!r}, which is not registered"
        )

    def test_busybox_is_a_builtin_so_overriding_it_warns(self, caplog):
        """The other built-ins warn on override; a profile absent from the set
        is silently replaceable, which is a different contract.

        Asserts the actual warning, not just membership in `_BUILTIN_NAMES` —
        membership alone would keep passing even if the warning code were
        deleted. Same pattern as `TestRegistry.test_overriding_builtin_warns`.

        This covers the `register_os_profile` override path only. `busybox`
        is unusual among the built-ins: it names no host class of its own, so
        it is in `BUILTIN_PROFILES` but never in `HOST_CLASSES`. The *other*
        override path — `register_host_class("busybox", ...)`, whose class
        layer shadows the built-in — is a separate guard, checked by
        `test_busybox_is_also_a_builtin_via_register_host_class` below.
        """
        import logging

        with caplog.at_level(logging.WARNING):
            register_os_profile("busybox", base="unix", defaults={"has_bash": False})
        assert any("built-in" in r.message for r in caplog.records)

    def test_busybox_is_also_a_builtin_via_register_host_class(self, caplog):
        """The override-warning guard on `register_host_class` must ALSO
        fire for `busybox`, even though `busybox` has never had an entry in
        `HOST_CLASSES` (it names no class of its own — only `unix` does).

        A guard that checked only `name in HOST_CLASSES` would stay silent on
        a first-ever registration under this name, while the class's own
        profile (the class layer resolves before the built-ins) shadows the
        real `has_bash`/`command_frame` defaults. So it checks
        `BUILTIN_PROFILES` too.
        """
        import logging

        from otto.models.host import UnixHostSpec

        class Rogue(UnixHost):
            pass

        with caplog.at_level(logging.WARNING):
            register_host_class("busybox", Rogue, spec=UnixHostSpec)
        assert any("built-in" in r.message for r in caplog.records)

    def test_a_hosts_own_field_still_beats_the_profile_default(self):
        """Profile defaults sit BENEATH a host's own lab.json fields.

        Without this, a profile could silently override an explicit declaration
        — the opposite of the documented merge order, and unfixable from lab
        data.
        """
        from otto.host.factory import create_host_from_dict

        host = create_host_from_dict(
            {
                "os_type": "busybox",
                "has_bash": True,
                "ip": "10.0.0.1",
                "creds": [{"login": "v", "password": "v"}],
            },
            element=Element("bb1"),
        )

        assert host.has_bash is True, (
            "an explicit lab.json value must win over the profile's default"
        )

    def test_a_busybox_host_builds_from_a_minimal_lab_entry(self):
        """Exit criterion 1: a minimal lab.json entry works.

        End-to-end through the factory, because every assertion above is about
        the profile record and none of them proves a host can actually be built
        from it.
        """
        from otto.host.command_frame import AshFrame
        from otto.host.factory import create_host_from_dict
        from otto.host.unix_host import UnixHost

        host = create_host_from_dict(
            {
                "os_type": "busybox",
                "ip": "10.0.0.1",
                "creds": [{"login": "v", "password": "v"}],
            },
            element=Element("bb1"),
        )

        assert isinstance(host, UnixHost)
        # A plain unix host's `command_frame` is None; the profile supplies the
        # STRING "ash" and the factory coerces it. Measured 2026-08-12 with a
        # throwaway profile: `command_frame: "bash"` in defaults produced a
        # BashFrame instance on the built host, so this asserts the coercion as
        # well as the profile value.
        assert isinstance(host.command_frame, AshFrame)
        assert host.has_bash is False
