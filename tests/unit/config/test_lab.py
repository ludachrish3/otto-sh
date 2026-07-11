from otto.config.lab import Lab
from otto.link import Link, LinkEndpoint, Provenance


def test_lab_default() -> None:

    lab = Lab("lab1")

    assert lab.name == "lab1"
    assert lab.resources == set()
    assert lab.hosts == {}
    assert lab.links == []


class _FakeHost:
    """Duck-typed stand-in for a runtime Host (id/ip/hop/term only)."""

    def __init__(
        self, host_id: str, ip: str = "203.0.113.1", hop: str | None = None, term: str = "ssh"
    ):
        self.id, self.ip, self.hop, self.term = host_id, ip, hop, term


class TestStaticLinks:
    def test_merges_implicit_and_declared(self):
        lab = Lab("lab1")
        lab.hosts = {
            "local": _FakeHost("local", ip="127.0.0.1"),
            "gw": _FakeHost("gw"),
        }
        declared = Link(
            a=LinkEndpoint(host="gw", ip="203.0.113.1"),
            b=LinkEndpoint(host="elsewhere", ip="198.51.100.1"),
            protocol="tcp",
            provenance=Provenance.DECLARED,
        )
        lab.links = [declared]

        static = lab.static_links()

        ids = {link.id for link in static}
        assert declared.id in ids
        implicit = [link for link in static if link.provenance is Provenance.IMPLICIT]
        assert len(implicit) == 1
        assert {implicit[0].a.host, implicit[0].b.host} == {"local", "gw"}

    def test_declared_wins_on_route_collision(self):
        """A declared link that duplicates an implicit hop-edge's route
        (same hosts, no interfaces, same protocol) overrides it in the merge.
        """
        lab = Lab("lab1")
        lab.hosts = {
            "local": _FakeHost("local", ip="127.0.0.1"),
            "gw": _FakeHost("gw", term="ssh"),
        }
        # Same route as the implicit local<->gw hop edge (protocol matches gw's term).
        duplicate = Link(
            a=LinkEndpoint(host="local"),
            b=LinkEndpoint(host="gw"),
            protocol="ssh",
            provenance=Provenance.DECLARED,
        )
        lab.links = [duplicate]

        static = lab.static_links()

        (route,) = [link for link in static if {link.a.host, link.b.host} == {"local", "gw"}]
        assert route.id == duplicate.id
        assert route.provenance is Provenance.DECLARED


class TestLabAddDedupesLinks:
    def test_shared_link_by_id_is_deduped(self):
        lab_a = Lab("a")
        lab_a.hosts = {"h1": _FakeHost("h1")}
        lab_b = Lab("b")
        lab_b.hosts = {"h2": _FakeHost("h2")}

        link_in_a = Link(a=LinkEndpoint(host="h1"), b=LinkEndpoint(host="h2"), protocol="tcp")
        link_in_b = Link(a=LinkEndpoint(host="h1"), b=LinkEndpoint(host="h2"), protocol="tcp")
        assert link_in_a.id == link_in_b.id  # same route -> deterministic id collision

        lab_a.links = [link_in_a]
        lab_b.links = [link_in_b]

        merged = lab_a + lab_b

        assert len(merged.links) == 1
        assert merged.links[0].id == link_in_a.id


def test_load_lab_forwards_preferences(monkeypatch):
    import otto.config.lab as lab_mod
    from otto.config.lab import Lab

    captured: dict[str, object] = {}

    class FakeRepo:
        def __init__(self, search_paths=None):
            pass

        def load_lab(self, name, preferences=None):
            captured["preferences"] = preferences
            return Lab(name=name)

    monkeypatch.setattr(lab_mod, "JsonFileLabRepository", FakeRepo)
    lab_mod.load_lab("x", [], preferences={".*": {"transfer": ["scp"]}})
    assert captured["preferences"] == {".*": {"transfer": ["scp"]}}
