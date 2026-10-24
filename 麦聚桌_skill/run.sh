#!/bin/sh
set -eu
project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
cd "$project_dir"
mkdir -p runtime/tmp runtime/cache
export TMPDIR="$project_dir/runtime/tmp"
export XDG_CACHE_HOME="$project_dir/runtime/cache"
export PYTHONDONTWRITEBYTECODE=1
# Directly use the project Conda interpreter; do not activate/change base.
exec "$project_dir/.conda/bin/python" "$project_dir/mcdonald-lunch-table/scripts/mcp_cli.py" "$@"
