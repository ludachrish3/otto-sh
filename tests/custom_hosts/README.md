# `custom_hosts` — a shared, third-party-style otto extension module

This directory emulates an **out-of-tree package** that projects depend on for
extra otto host/shell capabilities — the kind of thing a vendor or a shared
internal library would ship. It is deliberately *not* part of otto core and
*not* owned by any single project.

It currently provides one custom command frame:

- **`zephyr-inline`** (`ZephyrInlineRetcodeFrame`) — the Zephyr 2.7 shell
  dialect that reads exit codes from an inline `retCode = <n>` line (emitted by
  `tests/firmware/zephyr/patches/v2_7-shell-retcode.patch`) instead of the
  `retval` builtin that 2.7 lacks.

## Why it's shared (not per-project)

The 2.7 host `zephyr27_fat` lives in the shared `embedded` lab
(`tests/lab_data/tech1/lab.json`), so *any* project that loads `--lab embedded`
must be able to construct it — which means the `zephyr-inline` frame must be
registered. Owning the frame here, and having each consuming project depend on it,
keeps it a single definition (no per-project copies, no absorbing a test-only
dialect into otto core). It also keeps the `register_command_frame` extension
point exercised by a realistic "external dependency" rather than by one project's
private code.

## How a project consumes it

In the project's `.otto/settings.toml`:

```toml
libs = [
    "../custom_hosts",   # put this dir on PYTHONPATH
]
init = [
    "custom_hosts",                # import at config load -> registers the frames
]
```

otto adds `libs` to `sys.path` and imports the `init` modules at config load
(before any lab is built), so the frame is registered by the time a lab host
references it by name.
