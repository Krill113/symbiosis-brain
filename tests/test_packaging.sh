#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
rm -rf dist
uv build --wheel >/dev/null 2>&1 || python -m build --wheel >/dev/null 2>&1
WHL=$(ls dist/*.whl | head -1)
python -c "import zipfile,sys; z=zipfile.ZipFile('$WHL'); names=z.namelist(); assert any(n.endswith('symbiosis_brain/data/tool-routing.json') for n in names), names"

python - "$WHL" <<'PY'
import sys
import zipfile
from pathlib import Path

from symbiosis_brain import install_cli

names = zipfile.ZipFile(sys.argv[1]).namelist()
registered = set(install_cli.SKILL_NAMES)
on_disk = {p.name for p in Path("skills").iterdir() if p.is_dir() and (p / "SKILL.md").exists()}
assert on_disk == registered, f"skills/ dirs {sorted(on_disk)} != SKILL_NAMES {sorted(registered)}"

def shipped(key):
    assert any(n.endswith(key) for n in names), f"missing in wheel: {key}"

for name in sorted(registered):
    shipped(f"symbiosis_brain/skills/{name}/SKILL.md")
for ref in ("action-rule-recipe.md", "automation-recipe.md"):
    shipped(f"symbiosis_brain/skills/brain-autolearn/references/{ref}")
# Hooks and slash commands are force-included the same way skills are, and a missing
# one is not cosmetic: cmd_setup raises on it and rolls the whole install back.
for hook in install_cli.HOOK_FILES_SH:
    shipped(f"symbiosis_brain/hooks/{hook}")
for cmd in install_cli.COMMAND_FILES:
    shipped(f"symbiosis_brain/commands/{cmd}")
print("skills, hooks and commands OK")
PY

echo OK
