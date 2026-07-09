from otto.link.discovery import Observation, parse_etime, parse_process_discovery  # noqa: F401


def test_parse_etime_formats():
    assert parse_etime("05") == 5  # SS (rare) — treat as seconds
    assert parse_etime("01:05") == 65  # MM:SS
    assert parse_etime("02:01:05") == 7265  # HH:MM:SS
    assert parse_etime("1-02:01:05") == 93665  # DD-HH:MM:SS


def test_parse_process_discovery_extracts_pid_age_link():
    out = (
        "  4021    01:05 otto-link:v1:lnk-abc-161:udp:test1::161:test2::161 "
        "UDP4-LISTEN:161,fork,reuseaddr TCP4:10.0.0.2:50001\n"
    )
    obs = parse_process_discovery(out)
    assert len(obs) == 1
    assert obs[0].pid == 4021
    assert obs[0].age_seconds == 65
    assert obs[0].link.id == "lnk-abc-161"
    assert obs[0].link.protocol == "udp"


def test_parse_process_discovery_excludes_non_otto():
    out = (
        "  777    10:00 socat UDP4-LISTEN:53,fork TCP4:1.2.3.4:53\n"
        "  778    00:30 otto-link:v1:lnk-x-161:udp:a::161:b::161 socat ...\n"
    )
    obs = parse_process_discovery(out)
    assert [o.link.id for o in obs] == ["lnk-x-161"]


def test_parse_process_discovery_ignores_garbage_lines():
    assert parse_process_discovery("\n   \nnot a ps line\n") == []
