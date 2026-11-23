"""requirements.txt exists only so Railway installs the runtime dependencies; it must match pyproject.toml exactly."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _normalize(requirement: str) -> str:
    """Lowercase, drop whitespace, and treat - _ . in the package name as the same (PEP 503)."""
    compact = re.sub(r"\s+", "", requirement).lower()
    match = re.match(r"[a-z0-9._-]+", compact)
    assert match, f"cannot parse requirement: {requirement!r}"
    name = re.sub(r"[-_.]+", "-", match.group())
    return name + compact[match.end():]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _requirements_txt() -> list[str]:
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    stripped = (line.split(" #")[0].strip() for line in lines)
    return [line for line in stripped if line and not line.startswith("#")]


def test_requirements_txt_matches_project_dependencies():
    declared = sorted(_normalize(d) for d in _pyproject()["project"]["dependencies"])
    mirrored = sorted(_normalize(d) for d in _requirements_txt())
    assert mirrored == declared, (
        "requirements.txt and [project.dependencies] differ: "
        f"only in requirements.txt {sorted(set(mirrored) - set(declared))}, "
        f"only in pyproject.toml {sorted(set(declared) - set(mirrored))}"
    )


def test_python_version_satisfies_requires_python():
    pinned = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+", pinned), f".python-version should be major.minor, got {pinned!r}"
    requires = _pyproject()["project"]["requires-python"]
    minimum = re.fullmatch(r">=(\d+)\.(\d+)", requires.strip())
    assert minimum, f"this test only understands a '>=X.Y' requires-python, got {requires!r}"
    assert tuple(map(int, pinned.split("."))) >= tuple(map(int, minimum.groups()))
