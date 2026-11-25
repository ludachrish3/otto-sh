# Option values reach every body, hook and helper: design

**Status:** approved by the owner on 2026-11-25, with the two recommendations of §13 (items 12
and 13). Reviewed to zero open important findings by two independent reviewers (Astra and a
fresh Fable, four rounds each, the second a cross-review) on 2026-11-24, after a builder check;
§14 records them.
**Issue:** #1331.
**Amends:** `2026-09-26-plain-pytest-tests-and-verb-options-design.md` §4.1 (the verb
vocabulary), §4.3 and §4.4 (construction and the errors `ctx.options` raises), §5.4 (what
`run_tests` builds for a class it was not given) and its `ensure` paragraph. §12 lists them.

## 1. Intent

A project declares its options once, as a class, and registers it for `otto run` and
`otto test`. The flags then appear on the command line. That is the easy half. The value does
not arrive where the work is done:

- A project-action body (`install`, `uninstall`, `status`, ...) is handed only its own options
  class. To see a project-wide value the project writes one subclass per action, inherits
  otto's class for that action, and overrides the body.
- A host method and a product hook (`install(self, host)`, `uninstall`, `is_installed`) are
  handed no options at all.
- The one way down is `otto.get_context().options(Cls)`, which nobody is told about, and which
  fails on two of the four ways a hook can be reached.

The owner's example (2026-11-24): a product supports a debug level. `--debug-level 3` on the
command line must reach the function that installs the product on a host, so it can start the
product's logging at that level. Today that takes a registered class, a per-action subclass,
a body override, and a lookup that raises under `otto host <id> install`.

After this lands a project writes its options classes and nothing else. Any body, hook or
helper asks for a class by name and receives this invocation's instance, typed, so the editor
completes its fields and a type checker catches a wrong one. Where a class's flags are offered
is a list on the class.

## 2. What exists today

Verified on the released 0.20.0 in a scratch project made by `otto init --all`, with one class
registered for `run` and `test` (`ProjectOptions.message`), one for `run` only (`RunOnly`), and a
product whose hooks print what they can read. The code on `main` (f3b7bd70c) has the same shape.

| Where | Behaviour |
| --- | --- |
| `otto run install --help` | Shows `--message` beside `--ensure` and `--recover-partial`. |
| An `install` body's `opts` | A plain `InstallOptions`; it has no `message`. |
| `self.ctx.options(ProjectOptions)` in the body | Returns the parsed value. |
| A product hook under `otto run install` or `otto run status` | Is passed nothing; `otto.get_context().options(ProjectOptions)` returns the parsed value. |
| The same hook under `otto test` with an `ensure("installed")` marker | `ProjectOptions` arrives; `RunOnly` raises `OptionsNotAvailableError: RunOnly is registered for run, not test`. |
| The same hook under `otto host <id> install` | Both classes raise `no verb's options are bound in this context`. The command's flags are `--stage-only` and `--owner` only. |

What a type checker (`ty` 0.0.95) sees, for a sample class with one field `variant: str`:

| Read site | Type | A misspelled field |
| --- | --- | --- |
| `otto.get_context().options(Sample)` | `Sample`; `.variant` is `str` | reported |
| `self.ctx.options(Sample)` in a `ProjectActions` body | `Any` | missed |
| `ctx.options(Sample)` in a test whose `ctx` parameter has no annotation | `Unknown` | missed |

The second row is `ProjectContextView.__getattr__`, which forwards every attribute it does not
define and so returns `Any`. The third is the shape the guides show.

How the pieces work today, for the builder:

- `otto.params.OPTIONS` records each registered class with its verbs (`run`, `test`).
  `verb_option_classes(verb)` resolves them; `merge_option_params` merges their fields into one
  command's flags, one flag per field by declaring class, and refuses the same name from two
  unrelated classes **on one command**.
- `OttoContext.bind_verb_options(verb=, kwargs=)` builds every class registered for the verb
  from the parsed flags and keeps the flat parsed values on the context object.
  `OttoContext.options(cls)` returns a built instance or raises one of four
  `OptionsNotAvailableError` cases.
- **Two matching rules exist side by side.** On the command line the flag *name* is the key:
  `OptionsSource.from_kwargs` gives a class every parsed flag whose name it has a field for.
  `docs/cli/run/defaults.md` documents this ("On the command line the flag NAME is the key"),
  and `install --ensure` relies on it to pass install's flags to the `status` probe and the
  recovery `uninstall`. A library caller that hands in an *instance* is matched by declaring
  class instead (`OptionsSource.from_instance`, `shared_field_values`). One input therefore
  behaves differently from two entry points.
- `otto.project.actions._options_for` rebuilds an instance as another body's class when one of
  otto's bodies calls another. `cleanup` first builds
  `UninstallOptions(product_logs=opts.product_logs)` by hand, because `CleanupOptions` and
  `UninstallOptions` each declare their own `product_logs` and the instance path would not
  carry it.
- The sequence "split the command's own fields from the registered ones, bind, validate, finish
  a dry run" is written three times: `otto.cli.invoke` (standalone instructions and
  `@cli_command(options=...)`), `otto.cli.run._project_leaf`, and `otto.cli.test`.
- A standalone instruction receives a registered class as a parameter when the parameter is
  annotated with it (`otto.instructions.bind_handler_kwargs`).
- `otto host <id> <command>` is built from a host method's signature (`otto.cli.expose`,
  `otto.cli.param_synth`), per host class. It binds no options, and under `-n` it stops before
  its leaf runs. Host classes are registered as lazy references, so their commands are known
  only once a class is imported.
- `OttoContext.bind_verb_options`, `verb_binding_preserved` and `verb_option_source` are in the
  public-API snapshot. Binding writes the context object and a read reads it, so two
  independent contexts bound to different values each read back their own.
  `otto.params.OptionsSource` is not in the snapshot, but projects meet it: it is in the `opts=`
  annotations of `otto.project` and is the second argument of a project's `render=` callback.
- Typer hands a command every flag, an unset one as its default, so the flat parsed values
  cannot tell "the user typed it" from "it was left alone".

## 3. Goals, non-goals, anti-goals

**Goals**

1. A registered options class can be read from any code otto runs: a test, a fixture, an
   instruction, a project-action body, a host method, a product or dev-tool hook, and any
   helper they call.
2. The read is typed. An editor completes the fields, and a type checker reports a field that
   does not exist or is used as the wrong type.
3. The read never fails because of which command is running or how a class was registered.
4. A project that wants a flag on one of otto's own commands (`otto run install`,
   `otto host <id> install`) declares a class and names the command. It writes no
   `ProjectActions` subclass and inherits nothing.
