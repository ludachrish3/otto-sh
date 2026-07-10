"""launch_command moved here from otto.tunnel.socat (#3 Task 1) — generic
detached-process launcher, shared by tunnel socats and impair expire timers."""

from otto.host.detached import launch_command


class TestLaunchCommand:
    def test_survival_template_shape(self) -> None:
        cmd = launch_command(
            "otto-impair:v1:lnk:eth1", ["bash", "-c", "sleep 5 && tc qdisc del dev eth1 root"]
        )
        # Whole if/then/else/fi conditional wrapped in an outer `bash -c` so the
        # returned string is one opaque word — safe for a caller to splice into
        # a larger command by naive textual prefixing (e.g. sudo). The real
        # systemd-run invocation is folded INTO the if condition (falls through
        # to setsid when systemd-run is present but unusable — no dbus session)
        # and bounded by `timeout 5` so a hang-shaped failure also folds through.
        assert cmd.startswith("bash -c ")
        assert "if command -v systemd-run >/dev/null 2>&1 && timeout 5 systemd-run --user" in cmd
        assert "setsid bash -c" in cmd
        assert "otto-impair:v1:lnk:eth1" in cmd

    def test_tunnel_reexport_is_same_object(self) -> None:
        from otto.tunnel.socat import launch_command as tunnel_launch

        assert tunnel_launch is launch_command
