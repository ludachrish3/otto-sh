# Configuration

A project is configured in two files, and `otto init` scaffolds both.
`.otto/settings.toml`, at the repository root, says what the project is — its
tests, products, coverage and labs' sources ({doc}`settings`). `lab.json` says
what the lab is — its hosts, links and monitoring ({doc}`lab-config`, one page
per area); hosts can instead come from an inventory ({doc}`inventory`), or be
rendered from one by a template ({doc}`lab/templating`), and {doc}`precedence`
says which layer wins when several state a host's value.
Everything else here refines one of those two.

```{toctree}
settings
lab-config
host-options
os-profiles
declared-products-tools
host-sources
inventory
precedence
```
