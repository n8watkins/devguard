// Builds the npm packages for one release from the release's files:
//   node packaging/npm.mjs <version> <dist-folder> <out-folder>
// <dist-folder> holds the devguard-<os>-<arch>.tar.gz builds and SHA256SUMS.
// <out-folder> gets one folder per package, ready for `npm publish`:
// agent-devguard-<os>-<arch> (one build each) and agent-devguard (the launcher),
// whose optionalDependencies pin every platform package to the same version.
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { chmodSync, copyFileSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const [version, dist, out] = process.argv.slice(2);
if (!/^\d+\.\d+\.\d+$/.test(version ?? "") || !dist || !out) {
	console.error("usage: node packaging/npm.mjs <version, e.g. 0.1.0> <dist-folder> <out-folder>");
	process.exit(2);
}

const sums = new Map(readFileSync(join(dist, "SHA256SUMS"), "utf8").trim().split("\n").map((line) => {
	const [hash, file] = line.split(/\s+/);
	return [file, hash];
}));

const shared = {
	version,
	license: "MIT",
	homepage: "https://github.com/n8watkins/devguard#readme",
	bugs: { url: "https://github.com/n8watkins/devguard/issues" },
	// npm checks provenance against this, so it must name the publishing repo.
	repository: { type: "git", url: "git+https://github.com/n8watkins/devguard.git" },
	author: "Nathan Watkins",
};

const platforms = [["linux", "x64"], ["linux", "arm64"], ["darwin", "x64"], ["darwin", "arm64"]];

rmSync(out, { recursive: true, force: true });
for (const [os, cpu] of platforms) {
	const archive = `devguard-${os}-${cpu}.tar.gz`;
	const name = `agent-devguard-${os}-${cpu}`;
	const dir = join(out, name);
	mkdirSync(dir, { recursive: true });
	// The smoke test builds this machine's platform only, and publishes empty
	// placeholders for the others (some package managers need every optional
	// dependency to exist). A release always has all four builds.
	if (process.env.DEVGUARD_NPM_PARTIAL === "1" && !sums.has(archive)) {
		mkdirSync(join(dir, "devguard"));
		writeFileSync(join(dir, "devguard", "placeholder"), "");
	} else {
		const hash = createHash("sha256").update(readFileSync(join(dist, archive))).digest("hex");
		if (sums.get(archive) !== hash) throw new Error(`${archive} does not match SHA256SUMS`);
		execFileSync("tar", ["-xzf", join(dist, archive), "-C", dir]);
	}
	writeFileSync(join(dir, "package.json"), `${JSON.stringify({
		name,
		...shared,
		description: `The devguard build for ${os === "darwin" ? "macOS" : "Linux"} ${cpu}. Install agent-devguard instead.`,
		os: [os],
		cpu: [cpu],
		files: ["devguard"],
		preferUnplugged: true,
	}, null, 2)}\n`);
	writeFileSync(join(dir, "README.md"), `# ${name}\n\nThe devguard build for this platform. Install [\`agent-devguard\`](https://www.npmjs.com/package/agent-devguard) instead; it picks this package for you.\n`);
}

const main = join(out, "agent-devguard");
mkdirSync(join(main, "bin"), { recursive: true });
copyFileSync(join(root, "packaging", "launcher.cjs"), join(main, "bin", "devguard.cjs"));
chmodSync(join(main, "bin", "devguard.cjs"), 0o755);
for (const doc of ["README.md", "LICENSE"]) copyFileSync(join(root, doc), join(main, doc));
writeFileSync(join(main, "package.json"), `${JSON.stringify({
	name: "agent-devguard",
	...shared,
	description: "Run many AI coding agents in parallel on one machine without running out of memory, ports or disk.",
	keywords: ["ai-agents", "dev-server", "worktree", "wsl", "memory", "ports", "claude-code", "codex"],
	bin: { devguard: "bin/devguard.cjs" },
	files: ["bin/devguard.cjs", "README.md", "LICENSE"],
	os: ["linux", "darwin"],
	optionalDependencies: Object.fromEntries(platforms.map(([os, cpu]) => [`agent-devguard-${os}-${cpu}`, version])),
}, null, 2)}\n`);

console.log(`npm packages for agent-devguard ${version} in ${out}`);
