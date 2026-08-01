# Settings path anchoring: one convention for every path in `settings.toml`

**Date:** 2026-08-01
**Status:** Design — approved in discussion, not yet planned

## 1. Problem

`.otto/settings.toml` has no single rule for what a relative path means. Three
different behaviours coexist, and which one applies depends on which subsystem
happens to read the field:

| Behaviour | Fields | Resolution site |
| --- | --- | --- |
| Anchored to the repo root | `[coverage.tiers] harvest_dirs`, `[reservations.json] path`, `[coverage.overrides] file` | `reporter.py:606`, `reservations/__init__.py:121`, `overrides.py:157` |
| Anchored to the process CWD | `labs`, `libs`, `tests`, `[docker]` `dockerfile`/`context`/`path` | nowhere — the raw value is used as-is |
| Anchored to `$HOME`, else CWD | `[monitor] tls_cert`, `tls_key` | `settings.py:133` (`expanduser` only) |

`${sut_dir}` — a literal `str.replace` applied over the whole settings dict
(`repo.py:777`, with independent copies at `coverage/tiers.py:38` and
`cli/init.py:80`) — papers over the difference. Where a field is already
repo-anchored the variable is redundant; where it is not, the variable is the
only thing making the path absolute, and users cannot tell the two apart.

The CWD-anchored group is a latent bug, not a design choice. `settings.toml` is
committed and shared across a team, so a CWD-relative value in it cannot mean
anything stable — it resolves differently depending on which directory `otto`
is invoked from. Worse, every consumer of that group skips silently on a miss:

- `repo.py:371` — `paths = [str(d) for d in self.tests if d.exists()]`
- `repo.py:729` — `if test_dir.is_dir()` in `iter_test_files`
- `repo.py:713` — `add_libs_to_pythonpath` appends the raw value to `sys.path`

So `tests = ["tests"]` today discovers zero tests and reports nothing.

## 2. Goals

1. One user-facing rule covering every path in `settings.toml`.
2. Make the rule structural — new path fields inherit it without anyone
   remembering to add a join.
3. Fix the CWD-anchored group (a real bug) without depending on the
   `${sut_dir}` decision.
4. Narrow `${sut_dir}` to the one job it is still uniquely needed for.

## 3. Non-goals

- Changing how `${sut_dir}` behaves in untyped passthrough tables (§6).
- `os.chdir`. The convention is a resolution-time join, never a process-wide
  directory change — otto currently never chdirs, and a chdir would also alter
  relative-path behaviour for user test code, subprocess launches, and docker
  build contexts.
- Env-var interpolation, or any expansion beyond `~`.

## 4. The convention (normative)

> Every path in `.otto/settings.toml` is `expanduser()`-expanded. If it is
> still relative afterwards, it resolves against the repo root — the directory
> containing `.otto/`. Absolute paths pass through unchanged.

Two anchors, distinguished by a character the user writes explicitly:

```toml
tests     = ["tests"]                  # <repo>/tests
libs      = ["../shared/pylib"]        # <repo>/../shared/pylib
tls_cert  = "~/.config/otto/tls/c.pem" # $HOME/.config/otto/tls/c.pem
path      = "/srv/otto/reservations.json"
```

In a multi-repo (`OTTO_SUT_DIRS`) run each `settings.toml` anchors to **its
own** repo root, which is what `${sut_dir}` already does. No change there, but
the docs must say it, since "act as though CWD is the repo root" could
otherwise be misread as "the first repo".

This preserves the `[monitor]` TLS convention established at
`settings.py:121-127` verbatim: the committed value points under `~` and
resolves per-user. `~` remains the opt-out; only the *fallback* changes, from
CWD to the repo root.

## 5. Scope: where the rule can be enforced

The settings file has two read paths, and this matters for implementation.

**Path A — the validated model.** `Repo.parse_settings` reads fields off
`SettingsModel` (`repo.py:609-635`).

**Path B — the raw dict.** Several subsystems re-read `repo.settings` (the
un-modelled `tomli` output) at runtime and do their own resolution. Their spec
classes are validation-only; `CoverageReportSpec` and `CoverageOverridesSpec`
say so in their docstrings.

