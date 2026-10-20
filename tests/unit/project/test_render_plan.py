from otto.host.product import ProductPlan
from otto.project.plan import HostPlan, ProductPlanEntry, ProjectPlan
from otto.project.render import render_plan

ENSURE = (
    "whether the lab is already installed (--ensure skips the whole install when it is; "
    "a partial install is torn down first because --recover-partial is on)"
)


def _entry(name, **phases):
    return ProductPlanEntry(name, ProductPlan(**{k: list(v) for k, v in phases.items()}))


LOGIN_HOME_GAP = (
    "kcov: the login home \u2014 product 'kcov' declares no stage_dir and test1 no default_dest_dir"
)
REPLACED_GAP = "`x.Host.install` replaces the default; its steps are not previewed"
TOOLCHAIN_GAP = (
    "toolchain tools: installed on this host once after every project (--toolchain); not previewed"
)


def test_install_prints_every_stage_line_before_any_install_line():
    agent = _entry(
        "agent",
        stage=["PUT build/agent.tar.gz -> /opt/stage"],
        install=["tar -xzf /opt/stage/agent.tar.gz -C /opt"],
    )
    kcov = _entry(
        "kcov",
        install=[
            "PUT build/kcov.ko -> <login home>",
            "sudo insmod '<login home>/kcov.ko'",
            "rm -f '<login home>/kcov.ko'",
        ],
    )
    plans = [ProjectPlan("project1", [HostPlan("test1", [agent, kcov], [LOGIN_HOME_GAP])])]
    lines = [
        "project1",
        "  test1",
        "    stage    agent  PUT build/agent.tar.gz -> /opt/stage",
        "    install  agent  tar -xzf /opt/stage/agent.tar.gz -C /opt",
        "    install  kcov   PUT build/kcov.ko -> <login home>",
        "                    sudo insmod '<login home>/kcov.ko'",
        "                    rm -f '<login home>/kcov.ko'",
        "  not checked:",
        f"    test1: {LOGIN_HOME_GAP}",
    ]
    assert render_plan("install", plans) == "\n".join(lines)


def test_install_tools_keeps_each_tool_together():
    plans = [
        ProjectPlan(
            "r",
            [
                HostPlan(
                    "h",
                    [
                        _entry("a", stage=["PUT a -> /s"], install=["sh a"]),
                        _entry("b", stage=["PUT b -> /s"], install=["sh b"]),
                    ],
                )
            ],
        )
    ]
    lines = render_plan("install-tools", plans).splitlines()[2:]
    assert [line.split()[0:2] for line in lines] == [
        ["stage", "a"],
        ["install", "a"],
        ["stage", "b"],
        ["install", "b"],
    ]


def test_uninstall_phase_column_fits_its_own_word():
    web = _entry("web", uninstall=["docker rm -f web", "docker volume rm web"])
    plans = [ProjectPlan("r", [HostPlan("h", [web])])]
    # 4 indent + 9 (``uninstall``) + 2 + 3 (``web``) + 2 puts a continuation at column 20.
    assert render_plan("uninstall", plans).splitlines()[2:] == [
        "    uninstall  web  docker rm -f web",
        "                    docker volume rm web",
    ]


def test_a_multi_line_command_keeps_its_extra_lines_in_the_step_column():
    script = _entry("agent", install=["set -e\ntar -xzf a.tgz\n./install.sh\n", "sh next"])
    plans = [ProjectPlan("r", [HostPlan("h", [script])])]
    assert render_plan("install", plans).splitlines()[2:] == [
        "    install  agent  set -e",
        "                    tar -xzf a.tgz",
        "                    ./install.sh",
        "                    sh next",
    ]


def test_project_gaps_come_before_host_gaps_and_a_host_without_steps_prints_only_its_line():
    plans = [ProjectPlan("r", [HostPlan("h", [], [REPLACED_GAP])], [ENSURE])]
    lines = ["r", "  h", "  not checked:", f"    {ENSURE}", f"    h: {REPLACED_GAP}"]
    assert render_plan("install", plans) == "\n".join(lines)


def test_nothing_unchecked_prints_no_block():
    plans = [ProjectPlan("r", [HostPlan("h", [_entry("a", install=["sh a"])])])]
    assert "not checked" not in render_plan("install", plans)


def test_a_lab_wide_gap_prints_once_under_the_first_project():
    plans = [
        ProjectPlan(
            "first", [HostPlan("h", [_entry("a", install=["sh a"])], [TOOLCHAIN_GAP])], [ENSURE]
        ),
        ProjectPlan(
            "second", [HostPlan("h", [_entry("b", install=["sh b"])], [TOOLCHAIN_GAP])], [ENSURE]
        ),
    ]
    out = render_plan("install", plans)
    assert out.count("not checked:") == 1
    assert out.count(ENSURE) == 1
    assert out.count(TOOLCHAIN_GAP) == 1
    assert out.index("not checked:") < out.index("second")
    assert out.splitlines()[-1] == "    install  b  sh b"


def test_uninstall_tools_prints_each_tools_removal():
    kmodcov = _entry("kmodcov", uninstall=["sudo rmmod otto_kmodcov"])
    helper = _entry("helper", uninstall=["sudo rmmod helper"])
    plans = [ProjectPlan("r", [HostPlan("h", dev_tools=[kmodcov, helper])])]
    lines = [
        "r",
        "  h",
        "    remove tool  kmodcov  sudo rmmod otto_kmodcov",
        "    remove tool  helper   sudo rmmod helper",
    ]
    assert render_plan("uninstall-tools", plans).splitlines() == lines


def test_cleanup_prints_products_then_dev_tools_then_the_lab_tail():
    from otto.project.plan import TailStep

    agent = _entry("agent", uninstall=["sh /opt/agent/uninstall.sh"])
    kmodcov = _entry("kmodcov", uninstall=["sudo rmmod otto_kmodcov"])
    plans = [
        ProjectPlan("r", [HostPlan("h", [agent], ["product logs: hauled"], [kmodcov])]),
    ]
    tail = [
        TailStep("debug logs", "fetch /var/log/messages*", "h"),
        TailStep("toolchain", "rm -f /usr/bin/gdb", "h"),
        TailStep("tunnels", "would remove otto tunnels on every tunnel-capable host"),
    ]
    assert render_plan("cleanup", plans, tail).splitlines() == [
        "r",
        "  h",
        "    uninstall    agent    sh /opt/agent/uninstall.sh",
        "    remove tool  kmodcov  sudo rmmod otto_kmodcov",
        "  not checked:",
        "    h: product logs: hauled",
        "after every project",
        "  h",
        "    debug logs  fetch /var/log/messages*",
        "    toolchain   rm -f /usr/bin/gdb",
        "  lab",
        "    tunnels     would remove otto tunnels on every tunnel-capable host",
    ]


def test_a_cleanup_with_no_applicable_project_still_prints_its_tail():
    from otto.project.plan import TailStep

    tail = [TailStep("impairments", "would attempt to clear otto-owned impairments")]
    assert render_plan("cleanup", [], tail).splitlines() == [
        "after every project",
        "  lab",
        "    impairments  would attempt to clear otto-owned impairments",
    ]
