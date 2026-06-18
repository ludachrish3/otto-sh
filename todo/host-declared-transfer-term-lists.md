# Host-declared `term` / `transfer` lists (completion narrowing)

Far-future feature. Today the `otto host --term` / `--transfer` completers
offer *every* registered backend (filtered only by host family for transfer).
A host could instead declare which backends it actually supports, and the
completer would narrow to *that host's* declared set.

The WS#4 completers already receive `ctx`, so this is a pure addition — no
redesign. The completer would read the already-typed `host_id` from
`ctx.params` and look up that host's declared `term` / `transfer` list,
falling back to the full registry set when the host hasn't declared one.

What it needs:

- A `hosts.json` schema addition: optional `supported_terms` /
  `supported_transfers` lists on `UnixHostSpec`
  (`src/otto/models/host.py`).
- Per-host completion data cached by host id (the way host IDs are cached
  today in `completion_cache.py`), so the narrowing works on the fast path.

Gated on a concrete need — defer until a host with a genuinely restricted
backend set makes the broad completion list a real footgun.
