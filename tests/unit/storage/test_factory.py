from pathlib import Path

import pytest

from otto.host import os_profile
from otto.host.command_frame import ZephyrFrame
from otto.host.embedded_filesystem import FatRamFileSystem
from otto.host.embedded_host import EmbeddedHost, ZephyrHost
from otto.host.options import SnmpOptions
from otto.host.os_profile import register_os_profile
from otto.host.toolchain import Toolchain
from otto.host.unix_host import UnixHost
from otto.storage.factory import (
    create_host_from_dict,
    validate_host_dict,
)


@pytest.fixture
def restore_profiles():
    """Snapshot/restore the global os-profile registry around a test."""
    saved = dict(os_profile._OS_PROFILES)
    try:
        yield
    finally:
        os_profile._OS_PROFILES.clear()
        os_profile._OS_PROFILES.update(saved)


class TestCreateHostFromDict:
    """Tests for create_host_from_dict function."""

    def test_create_remotehost_with_complete_data(self):
        """Test creating UnixHost with all fields."""
        host_data = {
            'ip': '10.10.200.11',
            'element': 'orange',
            'board': 'seed',
            'creds': {'vagrant': 'vagrant'},
            'resources': ['orange'],
        }
        host = create_host_from_dict(host_data)

        assert isinstance(host, UnixHost)
        assert host.ip == '10.10.200.11'
        assert host.element == 'orange'
        assert host.board == 'seed'
        assert host.creds == {'vagrant': 'vagrant'}
        assert host.resources == {'orange'}

    def test_resources_list_converted_to_set(self):
        """Test that resources list is converted to set."""
        host_data = {
            'ip': '10.10.200.11',
            'element': 'orange',
            'creds': {'vagrant': 'vagrant'},
            'resources': ['orange', 'tomato'],
        }
        host = create_host_from_dict(host_data)

        assert isinstance(host.resources, set)
        assert host.resources == {'orange', 'tomato'}

    def test_resources_set_preserved(self):
        """Test that resources set is preserved."""
        host_data = {
            'ip': '10.10.200.11',
            'element': 'orange',
            'creds': {'vagrant': 'vagrant'},
            'resources': {'orange', 'tomato'},
        }
        host = create_host_from_dict(host_data)

        assert isinstance(host.resources, set)
        assert host.resources == {'orange', 'tomato'}

    def test_missing_ip_raises_typeerror(self):
        """Test that missing ip field raises ValueError."""
        host_data = {
            'element': 'orange',
            'creds': {'vagrant': 'vagrant'},
        }
        with pytest.raises(TypeError) as exc_info:
            create_host_from_dict(host_data)

        assert 'ip' in str(exc_info.value)

    def test_missing_creds_raises_typeerror(self):
        """Test that missing creds field raises ValueError."""
        host_data = {
            'ip': '10.10.200.11',
            'element': 'orange',
        }
        with pytest.raises(TypeError) as exc_info:
            create_host_from_dict(host_data)

        assert 'creds' in str(exc_info.value)

    def test_missing_ne_raises_valueerror(self):
        """Test that missing ne field raises ValueError."""
        host_data = {
            'ip': '10.10.200.11',
            'creds': {'vagrant': 'vagrant'},
        }
        with pytest.raises(TypeError) as exc_info:
            create_host_from_dict(host_data)

        assert 'element' in str(exc_info.value)

    def test_optional_fields(self):
        """Test that optional fields are handled correctly."""
        host_data = {
            'ip': '10.10.200.11',
            'element': 'orange',
            'user': 'vagrant',
            'creds': {'vagrant': 'vagrant'},
            'board': 'seed',
            'slot': 0,
            'element_id': 1,
            'name': 'CustomName',
        }
        host = create_host_from_dict(host_data)

        assert host.board == 'seed'
        assert host.slot == 0
        assert host.element_id == 1
        # Note: name will be overridden by __post_init__ if None, but we provide custom name