| Field | Read path | Today | Action |
| --- | --- | --- | --- |
| `labs`, `libs`, `tests` | A | CWD | **anchor** |
| `[[docker.images]] dockerfile`, `context` | A | CWD | **anchor** |
| `[[docker.composes]] path` | A | CWD | **anchor** |
| `[monitor] tls_cert`, `tls_key` | A | `~`, else CWD | **anchor** (fallback only) |
| `[coverage.tiers] harvest_dirs` | B | repo | none — already correct (typed `Path`, but read raw) |
| `[reservations.json] path` | B | repo | none — already correct |
| `[coverage.overrides] file` | B | repo | none — already correct |
| `[host_preferences]` `ssh_options` values | B | passthrough | §6 |
| `[lab.<backend>]` kwargs | B | passthrough | §6 |
| `[reservations.<backend>]` kwargs | B | passthrough | §6 |

Path A gets the rule mechanically via §7. Path B's three anchored fields keep
their existing explicit joins — they already implement the convention, and
moving them would mean routing runtime reads through the model, which is a
larger change than this design warrants.

## 6. Untyped passthrough tables

Three tables forward values otto does not type:

- `[lab.<backend>]` → `cls(repo_dir=repo_dir, **extra_kwargs)`
  (`labs/__init__.py:86`); `LabConfigSpec` is `extra="allow"` because, per its
  docstring, "otto-core cannot type a third-party backend's kwargs".
- `[reservations.<backend>]` → `cls(url=url, **extra_kwargs)`
  (`reservations/__init__.py:128-130`).
- `[host_preferences]."<sel>".ssh_options` → `known_hosts: Any`
  (`options.py:88`), `client_keys: list[str]`, and an open
  `extra: dict[str, Any]` (`models/options.py:44`), all forwarded to asyncssh.

The convention **applies** to these as documentation, but otto cannot
**enforce** it: there is no field to attach a validator to, and a heuristic
over "strings that look like paths" is unacceptable — `ssh_options.extra`
carries arbitrary asyncssh kwargs, so `passphrase = "hunter2"` would become
`<repo>/hunter2`.

Therefore `${sut_dir}` is **retained, and documented as valid only here**. Its
role changes from "the way you write paths" to "the explicit escape hatch for
tables otto does not type". It also remains the only way to express a path
*embedded in a larger string*, which no amount of typing reaches:

```toml
[lab.sqlbackend]
db_url = "sqlite:///${sut_dir}/lab.db"
```

Two notes for the plan:

1. Custom **lab** backends receive `repo_dir=` and can self-anchor. Custom
   **reservation** backends do not (`reservations/__init__.py:128-130` passes
   only `url` and `**extra_kwargs`), so `${sut_dir}` is strictly necessary for
   them today. Passing `repo_dir=` there too would be a sensible follow-up but
   is a breaking change to the custom-backend constructor contract, so it is
   out of scope here.
2. No usage of the embedded-path form exists anywhere in the otto tree — every
   `${sut_dir}` occurrence is a whole-value path. The capability is retained on
   principle, not on observed demand.

## 7. `RepoPath`: making the rule a type

Rather than adding joins at each of the eight Path-A fields, declare the rule
once as an annotated type in `otto/models/settings.py`:

```python
def _anchor(v: Path, info: ValidationInfo) -> Path:
    v = v.expanduser()
    if v.is_absolute():
        return v
    sut_dir = (info.context or {}).get("sut_dir")
    return sut_dir / v if sut_dir is not None else v

RepoPath = Annotated[Path, AfterValidator(_anchor)]
```

`sut_dir` reaches the validator through pydantic's validation context, which
means one call-site change in `repo.py:610`:

```python
model = SettingsModel.model_validate(expanded, context={"sut_dir": self.sut_dir})
```

Then `labs`/`libs`/`tests`, the two `DockerImageSpec` fields,
`DockerComposeSpec.path`, and the two `MonitorSettingsSpec` fields change type
from `Path` to `RepoPath`. `MonitorSettingsSpec._expand_user` is deleted — the
annotation subsumes it.

Why a type rather than joins:

- New path fields inherit the convention by construction, not by memory.
- "Which fields are repo-anchored" becomes greppable and reviewable.
- The `~`-then-anchor sequence exists in exactly one place.

**Absent context.** When `sut_dir` is missing from the context the validator
leaves the value relative rather than raising, so `SettingsModel` stays
independently validatable (several unit tests construct it without a repo).
`Repo.parse_settings` is the only production caller and always supplies it.

