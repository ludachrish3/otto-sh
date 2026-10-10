# OS profiles
The `os_type` field in a `lab.json` entry is a *selector* that resolves to an
`OsProfile`.  A profile names a registered host class (its `base`) and carries
an optional bundle of raw field defaults merged beneath each host's own fields.
This lets many hosts that share a characteristic bundle — a particular Zephyr
build's `command_frame`, `filesystem`, and `max_filename_len` — name that
bundle once instead of copy-pasting it into every entry.

The built-in profiles — `unix`, `embedded` and `zephyr` are each their host
class's own profile; `busybox` names no class of its own:

| `os_type` | Host class | Console `login_prompt` / `password_prompt` | Notes |
|----------|------------|-----------------|-------|
| `unix` | `UnixHost` | `login: ?$` / `[Pp]assword: ?$` | Default when `os_type` is absent. |
| `busybox` | `UnixHost` | `login: ?$` / `[Pp]assword: ?$` | A BusyBox userland: `ash` framing, no bash, the `shell` transfer first. |
| `embedded` | `EmbeddedHost` | none | OS-agnostic bare-metal/RTOS.  Fails loud without a `command_frame`. |
| `zephyr` | `ZephyrHost` | none | Concrete Zephyr subclass; supplies `ZephyrFrame` and `os_name: "Zephyr"`. |

The prompt patterns are what the `console` term's login matches
({ref}`console-term`): regexes against the end of what the line shows. A
host's own `console_options.login_prompt` / `password_prompt` win over its
profile's. The two embedded profiles carry none because an RTOS shell has
no login. A profile of your own carries none either unless its code
registration passes them
({doc}`../cookbook/extending/custom-host-classes`) — a data profile cannot
— so a console host that logs in under such a profile needs them in its
`console_options`, or its first connect fails with `no login prompt
pattern`.

## Where a profile comes from

A profile can come from three places. Otto looks for the `os_type` name in
each, in this order, and the first that has it supplies the **whole**
profile:

1. **Code** — `register_os_profile()` called from an init module listed in
   `settings.toml` ({doc}`../cookbook/extending/custom-host-classes`).
2. **Data** — an `[os_profiles.<name>]` table in a selected repo's
   `.otto/settings.toml` (below). When two repos declare the same name, the
   later repo in `OTTO_SUT_DIRS` wins.
3. **The host class's own profile** — the one `register_host_class()` was
   given, or a built-in from the table above.

Fields never merge across these layers. A data table named `unix` replaces
the `unix` class's profile, console prompts included, so a console host
under it needs its prompts in `console_options`. A code profile wins over a
data table of the same name whichever is defined first, and a data table
named after a host class wins over that class's own profile even when the
class is registered after the table is read. A data table is not a
registration: it is read for the repos otto runs with, so a data profile
whose `base` is a host class an init module registers works.

## Data profiles

Add an `[os_profiles.<name>]` sub-table to `.otto/settings.toml`.  The only
required key is `base` — the name of a registered host class.  Every other key
is a raw field default merged beneath each matching host's own fields, exactly
as written — otto does not expand or anchor paths in them, so write any path
here absolute.  (`~` works only if the field's own consumer expands it; otto
does not do so on the way in.)

Example — a profile for a specific Zephyr 3.7 FAT build:

```toml
[os_profiles.zephyr-3.7-fat32]
base            = "zephyr"
os_version       = "3.7"
filesystem      = "fat-ram"
max_filename_len = 32
```

With this profile in place, a host entry only needs to name the profile:

```json
{
    "name": "zephyr37_fat",
    "labs": ["embedded"],
    "hosts": [
        {
            "ip": "192.0.2.1",
            "os_type": "zephyr-3.7-fat32",
            "hop": "test4"
        }
    ]
}
```

Settings parsing checks a table's shape: a `base` string and default
values. What it means is checked after every repo's `init` modules have run,
so `base` may name a host class one of them registers. An unknown `base` or
an unknown default field name is that repo's load error, named the way a
malformed settings file is (`[os_profiles.<name>] in repo '<repo>': ...`).
Otto treats it as it treats a repo whose `init` module failed to import:
the repo's `init` modules have already run, so the repo keeps its place in
the dependency order and its dependents still load, and the other repos keep
working; a run the broken repo is part of fails loudly, and one it is not
part of reports the error as a warning, so typos never silently no-op.
A repo the dependency pass skips never ran its `init` modules, so only its
tables' shape is checked: what they mean is not judged.
`otto init`'s doctor reports the same problems, but it does not run `init` modules,
so it also reports a table whose `base` is a host class only an `init` module
registers.

Registering a *code* profile — a new host class, or a subclass of one otto
ships — is a Python author's job; see {doc}`../cookbook/extending/custom-host-classes`.
