"""Unit guards for the chaos guest seed/wrapper/unit renderer."""

import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.render_chaos_guest_seed import (
    ARCHES,
    CHAOS_GUEST,
    hold_script_text,
    hold_unit_text,
    image_pin,
    main,
    qemu_argv,
    reboot_target_override_text,
    render_meta_data,
    render_network_config,
    render_unit,
    render_user_data,
    render_wrapper,
)
from tests._fixtures.paths import CHECKOUT_ROOT


def test_the_identity_matches_the_spec():
    g = CHAOS_GUEST
    assert (g.element, g.host_id, g.hop, g.lab, g.unit) == (
        "ubu2404",
        "ubu2404-qemu",
        "test4",
        "chaos",
        "chaos-qemu-ubu2404.service",
    )
    assert (g.mgmt.netdev, g.mgmt.tap, g.mgmt.guest_ip, g.mgmt.host_ip, g.mgmt.subnet) == (
        "eth1",
        "ubeth-mgmt",
        "203.0.113.1",
        "203.0.113.2",
        "203.0.113.0/30",
    )
    assert (g.data.netdev, g.data.tap, g.data.guest_ip, g.data.host_ip, g.data.subnet) == (
        "eth2",
        "ubeth-data",
        "203.0.113.5",
        "203.0.113.6",
        "203.0.113.4/30",
    )
    assert g.nat.netdev == "eth0"
    assert g.nat.tap is None
    assert g.nat.guest_ip is None
    macs = (g.nat.mac, g.mgmt.mac, g.data.mac)
    assert len(set(macs)) == 3
    assert all(m.startswith("52:54:00:") for m in macs)
    assert [(u.login, u.password, u.sudo) for u in g.users] == [
        ("vagrant", "vagrant", True),
        ("test", "Password1", False),
    ]
    assert (g.memory_mb, g.cpus, g.disk_gb, g.hold_timeout_stop_s, g.reboot_job_timeout_s) == (
        1024,
        2,
        4,
        600,
        90,
    )
    assert (g.home, g.wrapper) == (
        "/home/vagrant/chaos-guest",
        "/home/vagrant/run-chaos-qemu-ubu2404.sh",
    )


def test_the_slash_30s_live_in_test_net_3_and_collide_with_no_other_bed():
    # TEST-NET-1 (192.0.2.0/24) is the Zephyr bed, TEST-NET-2 (198.51.100.0/24) the BusyBox bed
    for nic in (CHAOS_GUEST.mgmt, CHAOS_GUEST.data):
        assert nic.guest_ip.startswith("203.0.113.")
        assert nic.host_ip.startswith("203.0.113.")
        g, h = int(nic.guest_ip.rsplit(".", 1)[1]), int(nic.host_ip.rsplit(".", 1)[1])
        assert g % 4 == 1
        assert h == g + 1


def test_user_data_is_cloud_config_with_both_users_and_password_ssh():
    text = render_user_data()
    assert text.startswith("#cloud-config\n")
    cfg = yaml.safe_load(text)
    assert cfg["hostname"] == "ubu2404"
    assert cfg["ssh_pwauth"] is True
    by_login = {u["name"]: u for u in cfg["users"]}
    assert by_login["vagrant"]["sudo"] == "ALL=(ALL) NOPASSWD:ALL"
    assert "sudo" not in by_login["test"]
    assert by_login["test"]["plain_text_passwd"] == "Password1"
    assert by_login["test"]["lock_passwd"] is False
    assert "socat" in cfg["packages"]


def test_user_data_writes_the_hold_unit_its_script_and_the_reboot_target_override():
    cfg = yaml.safe_load(render_user_data())
    paths = {f["path"]: f for f in cfg["write_files"]}
    assert paths["/etc/systemd/system/chaos-shutdown-hold.service"]["content"] == hold_unit_text()
    assert paths["/usr/local/sbin/chaos-shutdown-hold"]["permissions"] == "0755"
    assert paths["/usr/local/sbin/chaos-shutdown-hold"]["content"] == hold_script_text()
    override = paths["/etc/systemd/system/reboot.target.d/otto.conf"]
    assert override["content"] == reboot_target_override_text()
    assert cfg["runcmd"] == [
        "systemctl daemon-reload",
        "systemctl enable --now chaos-shutdown-hold.service",
    ]


def test_the_hold_unit_stops_before_sshd_and_user_sessions():
    unit = hold_unit_text()
    after = next(ln for ln in unit.splitlines() if ln.startswith("After="))
    assert set(after.removeprefix("After=").split()) == {
        "systemd-user-sessions.service",
        "ssh.service",
        "ssh.socket",
    }
    for needle in (
        "Type=oneshot",
        "RemainAfterExit=yes",
        "TimeoutStopSec=600",
        "ExecStop=/usr/local/sbin/chaos-shutdown-hold",
    ):
        assert needle in unit
    assert "/run/chaos/shutdown_hold" in hold_script_text()
    assert hold_script_text().rstrip().endswith("exit 0")


