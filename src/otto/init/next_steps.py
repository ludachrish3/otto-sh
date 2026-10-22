"""The commands ``otto init``'s Next steps panel offers to try, from what the project holds.

A pure read of the project root: lab files, detected areas, the vendored
kmodcov library. The CLI draws the answer (:func:`otto.cli.init.next_steps_panel`).
"""

import json
from pathlib import Path

from .areas import detect_areas, lab_files
from .templates import EXAMPLE_LAB_NAME


def declares_lab(root: Path, name: str) -> bool:
    """Return whether a lab file of the project at *root* declares the lab *name*.

    Read leniently: an unreadable or malformed lab file declares nothing here;
    reporting it is the doctor's job.
    """
    try:
        files = lab_files(root)
    except ValueError:
        return False
    for lab_file in files:
        try:
            labs = json.loads(lab_file.read_text(encoding="utf-8")).get("labs", {})
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(labs, dict) and name in labs:
            return True
    return False


def try_it_commands(root: Path, kmodcov_dir: str, *, cwd: Path) -> list[str]:
    """Return the commands worth trying in *root*: only those whose lab, tests or library it has.

    ``otto init --kmodcov`` on an empty directory scaffolds settings and the
    library but no lab and no tests, so it is never told to run against an
    ``example_lab`` it does not have. The library check names *kmodcov_dir*
    relative to *cwd* when it lies beneath it, else absolute.
    """
    areas = detect_areas(root)
    lab = declares_lab(root, EXAMPLE_LAB_NAME)
    commands: list[str] = []
    if lab:
        commands.append(f"otto --lab {EXAMPLE_LAB_NAME} --list-hosts")
    if "tests" in areas:
        commands.append("otto test --list-tests")
        if lab:
            commands.append(f"otto --lab {EXAMPLE_LAB_NAME} test TestExample")
            commands.append(f"otto --lab {EXAMPLE_LAB_NAME} test test_example_function")
    if lab and "instructions" in areas:
        commands.append(f"otto --lab {EXAMPLE_LAB_NAME} run smoke")
    library = root / kmodcov_dir
    if (library / "kmodcov.h").is_file():
        here = cwd.resolve()
        resolved = library.resolve()
        shown = resolved.relative_to(here) if resolved.is_relative_to(here) else library
        commands.append(f"otto cov kmodcov check {shown}")
    return commands
