"""Legacy SSH algorithms otto must keep being able to negotiate.

A 2012-era dropbear (the bb1350 bed guest runs one) offers only
diffie-hellman-group{1,14}-sha1, ssh-rsa/ssh-dss, CTR/CBC ciphers and
SHA-1/MD5 MACs. asyncssh 2.24 still registers every one of them, and offers
group14-sha1, ssh-rsa and hmac-sha1 by default. cryptography 50 has already
deprecated finite-field Diffie-Hellman "for removal in a future release";
when that removal lands, asyncssh either keeps its own DH or drops the
finite-field groups, and this module is what turns the second case red on
the dependency bump rather than on the next bed run.

The first half pins the WHOLE matrix (tests/_fixtures/ssh_algorithm_matrix.py)
against the installed asyncssh, both directions: every registered algorithm
has a row (so a version bump that adds one fails here before it fails the
docs page), and every row is registered unless its note names the optional
library that gates it. The stock column is pinned the same way, both
directions, because a row LEAVING or ENTERING asyncssh's default offer is a
change users must see on the docs page either way. The 2012-dropbear subset
gets its own pin: those rows must stay unconditionally registered (no
optional library gates any of them) or the bed guest goes dark.

The second half proves the filter otto installs for the FFDH deprecation by
INJECTING the warning: the locked cryptography 49 cannot raise it, so a
test that waited for the real thing would be green for the wrong reason.
"""

import warnings

import pytest
from asyncssh.compression import get_compression_algs, get_default_compression_algs
from asyncssh.encryption import get_default_encryption_algs, get_encryption_algs
from asyncssh.kex import get_default_kex_algs, get_kex_algs
from asyncssh.mac import get_default_mac_algs, get_mac_algs
from asyncssh.public_key import get_default_public_key_algs, get_public_key_algs
from cryptography.utils import CryptographyDeprecationWarning

from otto.host.connections import (
    FFDH_DEPRECATION_MESSAGE,
    FFDH_DEPRECATION_MODULE,
    install_asyncssh_warning_filters,
)

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - 3.10 only, otto's floor
    import tomli as tomllib

from tests._fixtures.paths import PROJECT_ROOT
from tests._fixtures.ssh_algorithm_matrix import (
    CIPHER,
    COMPRESSION,
    DROPBEAR_2012,
    FAMILIES,
    HOST_KEY,
    IMPORT_GATED,
    KEX,
    MAC,
    MATRIX,
    NEEDS_FIDO,
    NEEDS_KERBEROS,
    NEEDS_LIBOQS,
    NEEDS_NETTLE,
    SshAlgorithm,
    names,
)

_REGISTRY = {
    KEX: get_kex_algs,
    HOST_KEY: get_public_key_algs,
    CIPHER: get_encryption_algs,
    MAC: get_mac_algs,
    COMPRESSION: get_compression_algs,
}
_DEFAULTS = {
    KEX: get_default_kex_algs,
    HOST_KEY: get_default_public_key_algs,
    CIPHER: get_default_encryption_algs,
    MAC: get_default_mac_algs,
    COMPRESSION: get_default_compression_algs,
}

FFDH_TEXT = (
    "Diffie-Hellman over finite fields (FFDH) is deprecated and support will be "
    "removed in a future release. Use a more modern key exchange algorithm."
)


def _names(algs) -> list[str]:
    return [a.decode("ascii") for a in algs]


@pytest.mark.parametrize("family", FAMILIES)
def test_every_registered_algorithm_has_a_matrix_row(family: str) -> None:
    """A new algorithm in asyncssh must be documented before the bump lands."""
    unlisted = [n for n in _names(_REGISTRY[family]()) if n not in names(family)]
    assert not unlisted, (
        f"asyncssh registers {unlisted} for {family} that tests/_fixtures/"
        "ssh_algorithm_matrix.py does not list. Add rows to MATRIX and to "
        "docs/architecture/ssh-algorithms.md before touching this pin."
    )