def test_the_reboot_target_override_mirrors_the_lab_vms():
    expected = "[Unit]\nJobTimeoutSec=90\nJobTimeoutAction=reboot-force\n"
    assert reboot_target_override_text() == expected


def test_network_config_matches_each_nic_by_mac_and_names_it_with_no_gateway_on_the_taps():
    net = yaml.safe_load(render_network_config())
    eths = net["ethernets"]
    g = CHAOS_GUEST
    assert net["version"] == 2
    assert list(eths) == ["eth0", "eth1", "eth2"]
    assert eths["eth0"] == {"match": {"macaddress": g.nat.mac}, "set-name": "eth0", "dhcp4": True}
    assert eths["eth1"] == {
        "match": {"macaddress": g.mgmt.mac},
        "set-name": "eth1",
        "addresses": ["203.0.113.1/30"],
    }
    assert eths["eth2"] == {
        "match": {"macaddress": g.data.mac},
        "set-name": "eth2",
        "addresses": ["203.0.113.5/30"],
    }


def test_meta_data_pins_the_instance_id_so_cloud_init_runs_once():
    expected = f"instance-id: {CHAOS_GUEST.instance_id}\nlocal-hostname: ubu2404\n"
    assert render_meta_data() == expected


def test_image_pins_cover_both_arches_from_one_dated_release():
    pins = {p.arch: p for p in CHAOS_GUEST.images}
    assert set(pins) == set(ARCHES) == {"arm64", "amd64"}
    for p in pins.values():
        assert len(p.sha256) == 64
        base = "https://cloud-images.ubuntu.com/releases/noble"
        assert p.url == f"{base}/release-{p.release}/{p.filename}"
        assert p.filename == f"ubuntu-24.04-server-cloudimg-{p.arch}.img"
    assert len({p.release for p in pins.values()}) == 1


@pytest.mark.parametrize("arch", ARCHES)
def test_the_qemu_command_has_three_nics_in_order_no_no_reboot_and_no_variable_store(arch):
    argv = qemu_argv(arch)
    g = CHAOS_GUEST
    nics = [a for i, a in enumerate(argv) if i and argv[i - 1] == "-nic"]
    assert [n.split(",")[0] for n in nics] == ["user", "tap", "tap"]
    assert f"ifname={g.mgmt.tap},mac={g.mgmt.mac}" in nics[1]
    assert f"ifname={g.data.tap},mac={g.data.mac}" in nics[2]
    assert f"mac={g.nat.mac}" in nics[0]
    assert "-no-reboot" not in argv
    assert argv[argv.index("-m") + 1] == str(g.memory_mb)
    assert argv[argv.index("-smp") + 1] == str(g.cpus)
    pflash = [a for a in argv if a.startswith("if=pflash")]
    assert not any("VARS" in a for a in argv)
    if arch == "arm64":
        assert argv[0] == "qemu-system-aarch64"
        assert pflash == ["if=pflash,format=raw,readonly=on,file=/usr/share/AAVMF/AAVMF_CODE.fd"]
    else:
        assert argv[0] == "qemu-system-x86_64"
        assert pflash == []
    joined = " ".join(argv)
    assert f"file={g.home}/ubu2404.qcow2" in joined
    assert f"file={g.home}/seed.iso" in joined


@pytest.mark.parametrize("arch", ARCHES)
def test_the_wrapper_execs_exactly_the_qemu_command(arch):
    text = render_wrapper(arch)
    exec_line = next(ln for ln in text.splitlines() if ln.startswith("exec "))
    assert shlex.split(exec_line)[1:] == qemu_argv(arch)
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    comments = [ln for ln in text.splitlines() if ln.startswith("#")]
    assert any("-no-reboot" in c for c in comments)  # explained in the header, absent from the cmd


def test_the_unit_creates_and_tears_down_both_taps_and_always_restarts():
    unit = render_unit()
    g = CHAOS_GUEST
    pre = [ln for ln in unit.splitlines() if ln.startswith("ExecStartPre=")]
    post = [ln for ln in unit.splitlines() if ln.startswith("ExecStopPost=")]
    for nic in (g.mgmt, g.data):
        assert [
            ln
            for ln in pre
            if f"ip tuntap add {nic.tap} mode tap user vagrant" in ln
            and f"ip addr add {nic.host_ip}/30 dev {nic.tap}" in ln
        ]
    assert len(pre) == 2
    assert len(post) == 2
    assert all("ip tuntap del" in ln for ln in post)
    assert f"ExecStart={g.wrapper}" in unit
    assert "Restart=always" in unit
    assert "User=vagrant" in unit


