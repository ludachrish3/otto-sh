# Lab configuration

A *lab* is a named set of hosts (and the routes between them), described in
one or more `lab.json` files.  A lab is **declared**: it exists only once
some source lists it in the top-level `labs` table, which is also where what
belongs to the lab as a whole — its reservable `resources`, its `metadata` —
is written.

Hosts are not listed loose.  Each one belongs to an **element**: one piece of
lab equipment (a box, a chassis, a VM).  The element is the smallest unit
that joins a lab — every host of an element is in every lab the element is
in — and it joins by **pattern**, so one element can serve a whole family of
labs named by a scheme.  A host entry is one addressable portion of its
element: a board, a console, a management interface.

This page starts with a minimal lab and lists the reference pages, one per
area, that cover all four levels: the `labs` table, the element entry, the
host entry, and the declared link.  For project-level
settings (paths, libs, init modules) see {doc}`settings`; for how a project
declares the sources it reads lab data from, see {doc}`host-sources`.

## A minimal lab

One lab, one element, one host:

```json
{
    "labs": {"bench": {}},
    "elements": [
        {
            "name": "devbox",
            "labs": ["bench"],
            "hosts": [
                {
                    "ip": "10.0.0.5",
                    "os_type": "unix",
                    "creds": [{"login": "dev", "password": "secret"}]
                }
            ]
        }
    ]
}
```

The `labs` table declares the lab `bench`; the element `devbox` joins it, and
its one host takes the element's name as its id. `otto --lab bench
--list-hosts` lists `devbox`. Everything else is optional and documented on
the pages below.

## The pages

```{toctree}
:maxdepth: 1

lab/files-and-membership
lab/elements
lab/host-fields
lab/inventory-references
lab/host-identity
lab/network-interfaces
lab/power
lab/snmp
lab/hops-and-embedded
lab/coverage-toolchain
lab/toolchain-tools
lab/links
lab/docker
lab/host-preferences
lab/project-scope
lab/migrating
```

- {doc}`lab/files-and-membership`: where lab data lives, splitting a lab
  across files, and the `labs` table that declares a lab.
- {doc}`lab/elements`: the element entry and how it joins labs by pattern.
- {doc}`lab/host-fields`: every per-host field, the host type, and option
  tables.
- {doc}`lab/inventory-references`: pointing a host entry at an inventory
  record.
- {doc}`lab/host-identity`: how a host's id is formed and kept unique.
- {doc}`lab/network-interfaces`: a host's named interfaces.
- {doc}`lab/power`: power control.
- {doc}`lab/snmp`: SNMP monitoring.
- {doc}`lab/hops-and-embedded`: reaching a host through another, and the
  embedded-only fields.
- {doc}`lab/coverage-toolchain`: the per-host coverage toolchain.
- {doc}`lab/toolchain-tools`: declaring toolchain tools in lab data.
- {doc}`lab/links`: declared links between hosts.
- {doc}`lab/docker`: docker-capable hosts.
- {doc}`lab/host-preferences`: a project's per-host preferences for terms,
  transfers and options.
- {doc}`lab/project-scope`: the labs and hosts a project targets.
- {doc}`lab/migrating`: moving a lab from the old `hosts` array.

## Example

Two elements — one Unix host, one Zephyr host — with the `labs` entries that
declare their labs:

```json
{
    "labs": {
        "unix": {
            "resources": ["test1", "test2", "test3"],
            "metadata": {"description": "unix regression bed"}
        },
        "busybox": {"resources": ["test1", "bb1161"]},
        "embedded": {"resources": ["test4", "zephyr37_fat"]}
    },
    "elements": [
        {
            "name": "test1",
            "labs": ["unix", "busybox"],
            "resources": ["test1-chassis"],
            "metadata": {"role": "hub"},
            "hosts": [
                {
                    "ip": "10.10.200.11",
                    "os_type": "unix",
                    "valid_terms": ["ssh", "telnet"],
                    "valid_transfers": ["scp", "sftp", "ftp", "nc"],
                    "is_virtual": true,
                    "docker_capable": true,
                    "creds": [
                        {"login": "vagrant", "password": "vagrant"},
                        {"login": "test", "password": "Password1"}
                    ],
                    "interfaces": {
                        "eth2": {"ip": "192.168.1.11", "subnet": "192.168.1.0/24"},
                        "bbeth-1350": {"ip": "198.51.100.18", "subnet": "198.51.100.16/30"}
                    }
                }
            ]
        },
        {
            "name": "zephyr37_fat",
            "labs": ["embedded"],
            "hosts": [
                {
                    "ip": "192.0.2.1",
                    "os_type": "zephyr",
                    "os_version": "3.7",
                    "valid_transfers": ["console"],
                    "filesystem": "fat-ram",
                    "max_filename_len": 32,
                    "is_virtual": true,
                    "hop": "test4",
                    "snmp": {
                        "address": "10.10.200.14",
                        "port": 16101,
                        "community": "public",
                        "oids": [
                            "1.3.6.1.2.1.1.3.0",
                            "1.3.6.1.4.1.63245.1.1.0",
                            "1.3.6.1.4.1.63245.1.2.0",
                            "1.3.6.1.4.1.63245.1.3.0",
                            "1.3.6.1.4.1.63245.1.4.0"
                        ]
                    }
                }
            ]
        }
    ],
    "links": []
}
```

`test1` is in two labs because its *element* is: every host it holds joins
`unix` and `busybox` together.  Note that the reservation identifier `test1` is
declared on both labs, so reserving either contends with the other — the
overlap rule the [labs table](lab/files-and-membership.md#the-labs-table) describes.  The element's own
`test1-chassis` is a second, independent lock: a run that touches this host
needs it whichever lab it came in through.  The Zephyr host's `hop` names
`test4`, an element not shown here: a management path to reach the device, not
a declared link.
