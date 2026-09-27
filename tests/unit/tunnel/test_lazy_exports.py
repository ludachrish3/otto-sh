"""``otto.tunnel``'s ``check_tunnel``/``TunnelCheckReport`` re-exports are lazy (PEP 562).

Only ``otto tunnel check`` needs ``otto.check``'s fingerprint/probe/render
machinery; every other otto.tunnel importer must not pay for it. Mirrors
``tests/unit/link/test_lazy_exports.py``'s ``.check`` proof, which uses the
same mechanism against a different module.
"""

import pytest

from otto.tunnel import check


def test_check_names_resolve_lazily_and_are_the_check_module_s():
    from otto.tunnel import check_tunnel as lazy_check_tunnel

    assert lazy_check_tunnel is check.check_tunnel

    import otto.tunnel as tunnel_mod

    assert tunnel_mod.check_tunnel is check.check_tunnel
    assert tunnel_mod.TunnelCheckReport is check.TunnelCheckReport


def test_unknown_attribute_raises_attribute_error():
    import otto.tunnel as tunnel_mod

    with pytest.raises(AttributeError, match=r"module 'otto\.tunnel' has no attribute 'nope'"):
        _ = tunnel_mod.nope
