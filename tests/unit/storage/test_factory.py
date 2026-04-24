from pathlib import Path

import pytest

from otto.host.remoteHost import RemoteHost
from otto.host.toolchain import Toolchain
from otto.storage.factory import (
    create_host_from_dict,
    validate_host_dict,
)


class TestCreateHostFromDict:
    """Tests for create_host_from_dict function."""

    def test_create_remotehost_with_complete_data(self):
        """Test creating RemoteHost with all fields."""
        host_data = {
            'ip': '10.10.200.11',
            'ne': 'orange',
            'board': 'seed',
            'creds': {'vagrant': 'vagrant'},
            'resources': ['orange'],
        }
        host = create_host_from_dict(host_data)

        assert isinstance(host, RemoteHost)
        assert host.ip == '10.10.200.11'
        assert host.ne == 'orange'
        assert host.board == 'seed'
        assert host.creds == {'vagrant': 'vagrant'}
        assert host.resources == {'orange'}

    def test_resources_list_converted_to_set(self):
        """Test that resources list is converted to set."""
        host_data = {
            'ip': '10.10.200.11',
            'ne': 'orange',
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
            'ne': 'orange',
            'creds': {'vagrant': 'vagrant'},
            'resources': {'orange', 'tomato'},
        }
        host = create_host_from_dict(host_data)

        assert isinstance(host.resources, set)
        assert host.resources == {'orange', 'tomato'}

    def test_missing_ip_raises_typeerror(self):
        """Test that missing ip field raises ValueError."""
        host_data = {
            'ne': 'orange',
            'creds': {'vagrant': 'vagrant'},
        }
        with pytest.raises(TypeError) as exc_info:
            create_host_from_dict(host_data)

        assert 'ip' in str(exc_info.value)

    def test_missing_creds_raises_typeerror(self):
        """Test that missing creds field raises ValueError."""
        host_data = {
            'ip': '10.10.200.11',
            'ne': 'orange',
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

        assert 'ne' in str(exc_info.value)

    def test_optional_fields(self):
        """Test that optional fields are handled correctly."""
        host_data = {
            'ip': '10.10.200.11',
            'ne': 'orange',
            'user': 'vagrant',
            'creds': {'vagrant': 'vagrant'},
            'board': 'seed',
            'slot': 0,
            'neId': 1,
            'name': 'CustomName',
        }
        host = create_host_from_dict(host_data)

        assert host.board == 'seed'
        assert host.slot == 0
        assert host.neId == 1
        # Note: name will be overridden by __post_init__ if None, but we provide custom name


class TestValidateHostDict:
    """Tests for validate_host_dict function."""

    def test_validate_complete_host_dict(self):
        """Test validation of complete host dictionary."""
        host_data = {
            'ip': '10.10.200.11',
            'ne': 'orange',
            'creds': {'vagrant': 'vagrant'},
        }
        # Should not raise any exception
        validate_host_dict(host_data)

    def test_validate_missing_required_field(self):
        """Test validation fails for missing required field."""
        host_data = {
            'ip': '10.10.200.11',
            'ne': 'orange',
        }
        with pytest.raises(ValueError) as exc_info:
            validate_host_dict(host_data)

        assert 'creds' in str(exc_info.value)

    def test_validate_ip_not_string(self):
        """Test validation fails when ip is not a string."""
        host_data = {
            'ip': 123,
            'ne': 'orange',
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
            'ne': 'orange',
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
            'ne': 123,
            'creds': {'vagrant': 'vagrant'},
        }
        with pytest.raises(ValueError) as exc_info:
            validate_host_dict(host_data)

        assert 'ne' in str(exc_info.value)
        assert 'str' in str(exc_info.value)


class TestToolchainDeserialization:
    """Tests for toolchain deserialization from host dict."""

    def _base_host(self, **extra):
        data = {
            'ip': '10.10.200.11',
            'ne': 'orange',
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