class TestValidateHostDict:
    """Tests for validate_host_dict function."""

    def test_validate_complete_host_dict(self):
        """Test validation of complete host dictionary."""
        host_data = {
            'ip': '10.10.200.11',
            'element': 'orange',
            'creds': {'vagrant': 'vagrant'},
        }
        # Should not raise any exception
        validate_host_dict(host_data)

    def test_validate_missing_required_field(self):
        """Test validation fails for missing required field."""
        host_data = {
            'ip': '10.10.200.11',
            'element': 'orange',
        }
        with pytest.raises(ValueError) as exc_info:
            validate_host_dict(host_data)

        assert 'creds' in str(exc_info.value)

    def test_validate_ip_not_string(self):
        """Test validation fails when ip is not a string."""
        host_data = {
            'ip': 123,
            'element': 'orange',
            'creds': {'vagrant': 'vagrant'},
        }
        with pytest.raises(ValueError) as exc_info:
            validate_host_dict(host_data)

        assert 'ip' in str(exc_info.value)
        assert 'str' in str(exc_info.value)

    def test_validate_creds_not_dict(self):
        """Test validation fails when creds is not a dict."""
        host_data = {
            'ip': '10.10.200.11',
            'element': 'orange',
            'creds': 'not_a_dict',
        }
        with pytest.raises(ValueError) as exc_info:
            validate_host_dict(host_data)

        assert 'creds' in str(exc_info.value)
        assert 'dict' in str(exc_info.value)

    def test_validate_ne_not_string(self):
        """Test validation fails when ne is not a string."""
        host_data = {
            'ip': '10.10.200.11',
            'element': 123,
            'creds': {'vagrant': 'vagrant'},
        }
        with pytest.raises(ValueError) as exc_info:
            validate_host_dict(host_data)

        assert 'element' in str(exc_info.value)
        assert 'str' in str(exc_info.value)


class TestToolchainDeserialization:
    """Tests for toolchain deserialization from host dict."""

    def _base_host(self, **extra):
        data = {
            'ip': '10.10.200.11',
            'element': 'orange',
            'creds': {'vagrant': 'vagrant'},
        }
        data.update(extra)
        return data

    def test_no_toolchain_uses_default(self):
        """Host without toolchain config gets default Toolchain."""
        host = create_host_from_dict(self._base_host())
        assert isinstance(host.toolchain, Toolchain)
        assert host.toolchain.sysroot == Path('/')
        assert host.toolchain.gcov_bin == '/usr/bin/gcov'

    def test_toolchain_with_sysroot_only(self):
        """Partial toolchain config: only sysroot provided."""
        host = create_host_from_dict(self._base_host(
            toolchain={'sysroot': '/opt/arm'}
        ))
        assert host.toolchain.sysroot == Path('/opt/arm')
        assert host.toolchain.gcov_bin == '/opt/arm/usr/bin/gcov'
        assert host.toolchain.lcov_bin == '/opt/arm/usr/bin/lcov'

    def test_toolchain_with_all_fields(self):
        """Full toolchain config: sysroot, gcov, and lcov."""
        host = create_host_from_dict(self._base_host(
            toolchain={
                'sysroot': '/opt/arm',
                'gcov': 'bin/arm-gcov',
                'lcov': 'bin/lcov',
            }
        ))
        assert host.toolchain.gcov_bin == '/opt/arm/bin/arm-gcov'
        assert host.toolchain.lcov_bin == '/opt/arm/bin/lcov'

    def test_toolchain_with_gcov_only(self):
        """Partial config: only gcov path, sysroot and lcov use defaults."""
        host = create_host_from_dict(self._base_host(
            toolchain={'gcov': 'bin/custom-gcov'}
        ))
        assert host.toolchain.sysroot == Path('/')
        assert host.toolchain.gcov_bin == '/bin/custom-gcov'
        assert host.toolchain.lcov_bin == '/usr/bin/lcov'


