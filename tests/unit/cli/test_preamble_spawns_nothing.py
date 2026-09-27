"""The command preamble starts no process: no shell session, no git."""

from tests._fixtures.budget_harness import load_harness

harness = load_harness()


def test_a_noop_dispatch_starts_no_shell_or_git():
    """``otto -R run noop`` opens no host session and starts no child process.

    The ``noop`` verb answers immediately without touching a host, so the
    exact set ``{"python"}`` is both the positive control (the surface still
    measures something) and the negative control (nothing else — no shell,
    no git, no ``ldconfig``/gcc/ld/collect2 from an asyncssh import — ever
    spawns): a ``by_process`` empty of ``"python"`` would satisfy a mere
    subset check by measuring nothing, while pinning the exact set catches
    that too.
    """
    ops = harness.measure_file_ops(harness.surface_by_key("dispatch_repo_warm"))
    assert set(ops.by_process) == {"python"}, ops.by_process
