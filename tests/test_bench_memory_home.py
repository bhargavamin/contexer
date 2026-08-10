from pathlib import Path

from benchmarks.memory_home import write_home_settings, memory_dir, memory_files


def test_write_home_settings_creates_file(tmp_path):
    p = write_home_settings(tmp_path, memory_enabled=True)
    assert p == tmp_path / ".claude" / "settings.json"
    assert p.exists()


def test_memory_dir_slug_matches_claude_adapter(tmp_path):
    repo = tmp_path / "my repo!"
    d = memory_dir(tmp_path, repo)
    assert d.name == "memory"
    assert "-my-repo-" in d.parent.name


def test_memory_files_excludes_index(tmp_path):
    repo = tmp_path / "r"
    d = memory_dir(tmp_path, repo)
    d.mkdir(parents=True)
    (d / "MEMORY.md").write_text("index")
    (d / "fact.md").write_text("---\ndescription: x\n---\nbody")
    assert [f.name for f in memory_files(tmp_path, repo)] == ["fact.md"]