**No `resolve()`.** The validator joins but does not resolve. Resolving would
collapse symlinks and change path identity for repos reached through symlinked
checkouts. `repo.py:509` resolves locally where it needs a containment check;
that stays a local concern.

## 8. Migration and compatibility

Phases 1-2 are effectively non-breaking. Expansion runs *before* validation
(`repo.py:609-610`), so a `${sut_dir}`-prefixed value is already absolute by
the time `RepoPath` sees it and the anchoring is a no-op. The two mechanisms
coexist cleanly. The only behaviour change is for bare relative values in the
CWD group — which is the bug being fixed, and which never resolved stably.

Phase 3 is breaking. Given the silent-skip idioms in §1, the migration must
fail loudly rather than degrade:

- Reject `${sut_dir}` inside any repo-anchored field with an error naming the
  key and stating the fix: *"drop it — relative paths resolve against the repo
  root"*.
- The check runs over the **raw** settings dict before `_expand_recursive`,
  since the model never sees the unexpanded text. An explicit key-path list
  keeps it greppable and avoids re-deriving field types at runtime.
- Open decision for the plan: hard error, or a deprecation warning for one
  release first. Recommend hard error — the value is trivially editable, the
  message is self-teaching, and a warning invites the silent-skip failure for
  anyone who does not read it.

Also in phase 3: collapse the duplicate substitution copies at
`coverage/tiers.py:38` and `cli/init.py:80` into the single implementation.

## 9. Testing

- **Per anchored field, a CWD-independence test**: write a repo under
  `tmp_path`, `chdir` somewhere unrelated, assert the field resolves under the
  repo root. This is the guard that would have caught the original bug.
- **`~` preservation**: `tls_cert = "~/x"` resolves under a monkeypatched
  `$HOME`, not under the repo — proving the anchor fallback did not swallow the
  home convention.
- **Absolute passthrough** for one field of each shape.
- **Coexistence**: a `${sut_dir}`-prefixed value and its bare-relative
  equivalent resolve identically (phase 1-2 only).
- **Multi-repo**: two repos in `OTTO_SUT_DIRS`, each anchoring to its own root.
- **Loud rejection** (phase 3): `${sut_dir}` in an anchored field raises, with
  the offending key in the message.
- **Passthrough untouched**: `${sut_dir}` inside `[lab.<backend>]` and
  `ssh_options` still expands after phase 3.

Existing fixtures (`tests/repo1`, `tests/repo3`, `tests/custom_hosts`) use
`${sut_dir}` throughout. They stay valid through phases 1-2; phase 3 migrates
them, which doubles as the end-to-end proof the new form works.

## 10. Documentation

- `docs/overview.md:104-110` and `docs/getting-started.md:357-363` — replace
  the `${sut_dir}` examples with bare relative paths and state the convention.
- `docs/guide/docker.md:35-46`, `docs/guide/reservations.md:60,115`,
  `docs/guide/coverage.md:126,146`, `docs/guide/hosts/os-profiles.md:31` —
  same, plus drop the now-redundant per-page explanations of what relative
  means.
- One canonical statement of the rule, with the passthrough carve-out, in the
  settings reference; every other page links to it.
- `cli/init_templates.py:23-28` — the scaffold that `otto init` writes is the
  most-copied example in the project; it must emit the new form.

## 11. Phasing

1. **Anchoring fix** — `RepoPath`, the validation context, the eight field
   retypings, CWD-independence tests. Stands alone as a bug fix; no user-facing
   change to existing settings files.
2. **Documentation** — state the convention; update examples and the `otto
   init` template.
3. **`${sut_dir}` narrowing** — reject it in anchored fields, retain and
   document it for passthrough tables, collapse the duplicate substitution
   copies, migrate fixtures.

Phase 1 delivers the correctness win and can land independently. Phase 3 is the
only breaking piece and can be deferred or dropped without stranding 1-2.

## 12. Open items for the plan

- Hard error vs. one-release deprecation warning in phase 3 (§8).
- Behaviour of `expanduser()` on an unresolvable `~user` — needs a defined,
  tested outcome rather than whatever the stdlib currently does.
- Whether to pass `repo_dir=` to custom reservation backends (§6, note 1) as a
  separate follow-up.
