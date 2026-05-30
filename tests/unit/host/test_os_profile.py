import pytest

from otto.host import os_profile
from otto.host.os_profile import (
    OsProfile,
    build_os_profile,
    get_os_profile,
    register_os_profile,
    registered_profile_names,
)


@pytest.fixture(autouse=True)
def restore_registry():
    """Snapshot and restore the global profile registry around each test.

    ``register_os_profile`` mutates module-global state; without this a custom
    profile registered by one test would leak into the next.
    """
    saved = dict(os_profile._OS_PROFILES)
    try:
        yield
    finally:
        os_profile._OS_PROFILES.clear()
        os_profile._OS_PROFILES.update(saved)


class TestBuiltins:

    def test_builtins_registered(self):
        assert set(registered_profile_names()) >= {'unix', 'embedded', 'zephyr'}

    def test_unix_and_embedded_have_no_defaults(self):
        assert build_os_profile('unix') == OsProfile('unix', 'unix', {})
        assert build_os_profile('embedded') == OsProfile('embedded', 'embedded', {})

    def test_zephyr_bundles_the_zephyr_defaults(self):
        z = build_os_profile('zephyr')
        assert z.base == 'embedded'
        assert z.defaults == {
            'osName': 'Zephyr', 'command_frame': 'zephyr', 'transfer': 'console',
        }


class TestRegistry:

    def test_unknown_profile_raises_with_known_list(self):
        with pytest.raises(ValueError, match='Unknown osType') as exc:
            build_os_profile('does-not-exist')
        # the registered names are listed so a typo is diagnosable
        assert 'unix' in str(exc.value)

    def test_get_returns_none_for_unknown(self):
        assert get_os_profile('does-not-exist') is None

    def test_register_then_build_round_trips(self):
        register_os_profile('riot', base='embedded', defaults={'osName': 'RIOT'})
        prof = build_os_profile('riot')
        assert prof == OsProfile('riot', 'embedded', {'osName': 'RIOT'})

    def test_register_defaults_are_optional(self):
        register_os_profile('bare', base='unix')
        assert build_os_profile('bare').defaults == {}

    def test_register_rejects_bad_base(self):
        with pytest.raises(ValueError, match='base must be one of'):
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
        register_os_profile('dup', base='unix', defaults={'osName': 'First'})
        register_os_profile('dup', base='embedded', defaults={'osName': 'Second'})
        prof = build_os_profile('dup')
        assert prof.base == 'embedded'
        assert prof.defaults == {'osName': 'Second'}

    def test_overriding_builtin_warns(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            register_os_profile('embedded', base='embedded',
                                defaults={'osName': 'Custom'})
        assert any('built-in' in r.message for r in caplog.records)
