(precedence)=

# Where a host's values come from

otto builds each host from several layers of configuration. This page is the
order they are applied in; each layer's own rules live on the page it links.
`otto -l NAME lab explain HOST_ID` shows, for one host, which layer each value
came from ([Seeing it for one host](#seeing-it-for-one-host)).

## The layers

From lowest to highest, a higher layer's value replacing a lower one's:

1. **The credentials store**: the `[creds]` store, or an inventory's own
   `creds` sub-table ({ref}`credentials-layered`). It holds credentials only.
2. **The OS profile**: the defaults of the host's `os_type` profile
   ({doc}`os-profiles`). A profile fills only the fields nothing above it
   states, and its `term`, `transfer` or `impairer` default yields to a list
   the entry writes ({ref}`the selection rule <selection-rule>`).
3. **The inventory record**: the machine facts of a host entry that
   references an inventory, and the credentials the record states
   ({doc}`inventory`).
4. **The lab file or the template**: the host entry itself, written in a
   `lab.json` ({doc}`lab-config`) or rendered by a Jsonnet template
   ({doc}`lab/templating`). The two are one layer: a rendered entry is read
   exactly as a written one.
5. **`[host_preferences]`**: a project's per-host option values and its
   term, transfer and impairer preferences ({doc}`lab/host-preferences`).
6. **The command line**: a flag of the command being run, such as `--term`
   or `--transfer`.

The layers do not all state every value. Three rules decide which ones do:

- **A field the inventory supplies comes from the record alone.** A host
  entry that references an inventory may not state a field that inventory
  supplies; the load fails if it does
  ([Two layers, one rule](inventory.md#two-layers-one-rule)). Every other
  field is the entry's. Apart from credentials, the record and the entry
  therefore never state the same field.
- **Credentials compose field by field.** For each login (and its
  `protocols` scope), the record's credentials override the store's, field by
  field, and the entry's override the record's: lab file over record over
  store ([Three files, one key, one order](inventory.md#three-files-one-key-one-order)).
- **Option tables merge key by key**, from the profile's defaults through the
  entry's table to `[host_preferences]`. `[host_preferences]` picks the term
  and transfer from the entry's `valid_terms` and `valid_transfers`, and the
  command line's `--term` and `--transfer` have the last word
  ({doc}`lab/host-preferences`).

Lab sources are not layers. When two sources hold the same element, each
source builds its own hosts from these layers first, and the later source's
element then replaces the earlier one whole
([Ordering and overrides](host-sources.md#ordering-and-overrides)).

## Seeing it for one host

`otto -l NAME lab explain HOST_ID` builds the lab exactly as a run builds it
and prints one row per value: the field, the layer it came from and which
part of that layer
([`otto lab explain`](../cli/lab/index.md#otto-lab-explain)). For a host of
the {ref}`advanced templated-lab example <lab-templating-advanced>`:

```{literalinclude} ../examples/templating/output/explain.txt
:language: text
:end-before: "host: east-dut0"
```

Two things about it:

- **It takes its lab from the global `-l`**, written before `lab`, as every
  other command does.
- **It presents no reservation gate.** It touches no host, so it works on a
  lab someone else has reserved.
