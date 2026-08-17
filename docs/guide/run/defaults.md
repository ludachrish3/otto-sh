# Default lab actions

Every repo with products gets a working `otto run install` — and `uninstall`,
`cleanup`, `get-logs`, `install-tools` and `status` — for free. Otto registers
those six instructions itself, before any repo's init module is imported, so a
lab that has only declared its products can already be installed, torn down,
and asked what state it is in.

Nothing about them is special-cased: they are ordinary instructions, registered
through the same `@instruction()` decorator any repo uses, whose whole body is a
call into the `otto.project` library. `otto run --list-instructions` shows them
in their own panel, attributed to otto rather than to a repo.

## The four surfaces, and the one override point

The same six behaviors are wanted in four places, and all four dispatch through
the same code:

| Surface | Looks like |
| ------- | ---------- |
| An instruction | `otto run install` |
| A script | `await otto.project.install()` |
| A suite body | the same call, inside a test |
| A test fixture | `async def test_x(self, ensure_installed): ...` |

```{important}
**Instructions and fixtures are never override points.** A repo customizes lab
behavior by registering a `ProjectActions` subclass — never by defining its own
instruction named `install`. If the *instruction* could be overridden, then
`otto run install` and the `ensure_installed` fixture would run different code,
and the lab a test converges would not be the lab you installed by hand.
```

