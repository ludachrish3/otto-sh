r"""The cases that pin down what a command's ``value`` is.

otto's rule: the ``value`` of a command is the command's output with exactly
one trailing newline removed, and nothing else changed. No other newline is
stripped, no ``\r`` is dropped, no whitespace is trimmed.

:data:`EXACT_OUTPUT_CASES` is the one table of programs and the bytes they
write. Unit tests, otto's own conformance suite and extension authors writing
a host class or an execution route all import it, so every route is judged on
the same inputs. Run :attr:`OutputCase.command` on a route, then compare the
result's ``value`` with :attr:`OutputCase.value` and its return code with
:attr:`OutputCase.retcode`.
"""

from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class OutputCase:
    """One program, the bytes it writes and the ``value`` otto must report."""

    id: str
    """A stable, unique name for the case (a test id)."""

    command: str
    """A command spelling, for the shell that runs it, that makes a program write :attr:`writes`."""

    writes: bytes
    """The bytes the program writes, to stdout or to stderr as the command says."""

    retcode: int = 0
    """The exit status the command ends with."""

    @property
    def text(self) -> str:
        """:attr:`writes` decoded as UTF-8, invalid bytes replaced."""
        return self.writes.decode("utf-8", errors="replace")

    @property
    def value(self) -> str:
        r""":attr:`text` with exactly one trailing ``"\n"`` removed."""
        return self.text.removesuffix("\n")


EXACT_OUTPUT_CASES: list[OutputCase] = [
    OutputCase(id=cmd, command=cmd, writes=writes, retcode=rc)
    for cmd, writes, rc in [
        ("true", b"", 0),
        ("echo 3", b"3\n", 0),
        (r"printf 'a\n\n'", b"a\n\n", 0),
        (r"printf 'abc'", b"abc", 0),
        (r"printf '\n'", b"\n", 0),
        (r"printf '\na\n'", b"\na\n", 0),
        (r"printf '\n\na\n'", b"\n\na\n", 0),
        (r"printf ' \n'", b" \n", 0),
        (r"printf 'a  \n'", b"a  \n", 0),
        (r"printf 'a\tb\n'", b"a\tb\n", 0),
        (r"printf 'a\nb\nc\n'", b"a\nb\nc\n", 0),
        (r"printf 'a\nb'", b"a\nb", 0),
        ("seq 1 2000", "".join(f"{i}\n" for i in range(1, 2001)).encode(), 0),
        ("echo err >&2", b"err\n", 0),
        ("sh -c 'echo x; exit 3'", b"x\n", 3),
        (r"printf 'é\n'", "é\n".encode(), 0),
        (r"printf 'a\r\nb\rc\n\n'", b"a\r\nb\rc\n\n", 0),
    ]
] + [
    OutputCase(id="invalid-utf8", command=r"printf '\377\n'", writes=b"\xff\n"),
    OutputCase(
        id="long-line-no-newline",
        command=r"head -c 100000 /dev/zero | tr '\0' x",
        writes=b"x" * 100_000,
    ),
]