@pytest.mark.parametrize("row", MATRIX, ids=lambda r: f"{r.family}:{r.name}")
def test_every_matrix_row_is_registered_or_explains_its_absence(row: SshAlgorithm) -> None:
    registered = row.name in _names(_REGISTRY[row.family]())
    if row.note in IMPORT_GATED:
        siblings = [r for r in MATRIX if r.note == row.note]
        present = {r.name in _names(_REGISTRY[r.family]()) for r in siblings}
        assert len(present) == 1, (
            f"{row.note}: some rows registered and some not ({[r.name for r in siblings]}); "
            "the optional library's availability is expected to gate them all together."
        )
        return
    assert registered, (
        f"asyncssh no longer registers {row.name} ({row.family}); the matrix and "
        "docs/architecture/ssh-algorithms.md are stale. Check asyncssh's changelog "
        "for the drop before touching this pin."
    )


@pytest.mark.parametrize("row", MATRIX, ids=lambda r: f"{r.family}:{r.name}")
def test_a_handshake_narrowing_note_cannot_be_added_by_a_fixture_edit_alone(
    row: SshAlgorithm,
) -> None:
    """I2: ``exercisable()`` (tests/_fixtures/ssh_algorithm_matrix.py) drops any
    row whose note is NEEDS_KERBEROS or NEEDS_FIDO from the handshake pin
    (test_ssh_algorithm_handshake.py), and
    ``test_the_case_builder_covers_every_exercisable_row`` only compares
    against ``exercisable()`` itself — so adding one of those notes to an
    exercisable row (e.g. a DROPBEAR_2012 row) would silently narrow the
    handshake with nothing red. Pin the note<->name equivalence, both
    directions, so a note cannot be used as an escape hatch: verified against
    the fixture's actual rows before writing (every NEEDS_KERBEROS row's name
    starts with 'gss-' and nothing else does; every NEEDS_FIDO row's name
    contains 'sk-' or starts with 'webauthn-' and nothing else does; every
    NEEDS_LIBOQS row's name contains 'sntrup' and nothing else does; every
    NEEDS_NETTLE row's name starts with 'umac-' and nothing else does)."""
    is_gss = row.name.startswith("gss-")
    assert (row.note == NEEDS_KERBEROS) == is_gss, (
        f"{row.name} ({row.family}): note={row.note!r} but name-starts-with-'gss-'={is_gss} "
        "disagree — NEEDS_KERBEROS must exactly name the gss-* rows in both directions"
    )
    is_fido = "sk-" in row.name or row.name.startswith("webauthn-")
    assert (row.note == NEEDS_FIDO) == is_fido, (
        f"{row.name} ({row.family}): note={row.note!r} but "
        f"contains-'sk-'-or-starts-'webauthn-'={is_fido} disagree — NEEDS_FIDO must exactly "
        "name those rows in both directions"
    )
    is_sntrup = "sntrup" in row.name
    assert (row.note == NEEDS_LIBOQS) == is_sntrup, (
        f"{row.name} ({row.family}): note={row.note!r} but contains-'sntrup'={is_sntrup} "
        "disagree — NEEDS_LIBOQS must exactly name the sntrup rows in both directions"
    )
    is_umac = row.name.startswith("umac-")
    assert (row.note == NEEDS_NETTLE) == is_umac, (
        f"{row.name} ({row.family}): note={row.note!r} but name-starts-with-'umac-'={is_umac} "
        "disagree — NEEDS_NETTLE must exactly name the umac-* rows in both directions"
    )


