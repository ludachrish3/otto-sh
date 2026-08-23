# Link & tunnel APIs

Registering your own impairers and carriers, and driving links and
tunnels from Python. For the commands, see {doc}`../guide/cli/link/index`
and {doc}`../guide/cli/tunnel/index`.

## Custom link impairers

Impairment is pluggable the same way term/transfer backends are — see
{doc}`extending-backends` for the shared registration philosophy. A
`LinkImpairer` builds the shell commands for one placement's impairment:

```python
class LinkImpairer:
    host_families: ClassVar[frozenset[str]] = frozenset()

    def apply_command(self, netdev: str, params: ImpairmentParams) -> str: ...
    def read_command(self, netdev: str) -> str: ...
    def clear_command(self, netdev: str) -> str: ...
    def parse_read(self, output: str) -> ImpairmentParams | None: ...
```

`otto.link.netem.NetEmImpairer` (`host_families = {"unix"}`) is the only
first-party registrant, built on `tc`/netem. A custom impairer registers
from an `init` module, before any lab data loads:

```python
# .otto/init.py — registered via [init] in .otto/settings.toml
from typing import ClassVar

from otto.link import ImpairmentParams, LinkImpairer, register_impairer


class MyImpairer(LinkImpairer):
    host_families: ClassVar[frozenset[str]] = frozenset({"unix"})

    def apply_command(self, netdev: str, params: ImpairmentParams) -> str:
        return f"my-shaper set {netdev} {params.describe()}"

    def read_command(self, netdev: str) -> str:
        return f"my-shaper show {netdev}"

    def clear_command(self, netdev: str) -> str:
        return f"my-shaper clear {netdev}"

    # parse `my-shaper show` output back into ImpairmentParams
    def parse_read(self, output: str) -> ImpairmentParams | None: ...


register_impairer("my_impairer", MyImpairer)
```

`host_families` is mandatory and non-empty — an impairer that could never
apply to any host family is rejected at registration time. Impairment is a
**Unix-host** capability today (the `impairer` field lives on the Unix host
spec; there is no embedded impairer analog).

Which impairer a placement host actually uses is resolved the same way as
`term`/`transfer`:

- The host's `valid_impairers` menu (defaults to `["netem"]`) gates what's
  selectable, the same as `valid_terms`/`valid_transfers`.
- The host's `impairer` field pins a specific selection from that menu.
- `[host_preferences]` in `.otto/settings.toml` can override the pin with an
  ordered preference list under the `impairer` key, product-wins-over-lab
  like `term`/`transfer` (see {ref}`host-preferences` in {doc}`../guide/configuration/lab-config`):

  ```toml
  [host_preferences.".*"]
  impairer = ["my_impairer"]
  ```

- Resolution happens **per placement host** at impair time — in endpoint
  mode the link's two endpoints may legitimately resolve to different
  impairers.


## Custom impairers and `--port`