def test_main_writes_the_three_seed_files(tmp_path):
    assert main(["--dest", str(tmp_path)]) == 0
    assert {p.name for p in tmp_path.iterdir()} == {"user-data", "meta-data", "network-config"}
    assert (tmp_path / "user-data").read_text() == render_user_data()


def test_main_prints_each_artifact(capsys):
    assert main(["--image-pin", "arm64"]) == 0
    pin = image_pin("arm64")
    assert capsys.readouterr().out.split() == [pin.url, pin.sha256, pin.filename]
    assert main(["--wrapper", "amd64"]) == 0
    assert capsys.readouterr().out == render_wrapper("amd64")
    assert main(["--unit"]) == 0
    assert capsys.readouterr().out == render_unit()


def test_main_refuses_an_unknown_arch_and_two_modes(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--image-pin", "riscv64"])
    assert e.value.code == 2
    assert "arm64" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["--unit", "--wrapper", "arm64"])
    with pytest.raises(SystemExit):
        main([])


def test_the_renderer_imports_under_bare_python3(tmp_path):
    """The Vagrantfile runs the renderer on test4's bare python3: stdlib only.

    Same technique as test_the_builder_imports_under_bare_python3: a subprocess whose
    ``sys.path`` holds only the repo root and the stdlib, run from ``tmp_path``.
    """
    code = (
        "import sys; sys.path = [p for p in sys.path if 'site-packages' not in p]; "
        f"sys.path.insert(0, {str(CHECKOUT_ROOT)!r}); "
        "from scripts.render_chaos_guest_seed import CHAOS_GUEST; "
        "print(CHAOS_GUEST.element)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ubu2404"


def _chaos_provisioner_text() -> str:
    """The body of the Vagrantfile's ``provision_chaos_guest``, as committed."""
    text = (CHECKOUT_ROOT / "Vagrantfile").read_text()
    start = text.index("def provision_chaos_guest(vm)")
    return text[start : text.index("\n    end\n", start)]


def _rendered_drive_files() -> tuple[str, str]:
    """The overlay and seed-iso file names ``qemu_argv`` renders (basenames)."""
    files = [
        a.rsplit("file=", 1)[1]
        for a in qemu_argv("amd64")
        if a.startswith("if=virtio") and "file=" in a
    ]
    overlay, iso = files
    return Path(overlay).name, Path(iso).name


def test_the_vagrantfile_provisioner_renders_everything_and_restates_nothing():
    text = _chaos_provisioner_text()
    g = CHAOS_GUEST
    for mode in ('--image-pin "$arch"', '--dest "$D/seed.new"', '--wrapper "$arch"', "--unit"):
        assert text.count(f"render_chaos_guest_seed.py {mode}") == 1, mode
    assert f"U={g.unit}" in text
    assert f"D={g.home}" in text
    assert f"{g.disk_gb}G" in text
    assert text.count(g.wrapper) == 2  # the cmp and the install destination
    overlay, iso = _rendered_drive_files()
    assert f'"$D/{overlay}"' in text
    assert f'"$D/{iso}"' in text
    for literal in (
        g.nat.mac,
        g.mgmt.mac,
        g.data.mac,
        g.mgmt.guest_ip,
        g.data.guest_ip,
        g.mgmt.tap,
        g.data.tap,
        "-no-reboot",
        "AAVMF",
    ):
        assert literal not in text, literal
    assert "SystemMaxUse=200M" in text
    assert "journalctl --vacuum-size=200M" in text
    assert "need 3072 MB free under /home/vagrant" in text


def test_the_provisioner_reseeds_only_when_the_seed_changed():
    text = _chaos_provisioner_text()
    assert 'diff -rq "$D/seed.new" "$D/seed"' in text
    overlay, iso = _rendered_drive_files()
    stop, rm_overlay, mv_seed = (
        text.index(s)
        for s in (
            'systemctl stop "$U"',
            f'rm -f "$D/{overlay}" "$D/{iso}"',
            'mv "$D/seed.new" "$D/seed"',
        )
    )
    assert stop < rm_overlay < mv_seed
    assert text.index("sha256sum -c") < text.index("cloud-localds")
    assert text.index("need 3072 MB") < text.index("curl -fL")
    assert (
        'if [ "$reseed" = 1 ] || [ "$changed" = 1 ] || ! systemctl is-active --quiet "$U"' in text
    )


def test_the_zephyr_define_calls_the_provisioner_last():
    vf = (CHECKOUT_ROOT / "Vagrantfile").read_text()
    block = vf[vf.index('config.vm.define "zephyr"') :]
    block = block[: block.index("\n    end\n")]
    assert block.rstrip().splitlines()[-1].strip() == "provision_chaos_guest(zephyr)"


def test_the_hygiene_oracle_lists_the_guests_hold_file_as_staging_dirt():
    from tests._fixtures import bed_hygiene

    assert CHAOS_GUEST.hold_file in bed_hygiene._STAGING_PROBE
