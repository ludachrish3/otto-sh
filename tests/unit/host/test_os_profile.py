import pytest

from otto.host import os_profile
from otto.host.embedded_host import EmbeddedHost, ZephyrHost
from otto.host.os_profile import (
    OsProfile,
    build_host_class,
    build_os_profile,
    get_host_class,
    get_os_profile,
    register_host_class,
    register_os_profile,
    registered_profile_names,
)
from otto.host.unix_host import UnixHost


@pytest.fixture(autouse=True)
def restore_registry():
    """Snapshot and restore the global profile and host-class registries around
    each test.

    ``register_os_profile`` and ``register_host_class`` mutate module-global
    state — including in-place overwrites of built-in entries (last-writer-wins
    is documented behavior for this module, see ``test_last_writer_wins_on_name_collision``
    / ``test_overriding_builtin_warns``) — so a diff-only cleanup (as used by the
    term/transfer registries, which never overwrite a built-in in tests) is not
    enough here; a full snapshot/restore of each ``Registry``'s internal entry
    and origin maps is required.
    """
    saved_profiles = dict(os_profile.OS_PROFILES._entries)
    saved_profile_origins = dict(os_profile.OS_PROFILES._origins)
    saved_classes = dict(os_profile.HOST_CLASSES._entries)
    saved_class_origins = dict(os_profile.HOST_CLASSES._origins)
    saved_specs = dict(os_profile._HOST_SPECS)
    try:
        yield
    finally:
        os_profile.OS_PROFILES._entries.clear()
        os_profile.OS_PROFILES._entries.update(saved_profiles)
        os_profile.OS_PROFILES._origins.clear()
        os_profile.OS_PROFILES._origins.update(saved_profile_origins)
        os_profile.HOST_CLASSES._entries.clear()
        os_profile.HOST_CLASSES._entries.update(saved_classes)
        os_profile.HOST_CLASSES._origins.clear()
        os_profile.HOST_CLASSES._origins.update(saved_class_origins)
        os_profile._HOST_SPECS.clear()
        os_profile._HOST_SPECS.update(saved_specs)


