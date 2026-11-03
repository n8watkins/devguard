#!/usr/bin/env node
// The `devguard` command from npm. The real program is the standalone build in
// the package for this platform (agent-devguard-<os>-<arch>), which npm, pnpm,
// yarn and bun install as an optional dependency. This file only starts it.
"use strict";

const { spawnSync } = require("node:child_process");
const { dirname, join } = require("node:path");

const name = `agent-devguard-${process.platform}-${process.arch}`;
let binary;
try {
	binary = join(dirname(require.resolve(`${name}/package.json`)), "devguard", "devguard");
} catch {
	const supported = ["linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64"];
	const here = `${process.platform}-${process.arch}`;
	console.error(supported.includes(here)
		? `devguard: the ${name} package is missing. Reinstall without --no-optional / --omit=optional, or use the installer: https://github.com/n8watkins/devguard#install`
		: `devguard: no build for ${here}; devguard runs on Linux and macOS (on Windows, inside WSL).`);
	process.exit(1);
}

const result = spawnSync(binary, process.argv.slice(2), { stdio: "inherit" });
if (result.error) {
	console.error(`devguard: could not start ${binary}: ${result.error.message}`);
	process.exit(1);
}
if (result.signal) process.kill(process.pid, result.signal);
process.exit(result.status ?? 1);
