# Safety rules

Two refusals are enforced on **every** resolved placement, in both endpoint
and in-path mode, and apply regardless of `--expire`. Neither is evaluated
under [`--dry-run`](index.md#previewing---dry-run) — both need a live address table:

- **Management-interface refusal.** otto refuses to impair the interface it
  reaches a host *through* — resolved live by matching the host's
  management `ip` against the placement's netdev. This covers the in-path
  case too: if a middlebox's facing interface toward an endpoint happens to
  also be its own management interface, that placement is refused. Without
  this, impairing a link could sever otto's own path to the host it just
  impaired. The same refusal also covers *transit*: a placement is refused
  when its netdev carries the hop/management path of any **other** host that
  reaches otto only by hopping through the placement host (its `hop` chain,
  transitively) — degrading that netdev would lock otto out of the dependent
  host one indirection away.
- **Local-host refusal.** A link with the **local host** as either endpoint
  is never impairable, in any placement mode — the local host's
  connectivity to the bed IS otto's own management path, so degrading it
  (even indirectly, at a middlebox) degrades otto itself.

Both refusals raise before any host is mutated and are reported as a plain
error (CLI exit code 1).

**Elevation.** `tc qdisc` needs root. `impair`/`repair` mutations run
through the placement host's elevation mechanism (`sudo` unless the
connected user is already `root`); reads (`list`, and the pre-mutation
current-state check) need no privilege. A placement host with no elevation
configured fails loud, naming the host, rather than silently no-opping.

