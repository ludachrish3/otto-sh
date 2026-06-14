# Ensure that the OttoLogger is initialized correctly before anything else happens
from otto.logger import get_otto_logger as get_otto_logger

get_otto_logger()

from otto.cli import app

from .configmodule import all_hosts, get_host, get_lab, run_on_all_hosts
from .context import OttoContext, get_context, open_context, try_get_context
