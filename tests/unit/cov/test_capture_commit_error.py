from otto.coverage.errors import CaptureCommitError, CoverageDataMismatchError


def test_commit_error_names_both_commits_and_remedies():
    e = CaptureCommitError(
        capture="run/cov/h/app/capture.json", taken_at="fc5899ffdfa2aaaa", head="1a5662e4b03ebbbb"
    )
    text = str(e)
    assert "fc5899ffdfa2" in text
    assert "1a5662e4b03e" in text
    assert "git checkout fc5899ffdfa2" in text
    assert "otto test --cov" in text
    assert "rebuilt" not in text
    assert "stamp" not in text
    assert not isinstance(e, CoverageDataMismatchError)
