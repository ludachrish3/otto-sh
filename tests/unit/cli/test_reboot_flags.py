"""The ``reboot`` flags carry no literal defaults: the host's ``reboot_options`` supply them."""

import typer

from otto.cli.param_synth import build_cli_binding
from otto.host.host import BaseHost


def test_the_reboot_flags_default_unset_and_say_so():
    binding = build_cli_binding(BaseHost.reboot)
    by_name = {p.name: p for p in binding.params}
    for name in ("timeout", "down_timeout", "poll_interval"):
        meta = getattr(by_name[name].annotation, "__metadata__", ())
        infos = [m for m in meta if isinstance(m, typer.models.OptionInfo)]
        assert infos, f"{name} is not a typer option"
        assert by_name[name].default is None
        assert "reboot_options" in (infos[0].help or "")
