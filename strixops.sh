#!/bin/sh
# Portable entry point; all lifecycle operations live in the Python manager.

STRIXOPS_SCRIPT_ROOT=$(CDPATH= cd -P "$(dirname "$0")" && pwd -P) || exit 1

strixops_python_usable() {
    "$1" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' >/dev/null 2>&1
}

STRIXOPS_MANAGER_PYTHON=
for STRIXOPS_PYTHON_NAME in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9; do
    STRIXOPS_PYTHON_CANDIDATE=$(command -v "$STRIXOPS_PYTHON_NAME" 2>/dev/null) || continue
    if strixops_python_usable "$STRIXOPS_PYTHON_CANDIDATE"; then
        STRIXOPS_MANAGER_PYTHON=$STRIXOPS_PYTHON_CANDIDATE
        break
    fi
done

if [ -z "$STRIXOPS_MANAGER_PYTHON" ] && strixops_python_usable "$STRIXOPS_SCRIPT_ROOT/.venv/bin/python"; then
    STRIXOPS_MANAGER_PYTHON=$STRIXOPS_SCRIPT_ROOT/.venv/bin/python
fi

if [ -z "$STRIXOPS_MANAGER_PYTHON" ] && command -v uv >/dev/null 2>&1; then
    STRIXOPS_PYTHON_CANDIDATE=$(uv python find --no-project --no-python-downloads '>=3.9' 2>/dev/null) || STRIXOPS_PYTHON_CANDIDATE=
    if [ -n "$STRIXOPS_PYTHON_CANDIDATE" ] && strixops_python_usable "$STRIXOPS_PYTHON_CANDIDATE"; then
        STRIXOPS_MANAGER_PYTHON=$STRIXOPS_PYTHON_CANDIDATE
    fi
fi

if [ -z "$STRIXOPS_MANAGER_PYTHON" ]; then
    printf '%s\n' '找不到可用的 Python 3.9 或更新版本，无法运行 StrixOps 管理脚本。' \
        '请先安装 Python 并加入 PATH；安装和运行 StrixOps 本身需要 Python 3.12 或更新版本。' >&2
    exit 1
fi

exec "$STRIXOPS_MANAGER_PYTHON" "$STRIXOPS_SCRIPT_ROOT/scripts/strixops_manager.py" "$@"
