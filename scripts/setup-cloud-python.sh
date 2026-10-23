#!/usr/bin/env bash
set -euo pipefail
cd /workspace/py
if [[ ! -x /workspace/.venvs/aimt/bin/python ]]; then
    python -m venv /workspace/.venvs/aimt
fi
/workspace/.venvs/aimt/bin/python -m pip install --disable-pip-version-check \
    --cache-dir /workspace/.cache/pip -r python/requirements-dev.txt
QT_QPA_PLATFORM=offscreen XDG_CACHE_HOME=/workspace/.cache/aimt \
    /workspace/.venvs/aimt/bin/python -m pytest python/tests -q
