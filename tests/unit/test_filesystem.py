import otto.filesystem as fs
from otto.filesystem import (
    _fstype_for_path,
    _parse_mountinfo,
    _unescape_mountinfo,
    is_network_fs,
    network_fs_type,
)

# A representative /proc/self/mountinfo: root ext4, an nfs4 mount, a nested
# local mount under it, a cifs mount, and a space-escaped mount point.
_MOUNTINFO = (
    "23 28 0:21 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
    "40 23 0:35 / /mnt/nfs rw,relatime shared:2 - nfs4 server:/export rw\n"
    "41 40 0:36 / /mnt/nfs/local rw,relatime shared:3 - ext4 /dev/sdb1 rw\n"
    "42 23 0:37 / /mnt/share rw,relatime shared:4 - cifs //srv/share rw\n"
    "43 23 0:38 / /mnt/my\\040share rw,relatime shared:5 - nfs //srv/x rw\n"
)


def test_unescape_mountinfo_decodes_octal_space():
    assert _unescape_mountinfo("/mnt/my\\040share") == "/mnt/my share"
    assert _unescape_mountinfo("/plain/path") == "/plain/path"


def test_parse_mountinfo_extracts_mountpoint_and_fstype():
    pairs = _parse_mountinfo(_MOUNTINFO)
    assert ("/", "ext4") in pairs
    assert ("/mnt/nfs", "nfs4") in pairs
    assert ("/mnt/share", "cifs") in pairs
    assert ("/mnt/my share", "nfs") in pairs  # unescaped


def test_fstype_for_path_picks_longest_prefix():
    pairs = _parse_mountinfo(_MOUNTINFO)
    assert _fstype_for_path("/home/user/x", pairs) == "ext4"  # root
    assert _fstype_for_path("/mnt/nfs/run/m.db", pairs) == "nfs4"  # nfs mount
    assert _fstype_for_path("/mnt/nfs/local/m.db", pairs) == "ext4"  # nested local wins
    assert _fstype_for_path("/mnt/share/m.db", pairs) == "cifs"


def test_network_fs_type_classifies(monkeypatch):
    monkeypatch.setattr(fs, "_read_mountinfo", lambda: _MOUNTINFO)
    monkeypatch.setattr(fs, "_resolve_existing", lambda p: "/mnt/nfs/run/m.db")
    assert network_fs_type("anything") == "nfs4"
    assert is_network_fs("anything") is True


def test_local_path_is_not_network(monkeypatch):
    monkeypatch.setattr(fs, "_read_mountinfo", lambda: _MOUNTINFO)
    monkeypatch.setattr(fs, "_resolve_existing", lambda p: "/home/user/m.db")
    assert network_fs_type("anything") is None
    assert is_network_fs("anything") is False


def test_unreadable_mountinfo_falls_back_to_local(monkeypatch):
    monkeypatch.setattr(fs, "_read_mountinfo", lambda: None)
    assert network_fs_type("/mnt/nfs/m.db") is None
    assert is_network_fs("/mnt/nfs/m.db") is False
