"""A fake ``gh``: scripted replies, consumed as a queue, in-process or on ``PATH``.

``scripted_gh`` is the in-process runner (a ``GhRunner``); ``install_fake_gh``
writes an executable ``gh`` for subprocesses. Both answer by :func:`take_reply`,
the one queue rule: a call takes the first unconsumed reply whose ``match``
tokens are all substrings of the space-joined argv, and consumes it unless the
reply is ``repeat``. A call nothing matches fails with status 1.
"""

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from scripts.local_gate_status import GhResult, GhRunner
from tests._fixtures.paths import CHECKOUT_ROOT

REPLIES_FILE = "gh-replies.json"
CONSUMED_FILE = "gh-consumed.json"
CALLS_FILE = "gh-calls.jsonl"


@dataclass
class GhReply:
    """One scripted answer: it matches argv containing every ``match`` token."""

    match: "list[str]"
    stdout: str = ""
    status: int = 0
    stderr: str = ""
    repeat: bool = False


def take_reply(replies: "list[GhReply]", consumed: "list[int]", argv: "list[str]") -> GhResult:
    """Answer *argv* from the queue, adding the index of a used reply to *consumed*."""
    line = " ".join(argv)
    for index, reply in enumerate(replies):
        if index in consumed or not all(token in line for token in reply.match):
            continue
        if not reply.repeat:
            consumed.append(index)
        return GhResult(reply.status, reply.stdout, reply.stderr)
    return GhResult(1, "", f"fake gh: unmatched: {line}")


class _ScriptedGh:
    def __init__(self, replies: "list[GhReply]") -> None:
        self._replies = replies
        self._consumed: list[int] = []
        self.calls: list[list[str]] = []

    def __call__(self, args: "list[str]") -> GhResult:
        self.calls.append(list(args))
        return take_reply(self._replies, self._consumed, args)


def scripted_gh(replies: "list[GhReply]") -> GhRunner:
    """The in-process twin of :func:`install_fake_gh`; ``.calls`` records the argv lists."""
    return _ScriptedGh(replies)


_SCRIPT = """#!{python}
import json
import sys
from pathlib import Path

sys.path.insert(0, {root!r})
from tests._fixtures.fake_gh import CALLS_FILE, CONSUMED_FILE, REPLIES_FILE, GhReply, take_reply

here = Path(__file__).resolve().parent
argv = sys.argv[1:]
with (here / CALLS_FILE).open("a") as calls:
    calls.write(json.dumps({{"argv": argv}}) + "\\n")
replies = [GhReply(**r) for r in json.loads((here / REPLIES_FILE).read_text())]
consumed_path = here / CONSUMED_FILE
consumed = json.loads(consumed_path.read_text()) if consumed_path.exists() else []
result = take_reply(replies, consumed, argv)
consumed_path.write_text(json.dumps(consumed))
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
sys.exit(result.status)
"""


def install_fake_gh(bin_dir: Path, replies: "list[GhReply]") -> Path:
    """Write ``bin_dir/gh`` answering from *replies*; return *bin_dir* (prepend it to PATH)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / REPLIES_FILE).write_text(json.dumps([asdict(r) for r in replies]))
    (bin_dir / CONSUMED_FILE).unlink(missing_ok=True)
    (bin_dir / CALLS_FILE).unlink(missing_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(_SCRIPT.format(python=sys.executable, root=str(CHECKOUT_ROOT)))
    gh.chmod(0o755)
    return bin_dir


def gh_calls(bin_dir: Path) -> "list[list[str]]":
    """The argv lists the installed fake ``gh`` was called with, in order."""
    path = bin_dir / CALLS_FILE
    if not path.exists():
        return []
    return [json.loads(line)["argv"] for line in path.read_text().splitlines()]
