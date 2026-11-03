#!/usr/bin/env bash
# Checks the npm packages the way users install them: builds them from release
# files, publishes them to a throwaway local registry (Verdaccio), then installs
# and runs devguard with npm, pnpm, yarn and bun.
#   packaging/smoke-npm.sh <dist-folder>
# <dist-folder> holds devguard-<os>-<arch>.tar.gz builds (at least this
# machine's) and their SHA256SUMS.
set -euo pipefail

dist="$(cd "$1" && pwd)"
here="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
port=4874
registry="http://127.0.0.1:$port"
trap 'kill "$verdaccio" 2>/dev/null || true; rm -rf "$work"' EXIT

version="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$here/pyproject.toml")"
DEVGUARD_NPM_PARTIAL=1 node "$here/packaging/npm.mjs" "$version" "$dist" "$work/out" >/dev/null

mkdir -p "$work/registry"
cat >"$work/registry/config.yaml" <<YAML
storage: $work/registry/storage
uplinks: { npmjs: { url: https://registry.npmjs.org/ } }
packages:
  'agent-devguard*': { access: \$all, publish: \$all }
  '**': { access: \$all, proxy: npmjs }
max_body_size: 200mb
log: { type: stdout, format: pretty, level: error }
YAML
npx -y verdaccio@6.1.6 --config "$work/registry/config.yaml" --listen "127.0.0.1:$port" >"$work/registry.log" 2>&1 &
verdaccio=$!
for _ in $(seq 60); do curl -sf "$registry/-/ping" >/dev/null && break; sleep 1; done
curl -sf "$registry/-/ping" >/dev/null || { cat "$work/registry.log"; echo "registry did not start"; exit 1; }

printf '//127.0.0.1:%s/:_authToken=smoke\n' "$port" >"$work/npmrc"
for package in "$work"/out/agent-devguard-* "$work/out/agent-devguard"; do
	(cd "$package" && npm publish --userconfig "$work/npmrc" --registry "$registry" >/dev/null 2>&1)
done

failed=0
check() {
	local name="$1" got="$2"
	if [[ "$got" == "devguard $version" ]]; then echo "ok    $name"; else echo "FAIL  $name: ${got:-no output}"; failed=1; fi
}
for manager in npm pnpm yarn bun; do
	if [[ "$manager" == bun ]] && ! command -v bun >/dev/null; then echo "skip  bun (not installed)"; continue; fi
	dir="$work/$manager"
	mkdir -p "$dir" && cd "$dir"
	echo '{"name":"smoke","version":"0.0.0","private":true}' >package.json
	printf 'registry=%s\n' "$registry" >.npmrc
	case "$manager" in
		npm) npm install agent-devguard >/dev/null 2>&1 ;;
		pnpm) npx -y pnpm@10.20.0 add agent-devguard >/dev/null 2>&1 ;;
		yarn) npx -y yarn@1.22.22 add agent-devguard --registry "$registry" >/dev/null 2>&1 ;;
		bun) BUN_CONFIG_REGISTRY="$registry" bun add agent-devguard >/dev/null 2>&1 ;;
	esac
	check "$manager install" "$(node_modules/.bin/devguard --version 2>&1 | head -n 1)"
done

cd "$work"
check "npx" "$(npx -y --registry "$registry" -p agent-devguard devguard --version 2>/dev/null | head -n 1)"
status=0; (cd "$work/npm" && node_modules/.bin/devguard slot -n 1 -- sh -c 'exit 7') >/dev/null 2>&1 || status=$?
[[ "$status" == 7 ]] && echo "ok    slot runs the command and passes its exit code" || { echo "FAIL  slot exit code: got $status, want 7"; failed=1; }

exit "$failed"
