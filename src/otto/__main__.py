"""Entry point for ``python -m otto``.

Routes through the same shim as the ``otto`` console script so the two paths
cannot diverge — ``from otto import app`` would import ``otto.cli`` (and its
440-module graph) before argv is even seen, making ``python -m otto --version``
slow while ``otto --version`` is fast.
"""

from otto._shim import main

main()
