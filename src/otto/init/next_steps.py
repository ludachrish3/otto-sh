"""The commands ``otto init``'s Next steps panel offers to try, from what the project holds.

A pure read of the project root: lab files, detected areas, the vendored
kmodcov library and embedded-gcov patch. The CLI draws the answer
(:func:`otto.cli.init.next_steps_panel`).
"""

from pathlib import Path

from .areas import (
    TEMPLATE_TEST_CLASS,
    TEMPLATE_TEST_FUNCTION,
    detect_areas,
    lab_files,
    library_present,
    patch_present,
    template_tests_present,
)
from .templates import EXAMPLE_LAB_NAME, EXAMPLE_PROBE_COMMAND

PROBE_STEP_LEAD = (
    "Once `lab_data/lab.json` names your real hosts, probe each Unix host"
    " (BusyBox included) and paste the pin it prints into its entry:"
)
"""The prose that leads the panel's probe step, before :data:`EXAMPLE_PROBE_COMMAND`."""


def declares_lab(root: Path, name: str) -> bool:
    """Return whether a lab file of the project at *root* declares the lab *name*.

    Each file is read by the one lab-document reader
    (:func:`otto.labs.documents.read_lab_document`), leniently: a file the
    loader refuses declares nothing here; reporting it is the doctor's job.
    A file otto does not decode itself
    (:func:`otto.labs.documents.otto_decodes`) is skipped: its backend reads
    it.
    """
    from ..labs import documents
    from ..labs.errors import LabRepositoryError

    try:
        files = lab_files(root)
    except ValueError:
        return False
    for lab_file in files:
        if not documents.otto_decodes(lab_file):
            continue
        try:
            labs = documents.read_lab_document(lab_file).sections["labs"]
        except LabRepositoryError:
            continue
        if name in labs:
            return True
    return False


def _shown(path: Path, cwd: Path) -> Path:
    """Return *path* relative to *cwd* when it lies beneath it, else as given."""
    here = cwd.resolve()
    resolved = path.resolve()
    return resolved.relative_to(here) if resolved.is_relative_to(here) else path


def try_it_commands(
    root: Path, kmodcov_dir: str, embedded_gcov_dir: str, *, cwd: Path
) -> list[str]:
    """Return the commands worth trying in *root*: only those whose lab, tests or library it has.

    ``otto init --kmodcov`` on an empty directory scaffolds settings and the
    library but no lab and no tests, so it is never told to run against an
    ``example_lab`` it does not have. The library check names *kmodcov_dir*, and
    the patch check *embedded_gcov_dir*, relative to *cwd* when it lies beneath
    it, else absolute.
    """
    areas = detect_areas(root=root)
    lab = declares_lab(root, EXAMPLE_LAB_NAME)
    commands: list[str] = []
    if lab:
        commands.append(f"otto --lab {EXAMPLE_LAB_NAME} --list-hosts")
    if "tests" in areas:
        commands.append("otto test --list-tests")
        if lab and template_tests_present(root):
            commands.append(f"otto --lab {EXAMPLE_LAB_NAME} test {TEMPLATE_TEST_CLASS}")
            commands.append(f"otto --lab {EXAMPLE_LAB_NAME} test {TEMPLATE_TEST_FUNCTION}")
    if lab and "instructions" in areas:
        commands.append(f"otto --lab {EXAMPLE_LAB_NAME} run smoke")
    library = root / kmodcov_dir
    if library_present(library):
        commands.append(f"otto cov kmodcov check {_shown(library, cwd)}")
    patch = root / embedded_gcov_dir
    if patch_present(patch):
        commands.append(f"otto cov embedded-gcov check {_shown(patch, cwd)}")
    return commands


def probe_step_command(root: Path) -> str | None:
    """Return the probe command for the panel's probe step, or ``None`` when *root* has no lab.

    The step is its own, after "Try it": those commands work on a fresh
    scaffold, and a probe of its placeholder host stalls until it gives up.
    """
    return EXAMPLE_PROBE_COMMAND if declares_lab(root, EXAMPLE_LAB_NAME) else None
