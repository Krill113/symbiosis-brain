"""Hygiene guard for everything the package ships: skills/ and tests/data/.

Three invariants, all learned the hard way (lens D §D4, and CP-8a):

1. A skill that ships must not carry the maintainer's workspace with it — absolute
   Windows paths and e-mail addresses. (The shape of the maintainer's own tree is a
   private marker; per CP-8 those live in the local $SB_PUBLISH_AUDIT auditor, never
   in this repo — see the NOTE under FORBIDDEN.)
2. A skill that ships must not order the agent to invoke a skill that does NOT
   ship. `brain-save` Step 0 did exactly that; `brain-self-critique` stays the
   owner's personal skill by decision 2, so every mention of it has to be guarded.
3. Test DATA ships too. `pyproject.toml:59` puts the whole `tests` directory in
   the sdist, so the synthetic eval fixtures under `tests/data/` reach PyPI with
   the package. "We agreed to write synthetic fixtures" is a promise, not a
   mechanism — this file is the mechanism, and it uses the same pattern list.

The patterns below are deliberately generic: this file is part of a public repo and
must not itself contain a private name, path, or address.
"""
import re
from pathlib import Path

from symbiosis_brain import install_cli

WINDOWS_ABS_PATH = re.compile(r"[A-Za-z]:\\(Users|Repos)\\")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

FORBIDDEN = (
    ("windows absolute path", WINDOWS_ABS_PATH),
    ("e-mail address", EMAIL),
)

# NOTE: the maintainer's own workspace layout is NOT matched here. That pattern is a
# private marker, and CP-8 put private markers behind $SB_PUBLISH_AUDIT precisely so a
# public repo does not publish the shape of a private tree. Keeping it in this file
# would contradict the docstring two paragraphs up. The check still happens — in the
# local pre-push auditor (tools/hooks/pre-push + $SB_PUBLISH_AUDIT) and in the manual
# grep of §2.2 "Контроль обезличивания".

# Skills that are referenced by shipped text but are NOT part of the product.
NOT_SHIPPED = ("brain-self-critique",)
GUARD_PHRASE = "is installed"


def _shipped_skill_files() -> list[Path]:
    root = install_cli._packaged_skills_dir()
    return sorted(p for p in root.rglob("*.md") if p.is_file())


def test_shipped_skills_have_no_private_markers():
    assert _shipped_skill_files(), "no skill files found — check _packaged_skills_dir()"
    hits = []
    for path in _shipped_skill_files():
        rel = f"{path.parent.name}/{path.name}"
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for label, pattern in FORBIDDEN:
                if pattern.search(line):
                    hits.append(f"{rel}:{lineno} ({label})")
    assert not hits, "private markers in shipped skills: " + "; ".join(hits)


def test_shipped_skills_never_require_an_unshipped_skill():
    """Additive (beyond 00-plan): the private-marker scan would happily pass a
    product that tells the agent to invoke something the product does not install —
    which is defect D4 itself."""
    for name in NOT_SHIPPED:
        assert name not in install_cli.SKILL_NAMES, f"{name} is not meant to ship"
    offenders = []
    for path in _shipped_skill_files():
        rel = f"{path.parent.name}/{path.name}"
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for name in NOT_SHIPPED:
                if name in line and GUARD_PHRASE not in line:
                    offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        "unguarded reference to a skill that does not ship: " + ", ".join(offenders)
    )


# ---------- CP-8a: the same patterns over shipped test data ----------

TESTS_DATA_DIR = Path(__file__).resolve().parent / "data"


def _shipped_data_files() -> list[Path]:
    if not TESTS_DATA_DIR.is_dir():
        return []
    return sorted(p for p in TESTS_DATA_DIR.rglob("*") if p.is_file())


def test_shipped_test_data_has_no_private_markers():
    """`pyproject.toml:59` ships `tests` inside the sdist, so every fixture under
    tests/data lands on PyPI. Same FORBIDDEN list as the skills above — one list,
    two shipping surfaces, no second opinion about what counts as private."""
    files = _shipped_data_files()
    assert files, "tests/data/** is empty — CP-8a was supposed to add the eval set"
    hits = []
    for path in files:
        rel = path.relative_to(TESTS_DATA_DIR.parent).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # A binary fixture cannot carry a Windows path or an address as text;
            # skipping it is not a hole, and decoding it as latin-1 would only
            # invent line numbers nobody can act on.
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, pattern in FORBIDDEN:
                if pattern.search(line):
                    hits.append(f"{rel}:{lineno} ({label})")
    assert not hits, "private markers in shipped test data: " + "; ".join(hits)
