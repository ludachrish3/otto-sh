"""A host whose SSH port refuses the connection fails with one line, end to end (#482).

The unit tests beside ``make_method_command`` pin the error leg itself. This
one runs the real ``otto`` binary against a real closed port, because what the
issue reported is what a user sees: a rendered traceback (170 KB of it, with
the lab's passwords among the locals) where one line naming the host belongs.
"""

import socket

from tests._fixtures.generated_repo import BUDGET_SSH_HOST, generate_repo
from tests.e2e._otto_subprocess import run_otto


def _closed_port() -> int:
    """A loopback port with nothing listening: bound, read, released."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_a_refused_ssh_connection_prints_one_line_naming_the_host(tmp_path):
    port = _closed_port()
    repo = generate_repo(tmp_path / "repo", files=1, dirs=1, realistic=True, ssh_lab_port=port)

    result = run_otto(
        ["host", BUDGET_SSH_HOST, "exec", "true"],
        xdir=tmp_path,
        sut_dirs=repo,
        lab="unix",  # the JSON lab `generate_repo` writes; its second source holds the host
        extra_env={"COLUMNS": "200"},  # so rich cannot fold the one line in two
    )

    assert result.returncode == 1, result.stderr
    output = result.stdout + result.stderr
    assert "Traceback" not in output
    assert "Password1" not in output  # a lab password the generated lab declares
    error_lines = [line for line in output.splitlines() if BUDGET_SSH_HOST in line]
    refused = f"[Errno 111] Connect call failed ('127.0.0.1', {port})"
    expected = f"host '{BUDGET_SSH_HOST}' exec: {refused}"
    assert error_lines == [expected], output
