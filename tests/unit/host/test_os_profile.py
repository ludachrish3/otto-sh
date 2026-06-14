import pytest

from otto.host import os_profile
from otto.host.os_profile import (
    OsProfile,
    build_os_profile,
    build_host_class,
    get_host_class,
    get_os_profile,
    register_host_class,
    register_os_profile,
    registered_profile_names,
)
from otto.host.embedded_host import EmbeddedHost, ZephyrHost
from otto.host.unix_host import UnixHost


@pytest.fixture(autouse=True)
def restore_registry():
    """Snapshot and restore the global profile and host-class registries around
    each test.

    ``register_os_profile`` and ``register_host_class`` mutate module-global
    state; without this a custom registration by one test would leak into the
    next.
    """
    saved_profiles = dict(os_profile._OS_PROFILES)
    saved_classes = dict(os_profile._HOST_CLASSES)
    try:
        yield
    finally:
        os_profile._OS_PROFILES.clear()
        os_profile._OS_PROFILES.update(saved_profiles)
        os_profile._HOST_CLASSES.clear()
        os_profile._HOST_CLASSES.update(saved_classes)


class TestBuiltins:

    def test_builtins_registered(self):
        assert set(registered_profile_names()) >= {'unix', 'embedded', 'zephyr'}

    def test_unix_and_embedded_have_no_defaults(self):
        assert build_os_profile('unix') == OsProfile('unix', 'unix', {})
        assert build_os_profile('embedded') == OsProfile('embedded', 'embedded', {})

    def test_zephyr_profile_points_to_zephyr_class(self):
        z = build_os_profile('zephyr')
        assert z.base == 'zephyr'
        assert z.defaults == {}


class TestRegistry:

    def test_unknown_profile_raises_with_known_list(self):
        with pytest.raises(ValueError, match='Unknown os_type') as exc:
            build_os_profile('does-not-exist')
        # the registered names are listed so a typo is diagnosable
        assert 'unix' in str(exc.value)

    def test_get_returns_none_for_unknown(self):
        assert get_os_profile('does-not-exist') is None

    def test_register_then_build_round_trips(self):
        register_os_profile('riot', base='embedded', defaults={'os_name': 'RIOT'})
        prof = build_os_profile('riot')
        assert prof == OsProfile('riot', 'embedded', {'os_name': 'RIOT'})

    def test_register_defaults_are_optional(self):
        register_os_profile('bare', base='unix')
        assert build_os_profile('bare').defaults == {}

    def test_register_rejects_bad_base(self):
        with pytest.raises(ValueError, match='base'):
            register_os_profile('weird', base='windows')

    def test_register_rejects_unknown_default_field(self):
        with pytest.raises(ValueError, match='unknown default field'):
            register_os_profile('typo', base='unix', defaults={'osTyp': 'unix'})

    def test_register_validates_fields_against_chosen_base(self):
        # ``docker_capable`` is a UnixHost field, not an EmbeddedHost field.
        with pytest.raises(ValueError, match='unknown default field'):
            register_os_profile('bad-embedded', base='embedded',
                                 defaults={'docker_capable': True})
        # but it is fine on a unix-base profile
        register_os_profile('ok-unix', base='unix',
                            defaults={'docker_capable': True})

    def test_last_writer_wins_on_name_collision(self):
        register_os_profile('dup', base='unix', defaults={'os_name': 'First'})
        register_os_profile('dup', base='embedded', defaults={'os_name': 'Second'})
        prof = build_os_profile('dup')
        assert prof.base == 'embedded'
        assert prof.defaults == {'os_name': 'Second'}

    def test_overriding_builtin_warns(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            register_os_profile('embedded', base='embedded',
                                defaults={'os_name': 'Custom'})
        assert any('built-in' in r.message for r in caplog.records)


class TestHostClassRegistry:

    def test_builtin_host_classes_registered(self):
        assert build_host_class('unix') is UnixHost
        assert build_host_class('embedded') is EmbeddedHost
        assert build_host_class('zephyr') is ZephyrHost

    def test_register_host_class_round_trips_and_autoregisters_profile(self):
        class FooHost(EmbeddedHost):
            pass

        register_host_class('foo', FooHost)
        assert build_host_class('foo') is FooHost
        # registering a class also makes os_type:"foo" resolvable as a profile
        prof = build_os_profile('foo')
        assert prof.base == 'foo'
        assert prof.defaults == {}

    def test_get_host_class_missing_returns_none(self):
        assert get_host_class('does-not-exist') is None

    def test_register_host_class_rejects_non_remotehost(self):
        with pytest.raises(ValueError, match='RemoteHost'):
            register_host_class('bad', dict)  # type: ignore[arg-type]

    def test_register_os_profile_base_must_be_registered_class(self):
        with pytest.raises(ValueError, match='base'):
            register_os_profile('bogus', base='not-a-class', defaults={})

    def test_profile_defaults_validated_against_subclass_inherited_fields(self):
        # max_filename_len is an EmbeddedHost field; a profile over 'embedded'
        # must accept it (MRO-union slots), not reject it as unknown.
        register_os_profile('emb-variant', base='embedded',
                            defaults={'max_filename_len': 32})
        assert build_os_profile('emb-variant').defaults['max_filename_len'] == 32

    def test_build_host_class_unknown_raises_with_known_list(self):
        with pytest.raises(ValueError, match='Unknown host class') as exc:
            build_host_class('does-not-exist')
        assert 'unix' in str(exc.value)


def test_custom_subclass_with_data_bundle_composes():
    """External pattern: register a subclass, then layer a data bundle over it."""
    from otto.storage.factory import create_host_from_dict
    from otto.host.embedded_host import EmbeddedHost

    class MyRtosHost(EmbeddedHost):
        pass

    register_host_class('myrtos', MyRtosHost)
    register_os_profile('myrtos-v2', base='myrtos',
                        defaults={'os_name': 'MyRTOS', 'command_frame': 'zephyr',
                                  'max_filename_len': 12})
    host = create_host_from_dict({
        'ip': '192.0.2.9', 'element': 'widget', 'os_type': 'myrtos-v2',
    })
    assert isinstance(host, MyRtosHost)
    assert host.os_type == 'myrtos-v2'      # selector recorded
    assert host.os_name == 'MyRTOS'         # from the data bundle
    assert host.max_filename_len == 12     # from the data bundle
