#!/usr/bin/env bash
# Build the standalone devguard for this machine with PyInstaller:
#   packaging/build-binary.sh [out-folder]
# Writes <out>/devguard-<os>-<arch>.tar.gz holding a devguard/ folder (the
# program and the Python runtime it needs; run devguard/devguard). One folder
# instead of one file, because a one-file build unpacks itself on every run,
# and `devguard port` runs inside `next dev -p $(devguard port)`.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
out="$(mkdir -p "${1:-$root/dist}" && cd "${1:-$root/dist}" && pwd)"
case "$(uname -s)" in Linux) os=linux ;; Darwin) os=darwin ;; *) echo "unsupported OS" >&2; exit 1 ;; esac
case "$(uname -m)" in x86_64|amd64) arch=x64 ;; aarch64|arm64) arch=arm64 ;; *) echo "unsupported CPU" >&2; exit 1 ;; esac
name="devguard-$os-$arch"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
# The same entry point as `python -m devguard`.
printf 'import sys\n\nfrom devguard.cli import main\n\nsys.exit(main())\n' >"$work/devguard_main.py"
uvx --python 3.13 pyinstaller@6.16.0 --noconfirm --clean --log-level WARN --onedir --name devguard \
	--paths "$root/src" --collect-submodules devguard \
	--distpath "$work/dist" --workpath "$work/build" --specpath "$work" "$work/devguard_main.py"
"$work/dist/devguard/devguard" --version
tar -C "$work/dist" -czf "$out/$name.tar.gz" devguard
echo "$out/$name.tar.gz"