class TestRepoLevelOptionDefaults:
    """Tests for the ``defaults=`` parameter on ``create_host_from_dict``."""

    def _base_host(self, **extra):
        data = {
            'ip': '10.10.200.11',
            'element': 'orange',
            'creds': {'vagrant': 'vagrant'},
        }
        data.update(extra)
        return data

    def test_defaults_none_reproduces_today_behavior(self):
        """``defaults=None`` is bit-for-bit identical to the prior signature."""
        before = create_host_from_dict(self._base_host())
        after = create_host_from_dict(self._base_host(), defaults=None)
        assert before.ssh_options == after.ssh_options
        assert before.telnet_options == after.telnet_options

    def test_defaults_only_applied_when_host_has_no_options(self):
        """A repo default fills in fields the host did not specify."""
        host = create_host_from_dict(
            self._base_host(),
            defaults={'ssh_options': {'connect_timeout': 99.0, 'port': 2222}},
        )
        assert host.ssh_options.connect_timeout == 99.0
        assert host.ssh_options.port == 2222

    def test_host_overrides_default_per_key(self):
        """Per-key merge: host wins for keys it sets, default fills the rest."""
        host = create_host_from_dict(
            self._base_host(ssh_options={'port': 9000}),
            defaults={'ssh_options': {'connect_timeout': 99.0, 'port': 2222}},
        )
        assert host.ssh_options.port == 9000          # host wins
        assert host.ssh_options.connect_timeout == 99.0  # inherited from default

    def test_defaults_for_one_protocol_dont_affect_another(self):
        """An ``ssh_options`` default doesn't leak into ``telnet_options``."""
        host = create_host_from_dict(
            self._base_host(),
            defaults={'ssh_options': {'connect_timeout': 99.0}},
        )
        # telnet_options is untouched — its dataclass defaults apply.
        from otto.host.options import TelnetOptions
        assert host.telnet_options == TelnetOptions()

    def test_defaults_apply_across_multiple_protocols(self):
        """Multiple ``[host_defaults.<key>]`` tables are honored simultaneously."""
        host = create_host_from_dict(
            self._base_host(),
            defaults={
                'ssh_options': {'connect_timeout': 99.0},
                'telnet_options': {'cols': 200},
            },
        )
        assert host.ssh_options.connect_timeout == 99.0
        assert host.telnet_options.cols == 200

    def test_unknown_field_in_defaults_table_raises(self):
        """Typos inside an options sub-table fail loudly via the builder."""
        with pytest.raises(TypeError):
            create_host_from_dict(
                self._base_host(),
                defaults={'ssh_options': {'totally_unknown_field': 1}},
            )

    def test_empty_defaults_dict_is_a_noop(self):
        """An empty defaults dict matches today's behavior."""
        before = create_host_from_dict(self._base_host())
        after = create_host_from_dict(self._base_host(), defaults={})
        assert before.ssh_options == after.ssh_options
        assert before.telnet_options == after.telnet_options


class TestOsTypeDispatch:
    """Tests for ``os_type``-based dispatch in ``create_host_from_dict``."""

    def test_absent_ostype_defaults_to_unix(self):
        """A host dict without ``os_type`` builds a UnixHost (backward compatible)."""
        host = create_host_from_dict({
            'ip': '10.10.200.11', 'element': 'orange', 'creds': {'v': 'v'},
        })
        assert isinstance(host, UnixHost)
        assert host.os_type == 'unix'

    def test_explicit_unix_ostype(self):
        host = create_host_from_dict({
            'ip': '10.10.200.11', 'element': 'orange', 'creds': {'v': 'v'},
            'os_type': 'unix',
        })
        assert isinstance(host, UnixHost)

    def test_embedded_ostype_builds_embedded_host(self):
        host = create_host_from_dict({
            'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'embedded',
            'command_frame': 'zephyr',
        })
        assert isinstance(host, EmbeddedHost)
        assert not isinstance(host, ZephyrHost)   # the generic base, not Zephyr
        assert host.ip == '192.0.2.1'
        assert host.element == 'sprout'
        assert host.os_type == 'embedded'
        assert host.os_name is None                # generic: no implicit OS name

    def test_embedded_ostype_without_frame_fails_loud(self):
        with pytest.raises(ValueError, match='command_frame'):
            create_host_from_dict({
                'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'embedded',
            })

    def test_embedded_creds_are_optional(self):
        """An embedded host needs no ``creds`` — the RTOS shell has no login."""
        host = create_host_from_dict({
            'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'embedded',
            'command_frame': 'zephyr',
        })
        assert host.creds == {}

    def test_embedded_osname_and_version(self):
        host = create_host_from_dict({
            'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'zephyr',
            'os_name': 'Zephyr', 'os_version': '3.7.0',
        })
        assert host.os_name == 'Zephyr'
        assert host.os_version == '3.7.0'

    def test_embedded_resources_converted_to_set(self):
        host = create_host_from_dict({
            'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'embedded',
            'command_frame': 'zephyr',
            'resources': ['sprout', 'mote'],
        })
        assert host.resources == {'sprout', 'mote'}

    def test_embedded_hop_is_honored(self):
        host = create_host_from_dict({
            'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'embedded',
            'command_frame': 'zephyr',
            'hop': 'basil_seed',
        })
        assert host.hop == 'basil_seed'

    def test_embedded_telnet_options_deserialized(self):
        host = create_host_from_dict({
            'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'embedded',
            'command_frame': 'zephyr',
            'telnet_options': {'port': 2323},
        })
        assert isinstance(host, EmbeddedHost)
        assert host.telnet_options.port == 2323

    def test_zephyr_ostype_builds_zephyr_host(self):
        host = create_host_from_dict({
            'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'zephyr',
        })
        assert isinstance(host, ZephyrHost)
        assert isinstance(host, EmbeddedHost)       # family still embedded
        assert host.os_type == 'zephyr'              # selector recorded
        assert host.os_name == 'Zephyr'              # from the class default
        assert isinstance(host.command_frame, ZephyrFrame)

    def test_unknown_ostype_raises(self):
        with pytest.raises(ValueError) as exc_info:
            create_host_from_dict({
                'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'windows',
            })
        assert 'os_type' in str(exc_info.value)
        assert 'windows' in str(exc_info.value)

    def test_embedded_docker_capable_rejected(self):
        """A bare-metal target cannot run Docker — reject the flag outright."""
        with pytest.raises(ValueError) as exc_info:
            create_host_from_dict({
                'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'embedded',
                'docker_capable': True,
            })
        assert 'docker_capable' in str(exc_info.value)

    def test_embedded_transfer_backend_honored(self):
        """An embedded host's ``transfer`` value flows through the factory."""
        host = create_host_from_dict({
            'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'embedded',
            'command_frame': 'zephyr',
            'transfer': 'tftp',
        })
        assert isinstance(host, EmbeddedHost)
        assert host.transfer == 'tftp'

    def test_embedded_transfer_defaults_to_console(self):
        host = create_host_from_dict({
            'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'embedded',
            'command_frame': 'zephyr',
        })
        assert isinstance(host, EmbeddedHost)
        assert host.transfer == 'console'


