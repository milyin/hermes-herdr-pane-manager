# Hermes Herdr Pane Manager

A portable [Agent Skill](https://agentskills.io/) and Python CLI for managing local Herdr workspaces and panes. It creates and groups panes, sends literal text, reads terminal snapshots, renames and closes panes, checks user locks, and exports the layout as JSON or HTML. The caller supplies the application, working directory, and input. No Hermes-specific tool or network access is required by the CLI.

## Install

You need CPython 3.10 or newer and a local [Herdr](https://herdr.dev/) installation on `PATH`. The CLI supports the Linux socket and native Windows named pipe used by [Herdr 0.9.5](https://github.com/herdrdev/herdr/releases/tag/v0.9.5) (protocol 22). `/bin/sh` is only needed for POSIX `create --command`. Windows can launch native applications; WSL is only needed if you want to run Linux applications inside a Windows pane. Start the intended Herdr session before using the CLI. The CLI does not start servers, and remote Herdr instances are not supported.

For Hermes, clone the repository into the active profile's skills directory. For example, in a POSIX shell:

```sh
skill_dir="${HERMES_HOME:-$HOME/.hermes}/skills/hermes-herdr-pane-manager"
mkdir -p "$(dirname "$skill_dir")"
git clone https://github.com/milyin/hermes-herdr-pane-manager.git "$skill_dir"
python3 "$skill_dir/scripts/pane_manager.py" --help
```

On Windows, clone to the same relative location under the active Hermes profile. For example, with `HERMES_HOME` set in PowerShell:

```powershell
$skillDir = Join-Path $env:HERMES_HOME 'skills/hermes-herdr-pane-manager'
git clone https://github.com/milyin/hermes-herdr-pane-manager.git $skillDir
python (Join-Path $skillDir 'scripts/pane_manager.py') --help
```

If the destination already contains a manual installation, back it up and move it aside before cloning. To update a Git installation, run `git pull --ff-only` from the skill directory.

For another Agent Skills-compatible agent, clone the repository as `hermes-herdr-pane-manager` into that agent's configured skills directory. Keep `SKILL.md`, `scripts/`, and `references/` together; installing only `SKILL.md` does not install the CLI. To use the CLI without an agent, clone it anywhere and invoke `python scripts/pane_manager.py` from the checkout.

### Prevent local edits by Hermes

After cloning and before first use, lock the skill in the same Hermes profile where you installed it. Replace `PROFILE_NAME` with that profile's name:

```sh
hermes -p PROFILE_NAME curator lock hermes-herdr-pane-manager
```

If you use the default profile, omit `-p PROFILE_NAME`. The lock keeps the skill usable while preventing Hermes from editing its files during normal use or background review. It is a [Hermes policy lock](https://hermes-agent.nousresearch.com/docs/user-guide/features/curator#locking-a-skill), not a filesystem lock, so you can still update the checkout yourself with Git. Cloning the repository does not apply the lock; run the command separately, then start a new agent session to discover the skill.

## Use

Use the bundled script to find the exact workspace ID or create a new workspace. Replace `SESSION` with an explicit session name (`default` is valid), and `WORKSPACE_ID` with the returned ID. For example, on Linux:

```sh
python3 scripts/pane_manager.py workspaces --instance SESSION
python3 scripts/pane_manager.py workspace-create --instance SESSION --label examples --cwd "$PWD"
python3 scripts/pane_manager.py create --instance SESSION --workspace WORKSPACE_ID \
  --cwd "$PWD" --group examples --suffix greeting --command 'printf "Hello from Herdr\n"'
python3 scripts/pane_manager.py layout --instance SESSION --workspace WORKSPACE_ID \
  --format html --output ./layout.html
```

On Windows, use an argument vector instead of a shell command line. For example, in PowerShell:

```powershell
python scripts/pane_manager.py workspaces --instance SESSION
python scripts/pane_manager.py workspace-create --instance SESSION --label examples --cwd $PWD.Path
'["wsl.exe","--cd","~","--exec","top"]' | python scripts/pane_manager.py create --instance SESSION --workspace WORKSPACE_ID --cwd $PWD.Path --group examples --suffix monitor --argv-stdin
```

The pipe avoids Windows shell quoting problems when passing JSON; for non-ASCII data in Windows PowerShell set `$OutputEncoding = [System.Text.UTF8Encoding]::new($false)` before piping. A native executable such as `cmd.exe` can be the first argument instead of `wsl.exe`.

`create` returns the pane ID for `send`, `read`, `rename`, `lock-status`, and `close`. See [the caller API](references/api.md) for arguments, paging, and response formats.

A zoomed pane stays readable but rejects input, renaming, and closing. The CLI never unzooms a pane. Herdr protocol 22 has no atomic check-and-mutate operation, so a simultaneous zoom change can race a mutation. This guard applies only to calls through this CLI.

## Tests and CI

Run `python -B -m unittest discover -s tests -v` with Herdr on `PATH` (`python3` on Linux if needed). The suite creates two disposable headless Herdr sessions under an isolated configuration and tears them down. It checks the endpoint path before either server starts and never requires a TUI. The workflows under test call only the bundled script; the test fixture uses native Herdr only to start/stop its disposable servers and simulate a user zoom. On Windows, set `HERDR_TEST_WSL=1` to also run the cross-pane `top` → `/proc/<PID>/status` check with a WSL distribution that has Bash and procps `top`. To use a specific scratch parent for the isolated test configuration, set `HERDR_TEST_TMPDIR` to an existing local directory (use a short path on Linux because of the Unix socket path limit).

[GitHub Actions](.github/workflows/ci.yml) is configured to run the native suite on Ubuntu and Windows with Python 3.10 and 3.12, verifying the SHA-256 and version of the pinned Herdr release first. The WSL-specific test is opt-in and is not run in CI.
