#!/bin/sh
set -eu
project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
cd "$project_dir"
mkdir -p runtime/tmp runtime/cache
export TMPDIR="$project_dir/runtime/tmp"
export XDG_CACHE_HOME="$project_dir/runtime/cache"
export PYTHONDONTWRITEBYTECODE=1
exec "$project_dir/.conda/bin/python" "$project_dir/maimate/scripts/preview_server.py" "$@"
