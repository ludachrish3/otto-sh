"""The console-term bed rows in lab.json build the hosts the bed actually wires."""

from otto.host.factory import create_host_from_dict
from tests._fixtures.labdata import element_for, host_data


def test_test2_and_bb1350_offer_a_console_on_test1():
    for ne, port in (("test2", 4001), ("bb1350", 2450)):
        h = create_host_from_dict(host_data(ne), element=element_for(ne))
        assert "console" in h.valid_terms
        assert h.term != "console", "the default term stays where it was"
        assert (h.console_options.server, h.console_options.port) == ("test1", port)


def test_the_arm_zephyr_guests_are_console_hosts_on_test4():
    for ne, port in (("zephyr37_nofs", 2325), ("zephyr37_llext", 2323), ("zephyr44_llext", 2324)):
        h = create_host_from_dict(host_data(ne), element=element_for(ne))
        assert h.term == "console"
        assert (h.console_options.server, h.console_options.port) == ("test4", port)
        assert h.console_options.write_chunk_size == 64
        assert h._connections.console_options.login is False