5. A class says where its flags are offered: a whole command group, or single commands.
6. No public name is removed, and every registration, instruction and test a project has
   written keeps working. The behaviour that changes is listed in §6.

**Non-goals**

- Values that differ per host within one invocation. One invocation has one value per flag.
  (The owner confirmed on 2026-11-24 that "host-specific" means the value reaching host
  functions, not a value per host.)
- Offering registered flags on commands outside `otto test`, `otto run`, `otto host` and a
  project's own `@cli_command(options=...)` commands. Code that runs under `otto cov`,
  `otto docker` or `otto monitor` can still read every class and receives its defaults. Another
  group can be added later without changing a registration.
- Typing the rest of `ProjectContextView`. Only `options` is typed here.
- Changing what `@instruction(options=...)` or plain instruction parameters do.

**Anti-goals**

- A class otto assembles at runtime and hands to a body. No editor or type checker can see it,
  which defeats goal 2.
- A generated stub file or any other build step the user has to run or keep fresh.
- A second mechanism beside the existing registry. There is one registry, one flag merge, one
  function that builds a class from an invocation's values, one leaf sequence.

## 4. Approaches considered

**A. One typed lookup that always answers (chosen).** Any code asks for a class and receives
this invocation's instance: `otto.get_options(Acme)`. otto keeps one set of option values per
invocation and builds any options class from it. No signature changes; it works at any depth;
the result is typed because the caller names a class it imported. The cost is that a function's
dependence on an option does not show in its signature. §5.7 gives tests and scripts a way to
set values, which answers the testability half of that cost.

**B. otto passes the class as a typed parameter.** The rule standalone instructions already
have, extended to project-action bodies and product hooks. It is explicit. It was rejected as
the mechanism because every override would then differ from its base signature (a type checker
reports that, and `super().install(opts)` no longer lines up), and because `host.install()`
sits between a body and a product hook and is also called directly from tests, scripts and
`otto host`: every layer would have to inspect signatures and forward. `ProjectContextView`
already documents why it refuses to sniff signatures. Helpers below the hook would still need
the value threaded by hand.

**C. Declare the class once on the actions or product class.**
`class BedActions(ProjectActions[Acme])` gives a typed `self.options` in every body. It reads
well. It was rejected because a product is built at lab load, long before flags are parsed, so
`self.options` has to be approach A underneath; because it allows one class per actions class
and so forces the user to write a combined class by inheritance, which is the boilerplate this
work removes; and because it is a second mechanism. It can be added over A later if the lookup
reads too noisily.

**Combining the layers into one object** was the owner's first picture (2026-11-24). It is not
possible with goal 2: Python has no type for "these two classes merged", and neither `ty` nor
pyright can be taught one. The owner accepted naming the class at the point of use instead.

**Where a class applies** had two candidates: areas only (tests, instructions, project actions,
host commands), or areas and single commands in one list. The owner chose the second on
2026-11-24, because it lets a flag for one command be declared the same way as a project-wide
one.

**What identifies a value** had two candidates, and round 1 changed the draft's answer.

- *The declaring class and the field name.* Draft 1 chose this. Two unrelated classes that both
  spell `variant` would have two values. It is the safer rule, but it contradicts the
  documented command-line rule, breaks `install --ensure` passing a project's flag to its own
  `uninstall` and `status` classes, forces `cleanup` to keep translating `product_logs` by hand,
  and makes a shared base class (inheritance) the only way for two classes to share a flag.
- *The flag name (chosen; both reviewers chose it in round 2, one reversing its round-1
  view).* One name has one value in an invocation, and any class with a field of that name
  reads it. This is the rule the command line already has and documents; it is extended to the
  instance path and to the lookup. Sharing a flag between two classes needs no base class. The
  cost is the one `defaults.md` already states, now workspace-wide: give a field the same name
  in two classes only when it means the same thing. Two unrelated classes that spell one name
  on the *same command* are still refused, as today (body classes at startup, registered
  classes when the command is built).

  Round 2 found the sharp edge of this choice: otto's own classes use plain names (`dev`,
  `full`, `ensure`, `toolchain`), and Typer passes every unset flag as its default. If defaults
  were shared by name, a project's unrelated `dev: bool = False`, read in a hook under
  `otto run install-tools`, would read otto's default `True`. So **only a value someone gave is
  shared**: a flag typed on the command line, or a field of an instance handed in. An unset
  flag's default is shared with nobody (§5.2).

## 5. Design

### 5.1 What the user writes

The common case, which is what the main documentation page shows (§8):

```python
@otto.options(verbs=["run", "test"])
class Acme:
    debug_level: Annotated[int, typer.Option(help="Product debug log level.")] = 0


class Agent(Product):
    async def install(self, host: Host) -> Result:
        level = otto.get_options(Acme).debug_level
        return await host.run(f"agent-install --log-level {level}")
```

`otto run install --debug-level 3` and `otto test TestAgent --debug-level 3` (with an
`ensure("installed")` marker) both reach `Agent.install` with 3. No `ProjectActions` subclass,
no class that inherits `InstallOptions`, no override.

A flag for one instruction is a second class, read the same way; this too is on the main page:

```python
@otto.options(verbs=["run install"])
class AcmeInstall:
    variant: Annotated[str, typer.Option(help="Build variant.")] = "field"
```

The fuller case, on the advanced page, also offers the flags on a host command:

```python
@otto.options(verbs=["test", "run", "host install"])
class Acme:
    debug_level: Annotated[int, typer.Option(help="Product debug log level.")] = 0


@otto.options(verbs=["run install", "host install"])
class AcmeInstall:
    variant: Annotated[str, typer.Option(help="Build variant.")] = "field"
```

- `otto host router1 install --debug-level 3 --variant debug` reaches the same hook with the
  same values.
- `otto run status` has `--debug-level` and no `--variant`; a hook that reads `AcmeInstall`
  there receives `variant="field"`.

### 5.2 One set of option values per invocation

An invocation has one set of option values: for each name, the value someone gave it. The name
is the Python field name, which is also the key of the parsed flag; a flag's long or short
spelling maps to it. A name with no entry has no value, and each class that has a field of that
name uses its own default.

**What enters the set.**

1. **Flags the user gave.** Every command that offers registered flags records, before any body
   runs, which of its flags were given. "Given" is what the parser reports: any source other
   than the default, so a typed flag, an environment variable or a prompt answer, and a typed
   value that happens to equal the default. An unset flag is not given (§4).
   `OttoContext.bind_verb_options`, and a command function called with no parser context, pass
   values directly, and everything they pass counts as given.
2. **Instances handed to a library entry point.** `run_instruction(ctx, name=..., opts=[...])`,
   `run_tests(..., options=[...])` and the `otto.project` functions (`install(opts=...)`, ...)
   lay the instances they are given over the set for the duration of the call.