A repo that tries to claim one of the six names is refused at registration; see
[The collision error](#the-collision-error) below.

## Zero effort: one repo, one command

With products registered (see
{doc}`Registering products <../hosts/capabilities>`) and nothing else done:

```bash
otto --lab my_lab run install
```

That walk is:

1. Otto resolves the configured repos in dependency order (bootstrap already
   computes it).
2. Each repo gets its `ProjectActions` — the subclass it registered, or otto's
   default — constructed with that repo and the live context.
3. Each repo's `install()` fans out across the **fleet**: `ctx.all_hosts()`, so
   the built-in `local` host and Docker containers are excluded exactly as they
   are everywhere else. Hosts proceed in parallel.
4. On each host, `install(owner=<repo name>)` installs that repo's products in
   declaration order.
5. The first repo that will not install stops the walk. Installing a dependent
   on top of a dependency known to be missing produces a lab nobody can reason
   about.

The `owner=` scope in step 4 is the whole point of the layer: on a host shared
by two repos, one repo's `install` can never touch the other's products.

The other five follow the same shape:

| Instruction | Options (default) | Walk |
| ----------- | ----------------- | ---- |
| `install` | `--ensure` (off), `--recover-partial` (on, meaningful with `--ensure`) | dependencies first, fail-fast |
| `uninstall` | `--product-logs` (on), `--debug-logs` (on) | dependents first, best-effort |
| `cleanup` | `--product-logs` (on), `--debug-logs` (on) | dependents first, best-effort |
| `get-logs` | `--product-logs` (on), `--debug-logs` (on), `--require-product-logs` (off) | order immaterial, best-effort |
| `install-tools` | `--dev` (on), `--toolchain` (off) | dependencies first, fail-fast |
| `status` | — | reads only; changes nothing |

Every one of those is a `--flag / --no-flag` pair, as usual.

`cleanup` is strictly more than `uninstall`: each repo also gives up its own dev
tools, and the host-global toolchain tools come off at the very end. `--ensure`
turns `install` into a converge — the lab's current state is read and only the
missing work is done — which is what the fixtures do before a test.

## The override ladder

Three rungs, and the question they each answer:

| Rung | Answers | Subclass |
| ---- | ------- | -------- |
| `Product` / `DevTool` | How does *this artifact* install on a host? | `Product`, `FileProduct`, `DevTool` |
| Host class | How does *this family of machines* do it? | `UnixHost`, `EmbeddedHost`, … |
| `ProjectActions` | What does *this repo* do to the whole lab? | `ProjectActions` |

Climb only as far as the question goes. A product that installs with a
different command is a `Product` override; a host family whose debug logs come
out of journald overrides `get_debug_logs`; a repo that must push a license
before anything installs overrides `ProjectActions.install`.

Register the subclass from a module listed in your settings file's `init`
field — that import is what attributes the class to its repo:

```python
# pylib/widget_instructions/__init__.py  (listed in .otto/settings.toml [init])
from pathlib import Path

from otto.project import ProjectActions, register_project_actions
from otto.result import Result
from otto.utils import Status


@register_project_actions
class WidgetActions(ProjectActions):
    """Widget's lab lifecycle: the defaults, plus a license every host needs."""

    async def install(self) -> Result:
        pushed = await self._push_license()
        if not pushed.is_ok:
            return pushed
        return await super().install()

    async def _push_license(self) -> Result:
        for host in self.ctx.all_hosts():
            result = await host.put(Path("licenses/widget.lic"), Path("/etc/widget"))
            if not result.is_ok:
                return result
        return Result(Status.Success)
```

Two things that example relies on:

- **`super()` keeps every default.** Override the one method that needs
  changing and call up for the rest; a subclass may also mix custom sequencing
  with the per-host verbs (`await host.install()` for chosen hosts) — nothing in
  the defaults is privileged.
- **`self.repo` and `self.ctx` are provided.** `self.repo.name` is the owner
  scope the defaults filter on, and `self.ctx` is the live context, so
  `self.ctx.all_hosts()` and `self.ctx.do_for_all_hosts(...)` are the fleet and
  its dispatch.

A repo registers **at most one** `ProjectActions`; a second registration from
the same repo fails loud. Different repos each registering their own is the
intended composition, not a collision. A repo that registers nothing gets
`ProjectActions` itself.

Two things are deliberately *not* per-repo, and the defaults refuse them:
**debug logs** and **toolchain tools**. Both belong to a host, not to a repo —
N repos each sweeping the same host's debug logs means N transfers each
overwriting the last, and one toolchain serves every owner on a host, so a repo
removing it would take its neighbours' tooling with it. Both are performed
once, by the layer above.

## Many repos: composition and order

Composition is by *iteration over resolved repos in dependency order*, never by
cross-repo subclassing — you cannot subclass a class that may be absent, and an
**optional** dependency may well be.

- **Build-up walks dependencies first.** `install` and `install-tools` take the
  order bootstrap computed. An optional dependency that is present is simply in
  the walk; an absent one simply is not.
- **Teardown walks it reversed.** `uninstall` and `cleanup` bring the dependent
  down before the thing it depends on.
- **Building is fail-fast, tearing down is best-effort.** A repo that will not
  come down must not strand the ones behind it, so every repo is attempted and
  the first failure is what gets reported — named:
  `uninstall failed in repo 'widget': …`.
- **Host-global steps happen once, at the ends.** The debug sweep runs after
  every repo has torn down (teardown-time activity is what those logs exist to
  capture); the toolchain removal is the last step of `cleanup`, after the
  sweep, so no log retrieval depends on tooling that step is deleting.

Ordering beyond dependency order is not configurable, and the orchestrator
itself is not overrideable: a repo customizes by overriding its own actions.

## Where the logs land

Retrieval writes into a documented tree, keyed by host id:

```text
<output-dir>/logs/<host-id>/product/…
<output-dir>/logs/<host-id>/debug/…
```

`<output-dir>` is the active command's output directory (see
[Logging and artifacts](index.md#logging-and-artifacts)) unless a caller passes
an explicit `dest=`. The shape is a tested contract, not an implementation
detail — an override that retrieves logs its own way should still land them
there wherever a host attribution exists.

Product logs are owner-scoped and hauled per repo; debug logs are the host's
and are swept once. `uninstall` and `cleanup` gather product logs **before**
each repo's teardown and sweep debug logs **after** the last one. Retrieving
zero logs is success — `--require-product-logs` is how a run whose whole
purpose was the logs turns an empty haul into a failure, and it is asked only
of hosts the repo actually has products on.

## Converging fixtures in suites

A suite that needs the lab in a known state requests a fixture instead of
scripting the walk:

```python
from otto.suite import OttoSuite


class TestWidget(OttoSuite):
    async def test_service_answers(self, ensure_installed) -> None:
        """Runs against a fully-installed lab, whatever state the last test left."""
        self.logger.info("lab is installed")
```

The three fixtures are `ensure_installed`, `ensure_uninstalled` and
`ensure_clean`. Each is a one-line wrapper over the same converge functions the
CLI calls, so a fixture and `otto run install --ensure` cannot diverge. See
{doc}`../test` for the full fixture list.

- **Function-scoped**: the guarantee is per test *case*. When the state already
  holds, the cost is one `status()` sweep.
- **`ensure_installed` recovers a PARTIAL lab** by tearing it down and
  installing fresh — installing over remnants is how a lab got into that state
  in the first place.
- **`ensure_clean` is stronger than `ensure_uninstalled`**: dev tools and
  toolchain tools are not products, so an uninstalled-but-tooled lab still gets
  cleaned.
- **Failure errors the test, naming the host — never a skip.** A host that
  cannot be brought to the state a test requires fails that test
  ({class}`~otto.errors.EnsureStateError`), rather than quietly removing it from
  the run.

## Reading `status`

`otto run status` prints each counted repo's state and exits on the lab-level
aggregate, so a script can branch without parsing the table:

| Exit | Aggregate | Meaning |
| ---- | --------- | ------- |
| `0` | INSTALLED | every counted repo is installed |
| `1` | UNINSTALLED | every counted repo is uninstalled |
| `2` | PARTIAL | anything in between — `otto run install --ensure` recovers it |

Three codes rather than a boolean, for the same reason the state is a tri-state:
a half-installed lab and a clean one need different handling, and reporting them
alike is how remnants get installed over.

A repo with nothing to say about its install state — no products anywhere, no
registered actions, a docs-only repo — is **not counted**: it is absent from the
table rather than listed with a made-up state, and it cannot drag the aggregate
to PARTIAL forever.

## The collision error

A repo instruction may not take one of the six first-party names. Otto refuses
it while that repo's init modules are being imported:

```text
repo 'widget' defines instruction 'install', which is a first-party default.
Override lab behavior by registering a ProjectActions subclass instead (see
docs/guide/run/defaults.md), or rename the instruction.
```

If you are upgrading a repo that already has an `install` instruction, the
migration is one of two moves:

1. **It really is your repo's install.** Move its body into a
   `ProjectActions.install` override, as above, and delete the instruction.
   Every surface — the command, scripts, suites, the `ensure_installed` fixture
   — picks the change up at once.
2. **It is unrelated** (`install` meaning something else entirely). Rename it;
   `otto run install-firmware` collides with nothing.
