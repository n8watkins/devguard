#!/usr/bin/env bash
# Build dist/devguard.pyz: one executable file that needs only python3 (3.11+).
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:-$root/dist/devguard.pyz}"
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
cp -R "$root/src/devguard" "$stage/"
find "$stage" -name __pycache__ -prune -exec rm -rf {} +
# zipapp's generated entry point drops the exit code, so write our own.
printf 'import sys\n\nfrom devguard.cli import main\n\nsys.exit(main())\n' >"$stage/__main__.py"
mkdir -p "$(dirname "$out")"
python3 -m zipapp "$stage" --python "/usr/bin/env python3" --output "$out"
echo "$out"
