"""The lab doctor's advisory warnings (spec §8.3 dead patterns, §9 overlap)."""

from otto.labs.doctor import lab_warnings
from otto.models.lab import ElementSpec, LabEntrySpec

_H = [{"ip": "10.0.0.1", "creds": [{"login": "u", "password": "p"}]}]


def _el(name, labs):
    return ElementSpec(name=name, labs=labs, hosts=_H)


def test_dead_pattern_warns_naming_element_and_pattern() -> None:
    docs = [("lab.json", {"unix": LabEntrySpec()}, [_el("t", ["unix", "gh.st"])])]
    (w,) = lab_warnings(docs)
    assert "'t'" in w
    assert "'gh.st'" in w
    assert "matches no declared lab" in w


def test_shared_element_with_disjoint_resources_warns() -> None:
    docs = [
        (
            "lab.json",
            {"unix": LabEntrySpec(resources={"a"}), "busybox": LabEntrySpec(resources={"b"})},
            [_el("test1", ["unix", "busybox"])],
        )
    ]
    (w,) = lab_warnings(docs)
    assert "'busybox'" in w
    assert "'unix'" in w
    # The element is named by its KEY, not its name: two same-named elements
    # with different ids are two elements, and a name-only rendering would
    # print one of them twice with nothing to tell them apart.
    assert "('test1', None)" in w
    assert "disjoint" in w


def test_shared_resource_silences_the_overlap_warning() -> None:
    docs = [
        (
            "lab.json",
            {
                "unix": LabEntrySpec(resources={"bed"}),
                "busybox": LabEntrySpec(resources={"bed", "x"}),
            },
            [_el("test1", ["unix", "busybox"])],
        )
    ]
    assert lab_warnings(docs) == []


def test_a_prefix_pattern_that_does_not_full_match_is_dead() -> None:
    """Membership is a FULL match, so the dead-pattern check has to be one too.

    ``re.search`` would find "uni" inside "unix" and stay silent, while
    ``ElementSpec.matches`` still refuses the membership — the element would
    join nothing and otto would never say why. This is the discriminator
    between the two spellings; every other case here passes under both.
    """
    docs = [("lab.json", {"unix": LabEntrySpec()}, [_el("t", ["uni"])])]
    (w,) = lab_warnings(docs)
    assert "'uni'" in w


def test_declarations_span_files() -> None:
    docs = [("a.json", {}, [_el("t", ["site.*"])]), ("b.json", {"site.b4": LabEntrySpec()}, [])]
    assert lab_warnings(docs) == []


def test_dead_pattern_names_the_declaring_file_and_the_declared_labs() -> None:
    """The warning must be actionable on its own: which file, which labs exist."""
    docs = [("repo/lab_data/lab.json", {"unix": LabEntrySpec()}, [_el("t", ["gh.st"])])]
    (w,) = lab_warnings(docs)
    assert w.startswith("repo/lab_data/lab.json:")
    assert "['unix']" in w


def test_no_declared_lab_at_all_reads_none_rather_than_an_empty_list() -> None:
    docs = [("lab.json", {}, [_el("t", ["unix"])])]
    (w,) = lab_warnings(docs)
    assert "declared: none" in w


def test_two_resource_less_labs_sharing_an_element_are_not_warned_about() -> None:
    """An empty ``resources`` is a valid declaration — it has no reservation to lose.

    ``set().isdisjoint(set())`` is ``True``, so a naive check warns about every
    pair of resource-less labs, contradicting the README `otto init` scaffolds
    ("an empty value is a perfectly good declaration").
    """
    docs = [
        (
            "lab.json",
            {"unix": LabEntrySpec(), "busybox": LabEntrySpec()},
            [_el("test1", ["unix", "busybox"])],
        )
    ]
    assert lab_warnings(docs) == []


def test_one_resource_less_lab_does_not_trigger_the_overlap_warning() -> None:
    """Only a pair that BOTH reserve something can fail to contend."""
    docs = [
        (
            "lab.json",
            {"unix": LabEntrySpec(resources={"a"}), "busybox": LabEntrySpec()},
            [_el("test1", ["unix", "busybox"])],
        )
    ]
    assert lab_warnings(docs) == []


