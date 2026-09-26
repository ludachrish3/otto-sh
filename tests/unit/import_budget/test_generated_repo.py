def test_realistic_repo_is_shaped_like_a_real_one(tmp_path):
    from tests._fixtures.generated_repo import generate_repo

    repo = generate_repo(tmp_path, files=4, dirs=2, realistic=True)
    top = (repo / "tests" / "test_top0.py").read_text()
    assert "import pytest" in top
    assert "OttoSuite" in top
    assert "class TestTop0(" in top
    nested = next((repo / "tests" / "sub0").glob("test_*.py")).read_text()
    assert "import pytest" not in nested  # nested files are walked and parsed, never executed
    init = (repo / "pylib" / "genrepo_instructions.py").read_text()
    assert "otto.monitor.parsers" in init
    assert "async def noop" in init
    settings = (repo / ".otto" / "settings.toml").read_text()
    assert 'backend = "json"' in settings
    assert "lab_data/tech1" in settings


def test_generate_repo_creates_requested_shape(tmp_path):
    from otto.config.repo import Repo
    from tests._fixtures.generated_repo import generate_repo

    repo = generate_repo(tmp_path, files=40, dirs=4, top_level=3)

    assert (repo / ".otto" / "settings.toml").is_file()
    assert (repo / "pylib" / "genrepo_instructions.py").is_file()
    tests_dir = repo / "tests"
    assert len(list(tests_dir.rglob("test_*.py"))) == 43
    assert len(list(tests_dir.glob("test_*.py"))) == 3
    assert len([d for d in tests_dir.iterdir() if d.is_dir()]) == 4
    assert [p.name for p in Repo(sut_dir=repo).iter_test_files()] == [
        "test_top0.py",
        "test_top1.py",
        "test_top2.py",
    ]
