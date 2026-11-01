# devguard

Run many AI coding agents in parallel on one machine without running out of memory, ports or disk.

```sh
devguard ps                          # every dev server: memory, port, branch, merged or not
devguard watch --install --apply     # every minute: stop runaway and finished dev servers
next dev -p $(devguard port)         # a stable port per worktree, no collisions between agents
devguard slot -- npx playwright test # at most 4 heavy jobs at once across all agents
devguard sweep                       # dry run: build output in finished worktrees you can delete
devguard report --since 7d           # how the watcher behaved and what to change
```

## Why

Coding agents (Claude Code, Codex, Cursor and others) each work in their own git worktree, and each one starts its own dev server, builds and browser tests.
On a laptop or a WSL2 machine this goes wrong in three ways:

- **Memory runs out.**
  A webpack `next dev` server grows to 4-6 GB within an hour, a `next build` wants 2-3 GB and every core, and five agents each start one.
  When memory runs out, Linux or WSL kills something, often the agents themselves, and everything dies at once.
- **Ports collide.**
  Every agent runs `next dev` on port 3000, so the second one fails or silently moves to 3001, and nobody knows which localhost URL belongs to which worktree.
- **The disk fills up.**
  Each worktree gets its own `node_modules`, `.next` and Cargo `target`, often several GB each, and nobody deletes them after the branch is merged.

devguard is one small command for all three.
It grew out of the scripts its author used every day to run four or five agents at once on a 20 GB WSL2 machine.

## Install

