"""Self-update for git checkouts: pull, reinstall when dependencies change, restart what was running."""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path

REPO_URL = "https://github.com/itsTylerIRL/middle-management.git"
DAEMON_UNIT = "middlemanagementd"
AUDIO_UNIT = "middlemanagementd-audio"

_EXTRA_RE = re.compile(r"""^([A-Za-z0-9][A-Za-z0-9._-]*).*;.*extra\s*==\s*["']([^"']+)["']""")


class UpdateError(RuntimeError):
    pass


def checkout() -> Path | None:
    """The git checkout this package runs from (an editable install), or None."""
    root = Path(__file__).resolve().parent.parent
    return root if (root / ".git").exists() and (root / "pyproject.toml").is_file() else None


def git(repo: Path, *args: str) -> str:
    try:
        res = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    except FileNotFoundError as e:
        raise UpdateError("git is not installed") from e
    except subprocess.CalledProcessError as e:
        raise UpdateError((e.stderr or e.stdout or str(e)).strip()) from e
    return res.stdout.strip()


def pending(repo: Path) -> list[str]:
    """Fetch, then list the upstream commits not yet in this checkout (oldest first)."""
    try:
        git(repo, "rev-parse", "--abbrev-ref", "@{u}")
    except UpdateError as e:
        raise UpdateError("this branch doesn't track a remote branch; check out `main`") from e
    git(repo, "fetch", "--quiet")
    log = git(repo, "log", "--reverse", "--format=%h %s", "HEAD..@{u}")
    return log.splitlines() if log else []


def pull(repo: Path) -> tuple[str, str, list[str]]:
    """Fast-forward to upstream. Returns (old, new, changed files)."""
    old = git(repo, "rev-parse", "--short", "HEAD")
    try:
        git(repo, "merge", "--ff-only", "@{u}")
    except UpdateError as e:
        raise UpdateError(f"couldn't fast-forward ({e.args[0].splitlines()[0]}). Local changes or commits are in "
                          f"the way; see `git -C {repo} status`.") from e
    new = git(repo, "rev-parse", "--short", "HEAD")
    changed = git(repo, "diff", "--name-only", old, new)
    return old, new, changed.splitlines()


def installed_extras(requires: Iterable[str], have: Callable[[str], bool]) -> list[str]:
    """Extras whose requirements are all installed, so a reinstall keeps them."""
    by_extra: dict[str, list[str]] = {}
    for req in requires:
        if m := _EXTRA_RE.match(req):
            by_extra.setdefault(m.group(2), []).append(m.group(1))
    return sorted(extra for extra, names in by_extra.items() if all(have(n) for n in names))


def _have(name: str) -> bool:
    try:
        version(name)
        return True
    except PackageNotFoundError:
        return False


def reinstall(repo: Path) -> None:
    extras = installed_extras(distribution("middlemanagementd").requires or [], _have)
    target = f"{repo}[{','.join(extras)}]" if extras else str(repo)
    res = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "-e", target])
    if res.returncode:
        raise UpdateError(f"pip install failed; retry with: {sys.executable} -m pip install -e '{target}'")


def unit_active(unit: str) -> bool:
    try:
        return subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit]).returncode == 0
    except FileNotFoundError:
        return False


def restart_units(units: list[str]) -> None:
    if units and subprocess.run(["systemctl", "--user", "restart", *units]).returncode:
        raise UpdateError(f"systemctl --user restart {' '.join(units)} failed")