class TestOsProfileDispatch:
    """Tests for custom ``os_type`` profiles in ``create_host_from_dict``."""

    def test_unix_profile_applies_defaults(self, restore_profiles):
        register_os_profile('custom-nix', base='unix',
                            defaults={'os_name': 'CustomNix', 'term': 'telnet'})
        host = create_host_from_dict({
            'ip': '10.10.200.11', 'element': 'orange', 'creds': {'v': 'v'},
            'os_type': 'custom-nix',
        })
        assert isinstance(host, UnixHost)
        assert host.os_name == 'CustomNix'
        assert host.term == 'telnet'

    def test_host_field_overrides_profile_default(self, restore_profiles):
        register_os_profile('custom-nix', base='unix', defaults={'os_name': 'CustomNix'})
        host = create_host_from_dict({
            'ip': '10.10.200.11', 'element': 'orange', 'creds': {'v': 'v'},
            'os_type': 'custom-nix', 'os_name': 'HostWins',
        })
        assert host.os_name == 'HostWins'

    def test_stored_ostype_is_selector_not_base_family(self, restore_profiles):
        register_os_profile('custom-nix', base='unix')
        host = create_host_from_dict({
            'ip': '10.10.200.11', 'element': 'orange', 'creds': {'v': 'v'},
            'os_type': 'custom-nix',
        })
        # The selector (lab-data os_type value) is recorded verbatim, so round-
        # trips are lossless and a future reader knows which profile was used.
        assert host.os_type == 'custom-nix'

    def test_options_three_layer_precedence(self, restore_profiles):
        """Per-key: host > profile > repo-default for an ``*_options`` table."""
        register_os_profile('nix-ssh', base='unix', defaults={
            'ssh_options': {'connect_timeout': 50.0, 'port': 3333},
        })
        host = create_host_from_dict(
            {
                'ip': '10.10.200.11', 'element': 'orange', 'creds': {'v': 'v'},
                'os_type': 'nix-ssh', 'ssh_options': {'port': 9000},
            },
            defaults={'ssh_options': {'connect_timeout': 99.0, 'keepalive_interval': 42.0}},
        )
        assert host.ssh_options.port == 9000               # host wins
        assert host.ssh_options.connect_timeout == 50.0    # profile beats repo-default
        assert host.ssh_options.keepalive_interval == 42.0  # repo-default fills the gap

    def test_embedded_profile_coerces_frame_and_filesystem_strings(self, restore_profiles):
        register_os_profile('zephyr-fat', base='embedded', defaults={
            'os_name': 'Zephyr', 'os_version': '3.7', 'command_frame': 'zephyr',
            'filesystem': 'fat-ram', 'transfer': 'console', 'max_filename_len': 32,
        })
        host = create_host_from_dict({
            'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'zephyr-fat',
        })
        assert isinstance(host, EmbeddedHost)
        assert host.os_type == 'zephyr-fat'
        assert host.os_version == '3.7'
        assert host.max_filename_len == 32
        assert isinstance(host.command_frame, ZephyrFrame)
        assert isinstance(host.filesystem, FatRamFileSystem)

    def test_embedded_profile_with_docker_capable_host_rejected(self, restore_profiles):
        register_os_profile('zephyr-fat', base='embedded', defaults={'os_name': 'Zephyr'})
        with pytest.raises(ValueError) as exc_info:
            create_host_from_dict({
                'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'zephyr-fat',
                'docker_capable': True,
            })
        assert 'docker_capable' in str(exc_info.value)


