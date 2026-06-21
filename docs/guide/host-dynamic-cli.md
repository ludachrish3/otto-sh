# Host methods on the CLI

Any host coroutine method decorated with `@cli_exposed` is automatically an
`otto host` subcommand, scoped to the host's class:

    otto host <host_id> reboot true        # host.reboot(hard=True)
    otto host <host_id> power on            # host.power("on")
    otto host <host_id> install             # host.install()
    otto host <host_id> ls /var/log         # host.ls("/var/log")

The menu is **class-scoped**: `otto host <id> --help` lists only the verbs defined on
that host's class. A unix host shows the file-ops verbs (`mkdir`, `cp`, `read-file`, …);
an embedded host shows `exists`/`ls`/`rm` but not the file-ops it doesn't implement.

This works for **project-defined** methods too — register a host subclass with a
`@cli_exposed` method and it appears under `otto host` for that class's hosts, with no
extra wiring:

    from otto.utils import cli_exposed

    class MyHost(UnixHost):
        @cli_exposed(help="Flash firmware to the board")
        async def flash_firmware(self, image: Path):
            ...

    # → otto host <my-host-id> flash-firmware ./build/app.bin

Arguments are passed positionally and coerced from the method's annotations
(`bool` accepts `true`/`false`/`1`/`0`/`yes`/`on`; `Path`/`int` are converted). A verb
returning `(Status, str)` exits non-zero when the status is not OK.