def test_labs_sharing_no_element_are_not_warned_about() -> None:
    """Disjoint resources are only a problem for labs that share an element."""
    docs = [
        (
            "lab.json",
            {"unix": LabEntrySpec(resources={"a"}), "busybox": LabEntrySpec(resources={"b"})},
            [_el("test1", ["unix"]), _el("bb1", ["busybox"])],
        )
    ]
    assert lab_warnings(docs) == []


def test_overlap_warning_names_the_remedy() -> None:
    docs = [
        (
            "lab.json",
            {"unix": LabEntrySpec(resources={"a"}), "busybox": LabEntrySpec(resources={"b"})},
            [_el("test1", ["unix", "busybox"])],
        )
    ]
    (w,) = lab_warnings(docs)
    assert "declare a shared lab identifier" in w
    assert "busybox.unix" in w  # the sub-lab spelling that makes the relation visible


def test_elements_and_declarations_from_different_files_still_pair_up() -> None:
    """Membership is computed across every document, not per file."""
    docs = [
        ("labs.json", {"unix": LabEntrySpec(resources={"a"})}, []),
        ("more.json", {"busybox": LabEntrySpec(resources={"b"})}, []),
        ("elements.json", {}, [_el("test1", ["unix", "busybox"])]),
    ]
    (w,) = lab_warnings(docs)
    assert "disjoint" in w


def _el_r(name, labs, *, resources=None, hosts=None):
    return ElementSpec(name=name, labs=labs, resources=set(resources or ()), hosts=hosts or _H)


_DISJOINT = {"unix": LabEntrySpec(resources={"a"}), "busybox": LabEntrySpec(resources={"b"})}


def test_shared_element_protected_by_the_element_does_not_warn() -> None:
    docs = [
        ("lab.json", _DISJOINT, [_el_r("test1", ["unix", "busybox"], resources=["test1-chassis"])])
    ]
    assert lab_warnings(docs) == []


def test_shared_element_protected_by_every_host_does_not_warn() -> None:
    hosts = [{**_H[0], "resources": ["s1"]}, {**_H[0], "ip": "10.0.0.2", "resources": ["s2"]}]
    docs = [("lab.json", _DISJOINT, [_el_r("test1", ["unix", "busybox"], hosts=hosts)])]
    assert lab_warnings(docs) == []


def test_one_unprotected_host_leaves_the_element_unprotected() -> None:
    hosts = [{**_H[0], "resources": ["s1"]}, {**_H[0], "ip": "10.0.0.2"}]
    docs = [("lab.json", _DISJOINT, [_el_r("test1", ["unix", "busybox"], hosts=hosts)])]
    (w,) = lab_warnings(docs)
    assert "('test1', None)" in w
    assert "no element- or host-level resource protects" in w


def test_warning_names_only_the_unprotected_shared_elements() -> None:
    docs = [
        (
            "lab.json",
            _DISJOINT,
            [
                _el_r("safe", ["unix", "busybox"], resources=["safe-lock"]),
                _el_r("open", ["unix", "busybox"]),
            ],
        )
    ]
    (w,) = lab_warnings(docs)
    assert "[\"('open', None)\"]" in w
    assert "safe" not in w


def test_two_same_named_elements_with_different_ids_are_not_a_shared_element() -> None:
    """Membership is keyed on ``ElementKey``, so ``chassis`` #1 and #2 do not pair up.

    Not a red-at-HEAD guard — this passes today. It is a REGRESSION pin: the
    shared-element intersection is the one place a name-keyed shortcut looks
    equivalent and is not, and a repo where each lab owns its own chassis would
    otherwise start getting an overlap warning for two elements that share
    nothing but a spelling.
    """
    docs = [
        (
            "lab.json",
            _DISJOINT,
            [
                ElementSpec(name="chassis", id=1, labs=["unix"], hosts=_H),
                ElementSpec(name="chassis", id=2, labs=["busybox"], hosts=_H),
            ],
        )
    ]
    assert lab_warnings(docs) == []


def test_r18_both_labs_must_reserve_something_even_with_unprotected_elements() -> None:
    docs = [
        (
            "lab.json",
            {"unix": LabEntrySpec(resources={"a"}), "busybox": LabEntrySpec()},
            [_el("test1", ["unix", "busybox"])],
        )
    ]
    assert lab_warnings(docs) == []