class TestValidateOsType:
    """Tests for ``os_type`` handling in ``validate_host_dict``."""

    def test_validate_embedded_minimal(self):
        """Embedded host needs only ip + ne (no creds)."""
        validate_host_dict({
            'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'embedded',
        })

    def test_validate_embedded_missing_ne(self):
        with pytest.raises(ValueError) as exc_info:
            validate_host_dict({'ip': '192.0.2.1', 'os_type': 'embedded'})
        assert 'element' in str(exc_info.value)

    def test_validate_unix_still_requires_creds(self):
        with pytest.raises(ValueError) as exc_info:
            validate_host_dict({
                'ip': '10.10.200.11', 'element': 'orange', 'os_type': 'unix',
            })
        assert 'creds' in str(exc_info.value)

    def test_validate_invalid_ostype_raises(self):
        with pytest.raises(ValueError) as exc_info:
            validate_host_dict({
                'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'windows',
            })
        assert 'os_type' in str(exc_info.value)

    def test_validate_embedded_docker_capable_raises(self):
        with pytest.raises(ValueError) as exc_info:
            validate_host_dict({
                'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'embedded',
                'docker_capable': True,
            })
        assert 'docker_capable' in str(exc_info.value)

    def test_validate_embedded_transfer_accepts_known_backends(self):
        for backend in ('console', 'tftp'):
            validate_host_dict({
                'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'embedded',
                'transfer': backend,
            })

    def test_validate_embedded_invalid_transfer_raises(self):
        with pytest.raises(ValueError) as exc_info:
            validate_host_dict({
                'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'embedded',
                'transfer': 'scp',
            })
        assert 'transfer' in str(exc_info.value)


class TestEmbeddedFilesystem:
    """Lab data's ``filesystem`` field resolves to a typed
    :class:`~otto.host.embedded_filesystem.EmbeddedFileSystem` instance on
    the built host; validation rejects unknown variants up-front so a typo
    is caught before the host is constructed.
    """

    def test_filesystem_defaults_to_no_filesystem(self):
        """No ``filesystem`` key in lab data means the host has no FS — the
        runtime transfer short-circuits with a clear error.
        """
        from otto.host.embedded_filesystem import NoFileSystem
        host = create_host_from_dict({
            'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'embedded',
            'command_frame': 'zephyr',
        })
        assert isinstance(host.filesystem, NoFileSystem)

    def test_filesystem_fat_ram_string_resolves_to_class(self):
        from otto.host.embedded_filesystem import FatRamFileSystem
        host = create_host_from_dict({
            'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'embedded',
            'command_frame': 'zephyr',
            'filesystem': 'fat-ram',
        })
        assert isinstance(host.filesystem, FatRamFileSystem)
        # `default_dest_dir` falls back to the FS mount when not explicitly set.
        assert str(host.default_dest_dir) == '/RAM:'

    def test_filesystem_littlefs_string_resolves_to_class(self):
        from otto.host.embedded_filesystem import LittleFsFileSystem
        host = create_host_from_dict({
            'ip': '192.0.2.5', 'element': 'sprout_lfs', 'os_type': 'embedded',
            'command_frame': 'zephyr',
            'filesystem': 'littlefs',
        })
        assert isinstance(host.filesystem, LittleFsFileSystem)
        assert str(host.default_dest_dir) == '/lfs'

    def test_validate_unknown_filesystem_raises(self):
        with pytest.raises(ValueError) as exc_info:
            validate_host_dict({
                'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'embedded',
                'filesystem': 'btrfs',  # not a registered embedded FS
            })
        assert 'filesystem' in str(exc_info.value)
        assert 'btrfs' in str(exc_info.value)
        # The error names the registered types so the typo is diagnosable.
        assert 'fat-ram' in str(exc_info.value)

    def test_explicit_default_dest_dir_overrides_filesystem_mount(self):
        """A host with a real FS but a non-default ``default_dest_dir`` (e.g.
        a sub-directory under the mount) should keep its lab-data value.
        """
        host = create_host_from_dict({
            'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'embedded',
            'command_frame': 'zephyr',
            'filesystem': 'fat-ram',
            'default_dest_dir': '/RAM:/uploads',
        })
        assert str(host.default_dest_dir) == '/RAM:/uploads'