@pytest.mark.parametrize("row", MATRIX, ids=lambda r: f"{r.family}:{r.name}")
def test_the_stock_column_matches_asyncssh_s_default_offer(row: SshAlgorithm) -> None:
    """Both directions: a stock row leaving the defaults, and an opt-in row
    entering them, are both changes users must see on the docs page."""
    if row.name not in _names(_REGISTRY[row.family]()):
        assert row.note in IMPORT_GATED
        return
    offered = row.name in _names(_DEFAULTS[row.family]())
    assert offered is row.stock, (
        f"{row.name} ({row.family}): asyncssh now offers it by default={offered}, but "
        f"the matrix says stock={row.stock}. Update the row and "
        "docs/architecture/ssh-algorithms.md; check asyncssh's changelog first."
    )


@pytest.mark.parametrize("family", FAMILIES)
def test_the_matrix_is_in_registry_order(family: str) -> None:
    """The docs page renders rows in matrix order; this is what keeps that
    order honest when asyncssh reorders its own registry.

    An import-gated row absent from this interpreter (sntrup without liboqs)
    has no registry position to compare, so it is dropped from the matrix
    side before comparing — the two other-direction pins already prove it
    belongs and is placed correctly relative to its siblings.
    """
    registered = _names(_REGISTRY[family]())
    matrix_order = [n for n in names(family) if n in registered]
    assert matrix_order == registered, (
        f"{family}: the matrix's registered rows are in {matrix_order}, asyncssh registers "
        f"{registered}. Check asyncssh's changelog for a reorder before touching this pin."
    )


@pytest.mark.parametrize("name", sorted(DROPBEAR_2012))
def test_the_2012_dropbear_subset_is_in_the_matrix_and_registered(name: str) -> None:
    row = next((r for r in MATRIX if r.name == name), None)
    assert row is not None, (
        f"{name}: dropbear 2012.55 offers it (bb1350 bed guest) but it has no matrix row"
    )
    assert row.note == "", (
        f"{name}: a 2012-era sshd needs this unconditionally, but the matrix gates it on "
        f"{row.note!r} — an optional library going missing would take the bed guest dark."
    )
    assert name in _names(_REGISTRY[row.family]()), (
        f"asyncssh no longer registers {name}; a 2012-era sshd cannot be reached with any "
        "ssh_options, and the bb1350 bed guest is about to go dark. Check asyncssh's "
        "changelog for the drop before touching this pin."
    )


def _emit(message: str, module: str) -> None:
    warnings.warn_explicit(
        message, CryptographyDeprecationWarning, filename="dh.py", lineno=30, module=module
    )


def test_the_ffdh_deprecation_attributed_to_asyncssh_dh_is_silenced():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        install_asyncssh_warning_filters()
        _emit(FFDH_TEXT, "asyncssh.crypto.dh")  # must not raise


def test_the_same_deprecation_from_any_other_module_still_surfaces():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        install_asyncssh_warning_filters()
        with pytest.raises(CryptographyDeprecationWarning):
            _emit(FFDH_TEXT, "otto.host.connections")


def test_a_different_deprecation_from_asyncssh_dh_still_surfaces():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        install_asyncssh_warning_filters()
        with pytest.raises(CryptographyDeprecationWarning):
            _emit("DHParameterNumbers is going away in 51.0.0", "asyncssh.crypto.dh")


def test_the_pytest_configuration_carries_the_same_filter():
    """The bed cell runs under ``filterwarnings = ["error"]``; the moment the
    lock moves to cryptography 50 it needs this ini line or it reds on the
    first legacy handshake."""
    filters = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())["tool"]["pytest"][
        "ini_options"
    ]["filterwarnings"]
    assert filters[0] == "error"
    assert (
        "ignore:Diffie-Hellman over finite fields \\(FFDH\\) is deprecated"
        ":cryptography.utils.CryptographyDeprecationWarning:asyncssh\\.crypto\\.dh"
    ) in filters
    assert (
        f"ignore:{FFDH_DEPRECATION_MESSAGE}:cryptography.utils."
        f"CryptographyDeprecationWarning:{FFDH_DEPRECATION_MODULE}"
    ) in filters
