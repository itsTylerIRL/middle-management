import subprocess

import pytest

from middlemanagementd.update import UpdateError, installed_extras, pending, pull


def sh(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True,
                   capture_output=True)


def commit(repo, name, text):
    (repo / name).write_text(text)
    sh(repo, "add", name)
    sh(repo, "commit", "-m", f"change {name}")


@pytest.fixture
def repos(tmp_path):
    """An upstream repo and a friend's clone of it."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    sh(upstream, "init", "-b", "main")
    commit(upstream, "pyproject.toml", "v1")
    sh(tmp_path, "clone", str(upstream), "clone")
    return upstream, tmp_path / "clone"


def test_pending_and_pull(repos):
    upstream, clone = repos
    assert pending(clone) == []
    commit(upstream, "README.md", "hello")
    commit(upstream, "pyproject.toml", "v2")
    new = pending(clone)
    assert [line.split(" ", 1)[1] for line in new] == ["change README.md", "change pyproject.toml"]
    old, head, changed = pull(clone)
    assert old != head and sorted(changed) == ["README.md", "pyproject.toml"]
    assert pending(clone) == []


def test_pull_refuses_diverged_history(repos):
    upstream, clone = repos
    commit(upstream, "a.txt", "theirs")
    commit(clone, "b.txt", "mine")
    pending(clone)
    with pytest.raises(UpdateError, match="couldn't fast-forward"):
        pull(clone)


def test_pending_needs_an_upstream(repos):
    _, clone = repos
    sh(clone, "checkout", "-b", "experiment")
    with pytest.raises(UpdateError, match="doesn't track"):
        pending(clone)


def test_installed_extras_keeps_complete_extras_only():
    requires = ["aiohttp>=3.9", 'faster-whisper>=1.0; extra == "audio"', 'piper-tts>=1.2; extra == "audio"',
                'pytest>=8.0; extra == "dev"', 'pytest-asyncio>=0.23; extra == "dev"']
    have = {"faster-whisper", "piper-tts", "pytest"}.__contains__
    assert installed_extras(requires, have) == ["audio"]
