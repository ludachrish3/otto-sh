# Editor schemas (autocomplete for `lab.json` & `settings.toml`)

otto can generate [JSON Schema](https://json-schema.org/) for the files you edit
by hand — `lab.json`, `settings.toml`, and the reservations JSON — so your
editor offers field autocomplete and flags typos. The schemas are generated from
the pydantic models inside the otto you have installed, so they always match your
version. There is nothing to download and nothing that can go stale.

New repos get all of this automatically: `otto init` exports the schemas to
`.otto/schemas/`, stamps the scaffolded `settings.toml` (`#:schema` directive)
and `lab.json` (`$schema` key) so single files self-wire, and writes
`.vscode/settings.json` + `.vscode/extensions.json` when they don't already
exist (an existing file is never modified — add the snippets below by hand).
The `otto init` doctor also flags stale schemas after an upgrade. The manual
steps below are for existing repos or other editors.

## `otto schema --help`

```{raw} html
:file: ../../_static/generated/termynal/help-schema.html
```

## Generate the schemas

```bash
otto schema export
```

This defaults to `.otto/schemas/` (pass `--out` to write elsewhere) and writes:

| File | Describes |
| --- | --- |
| `lab.schema.json` | the whole `lab.json` object — its `hosts` array (any registered `os_type`) and its `links` array |
| `link.schema.json` | a single entry in the `links` array |
| `unix-host.schema.json`, `embedded-host.schema.json` | a single host of one type |
| `settings.schema.json` | `settings.toml` |
| `reservations.schema.json` | the reservations JSON file |
| `monitor-meta.schema.json` | the monitor dashboard's internal chart/tab-layout model — not a file you edit, and not served at any endpoint; it drives the generated TypeScript types the web dashboard builds against (`scripts/gen_web_types.sh`) |

Run it again after upgrading otto to pick up new fields. Custom host classes
registered via an init module in `.otto/settings.toml` are included
automatically — each gets its own `<type>-host.schema.json` and an entry in
`lab.schema.json`. Pass `--builtins-only` to emit just the built-in types
(`unix`, `embedded`, `zephyr`), excluding any custom ones.

## VS Code

`lab.json` and the reservations JSON are covered by the built-in JSON
language server. Add to your workspace `.vscode/settings.json`:

```json
{
  "json.schemas": [
    { "fileMatch": ["**/lab.json"], "url": "./.otto/schemas/lab.schema.json" },
    { "fileMatch": ["**/reservations.json"], "url": "./.otto/schemas/reservations.schema.json" }
  ]
}
```

For `settings.toml`, install the
[Even Better TOML](https://marketplace.visualstudio.com/items?itemName=tamasfe.even-better-toml)
extension and add:

```json
{
  "evenBetterToml.schema.associations": {
    ".*/settings\\.toml$": "./.otto/schemas/settings.schema.json"
  }
}
```

## Neovim

With the JSON language server (`jsonls`, from `vscode-json-languageserver`) via
`nvim-lspconfig`:

```lua
require('lspconfig').jsonls.setup({
  settings = {
    json = {
      schemas = {
        { fileMatch = { 'lab.json' }, url = './.otto/schemas/lab.schema.json' },
        { fileMatch = { 'reservations.json' }, url = './.otto/schemas/reservations.schema.json' },
      },
    },
  },
})
```

For `settings.toml`, the [taplo](https://taplo.tamasfe.dev/) language server
honours schema directives. Either add a directive at the top of the file:

```toml
#:schema ./schemas/settings.schema.json
```

This path is relative to `.otto/settings.toml` itself, not the repo root — so
`./schemas/` here means `.otto/schemas/`. (`otto init` stamps this line for
you.)

Or associate it in the taplo config (`.taplo.toml`):

```toml
[[rule]]
include = ["settings.toml"]
[rule.schema]
path = ".otto/schemas/settings.schema.json"
```

## Note on drift

The schemas reflect the otto version that generated them. There is no
committed copy in the otto repo — the `otto init` doctor flags a stale
`.otto/schemas/` after an upgrade; refresh with `otto init --schemas` or
`otto schema export`.
