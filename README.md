# Herdr Pane Manager

A portable [Agent Skill](https://agentskills.io/) and Python CLI for managing local Herdr panes. It creates and groups panes, sends literal text, reads terminal snapshots, renames and closes panes, checks user locks, and exports the layout as JSON or HTML. The caller supplies the application, working directory, and input. No Hermes-specific tool is required by the CLI.

## Install

You need Python 3.10 or newer, `/bin/sh`, and a local [Herdr](https://herdr.dev/) installation on `PATH`. The CLI requires socket protocol 22; the tested release is [Herdr 0.9.5](https://github.com/herdrdev/herdr/releases/tag/v0.9.5). Start the intended Herdr session before using the CLI. The CLI does not start servers, and remote Herdr instances are not supported.

For Hermes, clone the repository into the active profile's skills directory. In a shell with the profile's `HERMES_HOME` set (or unset for the default profile):

```sh
skill_dir="${HERMES_HOME:-$HOME/.hermes}/skills/herdr-pane-manager"
mkdir -p "$(dirname "$skill_dir")"
git clone https://github.com/milyin/herdr-pane-manager.git "$skill_dir"
python3 "$skill_dir/scripts/pane_manager.py" --help
```

Start a new agent session to discover the skill. If the destination already contains a manual installation, back it up and move it aside before cloning. To update a Git installation, run `git -C "$skill_dir" pull --ff-only`.

For another Agent Skills-compatible agent, clone the repository as `herdr-pane-manager` into that agent's configured skills directory. Keep `SKILL.md`, `scripts/`, and `references/` together; installing only `SKILL.md` does not install the CLI. To use the CLI without an agent, clone it anywhere and invoke `python3 scripts/pane_manager.py` from the checkout.

## Use

Find the exact workspace ID in `herdr --session SESSION workspace list`. Replace `SESSION` and `WORKSPACE_ID` below with the desired running session and that ID. From the checkout:

```sh
python3 scripts/pane_manager.py create --instance SESSION --workspace WORKSPACE_ID \
  --cwd "$PWD" --group examples --suffix greeting --command 'printf "Hello from Herdr\n"'
python3 scripts/pane_manager.py layout --instance SESSION --workspace WORKSPACE_ID \
  --format html --output ./layout.html
```

`create` returns the pane ID for `send`, `read`, `rename`, `lock-status`, and `close`. See [the caller API](references/api.md) for arguments, paging, and response formats.

A zoomed pane stays readable but rejects input, renaming, and closing. The CLI never unzooms a pane. Herdr protocol 22 has no atomic check-and-mutate operation, so a simultaneous zoom change can race a mutation. This guard applies only to calls through this CLI.

## Tests and CI

Run `python3 -B tests/test_live.py -v` with Herdr on `PATH`. The suite creates two disposable headless Herdr sessions under an isolated configuration and tears them down; it does not use an existing workspace or require a TUI. It tests command launch and working directory, grouping and focus, literal input, output paging, zoom-lock enforcement, instance and workspace isolation, and layout exports.

[GitHub Actions](.github/workflows/ci.yml) runs the same suite on Ubuntu with Python 3.10 and 3.12. It verifies the SHA-256 and version of the pinned Herdr release binary before starting the tests.
