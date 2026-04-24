# Repository Setup

Otto discovers your project through a `.otto/settings.toml` file at the
repository root.  This page explains every setting and what happens during
project initialization.

## The settings file

Create `.otto/settings.toml` in your repo root:

```toml
name = "my_project"
version = "1.0.0"

labs  = ["${sutDir}/../lab_data"]
libs  = ["${sutDir}/pylib"]
tests = ["${sutDir}/tests"]
init  = ["my_instructions", "my_shared_options"]
```

### Variable expansion

`${sutDir}` is replaced with the absolute path to the repo root at load
time.  Use it to keep paths relative and portable.

### Field reference

name
: **Required.** Product or repository name.  Displayed in CLI panels and log
  output.

version
: **Required.** Semantic version string (e.g. `"1.0.0"`).

labs
: List of directory paths to search for lab JSON files.  When you pass
  `--lab my_lab`, otto looks in these directories for a file matching that
  name.  Defaults to `[]`.

libs
: List of Python package directories to add to `sys.path` at startup.
  This is where you put your instruction modules, shared options, and helper
  libraries.  Defaults to `[]`.

tests
: List of directories to scan for `test_*.py` files.  Each matching file
  is imported at startup, which triggers `@register_suite()` decorators and
  makes suites available as `otto test` subcommands.  Defaults to `[]`.

init
: List of Python module names (dot-separated) to import at startup.  Use
  this to register instructions (`@command()`) and shared option classes.
  These modules must be importable from one of the `libs` directories.
  Defaults to `[]`.

## What happens at startup

When you run any `otto` command, the following initialization sequence
occurs:

1. **Environment parsing** -- Otto reads `OTTO_SUT_DIRS` to find repo root
   directories.

2. **Repo discovery** -- For each path in `OTTO_SUT_DIRS`, otto creates a
   `Repo` object and reads its `.otto/settings.toml`.

3. **Apply settings** -- For each repo, otto:
   - Adds `libs` directories to `sys.path`
   - Imports modules listed in `init` (this registers instructions)
   - Auto-imports all `test_*.py` files from `tests` directories (this
     registers suites)

4. **Lab loading** -- Otto collects all `labs` search paths from every repo
   and loads the lab(s) specified by `--lab` or `OTTO_LAB`.  Multiple labs
   are merged, combining their hosts.

5. **Config module creation** -- The global `ConfigModule` is created with
   the loaded repos and lab, making hosts available to all commands.

## Multiple repos

Otto supports multiple repos simultaneously.  Set `OTTO_SUT_DIRS` to a
comma-separated list:

```bash
export OTTO_SUT_DIRS=/path/to/repo1,/path/to/repo2
```

Each repo has its own settings, libs, tests, and lab search paths.  They
are all merged at startup -- instructions and suites from every repo appear
in the CLI, and lab search paths from all repos are combined.

## Lab files

Labs are JSON files that describe a set of hosts and their topology.
Place them in one of your `labs` directories.

```json
{
    "name": "my_lab",
    "hosts": {
        "router1": {
            "ip": "192.168.1.1",
            "user": "admin",
            "password": "secret",
            "term": "ssh"
        }
    }
}
```

### Merging labs

Pass multiple lab names to combine them:

```bash
otto --lab lab_a,lab_b test TestDevice
```

Hosts from all labs are merged into a single lab.  If two labs define the
same host ID, the later lab's definition wins.

### Exploring labs

```bash
otto --lab my_lab --list-labs      # list all available lab names
otto --lab my_lab --list-hosts     # list host IDs in the loaded lab
otto --lab my_lab --show-lab       # full lab details (use -v for expanded output)
```
