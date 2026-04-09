---
name: "electron-app-driver"
description: "Drive Electron desktop apps locally with a manifest-based Playwright harness for screenshots and UI scenarios."
---

# Electron App Driver

Use this skill to validate Electron desktop app behavior during development:
- run named scenarios
- run ad-hoc action steps
- take screenshots
- inspect manifest resolution and Xvfb launch prerequisites

Path note:
- Script paths below are runtime paths for the installed skill under `$CODEX_HOME/skills/electron-app-driver`, not the checked-in canonical repo paths.
- When a script belongs to this skill, treat it as relative to this installed skill directory.
- Run these commands from the target workspace root so relative manifest paths like `agent_suite/.../manifest.json` resolve against the workspace you are testing.

Script:
- `python scripts/electron_app_driver.py --help`

## Prerequisite Check
- Before using this skill, first check whether `Xvfb` is installed with `command -v Xvfb`.
- If `Xvfb` is not installed, stop and ask the user to install it.
- Provide this exact install command to the user:
```bash
sudo apt-get update && sudo apt-get install -y xvfb
```

## Core commands
Run a named scenario:
```bash
python scripts/electron_app_driver.py \
  run \
  --manifest agent_suite/agent_secrets/e2e/manifest.json \
  --scenario app-launch
```

Run an actions file directly:
```bash
python scripts/electron_app_driver.py \
  step \
  --manifest agent_suite/agent_secrets/e2e/manifest.json \
  --actions agent_suite/agent_secrets/e2e/scenarios/select-first-profile.json
```

Take one screenshot:
```bash
python scripts/electron_app_driver.py \
  screenshot \
  --manifest agent_suite/agent_secrets/e2e/manifest.json \
  --name initial-shell
```

Inspect resolved manifest + Xvfb launch environment:
```bash
python scripts/electron_app_driver.py status \
  --manifest agent_suite/agent_secrets/e2e/manifest.json
```

## Manifest format
- Each app supplies a JSON manifest that explicitly defines:
  - `app_dir`
  - `cwd`
  - `electron_executable`
  - `electron_args`
  - optional `env`
  - optional `scenario_dir`
  - optional timeouts/output defaults

## Scenario format
- Scenario files are JSON action lists.
- Default scenario lookup path is `scenario_dir` from the manifest.
- Supported action types include:
  - `wait_for_timeout`
  - `wait_for_selector`
  - `wait_for_enabled`
  - `click`
  - `hover`
  - `fill`
  - `select_option`
  - `press`
  - `type`
  - `drag`
  - `scroll`
  - `screenshot`

## Notes
- This skill runs Electron locally through Playwright; it does not rely on Drawbridge.
- This installed skill carries its own `package.json` and `package-lock.json`; if `node_modules` is missing under the installed skill dir, repair the skill install before continuing.
- This skill is app-agnostic for Electron apps and now requires an explicit manifest per app.
- The official driver now launches apps inside `Xvfb` so automation stays off the user’s visible desktop.
- Default artifact output is worktree-local under `.workspace/playwright/electron/<manifest-name>` unless overridden.
- `--out-dir` still overrides the default output location.
- The driver creates local runtime dirs for XDG config/data/cache and temp storage to avoid polluting your normal desktop config during automated runs.
- `Xvfb` and `xdpyinfo` must be installed on the machine; the driver does not require a host `DISPLAY`.
- Pass additional launch env overrides with `--env KEY=VALUE`.
- Screenshot action `path` values are resolved relative to `--out-dir` unless absolute.

## Xvfb Options
- `--xvfb-display 99` pins the virtual display to `:99`; otherwise the driver picks the first free display in `:90-:109`.
- `--xvfb-size 1440x960` sets the virtual display size.
- `--xvfb-color-depth 24` sets the virtual display color depth.
- `--xvfb-extra-arg ...` passes raw flags through to `Xvfb`.
