# Dev tools and toolchain

Full signatures: {class}`~otto.host.dev_tool.DevTool` and
{class}`~otto.host.toolchain.ToolchainTool`.

Tooling is not product: a debug probe on a board is not software under test, so
it lives in its own list and is never part of `is_installed()`'s answer. Two
seams, because the two kinds of tooling are owned differently:

| Kind | Declared | Owned by | Installed by |
|------|----------|----------|--------------|
| **Dev tool** | in `settings.toml` (`[[dev_tools]]`, {doc}`../../../configuration/declared-products-tools`) or in code, via a provider | the repo that declared/registered it | `install-tools` (on by default) |
| **Toolchain tool** | in lab data, per host | the host — shared by every repo | `install-tools --toolchain` |

| Method | Behavior |
|--------|----------|
| `await host.install_tools(dev=True, toolchain=False)` | Dispatcher over the two below. |
| `await host.install_dev_tools(owner=None)` | Stage then install each dev tool, in declaration order (first failure wins). `owner` narrows the walk to one repo's tools. |
| `await host.uninstall_dev_tools(owner=None)` | Remove each dev tool (best-effort, first failure returned), narrowed by `owner`. |
| `await host.install_toolchain_tools()` | Put each declared tool, rename it to its declared `name`, `chown` it to its declared `user`. |
| `await host.remove_toolchain_tools()` | Remove each declared tool (best-effort). |
| `await host.toolchain_tools_absent()` | True iff none of them is present — the host-wide half of `is_clean()`. |

The asymmetric defaults are deliberate: dev tools are small and wanted on
nearly every run, while toolchain artifacts are large and rarely needed, so
asking for them is a decision.
