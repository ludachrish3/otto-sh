"""The options ``otto <argv> --help`` shows, as rows: the help screen as data.

:func:`help_rows` runs ``otto <argv>`` in a subprocess through
``otto.cli.main.entry()``, the full path a subcommand's ``--help`` takes, and
replaces only the last step: where Typer would render the command's options
(``typer.rich_utils.rich_format_help``), the child writes one row per option
it would show, in order, to a file. Everything before that, the bootstrap,
the selection and the command's resolution, is the real one.
"""

import enum
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from tests._fixtures.paths import CHECKOUT_ROOT

ROWS_FILE_VAR = "HELP_ROWS_FILE"
"""The variable naming the file the child writes its rows to."""


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, enum.Enum):
        return _jsonable(value.value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return repr(value)


def _row(param: Any) -> "dict[str, Any]":
    # Long spellings first, then short ones, each in declared order: the
    # columns the screen shows them in. Click builds the help option's
    # spellings from a set, so their declared order is the hash seed's.
    long = [o for o in param.opts if o.startswith("--")]
    short = [o for o in param.opts if not o.startswith("--")]
    return {
        "name": param.name,
        "spellings": [*long, *short, *param.secondary_opts],
        "help": param.help,
        "show_default": param.show_default,
        "default": _jsonable(param.default),
        "takes_value": not param.is_flag and not param.count,
    }


def _main() -> None:
    """The child: ``otto <argv>`` whose options help writes rows instead of a screen."""
    import typer.rich_utils

    out = Path(os.environ[ROWS_FILE_VAR])

    def record(*, obj: Any, ctx: Any, markup_mode: Any) -> None:
        params = obj.get_params(ctx)
        rows = [_row(p) for p in params if p.param_type_name == "option" and not p.hidden]
        out.write_text(json.dumps(rows))

    typer.rich_utils.rich_format_help = record
    sys.argv = ["otto", *sys.argv[1:]]
    from otto.cli.main import entry

    entry()


def help_rows(env: "dict[str, str]", *argv: str) -> "list[dict[str, Any]]":
    """The rows of the options ``otto <argv>`` shows under *env* (*argv* ends in ``--help``).

    Each row is the option's ``name``, its ``spellings`` (long ones, then
    short ones, then secondary ones such as a boolean's ``--no-`` form), its
    ``help``, its ``show_default`` as declared, its ``default`` as plain data,
    and whether it ``takes_value``.
    Hidden options are left out, as the screen leaves them out.
    """
    with tempfile.TemporaryDirectory(prefix="otto-help-rows-") as scratch:
        out = Path(scratch) / "rows.json"
        child = {
            **env,
            ROWS_FILE_VAR: str(out),
            "PYTHONPATH": os.pathsep.join(
                p for p in (str(CHECKOUT_ROOT), env.get("PYTHONPATH")) if p
            ),
        }
        result = subprocess.run(
            [sys.executable, "-c", "from tests._fixtures.help_rows import _main; _main()", *argv],
            env=child,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (argv, result.stdout, result.stderr)
        assert out.is_file(), (argv, "help never reached its options", result.stdout, result.stderr)
        return json.loads(out.read_text())
