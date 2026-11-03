#!/bin/sh
# Install devguard from a GitHub release: picks the build for this system,
# checks it against the release's SHA256SUMS (and its build provenance when gh
# is signed in), unpacks it to ~/.local/share/devguard and links
# ~/.local/bin/devguard to it. No Python needed.
#
#   curl -fsSL https://raw.githubusercontent.com/n8watkins/devguard/main/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- --version v0.1.0 --dir /usr/local/bin
set -eu

REPO="n8watkins/devguard"
version="${DEVGUARD_VERSION:-latest}"
dir="${DEVGUARD_INSTALL_DIR:-$HOME/.local/bin}"
home="${XDG_DATA_HOME:-$HOME/.local/share}/devguard"

usage() {
	cat <<EOF
Install devguard from a GitHub release.

Options:
  --version <tag>   a release tag such as v0.1.0 (default: the latest release)
  --dir <folder>    where to put the devguard command (default: ~/.local/bin)
  -h, --help        show this help

The same settings as environment variables: DEVGUARD_VERSION, DEVGUARD_INSTALL_DIR.
The program itself goes in \$XDG_DATA_HOME/devguard (default: ~/.local/share/devguard).
EOF
}

fail() { echo "devguard install: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
	case "$1" in
		--version) [ $# -ge 2 ] || fail "--version needs a tag, e.g. v0.1.0"; version="$2"; shift 2 ;;
		--dir) [ $# -ge 2 ] || fail "--dir needs a folder"; dir="$2"; shift 2 ;;
		-h|--help) usage; exit 0 ;;
		*) fail "unknown option $1 (see --help)" ;;
	esac
done

case "$(uname -s)" in
	Linux) os=linux ;;
	Darwin) os=darwin ;;
	*) fail "devguard runs on Linux and macOS (on Windows, install it inside WSL)" ;;
esac
case "$(uname -m)" in
	x86_64|amd64) arch=x64 ;;
	aarch64|arm64) arch=arm64 ;;
	*) fail "no devguard build for $(uname -m)" ;;
esac
asset="devguard-$os-$arch.tar.gz"

if [ -n "${DEVGUARD_DOWNLOAD_BASE:-}" ]; then
	base="$DEVGUARD_DOWNLOAD_BASE" # a mirror, or file:// in the packaging check
elif [ "$version" = latest ]; then
	base="https://github.com/$REPO/releases/latest/download"
else
	case "$version" in v*) ;; *) version="v$version" ;; esac
	base="https://github.com/$REPO/releases/download/$version"
fi

if command -v curl >/dev/null 2>&1; then
	fetch() { curl -fsL --retry 2 -o "$2" "$1"; }
elif command -v wget >/dev/null 2>&1; then
	fetch() { wget -q -O "$2" "$1"; }
else
	fail "needs curl or wget"
fi

if command -v sha256sum >/dev/null 2>&1; then
	sha256() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
	sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
	fail "needs sha256sum or shasum to check the download"
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT INT TERM

fetch "$base/$asset" "$tmp/$asset" || fail "could not download $asset ($version); check the tag at https://github.com/$REPO/releases"
fetch "$base/SHA256SUMS" "$tmp/SHA256SUMS" || fail "could not download SHA256SUMS ($version)"

expected="$(awk -v name="$asset" '$2 == name { print $1 }' "$tmp/SHA256SUMS")"
[ -n "$expected" ] || fail "SHA256SUMS has no line for $asset"
[ "$(sha256 "$tmp/$asset")" = "$expected" ] || fail "checksum mismatch for $asset; nothing was installed"
checks="checksum ok"

# Build provenance proves the build came from this repository's release
# workflow. It needs gh signed in; without it the checksum still applies.
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
	gh attestation verify "$tmp/$asset" --repo "$REPO" \
		--signer-workflow "$REPO/.github/workflows/release.yml" --deny-self-hosted-runners >/dev/null 2>&1 \
		|| fail "build provenance check failed for $asset; nothing was installed"
	checks="$checks, provenance ok"
else
	checks="$checks, provenance not checked (needs gh signed in)"
fi

tar -xzf "$tmp/$asset" -C "$tmp"
[ -x "$tmp/devguard/devguard" ] || fail "$asset does not contain devguard/devguard"
mkdir -p "$(dirname "$home")" "$dir"
rm -rf "$home.new"
mv "$tmp/devguard" "$home.new"
rm -rf "$home"
mv "$home.new" "$home"
ln -sf "$home/devguard" "$dir/devguard"
installed="$("$dir/devguard" --version 2>/dev/null || true)"
echo "Installed ${installed:-devguard} to $dir/devguard ($checks)."

case ":$PATH:" in
	*":$dir:"*) ;;
	*) echo "Add $dir to your PATH, e.g. echo 'export PATH=\"$dir:\$PATH\"' >> ~/.bashrc" ;;
esac
echo "Next: devguard config, then devguard ps"
