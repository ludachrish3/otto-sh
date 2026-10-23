(host-preferences)=

# Product host preferences

Most products share a common set of connection conventions — a non-standard
SSH port, a longer connect timeout, an alternate `nc` binary, preferred
terminal or transfer backend.  Restating those values on every host entry is
repetitive and error-prone.  Move the shared values into
`[host_preferences]` in `.otto/settings.toml`.

> **Migration note:** `[host_defaults]` was removed; its option tables move
> under `[host_preferences."<selector>".<opt>]`.

The `[host_preferences]` block is a map whose keys are **Python regexes**
matched (`re.fullmatch`) against each host's **id** (e.g. `test1`,
`router-seed-2`).  Every seam of an id is `-`, so a prefix selector such as
`test1-.*` matches the hosts of `test1` **and** of any element whose name
begins `test1-`; anchor on the board (`test1-bb-.*`) or spell the full id when
that matters.  Under each selector, two kinds of values are allowed:

- **Selection lists** (`term`, `transfer`, `impairer`) — an ordered list of
  preferred backends.  Otto picks the first entry that is in the host's
  lab-defined `valid_terms` / `valid_transfers` / `valid_impairers` menu;
  out-of-menu entries are skipped.
- **Option tables** (`ssh_options`, `telnet_options`, `sftp_options`,
  `scp_options`, `ftp_options`, `nc_options`, `userland_options`) — per-key
  value overrides.

```toml
# .otto/settings.toml

# Selector = Python regex matched against host id (".*" = all hosts).
# Selections (term/transfer) are ordered preferences gated by each host's
# lab menu; option tables are per-key values that win over lab.json.
[host_preferences.".*"]
term = ["telnet"]
transfer = ["nc"]
ssh_options = { connect_timeout = 5.0, keepalive_interval = 30 }
telnet_options = { cols = 200, echo_negotiation_timeout = 1.0 }
nc_options = { exec_name = "ncat", port_strategy = "proc" }

# Narrower selectors overlay specific host groups.
[host_preferences."router.*"]
telnet_options = { port = 9023 }
```

Valid option-table names are: `ssh_options`, `telnet_options`,
`sftp_options`, `scp_options`, `ftp_options`, `nc_options`,
`userland_options`.  Unknown keys raise at startup so typos fail loudly
instead of silently no-opping.

**Precedence (lowest to highest):**

1. The hardcoded dataclass defaults in `otto.host.options`.
2. The host's own `*_options` table and `term`/`transfer` pin in
   `lab.json` (the `valid_*` menu hard-gates selections).
3. `[host_preferences]` from each project — product values **win over**
   `lab.json`.  Projects are applied in `OTTOPATH` order (later project
   wins); within a project, selectors are applied in definition order (later
   selector wins on the same key).
4. CLI `--term` / `--transfer` — final word, applied at invocation time.

Merging happens **per key** at every option-table layer.  Setting only
`port` on a host in `lab.json` still inherits `connect_timeout` from the
product preference, and so on down to the dataclass default.

The merge happens once, at host construction time.

For the full `*_options` field reference and per-field semantics, see
{doc}`../host-options`.