3. **An explicit block** (§5.7).

Only fields of options classes enter. A standalone instruction's plain parameters, `otto test`'s
own flags (`--iterations`, `--cov`, ...), a host method's parameters and the `otto host` group's
options never do: one projection, "the fields of the classes this command offers", is applied on
every path in. For each name the set also remembers which class supplied it, for error messages
only.

**Matching and building.**

- **The name is the key**, on every path (§4). What is stored is the value as given: a parsed
  flag is the raw input, an instance's attribute is that class's validated value. Each class
  validates its own instance when it is built, so a class whose validator rewrites a value can
  hand a same-named reader a different value on the command line than through an instance.
  That is stated in the guide; it is not engineered away.
- **A class the command offers is built from everything the command parsed, as today.** Its
  instance is prepared once, before any body (§5.5), and travels with the recorded flags; it is
  not a layer, and its unset fields are not "given" to anyone. **Any other class takes only the
  names that were given**, and its own default for the rest.
- **`OptionsSource` stays a snapshot with no side effects.** Its `kwargs` still hold every
  parsed flag, so a project's `render=` callback that reads them keeps working; it also knows
  which were given, and `build()` hands a class it was not prepared for only those.
  `from_instance` matches by name, where it matched by declaring class, so the two entry points
  agree. An entry point works out the values in force, builds its bodies from a snapshot of
  them, and holds the layer for the duration of the call. The lookup builds from the recorded
  flags and the layers. These are the only two builders, and both end in `build_options`.
- **A class that cannot take a value says where it came from.** When a name reaches a class
  whose field rejects it, the `OptionsValidationError` names the class being built, the field,
  and the class that supplied the value.

**Layers.**

- **A caller's layer is whole instances.** Handing in `Acme(other=1)` sets every field of
  `Acme`, the ones left at their defaults included: a dataclass cannot tell the two apart. A
  caller that wants to change one field of the current values writes
  `dataclasses.replace(otto.get_options(Acme), other=1)`.
- **An instance handed in is the instance read back.** In its layer, a read of that class
  returns that object, not a rebuilt copy, so an instruction's parameter and the lookup hold
  one object, as `bind_handler_kwargs` promises today. Other classes are built from the fields.
- **Instances handed in together must agree** on a name they share, as
  `flatten_option_instances` requires today (`ValueError`). An inner layer overrides an outer
  one.
- **Layers add.** An inner call sees its own values over the outer ones, and the outer set is
  back when it returns, also on an exception or a cancellation. A name the inner call did not
  set keeps the outer value.
  - `run_instruction` and `run_tests` keep the invocation's values for every class they were
    not handed. Today they rebuild those from defaults (§6). Only a library caller's
    `options=` is a layer; the instances `otto test` prepared from its own flags travel with
    the recorded flags (above), so an unset `otto test` flag is given to nobody.
