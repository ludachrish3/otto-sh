# TODO

## General

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
- Look into hyperfine <https://github.com/sharkdp/hyperfine> to help with profiling
- Integration tests for all new functionality except power on/off
  - A soft reboot stability test should be added. Possibly with a different marker and makefile target just because it's such a lengthy test.
- Add TFTP to one of the zephyr hosts
- Consider <https://pypi.org/project/pyftpdlib/> to replace aioftp. It's much faster in all benchmarks.
- Add other Zephyr configs and versions so that the embedded OS support is hardened.
- Add REPL sessions that can live within an active shell session and has a special prompt. The session can be recognized as ending when a REPL end sentinel is seen maybe?
- Have one of the projects define a custom stat collector beyond the default definitions. This helps prove out the workflow for defining custom parsers and graphs.
- Change the `--project-name` option (or similar) to default to the product name from the repo info. Change the options to `--report-name`, which will append the name as a subtitle to the project name, still derived from the repo's name
- Add a `--list-markers` option to the test subcommand
- Make --show-hosts display a Rich.Tree of hosts within an NE. Group by NE in panels and order by slot number.
- Terminal examples use `termynal/termynal.py` within typer. It can be integrated into sphinx docs directly in the markdown files. See <https://claude.ai/share/51ce70f6-c618-4499-898b-fad78d68123e> for more.
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

## Performance Monitoring

- Add an import button and a clear data button so that users can launch a server once and keep viewing different data sets.
- Anomoly detection
  - See <https://claude.ai/chat/2f1b6165-1325-482c-b430-f788ea80d691>
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

## Test

## Publishing

- Ask Claude what kinds of things should be in place for it to be a fully fleshed out open source github repo. I'd like to use an MIT License.
- How to publish to PyPI: <https://www.youtube.com/watch?v=NMQwzI9hprg>
