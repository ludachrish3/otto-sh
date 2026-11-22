# Case study: otto's own test bed

The pages below define otto's own test bed — four Ubuntu VMs, five BusyBox
guests, seven Zephyr targets — as the checked-in project
`docs/examples/getting-started/`.

(bed-example-projects)=

## Getting the example projects

Download {download}`getting-started.tar.gz </_static/generated/examples/getting-started.tar.gz>`
and unpack it:

```bash
tar xzf getting-started.tar.gz
```

It holds two projects, side by side. `getting-started/` is the one these
pages define. {doc}`defining-hosts/inventory` uses the second,
`getting-started-inventory/`, which loads the first one's code from
`../getting-started/libs`, so keep the two beside each other. otto finds a
project through `OTTOPATH`
([Environment variables](../../cli/index.md#environment-variables)).

```{toctree}
:maxdepth: 1

defining-hosts/index
customizations
boards-of-interest
reservations
```
