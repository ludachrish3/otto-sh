# Configuration

A project is configured in two files, and `otto init` scaffolds both.
`.otto/settings.toml`, at the repository root, says what the project is — its
tests, products, coverage and labs' sources ({doc}`settings`). `lab.json` says
what the lab is — its hosts, links and monitoring ({doc}`lab-config`); hosts can
instead come from an inventory ({doc}`inventory`). Everything else here refines
one of those two.

```{toctree}
settings
lab-config
host-options
os-profiles
declared-products-tools
host-sources
inventory
```
