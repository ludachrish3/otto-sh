<!-- GENERATED FILE -- do not edit by hand.
     scripts/render_netcat_compatibility.py renders this from BED_NETCAT in
     tests/conformance/_nc_rungs.py. Run `make netcat-compatibility` to
     regenerate it; tests/unit/docs/test_netcat_compatibility_page.py fails when
     it is stale. -->

# Netcat compatibility

Which netcat to put in `nc_options.exec_name`, and which port and listener
strategies work with it. Everything below comes from the test bed. The
strategies themselves, and how to pin one, are on {doc}`netcat`.

## What "works" means

A listener check **works** with a netcat when otto moved a file with that
check pinned, in the test bed's gate lane. **Tool absent** means the host
does not have the tool the check runs, so pinning it fails. **Does not
work** means the tool is there and the check fails with that netcat; the
cell gives the reason and the issue that tracks it. A port strategy that a
host can run was likewise used to find a port and move a file.

A direct get runs neither ladder. The host sends the file to otto, so there
is no port to find and no listener to check. The listener columns therefore
describe a put, and a get through a hop. The sender form of a direct get
(`<exec_name> <ip> <port> < file`) was run only with
netcat-openbsd. No bed host runs any other netcat on a direct route.
The other netcats were not tried in this form on the bed.
That does not mean they fail.

Each row names the route it was run on. `direct` is a host otto reaches
itself; `tunnelled` is a host behind a hop.

## The bed hosts

The host names below are the test bed's own. Match your host by its netcat
implementation and its userland, not by a bed host's name.

| Bed hosts | What they are |
| --- | --- |
| `test1`, `test2`, `test3`, `test4` | Ubuntu lab VM |
| `bb1161` | BusyBox 1.16.1 guest |
| `bb1211` | BusyBox 1.21.1 guest |
| `bb1281` | BusyBox 1.28.1 guest |
| `bb1310` | BusyBox 1.31.0 guest |
| `bb1350` | BusyBox 1.35.0 guest |
| `ubu2404` | Ubuntu 24.04 guest |

## Netcat implementations

Set `nc_options.exec_name` to the name in the second column. A row is shared
by every bed host listed in it.

| Netcat | `exec_name` | Bed hosts | Route | `ss` | `netstat` | `proc` |
| --- | --- | --- | --- | --- | --- | --- |
| netcat-openbsd | `nc` | `test1`, `test2`, `test3`, `test4` | direct | works | works | works |
| BusyBox 1.16.1 applet | `nc` | `bb1161` | tunnelled | tool absent | works | works |
| BusyBox 1.21.1 applet | `nc` | `bb1211` | tunnelled | tool absent | works | works |
| BusyBox 1.28.1 applet | `nc` | `bb1281` | tunnelled | tool absent | works | works |
| BusyBox 1.31.0 applet | `nc` | `bb1310` | tunnelled | tool absent | works | works |
| BusyBox 1.35.0 applet | `nc` | `bb1350` | tunnelled | tool absent | works | works |
| netcat-openbsd | `nc.openbsd` | `ubu2404` | tunnelled | works | works | works |
| netcat-traditional | `nc.traditional` | `ubu2404` | tunnelled | works | works | works |
| ncat (Nmap) | `ncat` | `ubu2404` | tunnelled | works | works | works |

The `listener_check` option pins one of these checks.

## Port strategies

A port strategy never runs the netcat binary, so it depends on the host and
not on the implementation. `port_strategy` pins one. "Not available" means the
host does not have the tool the strategy runs.

| Bed hosts | `ss` | `netstat` | `python` | `proc` |
| --- | --- | --- | --- | --- |
| `test1`, `test2`, `test3`, `test4`, `ubu2404` | works | works | works | works |
| `bb1161`, `bb1211`, `bb1281`, `bb1310`, `bb1350` | not available | works | not available | works |
