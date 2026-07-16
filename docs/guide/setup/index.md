# Project setup

Everything otto knows about your project starts in two files: a
`.otto/settings.toml` at the repository root, and one or more `lab.json`
files describing the hosts otto can reach. `otto init` scaffolds both and
doctors an existing setup:

```console
$ otto init
```

The pages below cover the settings file and project discovery
({doc}`repo-setup`), defining hosts and links ({doc}`lab-config`), plugging
in a host source other than `lab.json` files ({doc}`host-database`), and
generating editor autocomplete schemas for the files you edit by hand
({doc}`editor-schemas`).

```{toctree}
repo-setup
lab-config
host-database
editor-schemas
```
