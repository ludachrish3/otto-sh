"""THE SPLIT-BRAIN GUARD for ``forget-key``: ``otto host <id> forget-key`` and
``host.forget_key()`` edit the same file the same way and say the same thing.

Both paths are the one method, :meth:`~otto.host.unix_host.UnixHost.forget_key`,
over :meth:`~otto.host.known_hosts.KnownHosts.forget`; the CLI adds only the
rendering of the returned :class:`~otto.host.known_hosts.ForgetResult`. Each
case plants one known-hosts file, runs the CLI against it, restores the
planted text into the SAME path and runs the library against it, then
compares the two: the lines left in the file, and the CLI's stdout against
``str()`` of the library's result. The path is the same on both sides, so
the messages may be compared whole.

The hostile conditions, one per case:

* an entry the user's own ``ssh`` wrote under the host's ADDRESS, and a host
  whose ID merely starts with the target's (``router10``), both of which
  must survive;
* a shared line written in OpenSSH's bracketed port form
  (``[router1]:2222,[router9]:2222``), which a Rich-markup render would read
  as style tags and delete from the printed "kept:" line;
* no entry at all, which must still exit 0 and say so, so a script can run
  the verb unconditionally;
* the DEFAULT file, which the verb must resolve through
  :func:`otto.host.known_hosts.known_hosts_path` looked up on its module, so
  the suite's ``_hermetic_known_hosts`` redirect reaches it (a ``from``
  import would bind the real ``~/.ssh/known_hosts``).
"""

import asyncio
from pathlib import Path
from unittest.mock import patch

import asyncssh
import pytest

from otto.cli.host import host_app
from otto.host import known_hosts
from otto.host.element import Element
from otto.host.known_hosts import ForgetResult
from otto.host.login_proxy import Cred
from otto.host.options import SshOptions
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from tests._fixtures.dispatch import DispatchRunner

runner = DispatchRunner()

_ADDRESS = "10.0.0.1"


def _pub() -> str:
    key = asyncssh.generate_private_key("ssh-ed25519").convert_to_public()
    return key.export_public_key("openssh").decode().strip()


def _host(options: SshOptions, name: str = "router1") -> UnixHost:
    """A real UnixHost; forget-key makes no connection, so none is faked."""
    return UnixHost(
        ip=_ADDRESS,
        element=Element(name),
        creds=[Cred(login="admin", password="secret")],
        log=LogMode.NORMAL,
        ssh_options=options,
    )


def _cli(options: SshOptions) -> tuple[int, str]:
    """Run ``otto host router1 forget-key``; return its exit code and stdout."""
    with patch("otto.fleet.get_host", return_value=_host(options)):
        result = runner.invoke(host_app, ["router1", "forget-key"])
    return result.exit_code, result.stdout


def _library(options: SshOptions) -> ForgetResult:
    """Run ``host.forget_key()`` on a fresh host with the same options."""
    return asyncio.run(_host(options).forget_key())


def _differential(path: Path, planted: str, options: SshOptions) -> tuple[str, str, ForgetResult]:
    """Run both sides over *planted* at *path*; return the file each left and the library result.

    Asserts what both sides must agree on: exit 0, the same remaining
    lines, and the CLI printing exactly ``str()`` of the library's result.
    """
    path.write_text(planted)
    code, printed = _cli(options)
    after_cli = path.read_text()

    path.write_text(planted)
    result = _library(options)
    after_library = path.read_text()

    assert code == 0, printed
    assert after_cli == after_library
    assert printed.rstrip("\n") == str(result)
    return after_cli, printed, result


def test_cli_and_library_remove_the_same_entries_and_say_so(tmp_path):
    path = tmp_path / "known_hosts"
    own, on_port, by_address, lookalike = _pub(), _pub(), _pub(), _pub()
    lines = [
        f"router1 {own}",
        f"[router1]:2222 {on_port}",
        f"{_ADDRESS} {by_address}",
        f"router10 {lookalike}",
    ]
    planted = "".join(f"{line}\n" for line in lines)
    options = SshOptions(port=2222, known_hosts=str(path))

    left, printed, result = _differential(path, planted, options)

    assert printed.rstrip("\n") == f"removed 2 entries for router1 from {path}"
    assert result.removed == 2
    assert left == f"{_ADDRESS} {by_address}\nrouter10 {lookalike}\n"


def test_a_kept_bracketed_line_is_printed_verbatim(tmp_path):
    path = tmp_path / "known_hosts"
    own, shared = _pub(), _pub()
    shared_line = f"[router1]:2222,[router9]:2222 {shared}"
    planted = f"router1 {own}\n{shared_line}\n"
    options = SshOptions(port=2222, known_hosts=str(path))

    left, printed, _ = _differential(path, planted, options)

    assert printed.splitlines() == [
        f"removed 1 entry for router1 from {path}",
        f"kept: {shared_line}",
    ]
    assert left == f"{shared_line}\n"


def test_no_entry_exits_zero_and_says_so(tmp_path):
    path = tmp_path / "known_hosts"
    other = f"router2 {_pub()}\n"
    options = SshOptions(known_hosts=str(path))

    left, printed, result = _differential(path, other, options)

    assert printed.rstrip("\n") == f"no entry for router1 in {path}"
    assert result.removed == 0
    assert left == other


def test_a_missing_file_is_no_entry_and_is_not_created(tmp_path):
    path = tmp_path / "absent" / "known_hosts"
    options = SshOptions(known_hosts=str(path))

    code, printed = _cli(options)
    result = _library(options)

    assert code == 0, printed
    assert printed.rstrip("\n") == str(result) == f"no entry for router1 in {path}"
    assert not path.exists()


@pytest.mark.parametrize("port", [22, 2222])
def test_the_default_file_is_resolved_through_the_one_resolver(port):
    """The verb reads ``known_hosts_path`` off its module, so the hermetic redirect applies.

    ``_hermetic_known_hosts`` patches the resolver in its module; a verb that
    bound the function at import time would edit the real
    ``~/.ssh/known_hosts`` and print that path instead.
    """
    options = SshOptions(port=port)
    path = known_hosts.known_hosts_path(options)
    assert path != Path(options.known_hosts).expanduser()  # the premise: redirected
    # The redirected file is this worker's, shared with every other test it
    # runs: whatever it held is put back afterwards.
    before = path.read_text() if path.exists() else None
    name = known_hosts.entry_name("router1", port)
    planted = f"{name} {_pub()}\nrouter2 {_pub()}\n"
    try:
        left, printed, _ = _differential(path, planted, options)
    finally:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(before)

    assert printed.rstrip("\n") == f"removed 1 entry for router1 from {path}"
    assert left == planted.splitlines(keepends=True)[1]
