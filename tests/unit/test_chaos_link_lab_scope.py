"""The link chaos scenarios run ``link repair --all`` on a lab that is CLOSED.

``test_reboot_x_link_repair_idempotent`` and
``test_sigkill_mid_impair_recovers_via_repair_all`` assert the sweep exits 0,
which the product only promises on a lab that contains EVERY endpoint of every
link it sweeps: a link whose far end is not loaded is filed as a failure, not a
skip (docs/cli/link/repair.md, "When one end is out of scope"; a14258e1d). Pin
the lab the scenarios actually load, so the chaos lane is not the only place a
half-outside link shows up -- the two scenarios were red for seven weeks on
exactly that (#473), because tech1's ``unix`` lab also loads the bb1350 TAP link
through test1 and nothing hostless was watching which lab they aimed at.
"""

from pathlib import Path

from otto.config.lab import load_lab
from otto.host.builtin_hosts import BUILTIN_LOCAL_HOST_ID
from tests._fixtures.labdata import lab_data_dir
from tests.e2e.chaos._bed import closed_unix_link_target, unix_link_id


def _dangling_ends(lab) -> list[str]:
    """``link: host`` for every static-link endpoint the lab did not load.

    The implicit hop edges end on ``local``, which is a builtin and never a
    lab host, so it is not a dangling end.
    """
    return [
        f"{link.id}: {end.host}"
        for link in lab.static_links()
        for end in (link.a, link.b)
        if end.host != BUILTIN_LOCAL_HOST_ID and end.host not in lab.hosts
    ]


def test_the_link_chaos_lab_is_closed_and_still_declares_the_unix_link(tmp_path: Path) -> None:
    """The target the link scenarios spawn otto against loads the test1<->test2
    link (so ``impair <id>`` has something to act on) and nothing that reaches
    out of the lab (so an honest ``repair --all`` has nothing to fail on).

    The SUT's docker use-case also declares local-endpoint placeholder edges
    (``local--test2.repo1.api``); they are deliberately out of this pin's
    scope. ``load_lab`` here carries no docker config, so it never sees them,
    and ``repair --all`` skips every local-endpoint edge by its own rule
    (proved on the bed: repaired 1, skipped 3, exit 0)."""
    target = closed_unix_link_target(tmp_path)
    lab = load_lab(target.lab, search_paths=[target.sut_dir / "lab_data"])

    ids = {link.id for link in lab.static_links()}
    assert unix_link_id() in ids, ids
    assert _dangling_ends(lab) == []


def test_the_bed_target_is_not_closed_which_is_why_the_scenarios_do_not_use_it() -> None:
    """Discriminator: tech1's ``unix`` IS half-outside (``test_load_lab.py``
    pins the containment rule that makes it so), so the guard above is watching
    a real difference between the two targets, not a vacuous property."""
    from tests.integration.chaos._target import make_bed_target

    target = make_bed_target("test1")
    lab = load_lab(target.lab, search_paths=[lab_data_dir() / "tech1"])

    assert unix_link_id() in {link.id for link in lab.static_links()}
    assert _dangling_ends(lab) == ["bb1350-qemu--test1: bb1350-qemu"]
