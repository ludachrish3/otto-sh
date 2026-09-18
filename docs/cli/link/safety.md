# Safety rules

Three refusals guard `impair` against degrading the path otto reaches the bed
through. The first two are enforced on **every** resolved placement, in both
endpoint and in-path mode, and apply regardless of `--expire`. Neither is
evaluated under [`--dry-run`](index.md#previewing---dry-run) — both need a live
address table:

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

The third needs no device at all, and so is the one refusal a dry run *can*
evaluate:

- **Incomplete-lab-scope refusal.** otto refuses to impair a link whose
  endpoints the loaded lab does not all contain. The two refusals above read
  the loaded lab to work out who depends on a placement, so a `--lab` scope
  that hides a dependent makes them report "safe" from an incomplete view
  rather than from the bed — the same wire, judged differently depending on
  how the command was scoped. Load a lab containing every endpoint and impair
  from there.

Every refusal raises before any host is mutated and is reported as a plain
error (CLI exit code 1).

## Clearing is not creating

`repair` does **not** enforce the management-interface or hop-transit
refusals. Those exist to stop otto *degrading* a path; clearing a qdisc cannot
degrade anything, so enforcing them on a clear would only ever protect an
impairment from the operator trying to remove it.

This matters beyond tidying up after a refusal. Both are evaluated when the
command runs, while lab data changes underneath them: impair a netdev
legitimately today, declare a host that hops through it tomorrow, and that
netdev is now hop transit — at which point otto would refuse to clear its own
live impairment.

The local-host refusal still applies to `repair` (otto does not run `tc` on
its own machine), as does the foreign-qdisc refusal — a root qdisc otto did
not create is not otto's to delete.

**Elevation.** `tc qdisc` needs root. `impair`/`repair` mutations run
through the placement host's elevation mechanism (`sudo` unless the
connected user is already `root`); reads (`list`, and the pre-mutation
current-state check) need no privilege. A placement host with no elevation
configured fails loud, naming the host, rather than silently no-opping.

