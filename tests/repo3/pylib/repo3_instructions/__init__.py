"""repo3 init module — embedded coverage bed.

Imported by otto at config load (see ``.otto/settings.toml`` ``init``), mirroring
``repo1_instructions``. This is where repo3 will register its install/uninstall
instructions for the LLEXT sample product, the embedded analogues of repo1's
``_install_on_host`` / ``_uninstall_from_host``
(``tests/repo1/tests/test_coverage_product.py``)::

    install   = hex-encode the extension ELF + ``llext load_hex``
    uninstall = ``llext unload <name>``

Status: SKELETON. No instructions are registered yet — the LLEXT product and the
console-dump decoder are deferred until the LLEXT feasibility gate proves the
``load_hex -> call_fn -> cov_dump -> unload`` loop on a live ``qemu_cortex_m3``
instance. See ``../../../todo/embedded_coverage.md`` ("The three pieces to build").
"""