class TestBuiltins:
    def test_builtins_registered(self):
        assert set(registered_profile_names()) >= {"unix", "embedded", "zephyr"}

    def test_unix_and_embedded_have_no_defaults(self):
        assert build_os_profile("unix") == OsProfile("unix", "unix", {})
        assert build_os_profile("embedded") == OsProfile("embedded", "embedded", {})

    def test_zephyr_profile_points_to_zephyr_class(self):
        z = build_os_profile("zephyr")
        assert z.base == "zephyr"
        assert z.defaults == {}


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
        assert prof == OsProfile("riot", "embedded", {"os_name": "RIOT"})

    def test_register_defaults_are_optional(self):
        register_os_profile("bare", base="unix")
        assert build_os_profile("bare").defaults == {}

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

    def test_last_writer_wins_on_name_collision(self):
        register_os_profile("dup", base="unix", defaults={"os_name": "First"})
        register_os_profile("dup", base="embedded", defaults={"os_name": "Second"})
        prof = build_os_profile("dup")
        assert prof.base == "embedded"
        assert prof.defaults == {"os_name": "Second"}

    def test_overriding_builtin_warns(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            register_os_profile("embedded", base="embedded", defaults={"os_name": "Custom"})
        assert any("built-in" in r.message for r in caplog.records)


class TestHostClassRegistry:
    def test_builtin_host_classes_registered(self):
        assert build_host_class("unix") is UnixHost
        assert build_host_class("embedded") is EmbeddedHost
        assert build_host_class("zephyr") is ZephyrHost

    def test_register_host_class_round_trips_and_autoregisters_profile(self):
        class FooHost(EmbeddedHost):
            pass

        register_host_class("foo", FooHost)
        assert build_host_class("foo") is FooHost
        # registering a class also makes os_type:"foo" resolvable as a profile
        prof = build_os_profile("foo")
        assert prof.base == "foo"
        assert prof.defaults == {}

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
        assert build_os_profile("emb-variant").defaults["max_filename_len"] == 32

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

        register_host_class("myos", MyHost, EmbeddedHostSpec)
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
            register_host_class("bad", UnixHost, dict)  # dict is not a HostSpec

    def test_register_no_spec_and_no_base_spec_raises(self):
        # A direct RemoteHost subclass: no base in its MRO has a registered
        # spec, and none was passed -> fail loud rather than store None.
        from otto.host.os_profile import register_host_class
        from otto.host.remote_host import RemoteHost

        class BareRemoteHost(RemoteHost):
            pass

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
            "element": "widget",
            "os_type": "myrtos-v2",
        }
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

        assert build_os_profile("busybox").defaults["has_bash"] is False

    def test_busybox_selects_the_ash_frame_by_its_registered_name(self):
        """Profiles hold RAW lab-data values — a string, coerced by the factory.

        Asserted as the string rather than an instance: a profile holding a
        built object would bypass the factory's own coercion and diverge from
        what a hand-written lab.json produces.
        """
        from otto.host.os_profile import build_os_profile

        assert build_os_profile("busybox").defaults["command_frame"] == "ash"

    def test_the_frame_the_profile_names_is_actually_registered(self):
        """A profile naming an unregistered frame fails at host BUILD time, on a
        real lab, not here. This closes the gap between the two registries."""
        from otto.host.command_frame import FRAME_CLASSES
        from otto.host.os_profile import build_os_profile

        named = build_os_profile("busybox").defaults["command_frame"]
        assert named in FRAME_CLASSES, (
            f"the busybox profile names frame {named!r}, which is not registered"
        )

    def test_busybox_does_not_yet_claim_a_transfer_backend(self):
        """Deliberate deferral, asserted so it cannot be forgotten.

        A real BusyBox device typically runs dropbear rather than OpenSSH,
        which ships no sftp-server; sftp/scp against dropbear is a named but
        *untested* risk (design doc, "Known entries at design time" / "The
        dropbear risk"), not a measured break — so the inherited `scp`
        default is unverified against a real device, not proven to work (see
        `_register_builtin_os_profiles` for the full reasoning, including why
        a busybox host still attempts scp on every put/get regardless). The
        honest replacement, the `shell` backend, does not exist yet, and
        setting `transfer` to it today would not fail at registration —
        `register_os_profile` only validates default *keys*, never values —
        it would fail later, at host-build time, when `CapabilityResolver`
        rejects the value against this host's `valid_transfers` *menu*
        (measured: `transfer 'shell' is not in this host's transfer menu
        ['scp', 'sftp', 'ftp', 'nc']`), not from any "is this backend
        registered" lookup. So the field is left alone until the backend
        lands, and this test documents that the omission is a choice rather
        than an oversight.
        """
        from otto.host.os_profile import build_os_profile

        defaults = build_os_profile("busybox").defaults
        assert "transfer" not in defaults
        assert "valid_transfers" not in defaults

    def test_busybox_is_a_builtin_so_overriding_it_warns(self, caplog):
        """The other built-ins warn on override; a profile absent from the set
        is silently replaceable, which is a different contract.

        Asserts the actual warning, not just membership in `_BUILTIN_NAMES` —
        membership alone would keep passing even if the warning code were
        deleted. Same pattern as `TestRegistry.test_overriding_builtin_warns`.

        This covers the `register_os_profile` override path only. `busybox`
        is unusual among the built-ins: it names no host class of its own, so
        it has an entry in `OS_PROFILES` but never one in `HOST_CLASSES`. The
        *other* override path — `register_host_class("busybox", ...)`, which
        also silently touches `OS_PROFILES` via its own auto-registration —
        is a separate guard, checked by
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

        `register_host_class`'s own guard historically checked only
        `name in HOST_CLASSES`, which is False here on a first-ever
        registration under this name — so a naive guard would stay silent
        while this call's own auto-registered trivial `OsProfile`
        (`base="busybox", defaults={}`) silently destroys the real
        `has_bash`/`command_frame` defaults underneath it. Measured directly
        before the guard was widened to check `OS_PROFILES` too: zero log
        records were emitted for this exact call, and
        `build_os_profile("busybox").defaults` came back `{}` afterward.
        """
        import logging

        from otto.models.host import UnixHostSpec

        class Rogue(UnixHost):
            pass

        with caplog.at_level(logging.WARNING):
            register_host_class("busybox", Rogue, UnixHostSpec)
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
                "element": "bb1",
                "os_type": "busybox",
                "has_bash": True,
                "ip": "10.0.0.1",
                # Required by UnixHostSpec — a dict without it raises
                # `ValidationError: creds Field required`, which reads like a
                # profile bug and is not one.
                "creds": [{"login": "v", "password": "v"}],
            }
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
                "element": "bb1",
                "os_type": "busybox",
                "ip": "10.0.0.1",
                "creds": [{"login": "v", "password": "v"}],
            }
        )

        assert isinstance(host, UnixHost)
        # A plain unix host's `command_frame` is None; the profile supplies the
        # STRING "ash" and the factory coerces it. Measured 2026-08-12 with a
        # throwaway profile: `command_frame: "bash"` in defaults produced a
        # BashFrame instance on the built host, so this asserts the coercion as
        # well as the profile value.
        assert isinstance(host.command_frame, AshFrame)
        assert host.has_bash is False
