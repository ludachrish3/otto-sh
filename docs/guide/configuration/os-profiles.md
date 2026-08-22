# OS profiles
The `os_type` field in a `lab.json` entry is a *selector* that resolves to an
`OsProfile`.  A profile names a registered host class (its `base`) and carries
an optional bundle of raw field defaults merged beneath each host's own fields.
This lets many hosts that share a characteristic bundle — a particular Zephyr
build's `command_frame`, `filesystem`, and `max_filename_len` — name that
bundle once instead of copy-pasting it into every entry.

Built-in profiles registered at startup:

| `os_type` | Host class | Notes |
|----------|------------|-------|
| `unix` | `UnixHost` | Default when `os_type` is absent. |
| `embedded` | `EmbeddedHost` | OS-agnostic bare-metal/RTOS.  Fails loud without a `command_frame`. |
| `zephyr` | `ZephyrHost` | Concrete Zephyr subclass; supplies `ZephyrFrame` and `os_name: "Zephyr"`. |

Profiles are authorable two ways, both feeding the same registry:

- **Data** — an `[os_profiles.<name>]` table in `.otto/settings.toml`,
  registered at settings-parse time.
- **Code** — `register_os_profile()` called from an init module listed in
  `settings.toml`, registered after settings parse.  A code registration
  overrides a data table of the same name (last writer wins).
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
    "ip": "192.0.2.1",
    "element": "sprout",
    "os_type": "zephyr-3.7-fat32",
    "hop": "basil_seed",
    "labs": ["embedded"]
}
```

Unknown `base` values and unknown default field names raise `ValueError` at
startup so typos fail loudly instead of silently no-opping.
Registering a *code* profile — a new host class, or a subclass of one otto
ships — is a Python author's job; see {doc}`../../library/custom-host-classes`.