- **An entry point's own instance comes first for its own bodies.** Every `otto.project`
  function, and every body that calls another body, has one instance of otto's class for the
  call: the one it was handed, the default it builds when handed none, or the one the calling
  body built. (Handed a snapshot instead, as the `ensure` marker and `install --ensure` do,
  otto's class is built from the snapshot by name, as today.) Its bodies are built from that
  instance name by name, as today, and only then
  from the set; and a read of that class inside the call returns that instance. So
  `otto.project.uninstall_tools()` called from inside `otto run install-tools --toolchain`
  still runs with `toolchain=False`, in the body and in any hook that reads the class, and a
  project's added `variant`, which otto's class does not have, keeps the invocation's value.
- **The three kinds of instance differ only in what they give other readers by name:**

  | The instance | Gives to other classes |
  | --- | --- |
  | One a caller built and handed in (`opts=X`, `options=[X]`, `use_options(X)`) | all of its fields, defaults included: a dataclass cannot say which were set |
  | The default an `otto.project` function builds for `opts=None` | nothing |
  | One a calling body built (`cleanup` for `uninstall`) | the fields the body set (`product_logs`), which the call site names, since the instance cannot say |

  So a hook's unrelated `dev: bool = False` reads its own default under
  `otto run install-tools` and under `otto.project.install_tools()`, and `True` under
  `otto.project.install_tools(opts=InstallToolsOptions())`. The guide states this once, with
  this example. Under `cleanup --no-debug-logs`, the `uninstall` body still receives
  `debug_logs=True` from the instance `cleanup` built, as today, and every other reader still
  sees the `False` the user typed.
  - A direct call of a body method with an instance the caller built lays nothing: the
    parameter is what was handed, and hooks below read the set. A caller that wants hooks to see
    it wraps the call in `otto.use_options`.
- **Values are read-only.** otto does not copy what it is given. Assigning to a returned
  instance, or mutating a list it holds, changes nothing for other readers by contract and is
  not supported.

**Where the values live.** These are the rules a user can observe; the data shape is the
builder's.

- **Recorded flags belong to a context and to every context opened inside it.** A nested
  `open_context(variant=...)` inside an instruction sees `--debug-level 3`. "Opened inside" is
  decided when a context is built: one built while another is active reads through to that
  one's recorded flags until it is bound itself, whether `open_context` built it or the caller
  constructed it by hand. Two contexts built independently keep independent flags, so
  `a.bind_verb_options(...)` and `b.bind_verb_options(...)` each read back their own, as today.
- **`ctx.verb` is the group of the command the context was last bound for.** A library call
  no longer changes it for its duration, since it no longer rebinds the context (§6).
- **Recorded flags are visible to every task of the invocation**, the closing sweep included.
  They are not task-scoped: `bind_verb_options` called inside a helper task must still be
  visible after it returns, as it is today.
- **Layers are task-scoped and belong to no context.** Two tasks that each lay values see only
  their own; a task started inside a layer keeps it for its lifetime. A layer applies over
  whichever context's flags are being read, so `with otto.use_options(...)` around
  `run_instruction(other_ctx, ...)` applies. This replaces the bind-and-restore on the shared
  context object that `run_instruction` and `run_tests` do today, which is not safe for two
  concurrent calls.
- **With no context at all** (a unit test of a product), a read sees the defaults and the
  layers. Nothing is cached process-wide.
- **A command that parses options with no context** records them for its own run. A project's
  `@cli_command(options=Cls, lab_free=True)` has no context to record on: what it was given is
  held for the command's body and everything the body starts, and dropped when the command
  returns, so a second run in one process starts clean. It is not a caller's layer, so its
  unset fields are given to nobody, and its "flags pending" mark (below) lives with it.
- **`bind_verb_options` replaces the context's recorded flags**, as today; it does not layer.
  `verb_binding_preserved` restores them. `bind_verb_options` gains an optional keyword naming
  the command (`command="install"`), so the classes registered for `"run install"` are built
  and validated with the group's; without it, it binds the group's classes, as today.
  `verb_option_source` returns a snapshot of the recorded flags.

**The window before the flags are read.** The context is active while the command line is still
being parsed, and `otto host` builds the host during that parse. A read there would see
defaults that the flags then contradict.

- The `run`, `test` and `host` groups, and a project's command that declares `options=`, mark
  "flags pending" when the command is entered, and every such command records before its body
  runs, even when it records nothing. The
  mark is cleared at the one place every executed command passes, not only in the shared
  preparation, so a command registered directly on a group, and the `-n --probe` dial of a host
  command with no covering class, do not stay pending.
- A read while pending raises `OptionsNotAvailableError` saying the command has not read its
  flags yet. `host_class_for_id` and `try_ensure_lab` catch every exception today; this one
  passes through them, so a constructor that reads an option fails loudly instead of quietly
  losing the host's command menu.
- Every other command and every library context is never pending, and reads defaults.
- Option values are read in bodies and hooks, not in constructors.

**Two limits, stated in the guide.**

- A field's secrecy is per class. A class that declares a name as plain `str` reads the raw
  value another class declares `SecretStr`.
- Names are shared across projects without a warning. A project gives the names that are its
  own a prefix (`acme_variant`).

### 5.3 The lookup

```python
def get_options(cls: type[T]) -> T: ...          # otto.get_options
class OttoContext:
    def options(self, cls: type[T]) -> T: ...    # unchanged signature
```

`otto.get_options(Cls)` and `ctx.options(Cls)` are one implementation.

- **It answers for any options class**: a class registered for commands, an instruction's own
  `options=` class, one of otto's (`InstallOptions`), or a plain `@otto.options` class that is
  registered for nothing.
- **It does not depend on the command or on registration.** Under a command that does not offer
  a class's flags, each field reads its own default unless someone gave that name a value
  (§5.2). A user who expects a flag on a command that does not have it is told `No such option`
  by the parser.
- **One instance per class per layer.** Two reads return the same object, as `ctx.options`
  does today. A new layer builds its own only for a class one of whose names it sets; a class
  the command prepared is still that prepared instance inside a block that touches none of its
  names.
- **`ctx.options` reads that context's recorded flags and the task's layers;**
  `otto.get_options` reads the active context's, or none.
- **It works without a command and without a context** (§5.2).
- **It is typed.** `type[T] -> T`.

The read can fail in these cases only:

| Case | Raised |
| --- | --- |
| *cls* is not an options class | `OptionsNotAvailableError` |
| The command has not recorded its flags yet (§5.2) | `OptionsNotAvailableError` |
| A field has no default and no value, or the class's own validation rejects a value | `OptionsValidationError`, from `build_options` as today |

A class the command offers is built before any body runs (§5.5), so for it the last row is an
exit-2 usage error as today. A class built on first read can raise it from inside a body; that
is a runtime failure of the command, or a setup error of the test, not a usage error. The
message names the field and the class, and does not claim the command lacks a flag: a library
caller can omit a required field on a command that offers it.

### 5.4 Where a class applies: `verbs=` names commands

The existing keyword keeps its name and its two existing values keep their meaning. An entry
is a command or a command group, written as it is typed after `otto`, in the dashed command
names:

| Entry | The class's flags are offered on |
| --- | --- |
| `"test"` | `otto test` |
| `"run"` | every `otto run` command: each instruction and each project instruction |
| `"run <name>"` | that one `otto run` command, for example `"run install"`, `"run get-logs"` |
| `"host"` | every `otto host <id>` command |
| `"host <name>"` | that one host command, for example `"host install"`, `"host is-installed"` |

Nothing deeper than two words exists.

- **One list, one registration per class**, as today. `register_options("mod:Cls", verbs=[...])`
  takes the same entries, and the entries are known without importing the class.
- **There is no entry that means every command.** `verbs=["test", "run", "host"]` says it in
  three, and a group added later cannot change what an existing registration means. An empty
  list is refused, as today.
- **Plain `@otto.options` still registers nothing.**
- **An entry covered by another is refused** (`["run", "run install"]`), like a repeated verb.
- **`verb_option_classes` becomes the one answer to "which classes cover this command"**, taking
  a command path. Flag synthesis, recording and the completion collectors all call it.

**When an entry is checked.** Its form and overlaps are checked at registration. Whether it
names a command is checked where the commands are known, without importing anything extra:

- `"run <name>"`: once every project's init modules have run, against the instruction tables.
- `"host <name>"`: when the host commands are built (an `otto host` dispatch or `--help`, and
  the completion-cache rebuild), because a host class's commands exist only once it is
  imported, and bootstrap must not import `otto.host`.

A registration made after its check has run is checked when it is made.

Either way the error names the class, the entry and close matches, is attributed to the project
that registered the class, and is judged by the classifier that judges every project load error
today (`otto.session.projects.check_projects`): fatal when the project is active, named by
`-I`, or required by an active project; one warning otherwise. This design restates none of
that policy.

- A `"run <name>"` error is found at bootstrap and travels with the other bootstrap errors.
- A `"host <name>"` error is found later, so it is added to the same result, printed as its own
  warning line, and judged at the command's existing gate before the body runs. It is seen only
  on an `otto host` command or a cache rebuild; `otto run` and `otto test` never see a mistyped
  host entry. A name is checked against the commands of every registered host class together.
- A cache rebuild contains either error, as it contains a command that cannot be built today.

**A command's flags** are its own (an instruction's `options=` class and plain parameters, a
project instruction's body classes, a host method's parameters) followed by every class
covering it, merged by `merge_option_params` under its existing rule.

**Host commands** have three extra points:

- **Only the command being run resolves registrations.** `HostGroup` builds every verb's command
  on any host dispatch today; the merge must happen only for the command actually resolved, so
  `otto host r1 ls` does not import a class registered for `"host install"`, and a collision on
  one command cannot stop another.
- **Reserved names.** A registered field may not spell a parameter of the host method or the
  wrapper's context parameter. The check compares the flag spellings as well as the Python
  names, in the shared merge, so `run` and `test` gain it too. It is reported when that host
  class's command is built, and a cache rebuild contains it. The `otto host` group's own options
  (`--hop`, `--term`, `--transfer`) are parsed before the command and are not reserved.
- **`"host"` shares one namespace with every host command's parameters**, present and future:
  `user`, `force`, `timeout`, `sudo`, `owner`, `wait` and about forty commands' worth. A new
  otto parameter can therefore collide with a project's field. The guides lead with named host
  commands and say so.

`verbs=["run", "test"]`, the form every existing project and the scaffold use, means exactly
what it means today.

### 5.5 What each entry point does

