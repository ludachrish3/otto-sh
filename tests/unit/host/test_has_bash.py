"""``has_bash`` host capability field.

Tunnel discovery (:mod:`otto.tunnel.discovery`) needs to scan only hosts that
can run our bash-tagged socat tunnels — the tagging shells out via
``bash -c 'exec -a …'``. ``has_bash`` replaces the old nominal
``isinstance(host, (UnixHost, LocalHost))`` filter with a proper capability
attribute: default ``True`` for Unix-family hosts (:class:`UnixHost`,
:class:`LocalHost`, :class:`DockerContainerHost`), default ``False`` for
:class:`EmbeddedHost` (and subclasses like :class:`ZephyrHost`), and
overridable per host in ``lab.json`` only when a host defies its norm (e.g. a
minimal ``alpine``/``centos6`` container with no bash).
"""

from unittest.mock import MagicMock

from otto.host.command_frame import ZephyrFrame
from otto.host.docker_host import DockerContainerHost
from otto.host.embedded_host import EmbeddedHost
from otto.host.local_host import LocalHost
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.storage.factory import create_host_from_dict


def _mock_parent(parent_id: str = "pepper_seed"):
    parent = MagicMock()
    parent.id = parent_id
    parent.name = parent_id
    return parent


class TestHasBashPerClassDefault:
    """Each concrete host class's own default, absent any lab-data override."""

    def test_unix_host_default_true(self):
        host = UnixHost(ip="10.0.0.1", element="box", creds=[Cred(login="u", password="p")])
        assert host.has_bash is True

    def test_embedded_host_default_false(self):
        host = EmbeddedHost(ip="192.0.2.1", element="sprout", command_frame=ZephyrFrame())
        assert host.has_bash is False

    def test_local_host_default_true(self):
        assert LocalHost().has_bash is True

    def test_docker_container_host_default_true(self):
        host = DockerContainerHost(
            parent=_mock_parent(),
            container_id="abc123def456",
            project="repo1",
            service="api",
            compose_project="otto-repo1-vagrant",
        )
        assert host.has_bash is True


class TestHasBashFromLabData:
    """Through ``create_host_from_dict``: unset -> per-class default; set -> honored."""

    def test_unix_dict_without_has_bash_defaults_true(self):
        host = create_host_from_dict(
            {
                "ip": "10.10.200.11",
                "element": "orange",
                "creds": [{"login": "v", "password": "v"}],
            }
        )
        assert host.has_bash is True

    def test_embedded_dict_without_has_bash_defaults_false(self):
        host = create_host_from_dict(
            {
                "ip": "192.0.2.1",
                "element": "sprout",
                "os_type": "embedded",
                "command_frame": "zephyr",
            }
        )
        assert host.has_bash is False

    def test_unix_dict_explicit_has_bash_false_is_honored(self):
        host = create_host_from_dict(
            {
                "ip": "10.10.200.11",
                "element": "orange",
                "creds": [{"login": "v", "password": "v"}],
                "has_bash": False,
            }
        )
        assert host.has_bash is False

    def test_embedded_dict_explicit_has_bash_true_is_honored(self):
        host = create_host_from_dict(
            {
                "ip": "192.0.2.1",
                "element": "sprout",
                "os_type": "embedded",
                "command_frame": "zephyr",
                "has_bash": True,
            }
        )
        assert host.has_bash is True
