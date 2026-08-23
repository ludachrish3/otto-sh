# Tunnel identity & discovery

## Tunnel identity

Every tunnel gets an id of the form `tun-<hex>-<port>` — `--hosts
test1@eth2,test2 --port 6001` yields `tun-48d9158aca92-6001`. The port stays
visible in `list`, in `remove <id>`, and in every tagged process's `argv[0]`,
so two tunnels on the same route with different ports are visibly distinct.
Ownership is **not** encoded in the id: `remove --all` reaps every otto tunnel
it finds, whoever created it.

For how the id is derived (a hash of the ordered chain), why the path is
deliberately not normalized, why `--dest` is excluded, and why tunnel ids never
collide with declared `lab.json` link handles, see
{doc}`../../../architecture/subsystems/network`.

## Live discovery

`otto tunnel list` finds tunnels by scanning live processes: a portable `ps` on
every `has_bash` host plus a pure parser, with each tagged process's `argv[0]`
self-describing the whole tunnel so any single survivor reconstructs it — which
is why discovery survives every other chain host being down. The design — and
how it reuses the monitor's `(command, parser)` parser shape — is covered in
{doc}`../../../architecture/subsystems/network`; see also
[Custom parsers](../../../library/custom-parsers.md#custom-parsers) in {doc}`../monitor/index` for the
parser contract it is shaped to plug into. Tunnels appear live in the
monitor's topology view, riding the links their path traverses — see
[Topology view](../monitor/dashboard.md#topology-view) in {doc}`../monitor/index`.