| Entry point | Flags offered | Values recorded |
| --- | --- | --- |
| `otto run <instruction>` | own class, plain parameters, classes covering it | given flags of the own class and the covering classes |
| `otto run <project instruction>` | each body's class, classes covering it | given flags of those classes |
| `otto test` | otto's test flags, classes covering `test` | given flags of the covering classes |
| `otto host <id> <command>` | the method's parameters, classes covering it | given flags of the covering classes |
| A project's `@cli_command(options=Cls)` | unchanged | given flags of `Cls` |
| Any other otto command | unchanged | none; every class reads as defaults |
| A script or library caller | none | the instances it hands in (§5.2, §5.7) |

**One preparation, shared.** "Split the command's own fields from the option fields, keep what
was given, record, build and validate the offered classes, collect the sensitive names" becomes
one synchronous function, used by the standalone-instruction wrapper, the project-instruction
leaf, `otto test`, the host command wrapper and `@cli_command`. It is written three times today
and host would be a fourth. What follows it stays where it is: `otto test` is synchronous and
drives pytest's runners, the instruction wrappers await an asynchronous dry-run tail, and those
tails are not merged. The instances it builds are carried into the call, not flattened and
built again: `otto test` builds its options and `run_tests` rebuilds them today, so a validator
that rewrites a value runs twice.

