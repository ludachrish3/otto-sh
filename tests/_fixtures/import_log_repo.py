"""A generated repo whose test modules log their import, and whose tests log their run.

Shared by the tests that drive real in-process pytest sessions over a repo
(``run_tests``, the collect-only seed and refresh, the test-name completers):
"which files did this session import" and "did anything run" are read from
two log files beside the repo. Between two sessions in one test the generated
modules are evicted from ``sys.modules`` (:meth:`ImportLogRepo.next_run`), so
a second import is a real one.
"""

import sys
from pathlib import Path


def logged_lines(path: Path) -> list[str]:
    """The lines a log file holds, in the order they were written; ``[]`` before any."""
    return path.read_text().split() if path.exists() else []


def logged_test(name: str, indent: str = "") -> str:
    """Source of a test function (or method, with *indent*) that logs its own run."""
    return f"{indent}def {name}(self=None):\n{indent}    _ran({name!r})\n"


class ImportLogRepo:
    """A generated repo whose test modules log their import and their tests log their run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.imports = root.parent / f"{root.name}-imports.log"
        self.ran = root.parent / f"{root.name}-ran.log"

    def module(self, stem: str, body: str) -> str:
        """Source of test module *stem*: logs its import, then *body*."""
        return (
            f"with open({str(self.imports)!r}, 'a') as _f:\n"
            f"    _f.write({stem!r} + '\\n')\n\n"
            f"def _ran(name):\n"
            f"    with open({str(self.ran)!r}, 'a') as f:\n"
            f"        f.write(name + '\\n')\n\n\n" + body
        )

    def write(self, rel: str, source: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        return path

    def imported(self) -> list[str]:
        return sorted(logged_lines(self.imports))

    def ran_tests(self) -> list[str]:
        return sorted(logged_lines(self.ran))

    def next_run(self) -> None:
        """Forget the last session as a new process would.

        Its modules (the repo's, and any beside it), the library stats the
        process kept for them, and both logs.
        """
        import otto.suite.plugin

        root = str(self.root.parent)
        for name, module in list(sys.modules.items()):
            if (getattr(module, "__file__", None) or "").startswith(root):
                del sys.modules[name]
        stats = otto.suite.plugin._LIB_DEP_STATS
        for path in [p for p in stats if p.startswith(root)]:
            del stats[path]
        self.imports.unlink(missing_ok=True)
        self.ran.unlink(missing_ok=True)