The scoped surface — `supports_selectors` plus the `scoped_*` command
builders and the scoped parser — is **optional** on a `LinkImpairer` (see
[Custom impairers](#custom-link-impairers)) and defaults off, so an
existing third-party impairer is unaffected by this feature. A `--port`
request routed to a host whose impairer doesn't declare
`supports_selectors = True` is a loud capability error naming the impairer
and the host — never a silent fallback to whole-link impairment.

```{note}
**Tunnels are out of scope.** Port-scoped impairment is a link-only
feature: it operates on `otto.link` placements exclusively, and
`otto.tunnel` is untouched by it — there is no "impair a tunnel" surface.
Impairing a link that tunnel traffic happens to ride
remains possible exactly as it is today (`tc` cannot know what a port
belongs to), with no added coupling between the two packages.
```


## The link Python API

`otto link impair`/`repair`/`list` are thin renderers over four functions in
`otto.link` — the single API the CLI, the monitor's topology overlay, and
any direct importer all call exactly the same way:

```python
from otto.link import ImpairmentParams, impair_link, read_link_states, repair_link

report = await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0))
for applied in report.applied:
    print(applied.placement.host_id, applied.placement.netdev, applied.params.describe())

states = await read_link_states(lab)  # every link's current impairment, list's feed

await repair_link(lab, "edge")
```

`selector` is the same optional keyword on both mutators — pass a `Selector`
to route through the port-scoped path instead of the whole-interface one;
omitted (the default), behavior is unchanged:

```python
from otto.link import Selector, impair_link, repair_link

report = await impair_link(
    lab, "edge", ImpairmentParams(delay_ms=200.0), selector=Selector(5201, "tcp")
)
await repair_link(lab, "edge", selector=Selector(5201, "tcp"))
```

`read_link_states`'s result shape follows: each `LinkState.by_direction`
value is a `DirectionState` (`whole: ImpairmentParams | None`, `scoped: dict[Selector,
ImpairmentParams]`, `foreign: bool` — at most one of `whole`/`scoped` is
ever populated, since the two are exclusive per placement) or `None` when
that direction's host couldn't be read this pass. When `impairable` is
`False`, `by_direction` is empty and `refusal: str | None` carries the reason
— the string `list` prints — covering both the structural refusals and the
live ones.

`find_link`, `repair_all`, and the
`ImpairReport`/`RepairReport`/`LinkState`/`Selector`/`DirectionState`/`ScopedState`
result types round out the surface. Nothing in this layer prints or knows
about exit codes — see the {doc}`API reference <../../api/link>` for full
signatures.

## Custom tunnel carriers

Tunnel transport is pluggable the same way link impairment is — see
{doc}`extending-backends` for the shared registration philosophy. A
`TunnelCarrier` builds the argv for one tagged process's role:

```python
class TunnelCarrier:
    supported_protocols: ClassVar[frozenset[str]] = frozenset()
    requirements_command: ClassVar[str] = ""
    tools_description: ClassVar[str] = ""

    def ingress_args(self, protocol, service_port, bind_ip, next_ip, carrier_port) -> list[str]: ...
    def relay_args(self, carrier_port, next_ip) -> list[str]: ...
    def egress_args(self, protocol, service_port, deliver_ip, carrier_port) -> list[str]: ...
```

`otto.tunnel.socat.SocatCarrier` (`supported_protocols = {"tcp", "udp"}`) is
the only first-party registrant, built on `socat`. A custom carrier
registers from an `init` module, before any lab data loads:

```python
# .otto/init.py — registered via [init] in .otto/settings.toml
from typing import ClassVar

from otto.tunnel import TunnelCarrier, register_carrier


class MyCarrier(TunnelCarrier):
    supported_protocols: ClassVar[frozenset[str]] = frozenset({"tcp"})
    requirements_command: ClassVar[str] = "command -v my-tool >/dev/null 2>&1 && echo ok || echo no"
    tools_description: ClassVar[str] = "my-tool"

    def ingress_args(self, protocol, service_port, bind_ip, next_ip, carrier_port):
        return ["my-tool", "listen", f"{bind_ip}:{service_port}", f"{next_ip}:{carrier_port}"]

    def relay_args(self, carrier_port, next_ip):
        return ["my-tool", "relay", str(carrier_port), next_ip]

    def egress_args(self, protocol, service_port, deliver_ip, carrier_port):
        return ["my-tool", "deliver", str(carrier_port), f"{deliver_ip}:{service_port}"]


register_carrier("my_carrier", MyCarrier)
```

`supported_protocols` is mandatory and non-empty — a carrier that could
never validate any tunnel is rejected at registration time. Select it per
tunnel with `otto tunnel add --carrier my_carrier`; the carrier applies
chain-wide (every hop in that one tunnel), and the choice isn't part of a
tunnel's identity or wire format — `remove` reaps a tunnel's processes by
pid regardless of which carrier built them.


## The tunnel Python API

Every command above has a callable counterpart in `otto.tunnel` — see the
{doc}`API reference <../../api/tunnel>` for full signatures:

```python
from otto.cli.run import instruction
from otto.config import get_lab
from otto.tunnel import add_tunnel, discover_tunnels, remove_tunnel


@instruction()
async def add_multi_hop_tunnel():
    lab = get_lab()
    added = await add_tunnel(lab, [("test1", None), ("test3", None), ("test2", None)], port=6001)
    live = await discover_tunnels(lab)  # every tunnel found right now
    report = await remove_tunnel(lab, added.tunnel.id)
```
