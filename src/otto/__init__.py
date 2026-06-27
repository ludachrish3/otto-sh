from otto.logger import get_otto_logger as get_otto_logger

# Blessed, concise form for declaring suite/instruction Options with validation:
# ``from otto import options`` then ``@options`` on the Options class. A re-export
# of ``pydantic.dataclasses.dataclass`` — otto introspects it as a plain dataclass,
# so fields, inheritance, and ``Annotated[..., typer.Option(...)]`` all work as before.
from pydantic.dataclasses import dataclass as options

from otto.cli import app

from .configmodule import all_hosts, get_host, get_lab, run_on_all_hosts
from .context import OttoContext, get_context, open_context, try_get_context
