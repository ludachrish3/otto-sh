from otto import link


def test_public_callables_exported():
    for name in (
        "add_link",
        "remove_link",
        "remove_all_links",
        "discover_dynamic_links",
        "all_links",
        "AddedTunnel",
        "RemovedReport",
        "Link",
        "LinkEndpoint",
        "Provenance",
    ):
        assert hasattr(link, name), name
