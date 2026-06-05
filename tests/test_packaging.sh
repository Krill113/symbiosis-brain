#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
rm -rf dist
uv build --wheel >/dev/null 2>&1 || python -m build --wheel >/dev/null 2>&1
WHL=$(ls dist/*.whl | head -1)
python -c "import zipfile,sys; z=zipfile.ZipFile('$WHL'); names=z.namelist(); assert any(n.endswith('symbiosis_brain/data/tool-routing.json') for n in names), names"
echo OK