class TestEmbeddedToolchainDeserialization:
    """Embedded hosts carry a per-host Toolchain, like Unix hosts."""

    def _embedded_host(self, **extra):
        data = {
            'ip': '192.0.2.99',
            'element': 'sproutx',
            'os_type': 'embedded',
            'os_name': 'Zephyr',
            'os_version': '3.7',
            'transfer': 'console',
            'filesystem': 'none',
            'command_frame': 'zephyr',
        }
        data.update(extra)
        return data

    def test_no_toolchain_uses_default(self):
        host = create_host_from_dict(self._embedded_host())
        assert isinstance(host.toolchain, Toolchain)
        assert host.toolchain.sysroot == Path('/')
        assert host.toolchain.gcov_bin == '/usr/bin/gcov'

    def test_sysroot_only_uses_default_relative_tools(self):
        """Partial config: sysroot only; gcov/lcov stay sysroot-relative."""
        host = create_host_from_dict(self._embedded_host(
            toolchain={'sysroot': '/opt/arm'}
        ))
        assert host.toolchain.sysroot == Path('/opt/arm')
        assert host.toolchain.gcov_bin == '/opt/arm/usr/bin/gcov'
        assert host.toolchain.lcov_bin == '/opt/arm/usr/bin/lcov'

    def test_cross_toolchain_resolves_gcov_under_sysroot(self):
        host = create_host_from_dict(self._embedded_host(
            toolchain={
                'sysroot': '/home/vagrant/zephyr-sdk-0.16.8/arm-zephyr-eabi',
                'gcov': 'bin/arm-zephyr-eabi-gcov',
                'lcov': '/usr/bin/lcov',
            }
        ))
        assert host.toolchain.gcov_bin == (
            '/home/vagrant/zephyr-sdk-0.16.8/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcov'
        )
        # lcov is absolute -> ignores the cross sysroot (host-side merge tool).
        assert host.toolchain.lcov_bin == '/usr/bin/lcov'


class TestSnmpBlock:
    """The lab ``snmp`` block deserializes to SnmpOptions on both bases."""

    def test_absent_snmp_is_none(self):
        host = create_host_from_dict({
            'ip': '10.10.200.11', 'element': 'orange', 'creds': {'v': 'v'},
        })
        assert host.snmp is None

    def test_embedded_snmp_block_parsed(self):
        host = create_host_from_dict({
            'ip': '192.0.2.1', 'element': 'sprout', 'os_type': 'embedded',
            'command_frame': 'zephyr',
            'snmp': {
                'address': '10.10.200.14', 'port': 16101, 'community': 'public',
                'oids': ['1.3.6.1.2.1.1.3.0', '1.3.6.1.4.1.63245.1.1.0'],
            },
        })
        assert isinstance(host, EmbeddedHost)
        assert isinstance(host.snmp, SnmpOptions)
        assert host.snmp.address == '10.10.200.14'
        assert host.snmp.port == 16101
        # JSON list coerced to tuple (frozen-friendly field type)
        assert host.snmp.oids == ('1.3.6.1.2.1.1.3.0', '1.3.6.1.4.1.63245.1.1.0')

    def test_unix_snmp_block_parsed(self):
        """SNMP is not embedded-only — a Unix host may declare one."""
        host = create_host_from_dict({
            'ip': '10.10.200.11', 'element': 'orange', 'creds': {'v': 'v'},
            'snmp': {'oids': ['1.3.6.1.2.1.1.3.0']},
        })
        assert isinstance(host, UnixHost)
        assert isinstance(host.snmp, SnmpOptions)
        # address omitted -> None (the monitor factory defaults it to host.ip)
        assert host.snmp.address is None
        assert host.snmp.community == 'public'  # field default
        assert host.snmp.version == '2c'        # field default
