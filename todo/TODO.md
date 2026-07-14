# TODO

## General

- tunnel creation for some reason uses docker???
- Turn off scrolling in graphs to zoom in. It makes zooming on the page impossible
- Add a random token to the URL for the sake of security. This makes it so that most visitors can only enter if they have that specific URL with the token.
- Make sure we're up-to-date for Untitled component versions: <https://www.untitledui.com/react/docs/upgrade>
- Update components to use Untitled UI components.
  - The time/date picker should be the Untitled `Range calendar card` (<https://www.untitledui.com/react/components/date-pickers>)
  - The options dropdown should be an `Icon Advanced` instead of the ellipses (<https://www.untitledui.com/react/components/dropdowns>)
    - Dark/light mode should have the appropriate icon next to the text
    - Import and export should be in a separate option group
- Add a topology screenshot to the guide — trivially rides the rewritten capture now.
- Retire the fixture-stem enumeration class (three near-misses this phase; derive all lists from one source).
- MiniMap: deferred from the spec with your veto invited — say the word and it's a small task.
- Slot ≥ 8 palette policy (top of the Plan-3 follow-ups): what a 9th series does — design decision, not code yet.
- One issue I've noticed is with the edges in the topology view. They really need a legend/key so that we know what each color and line type mean. Also, the lines connect in odd locations on the boxes in the topology view. I'm looking at the kitchen-sink for reference: `db-01` has connections to other elements at the same depth, but its lines all start on its left side and end on its peers'right sides. I think it would look more natural if those lines originated on the side of `db-01` closest to each host. In this case, all the peers are below `db-0`, so they'd start on `db-01`'s bottom edge and end on all the peers' top edges. The lines that connect elements at different depth layers looks right to me
- safari's chart titles bleed into the menu on the lefthand side
- socat tunnel stability tests (bringing up and down many times is good enough)
- External libraries should also be able to provide data plots. A possible example is an external traffic generator. If it has metrics to share and record (packets/sec, connections/sec), it should also be able to record these in the graphs and DB entries.
- per-ticket coverage report
- Should `otto init` automatically place the JSON schemas in the correct `~/.vscode` location? That simplifies otto's command tree and makes the schema aspect just a flag that's on by default when running `otto init`. If the schemas are already there, validation could be done and prompt the user if they'd like to replace them.
- Monitor GUI displays UTC times instead of local times
- Untitled UI component adoption: the token foundation and primitives
  (Button, Badge, segmented control, Select, dropdown/Menu, slideout-menu,
  empty-state, table, tabs, checkbox, tags, input, tooltip) are DONE, and the
  dashboard is swept onto their semantic tokens (`worktree-monitor-5b-followups`).
  What remains: `ui/Disclosure` has no free-tier Untitled UI equivalent (kept,
  restyled onto their tokens); the PRO-tier components are unexplored; the
  ECharts and React Flow layers stay as-is (Untitled UI's `charts-base` is
  Recharts, and it ships no node-graph canvas on any tier). See
  [untitled-ui-adoption-followups.md](untitled-ui-adoption-followups.md) for
  what the adoption leaves behind.
- If manual coverage reports track the line numbers at the time of test (even after correcting for local change drift), would further changes that shift line numbers in the file be resilient to older manual test runs? Like for instance if line 5 was manually covered and then multiple commits later added code above it (but never touching that manually tested line of code), how would we continue to correlate the manual coverage in later reports? Especially if the manual coverage is committed to the repo and always used as an input for future coverage reports?
- Maybe each coverage run can take an annotation (manual tests already do), which can be treated like a context in python coverage reports. If a line is covered by multiple tiers and runs of coverage, expanding the line with a dropdown error on the righthand side of the page will highlight all the contexts with the appropriate tier color and the context name.
- Look into code quality (linting and type checking), coverage reports for the frontend typescript code. I'd like there to be parity in terms of code quality enforcement for all code, python and typescripit alike.
- Use sonnet to clean up type annotations throughout the repo. I noticed that the host.py file has quoted strings for many types. Annotations should only be strings if they're self-referential to support Python 3.10.
- Docker redesign to use remote docker daemon management. Can pass through HTTP, TCP, and raw socket data over a tunneled SSH connection to a docker daemon host and control the docker daemon. The daemon needs to open a port that it listens on, and the SSH client sets up a port forwarding rule to access the docker daemon's open port.
- E2E testing and compatibility matrix
- Log tracebacks to the complete log file (the otto.log that currently gets saved)
- Clean up and rename labs. The names are currently so obscure that they are difficult to intuitively tell what kinds of hosts they have. The embedded lab name is okay, but the hosts themselves should be more technically focused. The unix labs are all over the place and should be reconsidered as well, even at the lab name level.
- Add RemoteHost `stat()` method if one does not already exist. Tricky part: format of stat has changed over the years. It might be tricky to flexibly parse all versions of stat output including all the different forks of unix (Linux 2.6 - present, other Unix OSes)
- Login utility
  - Make sure logging into docker containers works
  - Scrutinize log file output
- Clang coverage
- Beef up monitoring metrics
  - Make sure they work with zephyr
- Integration tests for all new functionality except power on/off
  - A soft reboot stability test should be added. Possibly with a different marker and makefile target just because it's such a lengthy test.
- Add TFTP to one of the zephyr hosts
- Evaluate faster FTP *client* alternatives to aioftp if FTP transfer speed ever matters. (Original note suggested pyftpdlib, but that is a **server** library — otto uses aioftp as a client; the roles were conflated.)
- Add other Zephyr configs and versions so that the embedded OS support is hardened.
- Add REPL sessions that can live within an active shell session and has a special prompt. The session can be recognized as ending when a REPL end sentinel is seen maybe?
- Have one of the projects define a custom stat collector beyond the default definitions. This helps prove out the workflow for defining custom parsers and graphs.
- Change the `--project-name` option (or similar) to default to the product name from the repo info. Change the options to `--report-name`, which will append the name as a subtitle to the project name, still derived from the repo's name
- Add a `--list-markers` option to the test subcommand
- Make --show-hosts display a Rich.Tree of hosts within an NE. Group by NE in panels and order by slot number.
- Graphical representation of a lab's hosts.
  - Plan available in the topology_plan.md file
- otto cov report --report should be changed to --dir and be a dir, just like the --cov-dir option
- Further nc transfer startup optimizations deferred to dedicated todos:
  - [hop_nc_transfer_flake.md](hop_nc_transfer_flake.md) — root-cause the nc-through-hop transfer hang (currently band-aided with `@pytest.mark.retry(3)`).
- Maybe hosts can have their own list of all supported protocols? This would allow for sane flexibility when choosing a host's term or transfer type, but the host might be limited.
- Move coverage collection to be a subpage of the test command
- Add darkmode toggle to coverage reports
- Consider symlinking .gcno files into the directories with .gcda files instead of copying.
- Make monitoring optionally take a --historical flag. historical mode will enable automatically if a --file is provided. otherwise, it will just bring up a server with empty data. the import button is the only way to view data.
- Move around the body of the test_instruction function into a library. Make the test suite and the instruction call that library function. Verify that behavior seems consistent in both cases.
- Add a `.tainted` property to hosts that checks the /proc/kernel/tainted flag.

- Add a method named something like `log_failures()` in the CommandStatus class so that there is a standard way of logging failed commands. Maybe call this automatically, with a specific option that defaults to True on run()?
- Look into using the `@cache` decorator. Could it help with making the host database accesses faster?
- Bug: Safari overdraws to the right side of the screen. It's the hovering toolbar that overhangs. Claude is quite confused by this and kept coming up with more and more ways to make it all line up. If this is fixed by Claude, it needs to be made very clear just what is overhanging so that it can be fixed.
- Add a `compress` argument to get/put, which is False by default.

## Big Picture

- Projects need to have more control over customizing host usage.
  - Specify custom monitor commands and objects. This probably required a pretty big refactor to allow out-of-band commands at custom frequencies. And to allow custom commands.
  - **In flight:** the monitor revamp Phase 1 plan (`docs/superpowers/plans/2026-07-02-monitor-phase1-backend-contract.md`) covers the monitor half — project-level parser registration, per-parser collection intervals, parser API v2 (`ParseContext`).
- No fleet-level connection cap: each host lazily opens its own connections, but nothing bounds the total. A 100+ host lab could exhaust target `MaxSessions`/`MaxStartups` or local fd limits. (Carried from the retired expert-feedback reassessment.)
- The `resources` field on hosts/labs is declared but nothing consumes or enforces it — either implement lease/locking semantics or document it as purely user-facing metadata. (Carried from the retired expert-feedback reassessment.)

## Performance Monitoring

> Most items below are sequenced by the monitor revamp roadmap
> (`docs/superpowers/specs/2026-07-02-monitor-revamp-roadmap-design.md`);
> frontend/UX items belong to the React rewrite phase, backend items to Phase 1.

- Bound the SSE subscriber queues (`asyncio.Queue(maxsize=N)` + drop-oldest) so a slow dashboard client can't grow memory unbounded. Natural home: `broadcast.py` in the Phase 1 backend decomposition. (Carried from the retired expert-feedback reassessment.)
- Batch metric DB writes per collection tick instead of per-point `INSERT`+`commit`. Natural home: `db.py` in the Phase 1 backend decomposition. (Carried from the retired expert-feedback reassessment.)
- Add an import button and a clear data button so that users can launch a server once and keep viewing different data sets.
- Default database should be `otto.db` in the xdir/`monitor` directory.
- Plots do not dynamically resize when the window changes size

### Human work

### Claude work

- Disable export buttons when disconnected
- Historical view should not display the event toolbar, play/pause button, and the export buttons.
- Make adding custom plots easy.
  - Each project might have custom commands that only apply to certain hosts. It's possible some kind of dashboard templating should be added, or whatever the industry standard might indicate for multi-host dashboards. Some plots might also have custom frequencies. If no frequency is provided to a plot, then the global frequency should be used. If a value is provided, then that value should be used for that plot and that plot alone.
  - Allow projects to define which parsers to use (basically override the DEFAULT_PARSERS value)
- Move play/pause button somewhere else. Doesn't really make sense where it is right now.
- Add a date picker and URL params (start and end timestamps to start) to scope the starting graphs
- Add the ability to fully collapse graphs in the monitor view
- Change page title whenever a host is selected to include the hosts's name

## Host

- Elements can have the same name, but exist in different labs. How does this get reconciled?
  - For something generic, like "client workstation", should the lab name be incorporated in the host ID? There are also instances where hosts belong to multiple labs, so it's hard to assign a single lab to some hosts. Possibly just need to rely on unique names for elements that are truly unique per lab, but have generic names.

### NetEm

- Add a robust, easy-to-use library to manage NetEm on a specific host
  - First step is checking if `tc` is installed
  - If installed, then run whatever command
  - See <https://claude.ai/chat/48c6ec62-a00f-4029-bbad-360ad3ec6680> for a chat about how this might work

- Possibly need a new database table for NetEm connections.
  - Defined as connecting 2 NEs together (the NE's would have lab names and NE IDs associated with them)

- **Dev-VM memory leak: VSCode server accumulates swap over a session (noticed ~late Jun/early Jul 2026).** Observed 2026-07-12 after ~7h uptime: swap 3.3Gi/3.7Gi used, of which ~2.5Gi is held by four `.vscode-server/cli/servers/Stable-*` processes (top holder 1.13Gi swapped alone) plus ~370Mi across two `anthropic.claude-code` extension hosts. PSI stays 0.00 (they're cold/idle pages, not active churn), so it doesn't stall — but it eats the headroom that `make coverage` needs, which is what made the full-suite target thrash the host. Investigate: (a) which extension host grows (disable-bisect, or `--disable-extensions` baseline); (b) whether it correlates with long-lived terminal scrollback / file-watcher counts on this repo (many worktrees = many watchers); (c) whether raising the VM's RAM or adding a periodic `vscode-server` restart is the pragmatic fix. Cheap mitigation meanwhile: reconnect the remote window (kills+respawns the server) before running full-suite gates.