Needs Python 3.11 or newer and git.
It has no other dependencies.
It works on Linux and WSL2; most commands also work on macOS (see [Platform support](#platform-support)).

With [uv](https://docs.astral.sh/uv/) (it fetches a suitable Python for you, even on Ubuntu 22.04, which ships 3.10):

```sh
uv tool install git+https://github.com/n8watkins/devguard
```

Or with pipx:

```sh
pipx install git+https://github.com/n8watkins/devguard
```

Or as a single file, with no installer at all:

```sh
git clone https://github.com/n8watkins/devguard && cd devguard
./scripts/build-zipapp.sh                       # writes dist/devguard.pyz
install -m 755 dist/devguard.pyz ~/.local/bin/devguard
```

Then check it:

```sh
devguard config    # where the config, logs and slot locks live, and the limits in effect
devguard ps
```

### Why Python

devguard reads `/proc`, runs git and takes file locks, which the Python standard library does well.
Python 3 is already installed on every WSL distribution and nearly every Linux machine, so nothing else needs installing.
It stays stdlib-only (TOML config through `tomllib`), so it installs as a normal package, runs from a clone, or ships as one `.pyz` file.
A Bun or Go binary would start a little faster, but it would add a toolchain to build and release, and devguard's slowest step is git, not startup.

## Commands

Every command prints plain text by default.
`ps`, `worktrees`, `sweep` and `report` also take `--json` for scripts and agents.

### devguard ps

Lists every running dev server of your user: `next dev`, `next start`, `vite`, `vite preview`, `wrangler dev`, `astro`, `nuxt`, `remix`, `react-router`, `webpack serve` and `storybook dev`, plus any command you add in the config.

```console
$ devguard ps
Memory: 7.0G used of 19.5G, 12.5G available, 4.2G swap in use
    PID  SERVER    PORT   MEM  UPTIME  PROJECT  BRANCH       STATE    WORKTREE
2433380  next dev  4102  4.7G    1.2h  app      feat/login   open     ~/code/app-wt/login
2433379  next dev  4101  2.1G    3.0h  app      feat/search  merged   ~/code/app-wt/search
2433381  vite dev  5173  310M     12m  site     main         default  ~/code/site

STATE merged/gone/removed means the work is done; `devguard watch --apply` stops those servers.
```

MEM is the resident memory of the server and all its child processes.
PORT comes from `-p`/`--port`, then from the sockets the process tree listens on, then from `$PORT`.
STATE is one of:

| State | Meaning |
| --- | --- |
| `merged` | The branch was pushed and is now merged into origin's default branch. |
| `gone` | The branch was deleted on origin, which usually happens after a squash merge. |
| `removed` | The server's worktree directory no longer exists. |
| `open` | The branch has its own upstream and is not merged. |
| `unpushed` | The branch was never pushed to a branch of its own, so devguard cannot know it is merged and treats it as active. |
| `default` | The server runs on `main` or `master`. |

`--fetch` runs `git fetch --prune origin` first so the merged state is current.
`--all` ignores `watch.roots`.

### devguard watch

Applies a memory and cleanup policy to running dev servers.
On each pass it:

1. stops dev and preview servers whose worktree was removed or whose branch is merged or gone (`stop_finished`);
2. warns about a dev server above `soft_gb` and stops one above `hard_gb`;
3. when available memory is below `floor_gb`, stops the largest dev server that uses more than `min_victim_gb`.

Without `--apply` it stops nothing and logs what it would do:

```console
$ devguard watch --once
2026-11-01 09:48:43 WOULD-STOP port 4101 pid 2433379 next dev 2.1G ~/code/app-wt/search: its work is done (branch feat/search is merged into origin/main) (dry run; use --apply to stop)
2026-11-01 09:48:43 WOULD-STOP port 4102 pid 2433380 next dev 5.4G ~/code/app-wt/login: over the 5 GiB limit (dry run; use --apply to stop)

$ devguard watch --once --apply
2026-11-01 09:48:44 STOP port 4101 pid 2433379 next dev 2.1G ~/code/app-wt/search: its work is done (branch feat/search is merged into origin/main)
```

A stopped server gets SIGTERM, then SIGKILL after `grace_seconds`.
Restart it by hand when you need it again.
Every event goes to `<state_dir>/watch.log`; `devguard watch --log` shows the latest ones.

Repeated lines are rate-limited, so the log stays readable when the timer runs every minute: a server's `WARN` (or `WOULD-STOP` in a dry run) is logged once, then again only when it has grown by 0.5 GiB or 15 minutes have passed.
The skipped repeats are still printed, just not written to the log.

When a dev server in a finished worktree keeps coming back (stopped 3 times within an hour), something is restarting it, usually an agent.
Instead of stopping it every minute forever, watch logs one `LOOP` line and leaves finished servers in that directory alone for an hour.
The memory limits and the floor still apply to them.

Each pass also records a small sample in `<state_dir>/metrics.jsonl`; see [Monitoring](#monitoring).

| Flag | What it does |
| --- | --- |
| `--once` | One pass, then exit. Without it, watch checks every `interval_seconds` until Ctrl-C. |
| `--apply` | Really stop servers. |
| `--install` | Install a systemd user timer that runs `devguard watch --once` every minute. It runs in dry-run mode unless you also pass `--apply`. |
| `--uninstall` | Remove the timer. |
| `--log` | Show recent events. |

A good way to start: `devguard watch --install`, read `devguard watch --log` for a day, then `devguard watch --install --apply`.

### devguard port

Prints a stable port for a directory, so every worktree always gets the same localhost URL and parallel agents never pick the same one.

```console
$ cd ~/code/app-wt/login
$ devguard port
4567
$ next dev -p $(devguard port)
$ PORT=$(devguard port) npm run dev
```

The port is `base + (first 8 hex digits of sha1(absolute path)) mod range`, which is 4000-4999 with the defaults.
The path is the directory as you typed it (like `pwd`, symlinks are not resolved), so a monorepo's `apps/web` and `apps/docs` get different ports.
If something already listens on the port, devguard says so on stderr and still prints it: usually that is your own server and you can reuse it.

Two directories can land on the same port.
With 1,000 ports, the chance that any two of 10 worktrees collide is about 4%, and about 17% for 20 worktrees.
`devguard port --list [repo]` shows every worktree's port and flags collisions; fix one with a `[port.overrides]` entry.
`--write` (or `port.write_file = true`) also writes the port to `<dir>/.dev-port`.

### devguard slot

Runs a heavy command (a build, a Playwright run, a Lighthouse audit) only when one of N global slots is free, so five agents do not start five builds at once.

```console
$ devguard slot -- npx playwright test
$ devguard slot -n 2 -- next build
devguard slot: all 2 slots are busy, waiting (see `devguard slot --status`)
$ devguard slot --status
slot 1: 2440121 1761990000 next build
slot 2: 2440380 1761990012 npx playwright test
slot 3: free
slot 4: free
```

Each slot is a lock file held with `flock`.
Once devguard holds a slot it replaces itself with your command (`exec`), so:

- the exit code and signals are the command's own; Ctrl-C and `kill` reach it directly;
- the slot stays taken while the command or any child that inherited the lock runs, and the kernel frees it when they exit, even after `kill -9`;
- there is no pid file to go stale and no `pgrep` polling (a shell loop like `until ! pgrep -f "next build"` matches its own command line and never ends).

The number of slots comes from `-n`, then `$DEVGUARD_SLOTS`, then `slot.count` (default 4).
`--timeout SECONDS` gives up waiting and exits with 75.
A server started in the background by the command (and never stopped) keeps the slot until it exits.

Pick N by your bottleneck: one slot per 2-4 GB of memory you can spare, and no more slots than cores divided by two, because builds use every core.

### devguard worktrees

One table for every worktree of a repository.

```console
$ devguard worktrees ~/code/app
WORKTREE              BRANCH       DIRTY  AHEAD/BEHIND  MERGED    PUSHED
~/code/app (primary)  main             0         +0/-0  default   yes
~/code/app-wt/search  feat/search      0         +3/-2  merged    yes
~/code/app-wt/login   feat/login       4         +7/-0  open      NO
~/code/app-wt/spike   spike            0         +2/-0  unpushed  tracks-default

Ahead/behind is against origin/main. MERGED: merged = merged into origin/main; gone = deleted on origin (usually squash-merged); unpushed = never pushed, so unknown; open = not merged.
```

PUSHED is `yes`, `NO` (local commits not on the upstream), `no-upstream`, `tracks-default` (created from `origin/main` and never pushed to a branch of its own) or `gone`.

### devguard sweep

Finds build output in finished worktrees and, with `--apply`, deletes it.

```console
$ devguard sweep ~/code/app
keep   ~/code/app [main]: primary worktree (use --include-primary to allow)
keep   ~/code/app-wt/login [feat/login]: branch feat/login is not merged; 4 uncommitted or untracked change(s)
sweep  ~/code/app-wt/search [feat/search]
         del     1.1G  node_modules
         del     640M  apps/web/.next

Would delete 2 directories, 1.7G.
Dry run: nothing was deleted. Run again with --apply to delete.

$ devguard sweep --apply ~/code/app
DELETED  1.1G  ~/code/app-wt/search/node_modules
DELETED  640M  ~/code/app-wt/search/apps/web/.next

Freed 1.7G. Receipt: ~/.local/state/devguard/sweep/20261101T174845Z-2433694.json
```

Artifact directories are `node_modules`, `.next`, `dist`, `build`, `out`, `.turbo`, `target`, `.svelte-kit`, `.nuxt`, `.output`, `.parcel-cache`, `.vite` and `coverage` by default, found up to 4 levels below each worktree root.
The worktree itself, its source files and its git history are never touched.
See [Safety guarantees](#safety-guarantees) for every check.

With no repository argument, sweep uses `sweep.repos` from the config, or the repository you are in.
`--include-primary` also considers the primary worktree (it still has to pass every other check).
`--verbose` measures and lists the artifacts of kept worktrees too.
Hard-linked files (for example from a pnpm store) are counted and reported separately, since deleting them frees less than their size.

### devguard config

`devguard config` prints the config path, state dir, slot dir and the limits in effect.
`devguard config --default` prints the fully commented default config, a good starting point:

```sh
mkdir -p ~/.config/devguard
devguard config --default > ~/.config/devguard/config.toml
```

## Monitoring

devguard keeps a record of every watch pass, so you can see how the watcher behaves over time and tune it.

### What is recorded

Each pass appends one JSON line to `<state_dir>/metrics.jsonl`:
the time, total and available memory, swap in use, the limits in effect, and for every dev server its kind, tool, port, project, worktree (shortened to `~/...`), subdirectory, branch and state, memory of its process tree, uptime and whether it is memory-exempt.
It also lists the actions of that pass (warn, stop, would-stop, or hold during a restart loop) with their reason, and whether the pass was a dry run.

```json
{"t":1793300013,"mode":"apply","mem":{"total":19.5,"avail":6.2,"swap":1.1},"lim":{"soft":3.0,"hard":5.0,"floor":1.5},"srv":[{"pid":2433380,"kind":"dev","tool":"next","port":"4102","proj":"app","wt":"~/code/app-wt/login","state":"open","gb":3.4,"up":1260,"dir":"apps/web","branch":"feat/login"}],"act":[{"verb":"warn","pid":2433380,"rule":"soft","why":"over 3 GiB, restart it soon"}]}
```

Numbers are rounded to keep lines small, typically 200-300 bytes per running server.
The file is created with mode 600.
It is rotated once a day (or at 2 MB) into `metrics.jsonl.<timestamp>`; files older than 30 days are deleted, and the oldest go first when all of them pass 20 MB.
A failed write (full disk, read-only state dir) prints a message and never stops the watcher.
Turn it off with `watch.metrics = false`.

### devguard report

`devguard report` reads the metrics (and, for times they do not cover, `watch.log`) and summarizes a period, the last 7 days by default:

- passes recorded, time spent under the memory floor and near it (less than 1 GiB above it), lowest available memory, peak use and peak swap;
- how many dev servers run at once (median and peak), and how many ran when memory fell below the floor;
- warnings and stops by reason: hard limit, floor, branch merged, branch deleted on origin, worktree removed;
- repeat offenders: the apps (project plus subdirectory, the same across worktrees) that pass `soft_gb` or hit `hard_gb` most, with their typical peak and how long after start they passed each limit;
- stops followed by a new server in the same directory within 10 minutes, a sign that the stop was disruptive, and stop-restart loops (3 or more stops, each within 30 minutes of the last);
- peaks of memory-exempt servers, and the most expensive projects in GiB-hours;
- **What to change**: a few rule-based suggestions, each with the numbers behind it, shown only when the data supports them.

| Flag | What it does |
| --- | --- |
| `--since 7d` | The period: a number with `m`, `h`, `d` or `w`. |
| `--json` | Everything as JSON. |
| `--import-dev-watch PATH` | Also read the log of the older `dev-watch` script (its `WARN` and `STOP` lines), marked as imported. It has events only, no memory samples, so the memory sections cover devguard's own passes. The file is only read. |

An example from a machine where agents run several worktrees of a Next.js monorepo:

```console
$ devguard report --since 14d --import-dev-watch ~/.local/state/dev-watch/log
devguard report: 2026-10-18 09:00 to 2026-11-01 09:00
Limits: soft 3 GiB, hard 5 GiB, floor 1.5 GiB
Watch passes recorded: 8640 (8640 apply), 2026-10-26 09:01 to 2026-11-01 09:00
Events: 214 devguard, 2026-10-26 10:12 to 2026-11-01 08:41
Events: 409 imported from dev-watch log ~/.local/state/dev-watch/log (events only, no memory samples), 2026-10-19 10:42 to 2026-10-26 08:31

Memory
  under the floor:  14m (14 passes)
  near the floor:   3.1h (186 passes with less than floor + 1 GiB available)
  lowest available: 1.1G; peak used 18.4G; peak swap 4.9G
  dev servers at once: median 3, peak 7; median 6 while under the floor; median 6 at floor stops

Actions: events (distinct servers)
  ACTION                             DEVGUARD  DEV-WATCH (IMPORTED)
  warned: over soft_gb               161 (24)  343 (31)
  stopped: branch merged               21 (9)  22 (22)
  stopped: over hard_gb                12 (12)  16 (16)
  stopped: branch deleted on origin      6 (6)  13 (13)
  stopped: memory below floor_gb         5 (5)  8 (8)
  stopped: worktree removed              2 (2)  7 (7)
  restart loop: left running             2 (2)  -

Repeat offenders (dev servers over soft_gb)
  APP                    RUNS>SOFT  LOG>SOFT  HARD  FLOOR  PEAK  TO-SOFT  TO-HARD  WORKTREES
  app apps/hub               19/23        23    21      9  5.1G       9m      41m  12
  app apps/web                7/31         8     5      0  4.2G      52m     2.4h  9
  site                        1/12         1     2      0  3.4G      3.1h       -  2
  RUNS>SOFT: runs over soft_gb / runs seen in metrics. LOG>SOFT: servers over soft_gb in log-only data.
  PEAK: typical peak of those servers. TO-SOFT, TO-HARD: median uptime when they passed the limit.

Stops followed by a restart in the same place within 10 minutes
  31 of 112 stops
   10 of  12  ~/code/app-wt/dino (10 merged)
    6 of   8  ~/code/app-wt/booking/apps/hub (6 hard)
  Stop-restart loops (3+ stops, each within 30 minutes of the last):
     10 stops in  64m  ~/code/app-wt/dino (merged, 2026-10-26 18:53)
      8 stops in  68m  ~/code/app-wt/booking/apps/hub (hard, 2026-10-27 16:05)
  devguard detected 2 loop(s) and left those servers running (LOOP lines in watch.log).

Memory-exempt servers
  app apps/hub: 6 runs, peak 7.2G, median peak 6.1G

Most expensive projects (GiB-hours of dev server memory)
    612.4   81%  app
    143.0   19%  site

What to change
  - app apps/hub passed 3 GiB in 19 of 23 runs, median 9m after start: try NODE_OPTIONS=--max-old-space-size=3072 or Turbopack (`next dev --turbopack`).
    evidence: typical peak 5.1 GiB, 21 hard-limit stops, 12 worktrees
  - Floor stops happened with 6+ dev servers running: lower slot.count or the number of parallel agents, or raise WSL's memory cap (memory= in .wslconfig).
    evidence: 13 floor stops; median 3 servers otherwise, peak 7
  - 6 stops of ~/code/app-wt/booking/apps/hub were followed by a restart within 10 minutes: consider raising hard_gb or adding it to watch.protected_paths.
    evidence: 6 of 8 stops there were restarted soon after
  - ~/code/app-wt/dino was stopped 10 times in 64 minutes after its work was done: something keeps restarting it; remove the worktree or find what restarts it.
    evidence: stop-restart loop, reason merged, dev-watch data
  - 71 of 112 stops were dev servers left running in finished worktrees: have agents stop their dev server when the branch merges, and remove merged worktrees.
    evidence: stopped: branch merged: 43, stopped: branch deleted on origin: 19, stopped: worktree removed: 9
```

The suggestions it can make:

| Suggestion | Fires when |
| --- | --- |
| Cap an app's heap (`NODE_OPTIONS=--max-old-space-size=3072`) or try Turbopack | An app passed `soft_gb` in at least half of 3 or more runs, or hit `hard_gb` 3 or more times. |
| Run fewer servers or agents at once, or raise WSL's memory cap | 2 or more floor stops happened with at least 4 dev servers running (and 2 more than usual), or 3 or more floor stops in log-only data. |
| Raise `hard_gb` or protect a directory | 2 or more hard-limit or floor stops in one directory were followed by a restart within 10 minutes. |
| Remove the worktree or find what restarts it | A finished worktree's server was stopped 3 or more times, each within 30 minutes of the last. |
| Stop dev servers when a branch merges, remove merged worktrees | At least 5 stops, and half or more of them, were for finished work. |
| Rate-limit warnings | 10 or more warnings in a row were logged for one server (the old dev-watch did this; devguard now rate-limits). |
| Turn on `--apply` | Watch ran only in dry run and would have stopped servers. |

## Configuration

The config file is `~/.config/devguard/config.toml` (or `$XDG_CONFIG_HOME/devguard/config.toml`, or `$DEVGUARD_CONFIG`, or `--config PATH`).
Every key is optional.
An unknown key is an error, so a typo never silently falls back to a default.

| Key | Default | Meaning |
| --- | --- | --- |
| `state_dir` | `~/.local/state/devguard` | Watch log, sweep receipts and fetch timestamps. Empty means `$XDG_STATE_HOME/devguard`. |
| `watch.soft_gb` | `3.0` | Log a warning when one dev server's process tree passes this many GiB. |
| `watch.hard_gb` | `5.0` | Stop a dev server whose process tree passes this. |
| `watch.floor_gb` | `1.5` | When available memory falls below this, stop the largest dev server... |
| `watch.min_victim_gb` | `1.0` | ...if it uses more than this. |
| `watch.stop_finished` | `true` | Stop servers whose worktree is gone or whose branch is merged or gone. |
| `watch.grace_seconds` | `10` | Seconds between SIGTERM and SIGKILL. |
| `watch.interval_seconds` | `60` | Seconds between passes for `devguard watch` without `--once`. |
| `watch.fetch_interval_minutes` | `10` | Minimum minutes between `git fetch --prune origin` per repository. `0` never fetches. |
| `watch.roots` | `[]` | Only manage servers whose working directory is under one of these paths. Empty means all of yours. |
| `watch.protected_paths` | `[]` | Never stop servers under these paths. |
| `watch.memory_exempt_env` | `[]` | Dev servers whose environment has one of these variables skip `soft_gb` and `hard_gb`. The floor still applies. Linux only. |
| `watch.metrics` | `true` | Append one sample per pass to `<state_dir>/metrics.jsonl` for `devguard report` (see [Monitoring](#monitoring)). |
| `port.base` | `4000` | First port. |
| `port.range` | `1000` | Number of ports. |
| `port.write_file` | `false` | Also write `<dir>/.dev-port`. |
| `port.overrides` | `{}` | Fixed ports, as `"absolute path" = port`. |
| `slot.count` | `4` | Number of global slots. |
| `slot.dir` | `$XDG_RUNTIME_DIR/devguard-slots` | Lock directory, or `/tmp/devguard-slots-<uid>` without `XDG_RUNTIME_DIR`. |
| `sweep.repos` | `[]` | Repositories to sweep by default. |
| `sweep.artifacts` | see above | Directory names that count as build output. |
| `sweep.max_depth` | `4` | How deep to look below each worktree root. |
| `sweep.min_idle_hours` | `24` | Skip worktrees whose git index or artifacts changed more recently. |
| `[[servers.custom]]` | none | Extra server commands: `name`, `kind` (`"dev"` or `"preview"`) and `match`, a list of strings that must all appear in the command line. |

An example for a machine where a test stack runs dev servers that need more memory during a full run, and where agents work under `~/code`:

```toml
[watch]
hard_gb = 6
memory_exempt_env = ["E2E_STACK_URL"]   # the test stack sets this; it stops its own servers
roots = ["~/code"]
protected_paths = ["~/code/infra"]

[port.overrides]
"~/code/app-wt/login" = 4601            # collided with another worktree

[slot]
count = 3

[sweep]
repos = ["~/code/app", "~/code/site"]

[[servers.custom]]
name = "django"
kind = "dev"
match = ["manage.py", "runserver"]
```

## WSL notes

**Turn on systemd** for `devguard watch --install`.
Add this to `/etc/wsl.conf` (create it if needed):

```ini
[boot]
systemd=true
```

Then run `wsl.exe --shutdown` from Windows and open a new WSL terminal.
`devguard watch --install` checks this and prints these steps when systemd is off.
To keep the timer running when no WSL terminal is open, run `loginctl enable-linger $USER`.
Without systemd, run `devguard watch --apply` in a spare terminal instead.

**WSL's memory is capped.**
By default WSL2 may use up to half of Windows' memory; `memory=` in `%UserProfile%\.wslconfig` changes the cap.
devguard's floor uses Linux's `MemAvailable`, which is what is left inside that cap, not what Windows has free.
When Windows itself is busy (browsers, IDEs), it can run short before WSL reaches its cap, so keep the cap a few GB below what Windows can really spare rather than lowering `floor_gb`.
`autoMemoryReclaim=dropcache` (or `gradual`) in the `[experimental]` section of `.wslconfig` returns cached memory to Windows sooner.

**Windows processes are invisible.**
devguard sees only Linux processes, so a dev server started from PowerShell or a Windows terminal is not listed or stopped.

**Keep worktrees on the Linux filesystem.**
Worktrees under `/mnt/c` are slow for git, file watching and sweep's size measurement.

## Platform support

| | Linux | WSL2 | macOS |
| --- | --- | --- | --- |
| `ps`, `watch --once`, `watch` | yes (`/proc`) | yes | yes, through `ps` and `lsof`, with approximate argument splitting |
| `watch.memory_exempt_env` | yes | yes | no: macOS does not expose other processes' environments |
| `watch --install` | yes (systemd user timer) | yes, with systemd turned on | no: run `devguard watch --once --apply` from launchd or cron |
| `port`, `slot`, `worktrees` | yes | yes | yes |
| `sweep` | yes | yes | yes; mount detection uses device numbers only (Linux also reads `/proc/self/mountinfo`) |

Windows outside WSL is not supported.

## Safety guarantees

devguard is meant to run unattended, so it is conservative by default.

**watch**

- Without `--apply` it never sends a signal, and `--install` sets up a dry-run timer unless you pass `--apply` too.
- It only considers processes of your own user, and never itself or its parent processes.
- A pid is signalled only while its start time still matches what devguard measured, so a reused pid is never hit.
- A branch counts as merged only after it was pushed to its own upstream; a fresh branch cut from `main` is an ancestor of `main` too, and is left alone.
- `main`, `master` and detached HEADs are never finished.
- `watch.roots` limits it to your agents' directories and `watch.protected_paths` excludes some entirely.
- Its metrics and state files are private (mode 600), and failing to write them never stops a pass.
- `devguard report` only reads; `--import-dev-watch` never changes the imported log.

**sweep**

A worktree is swept only when all of these hold:

- it is not the primary worktree (unless `--include-primary`) and not locked with `git worktree lock`;
- its branch is merged into origin's default branch, or was deleted on origin;
- it has no uncommitted or untracked changes;
- no process of yours runs in it, judged by working directory and by path arguments; processes that hide their working directory (non-dumpable ones like `gpg-agent`) are judged by their arguments, and if the process list cannot be read at all, nothing is swept;
- its git index and the artifact have not changed for `min_idle_hours`.

An artifact directory is deleted only when all of these hold:

- it is a real directory, not a symlink and not under a symlinked parent (its real path equals its path), and inside the worktree's real path;
- git ignores it and it contains no tracked files;
- it is not a mount point and holds no other filesystem and no nested `.git`.

Right before deleting, devguard checks all of the above again against a fresh process list and confirms the directory's device and inode are unchanged since it was measured.
It then renames the directory to a sibling `.devguard-sweep-*` quarantine (an atomic, no-replace rename on Linux) and deletes it through directory file descriptors that never follow symlinks and refuse to cross into another filesystem.
A symlink inside an artifact is removed as a link; its target is never touched.
An interrupted sweep leaves a quarantine, which does not count as a dirty change, and the next sweep finishes it.
Only one `sweep --apply` runs at a time, and each one writes a JSON receipt (the newest 50 are kept).

**slot**

- The lock directory is created with mode 0700, and devguard refuses one that is a symlink or belongs to another user.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. For `slot`, the command's own exit code is passed through instead. |
| `1` | A runtime error, a port collision in `port --list`, or a sweep action that was refused or failed. |
| `2` | Invalid arguments or config. |
| `75` | `slot --timeout` expired before a slot was free. |
| `126`, `127` | `slot` could not run the command, or did not find it. |

## Coming from the earlier scripts

devguard replaces a handful of personal scripts; if you used them, this is the mapping.

| Script | devguard |
| --- | --- |
| `dev-watch status` | `devguard ps` |
| `dev-watch enforce` | `devguard watch --once --apply` |
| `dev-watch log` | `devguard watch --log` |
| `~/.local/state/dev-watch/log` | `devguard report --import-dev-watch ~/.local/state/dev-watch/log` reads it as a baseline |
| `dev-watch.timer` | `devguard watch --install --apply` (disable the old timer first so two watchers do not compete) |
| `~/.config/dev-watch.conf` `SOFT_GB` etc. | `[watch]` `soft_gb`, `hard_gb`, `floor_gb`, `min_victim_gb` |
| `dev-port.sh` | `devguard port` (same numbers; `.dev-port` is now opt-in with `--write`) |
| `browser-slot <cmd>` | `devguard slot -- <cmd>` (`BROWSER_SLOTS` becomes `DEVGUARD_SLOTS`) |
| `worktree-status.sh` | `devguard worktrees` (MERGED no longer reports fresh branches as merged) |
| `rust-storage clean` | `devguard sweep` for any build output, with the same safety rules |

## Prior art

Phaedrus Raznikov's [mistystack](https://github.com/misty-step/mistystack) has a related postmortem, [the engineer fleet was one oomd victim](https://github.com/misty-step/mistystack/blob/master/docs/postmortems/2026-09-26-shared-terminal-oom.md): systemd-oomd killed the terminal that held a whole fleet of agents at once.
Its fix moves the fleet into its own systemd slice and admits heavy jobs through fixed, memory-bounded job scopes.
devguard takes a different route to the same problem: it watches and stops individual dev servers, and limits heavy jobs with file locks, which also works on WSL2 where systemd is often off.
devguard was written independently from its author's own scripts; the postmortem is worth reading for the cgroup side of the problem.

## Development

```sh
cd tests && python3 -m unittest discover -s .   # unit tests, no dependencies
./demo/e2e.sh                                   # end-to-end walk-through in a temp directory
DEVGUARD_BIN=dist/devguard.pyz ./demo/e2e.sh    # the same against a built zipapp
uvx ruff check src tests && uvx ruff format --check src tests
```

The tests use a fake `/proc` tree, temporary git repositories with real worktrees, and disposable processes; they never touch your real dev servers or files.
The demo builds a git origin with worktrees in a temp directory, starts processes that look like `next dev`, and lets devguard find, stop and clean up after them; `watch.roots` keeps it from seeing anything else on the machine.

## License

[MIT](LICENSE), Nathan Watkins.
