# Host capabilities
Beyond the four core commands, hosts expose **capabilities** — richer behaviors
like power control, product lifecycle, privilege elevation, and on-host file
operations. Many are also `otto host` verbs (auto-exposed from `@cli_exposed`
methods); some are Python-only. Full method signatures live in the
{doc}`API reference <../../../../api/host/index>`; this page covers what each capability
is for and how to use it.

| Capability | CLI verbs | Python-only |
|------------|-----------|-------------|
| Power, reboot & reachability | `power`, `reboot`, `shutdown` | `is_reachable`, `wait_until_up`, `wait_until_down` |
| Products & lifecycle | `stage`, `install`, `uninstall`, `cleanup`, `is-installed`, `is-uninstalled`, `is-clean` | — |
| Log retrieval | `get-logs`, `get-product-logs`, `get-debug-logs` | `log_dest` |
| Dev tools & toolchain tools | `install-tools`, `install-dev-tools`, `uninstall-dev-tools`, `install-toolchain-tools`, `remove-toolchain-tools` | `toolchain_tools_absent` |
| Remote file operations | `exists`, `ls`, `glob`, `mkdir`, `rm`, `cp`, `mv`, `read-file`, `write-file` | — |
| Kernel modules | `lsmod`, `load`, `unload` | — |
| Userland capabilities | `probe` | — |
| Privilege elevation | — | `run(sudo=True)`, `as_user`, `switch_user`, `current_user` |






```{toctree}
:caption: Topics
:hidden:

power
products
dev-tools
files
modules
userland
privilege
```