**What is validated before any body runs** is what is validated today: the classes covering the
command, a standalone instruction's own class, and the own class of each project body that
*applies* to this run (`_walk_order`; an inactive project's body is not built). A bad value is
an exit-2 usage error. A class the command does not offer is built on first read.

**Dry run.** `-n` builds and shows the same classes and fails the same way. A host command
that carries registered classes becomes a self-finishing leaf, as project instructions are, so
its options are validated and shown; a host command with none keeps today's stop before the
leaf. The registered fields are taken out before the method is called, so the method receives
only its own parameters.

**The project walks.** A walk builds and validates every applicable body's class before its
first body runs, and runs the bodies with those instances, for every caller: the command line,
`otto.project.install()`, and an `ensure` marker, which today reaches the first body with later
bodies unvalidated. `status` has its own walk, with its own choice of which projects count; it
calls the same build-and-validate step and keeps its choice. The values stay in force across
`ensure`'s status probe, recovery `uninstall`, install and the render. A failure here is a usage
error on the command line, the exception itself for a library caller, and a setup error under
pytest; the three are not folded into one.

**A body that calls another body** (`cleanup` calls `uninstall`, `is_uninstalled` calls
`status`) keeps `_options_for`: the caller builds otto's class for the callee by hand
(`UninstallOptions(product_logs=opts.product_logs)`), `_options_for` turns it into the class the
project's override declares, and its refusal for a field with no default stays as it is. Two
things change: the rebuild matches by name, and that instance is the callee's own for the call
(§5.2), so a hook under `cleanup --no-product-logs` reads the same `product_logs` the body was
handed.

**A body's `opts` parameter is what it was handed** (§5.2).

### 5.6 Flags for one command

| The flag belongs to | Declare it as | Read it |
| --- | --- | --- |
| A standalone instruction you wrote | a plain parameter, or `@instruction(options=Cls)` | the parameter |
| A command you did not write (otto's seven, a host command), or several commands | a class with `verbs=["run install", ...]` | `otto.get_options(Cls)` |
| A project-action body you override because it must do different work | `@instruction(options=Cls)` on the method, `Cls` inheriting otto's class | the parameter |

All three record the same fact, "this class's flags are on this command", and go through the
one flag merge and the one preparation. `options=` additionally passes the instance as the
parameter. The third row is no longer needed to add a flag; it remains for a project that
replaces what an action does.

- A class attached with `options=` can be read with `otto.get_options(Cls)` from anything the
  instruction calls. Today only the function holds it, unless the class is also registered.
- A standalone instruction's parameter annotated with a class covering the command receives the
  instance. That is today's rule in the new vocabulary.
- A plain parameter is not a class, so nothing below the function can look it up. A value a
  host method or product hook needs belongs in a class.

**A hook runs under more than one command.** `is_installed` runs under `otto run install`,
`otto run status`, the matching host commands and an `ensure` marker, and hooks also run under
`otto cov` and `otto docker`, which offer no registered flags. A hook that reads a class whose
flags are on `install` only receives the default everywhere else and cannot tell. That is often
what the author wants, so it is guidance, not a rule otto enforces: *offer a flag on every
command where a user must be able to set what the hook reads, and say what the hook does with
the default elsewhere.* The advanced page carries a table of which commands run which hook
(§8).

### 5.7 Scripts and tests of hooks

A caller that is not a command sets values for a block:

```python
with otto.use_options(Acme(debug_level=3)):
    await host.install()
```

`otto.use_options(*instances)` lays the instances over the set for the block, by the rules of
§5.2. It needs no active context, so a unit test of a product with a fake host can use it. It
is the same operation the library entry points perform for their `opts=` arguments.

Under `otto test` a block opened in a test body, in a sync fixture around its `yield`, and in an
async fixture around its `yield` is visible to the test on every supported Python, 3.10
included. Both reviewers checked this against the pytest-asyncio otto requires (1.4 or later),
which runs fixtures and tests with an explicit context and copies a fixture's changes back; §9
keeps a test as the proof.

One ordering fact goes in the guide: the `ensure` marker's converge is an autouse fixture and
runs before a test's own function-scoped fixtures, so a block opened in one of those does not
steer it. A block in a class-, module- or session-scoped fixture does.

### 5.8 Typing and the editor

- `ProjectContextView` gains an explicit `options(cls: type[T]) -> T`.
- `otto.options` gets precise overloads in place of `-> Any`, so a checker keeps the decorated
  class.
- **`otto.options(Acme)` on a class that was itself already decorated is refused**, with a
  message naming `otto.get_options`. Today it re-decorates and returns the class, so
  `otto.options(Acme).debug_level` quietly reads the class default beside
  `otto.get_options(Acme)` and `ctx.options(Acme)`, which read the value. A subclass of an
  options class is not "already decorated" and is decorated as today. (One reviewer would leave
  this to the guides and typing; §13.)
- Every guide and scaffold example that takes the `ctx` fixture annotates it, or reads with
  `otto.get_options`, which needs no annotation and must work in a test body and a fixture.
- A test in otto's own suite type-checks a small sample (a body, a product hook, a test) with
  `ty`, and with pyright if the project carries it, and asserts the revealed types and the
  reported misspelling.

### 5.9 What this removes

- All four `OptionsNotAvailableError` cases that depend on registration or on the command: "not
  registered", "registered for another verb", "no verb's options are bound", and "registered
  after the verb was bound".
- The need for a `ProjectActions` subclass and a class inheriting `InstallOptions` to add a
  flag to one of otto's commands, and the multiple inheritance to share a field
  (`BedInstall(BedVariant, InstallOptions)` in the worked example).
- The two matching rules: one remains.
- Two of the three copies of the leaf preparation, the three copies of the sensitive-name
  union, and the second build of `otto test`'s options inside `run_tests`.
- The check in `flatten_option_instances` that an instance's class is registered for the verb.
- Internal use of `verb_option_source`, and the bind-and-restore on the shared context object in
  `run_instruction` and `run_tests`. The public methods stay (§6).

## 6. Compatibility

No public name is removed, and no signature loses a parameter. `OptionsSource`,
`bind_verb_options` (which gains one optional keyword), `verb_binding_preserved`,
`verb_option_source`, `OptionsNotAvailableError`, `OptionsRegistrationError`, `ctx.options`,
`ctx.verb`, `register_options` and `@otto.options(verbs=[...])` keep working. Two independent
contexts keep independent bindings. `verb_option_source` is no longer used by otto and its
docstring says to read with `ctx.options`.

This is the list of behaviour that changes. Anything not on it is meant to behave as it does
today, and a difference found later is a defect.

1. **Lookups that raised now return an instance**: `ctx.options(Cls)` for a class that is not
   registered, registered for another verb, read with no verb bound, or registered late.
2. **`run_instruction` and `run_tests` keep the invocation's values** for classes they were not
   handed; they built those from defaults. An `otto.project` function called with `opts=None`
   still runs with its own class's defaults, and now keeps the invocation's values for names
   its own class does not have (a project's added `variant`). A caller that wants defaults
   hands in a default instance.
3. **The instance path matches by name** where it matched by declaring class. More arrives than
   before: a field of the same name in an unrelated class now receives the value, and if its
   type cannot take the value the call fails validation where it passed. Nothing that arrived
   stops arriving.
4. **An unset flag's default is no longer passed by name to another class.** `install --ensure`
   hands install's flags to the project's `status` and `uninstall` classes, and an `ensure`
   marker hands `otto test`'s; a flag the user typed still arrives, a flag left alone now leaves
   the other class its own default. For a project whose own classes repeat a name, that shows
   in four cases:
   - the two classes give the name different defaults: the receiving class now uses its own;
   - the receiving class's field has no default: the other class's default used to satisfy it,
     and it is now a missing-field error unless the flag is typed;
   - the receiving class has a validator on the field: it ran on the forwarded default and does
     not run on the class's own default, as pydantic does not validate defaults;
   - the field has a `default_factory`: it now runs for each class built instead of once for
     the command.
5. **`run_instruction` and `run_tests` accept an instance of any options class**, where an
   unregistered one raised `OptionsRegistrationError`.
6. **`otto.options(Cls)` on a class that was itself already decorated is refused.**
7. **`OptionVerb`** widens from `Literal["run", "test"]` to `str`, and `verbs_for` returns
   command entries.
8. **`ctx.verb`** is the command group (`"run"`, `"test"`, `"host"`), also under a host command,
   where it was `None`; and inside a `run_instruction` called from a test it stays `"test"`,
   where it read `"run"` for the call.
9. **A host command covered by a registered class** validates and shows its options under `-n`
   instead of stopping before its leaf.
10. **Two flags on one `run` or `test` command that share a spelling** are refused when the
    command is built; today only a shared Python name is.
11. **A read during command-line parsing** raises with a new message under `otto run` and
    `otto test`, where it raised "no verb's options are bound".
12. **A project walk validates every applicable body's class before its first body** on the
    library and `ensure` paths too, so a bad value in a later project's class stops the walk
    before the first project acts.

Whether any of these is breaking in the commit sense is the owner's (§13).

## 7. Constraints

- **Import cost.** A command that offers no registered class's flags imports nothing it does
  not import today: no pydantic, no options module, and bootstrap never imports `otto.host`.
  The string form of `register_options` stays lazy: its module is imported when a command it
  covers is dispatched, when the completion cache is built cold, or when something reads the
  class. The import-budget ceilings do not move for a project with no registration covering
  `host`.
- **Completion has two readers and both must show host flags.** The shim's per-host-class
  views serialise whatever the live host command builds, so they gain the flags when the cache
  is rebuilt. The Typer fast path rebuilds commands from cached options with no registry, as it
  does for `test` and `run`; it needs the same for host commands, or an unknown flag's value
  shifts the positionals. Both reuse the existing serialisation and stub machinery, keep the
  per-class scoping, omit a command a class does not expose, contain a command whose flags
  cannot be built, and invalidate an older cache. A warm TAB imports no options module, loads
  no lab and stays O(1) in file operations.
- **"Given" comes from the parser's own record.** Typer's vendored click keeps a source for
  every parameter (`Context.get_parameter_source`; checked on typer 0.29.2, which reports
  `DEFAULT` for an unset flag and `COMMANDLINE` for `--level 0` and `--no-flag`). It lives in a
  module Typer treats as private, so otto reaches it only through its one Typer adapter
  (`otto.config._typer`), which gains this reach and a test that pins it.
- **No threads with asyncio.** Layers are task-scoped by `contextvars`.
- **Public surface.** `otto.get_options` and `otto.use_options` are new public names, exported
  lazily like `otto.options`, recorded in the public-API manifest, and follow the keyword-only
  rule with the class (or the instances) as the positional argument, as `ctx.options` has.
- **One decision, one place.** §5.2's name matching, §5.4's coverage resolver and merge, §5.5's
  preparation and walk validation are each one function with every caller named.
- **Secrets.** A field that hides its default from `--help` today does so on every command the
  class covers, host commands included, and its value is masked in the dry-run display.

## 8. Documentation

Documentation is part of each stage, not a follow-up.

**The simple path comes first, alone** (owner, 2026-11-24). Today's options guide puts the lazy
registration form, the collision rules and inheritance in the main body, and that made the
common case hard to find. The rule for every page this work touches:

- **The main page is the owner's debug-level example, told almost verbatim** (owner,
  2026-11-24: "critical to people being able to understand and use this tool"). A product
  supports a debug level; `--debug-level 3` on the command line must reach the function that
  installs the product on a host. The page walks it in three short steps, each one snippet and
  one command line:
  1. *A project-wide option.* One class with `verbs=["run", "test"]` and a `debug_level` field.
  2. *Using it on the host.* The product's `install(self, host)` reads
     `otto.get_options(Acme).debug_level` and starts the product's logging at that level; then
     the same read in a method of a project's own host class, in a test and in an instruction.
  3. *An option for one instruction.* A second class with `verbs=["run install"]` and a
     `variant` field, read in the same `install`, with one sentence and a link on why a hook
     should read classes that cover every command it runs under (§5.6).

  Field validation follows in a few lines. Nothing else is on the page above its "Going
  further" links.
- **The example is one runnable file, included into the page.** It lives under
  `docs/examples/` and is pulled in with `literalinclude`, as the getting-started example is
  today, and a test drives it, so the page cannot drift from code that works.
  `src/otto/examples/options.py` and the `otto init` scaffold carry the same example: a new
  project starts with the project-wide class live and the one-instruction class and the
  host-level read as commented lines beside it.
- **Everything else moves to a second page, linked from the first.** A new
  `docs/setup/options-advanced.md` holds: flags on host commands (`"host install"`) and the
  shared-namespace caution, the table of
  §5.6, the table of which commands run which hook with the rule beside it,
  `@instruction(options=...)`, `otto.use_options` for scripts and unit tests, the lazy string
  form, the collision rule, the name-is-the-key rule, sharing fields by inheritance, and the
  errors.
- **One home per topic.** Other pages link to these two and restate nothing.
- **The reader test.** Each rewritten page is walked by a Haiku or Sonnet reader who is given
  only the main page and asked to add a project-wide flag, add a flag for one instruction, and
  read both where a product is installed on a host. A reader who needs the advanced page for
  that has found a defect in the main page.

The pages:

- `docs/setup/options-classes.md` and the new `docs/setup/options-advanced.md`, as above.
- `docs/setup/customizing-project-instructions.md` and `docs/cli/run/defaults.md`: "A flag of
  your own" leads with a class and `verbs=["run install"]`; overriding the body stays, below
  it, for changing what the action does. The paragraph "On the command line the flag NAME is
  the key" becomes the general rule and moves to the advanced options page, linked from here.
- `docs/examples/getting-started/libs/gs_example/actions.py`: `BedVariant` and `BedInstall`
  reduce to the class and the read.
- `docs/setup/writing-instructions.md`, `writing-tests.md`: annotated `ctx`, the lookup.
- `docs/extend/cli-exposed-verbs.md` and `docs/cli/host/`: registered flags on host commands.
- `docs/configuration/declared-products-tools.md`: how a `class =` product reads an option.
- `docs/cli/dry-run.md`: a covered host command under `-n`.
- The `otto init` templates (`src/otto/init/templates.py`) and `src/otto/examples/options.py`.
- The architecture pages that describe the registry and the completion cache.
- A release note that lists the changes of §6, in the stage that makes each.

## 9. Testing

The entry points must agree, so the tests drive them through one input:

1. **One hook, every entry point.** A product whose `install` records
   `otto.get_options(Cls)`, driven by `otto run install`, `otto host <id> install`, `otto test`
   with an `ensure` marker, `run_instruction`, `otto.project.install()` and
   `otto.use_options`, with the same value given each way. Every one records that value.
2. **Defaults where a flag is not offered**: a class covering one command read under another,
   under `otto cov`, in a bare context and with no context.
3. **The name is the key, and only given values are shared**: `cleanup --no-product-logs`
   reaches a project's `uninstall` class and a hook under it; a flag typed on
   `install --ensure` reaches the project's `status` and `uninstall` classes as it does today;
   an instance handed to `otto.project.install()` reaches a same-named field; a project's
   unrelated `dev` field read under `otto run install-tools` keeps its own default, and takes
   the value when `--no-dev` is typed, and reads its own default under
   `otto.project.install_tools()` with no options; an unset `otto test` flag does not reach an
   install body's same-named field under an `ensure` marker, and a typed one does;
   `otto test --iterations 3` does not reach a class with an `iterations` field; a `render=`
   callback still finds every parsed flag in the source's `kwargs`; a type clash on read names
   both classes.
4. **Layering and isolation**: nested `run_instruction`, `run_tests` and `use_options`; an
   instance handed in is the object read back; `otto.project.uninstall_tools()` inside
   `install-tools --toolchain`; two concurrent `run_instruction` calls on one context;
   concurrent tasks under `do_for_all_hosts`, each inside its own block; restoration after an
   exception and a cancellation; a nested `open_context` sees the flags and two independent
   contexts keep their own; flags recorded in a helper task are visible after it; a read during
   command-line parsing raises, also through the host-class lookup; a host command with no
   covering class reads defaults in its body.
5. **Entries**: each form of §5.4, a covered entry, an unknown `run` command and an unknown
   `host` command with their attribution (active and inactive project), the lazy string form,
   a collision with a host method's parameter by name and by flag spelling.
6. **Validation before any body** on every walk caller (command, library, `ensure`) and on the
   `status` walk, each failing in its own way (usage error, exception, pytest setup error); a
   rewriting validator runs once under `otto test`; and the dry-run display on a host command
   with and without a covering class.
7. **Typing**: the sample of §5.8, and the refused `otto.options(Built)`.
8. **Import contracts and the import budget** for a command with no covering class, and
   `otto host r1 ls` with a class registered for `"host install"` only.
9. **Completion**: a registered flag on a host command completes through both readers with no
   import, scoped by host class; an older cache is rebuilt.
10. **`use_options` under `otto test`** on Python 3.10: test body, sync fixture, async fixture,
    and the `ensure` ordering of §5.7.
11. **Existing options tests** pass unchanged, apart from those asserting the behaviour §6
    lists as changed.

## 10. Stages

One design, built in stages that each leave `main` working:

1. The set of values, the total typed lookup, `otto.get_options`, `otto.use_options`, the
   layering and isolation rules, name matching on both paths, the shared preparation for the
   existing callers, walk validation, the typing fixes, the removals of §5.9, and the main
   options page. After this stage a hook reads every class on every entry point; under
   `otto host` it reads defaults. Most of §6 lands here, so its release note does too.
2. Command entries for `otto run` (`"run <name>"`), the coverage resolver and the entry check.
3. The `host` group: flags offered, recorded, validated, shown in a dry run and completed.
4. The guides, examples and scaffold that were not already rewritten with their stage.

## 11. Left to the builder

- The data shape of the recorded flags and the layers, within the observable rules of §5.2.
- The wording of the errors and of the unknown-command suggestion.
- The name and home of the shared preparation function.
- The cache schema for host-command flags.

## 12. Amendments to other documents

`2026-09-26-plain-pytest-tests-and-verb-options-design.md`:

- §4.1: "Verbs must come from `{"run", "test"}`" becomes the entry table of §5.4 here.
- §4.3: a class a command does not offer is built on first read.
- §4.4: `ctx.options(Cls)` no longer raises for a class that is unregistered or registered for
  other verbs.
- §5.4: `run_tests` keeps the invocation's values for a class it was not given.
- The `ensure` paragraph: an install body still sees only the flags `otto test` offers; a class
  that does not cover `test` reads as defaults instead of raising.
- Its out-of-scope line "Verb-wide options for verbs other than `run` and `test`" is taken up
  here for `host`.

`docs/cli/run/defaults.md`: the name-is-the-key paragraph becomes the rule for every path, with
"only a value someone gave". Its line "Different declaring classes: a bootstrap error" is
narrowed to one command: one flag per name on a command, one value per name in an invocation.

`otto.params.declaring_class`: its docstring ("two projects each spelling `lab_env` have two
fields") is narrowed the same way; it still decides collisions on one command.

## 13. Calls made in this draft, for the owner to check

Settled by both reviewers:

1. The keyword stays `verbs=`; the conversation's sketches used `applies=`.
2. Entries are command paths (`"run install"`, `"host install"`), not bare names
   (`"install"`). A bare name could not say whether it means the `otto run` command, the host
   command or both, and would collide with an instruction named `test` or `host`.
3. There is no entry that means every command; `["test", "run", "host"]` says it.
4. `"host"` covers every host command, `exec` and `login` included, and so shares their
   parameter names. A project that wants its flags only where products are handled names those
   commands, and the guides lead with that.
5. The read is spelled `otto.get_options(Cls)` and the block `otto.use_options(...)`.
6. `run_instruction` and `run_tests` keep the invocation's values (§6 item 2).
7. An unknown command in an entry is an error attributed to the registering project and judged
   by the existing project-error classifier; host names are checked when host commands are
   built.
8. Flags are not offered under `otto cov` or `otto docker`, where product hooks also run.
9. The flag name is the key on every path (§4).
10. Only a value someone gave is shared by name (§4, §5.2). Both accepted it in the
    confirmation round; one called it better than its own proposal.
11. Recorded flags belong to a context and to contexts opened inside it; independent contexts
    stay independent (§5.2). The reviewers split in round 2 and both accepted this rule in the
    confirmation round.

Decided by the owner, 2026-11-25 (the draft's recommendations, accepted with the document):

12. `otto.options(Acme)` on a class that was itself already decorated is refused (§5.8). One
    reviewer would have left it to the guides and typing, as a break unrelated to the feature;
    the other accepted the narrowed refusal.
13. Of §6, items 2, 3 and 4 are breaking changes in the commit sense: each can change what an
    existing project's code receives. The rest are not: a call that raised now answers, a
    command does more, or a name is refused that never did what it appeared to.

The owner also confirmed that `"run <name>"` takes any `otto run` command: otto's seven, a
project's standalone instructions and its own project instructions alike.

## 14. Review record

**Round 1, 2026-11-24.** Astra (14 findings), a fresh Fable (about 30), a builder check (3
questions). All three confirmed §2 against the code and accepted approach A. Folded in:

- The startup check could not see host commands (lazy host classes; bootstrap may not import
  `otto.host`): §5.4 now checks each entry where its commands are known.
- Draft 1 removed or left unstated four public names: §6 now keeps all of them.
- Draft 1's declaring-class key broke `install --ensure` and `cleanup`'s `product_logs`, and
  contradicted `defaults.md`: §4 and §5.2 now key by name. (Astra and Fable found the breaks;
  the change of key is this draft's answer and is not yet reviewed.)
- Concurrency and where the layers live were left to the builder: §5.2 now states the
  observable rules.
- Reads before the flags are recorded would cache defaults: §5.2 now refuses them.
- A host dry run never reached its leaf: §5.5 makes a covered host command self-finishing.
- A fourth copy of the leaf sequence: §5.5 requires one function.
- `HostGroup` builds every verb eagerly: §5.4 requires resolving only the command run.
- Host flag collisions, reserved names and flag spellings: §5.4.
- Two completion readers: §7.
- `ensure` reached bodies unvalidated: §5.5 moves validation into the walk.
- Compatibility list was short: §6 rewritten.
- Hooks run under several commands; `otto.options(Acme)` reads the class default: §5.6, §5.8.

**Round 2, 2026-11-24** (each reviewer read the other's round-1 findings and draft 2).

- Both chose the flag name as the key; Astra reversed its round-1 view.
- Both retracted or rejected the claim that a block in an async fixture is invisible on Python
  3.10: pytest-asyncio 1.4, which otto requires, carries it. §5.7's fallback was removed.
- Fable found that Typer's defaults would be shared by name, and that `opts=None` laying nothing
  would let a nested `uninstall_tools()` read an outer `--toolchain`: §4 and §5.2 now share only
  given values, and `opts=None` lays the function's own defaults as today.
- Both found that draft 2 left the storage and the public binding methods unspecified, and
  split on what the values belong to (§13 item 11). Fable showed a purely task-scoped store
  cannot work: the closing sweep and a `bind_verb_options` call inside a helper task would not
  see it. §5.2 now states the observable rules.
- Astra: only option-class fields may enter the set; `status` has its own walk; the shared
  preparation must stay synchronous and must not rebuild validated instances; a late host-entry
  error needs a route into the existing classifier; `OptionsSource` must stay a snapshot; a
  missing required field stays `OptionsValidationError`. All folded in.
- Fable: who knows a command will record, and the two catch-alls that would swallow the error;
  `cleanup` must keep its hand-built `UninstallOptions`; the new spelling check and the
  no-longer-shared defaults belong in §6. All folded in.
- Astra against Fable, settled here: the `otto host` group's own options are not reserved names
  (they are parsed in another scope); the hook rule is guidance, not a rule otto enforces.

**Confirmation round on draft 3, 2026-11-24.**

- Both accepted the context-ownership rule; Astra withdrew its requirement that a nested
  context start without the outer flags, and Fable withdrew its shared-holder wording.
- Both accepted "only a given value is shared". The parser's record of where each value came
  from was checked on typer 0.29.2 (§7).
- Fable: `otto test` re-hands its instances to `run_tests`, which would have made every unset
  flag given; `opts=None` laying a default instance would have shared otto's defaults by name on
  the library path; the parser reach is private to Typer and belongs in the adapter. Folded in
  (§5.2, §7).
- Astra: draft 3 stated three different rules for what a body-to-body call forwards; a
  lab-free project command that declares options had nowhere to record; §6 item 4 understated
  what stops arriving. Folded in: §5.2 now has one rule (an entry point's own instance comes
  first for its own bodies, and the three kinds differ only in what they give other readers),
  the contextless command, and four cases under §6 item 4.

**Closing check on draft 4, 2026-11-24.** Astra: all three of its findings closed, no open
important finding. Fable: all of its findings closed, no open important finding; four minors,
each one sentence, folded in (the call site names the fields a body-built instance gives; the
snapshot arm of `opts=`; a prepared instance inside a block that touches none of its names; the
pending mark of a context-free command).
