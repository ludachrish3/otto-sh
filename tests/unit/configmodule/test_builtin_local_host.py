"""load_lab injects a built-in `local` host into every lab, backend-agnostically."""

from otto.configmodule.lab import Lab, load_lab
from otto.host.builtin_hosts import BUILTIN_LOCAL_HOST_ID, make_builtin_local_host
from otto.host.host import BaseHost


class _StubRepo:
    """Minimal LabRepository stand-in: returns a lab with the given hosts."""

    def __init__(self, hosts: list[BaseHost]) -> None:
        self._hosts = hosts

    def load_lab(self, name: str, preferences: object = None) -> Lab:
        lab = Lab(name=name)
        for h in self._hosts:
            lab.add_host(h)
        return lab

    def list_labs(self) -> list[str]:
        return []


def test_load_lab_injects_local_when_absent() -> None:
    lab = load_lab(["any"], repository=_StubRepo(hosts=[]))
    assert BUILTIN_LOCAL_HOST_ID in lab.hosts
    assert isinstance(lab.hosts[BUILTIN_LOCAL_HOST_ID], BaseHost)


def test_load_lab_does_not_override_user_local() -> None:
    # Stand in for a user-defined host with id="local" using the LocalHost type.
    mine = make_builtin_local_host()
    lab = load_lab(["any"], repository=_StubRepo(hosts=[mine]))
    assert lab.hosts[BUILTIN_LOCAL_HOST_ID] is mine  # user's instance, not a fresh injection
