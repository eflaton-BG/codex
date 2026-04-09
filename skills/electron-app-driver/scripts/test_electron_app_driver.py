from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("electron_app_driver.py")
SPEC = importlib.util.spec_from_file_location(
    "standalone_electron_app_driver", MODULE_PATH
)
assert SPEC is not None
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_load_manifest_resolves_placeholders(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "app_dir": str(app_dir),
                "electron_executable": "{app_dir}/node_modules/.bin/electron",
                "electron_args": ["--no-sandbox", "{app_dir}/main.js"],
                "default_out_dir": str(tmp_path / "artifacts"),
                "env": {"TMPDIR": "{runtime_dir}/tmp"},
            }
        ),
        encoding="utf-8",
    )

    manifest = MODULE.load_manifest(manifest_path)

    assert manifest["app_dir"] == str(app_dir.resolve())
    assert manifest["electron_executable"].endswith("node_modules/.bin/electron")
    assert manifest["electron_args"][1] == str((app_dir / "main.js").resolve())
    assert manifest["env"]["TMPDIR"].endswith("/runtime/tmp")


def test_load_manifest_resolves_app_dir_from_manifest_dir_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    manifest_dir = tmp_path / "nested"
    manifest_dir.mkdir()
    manifest_path = manifest_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "app_dir": "{manifest_dir}/../app",
                "cwd": "{app_dir}",
                "electron_executable": "{app_dir}/node_modules/.bin/electron",
                "electron_args": ["--no-sandbox", "{app_dir}/main.js"],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(MODULE, "INVOCATION_ROOT", Path("/tmp/should-not-matter"))
    manifest = MODULE.load_manifest(manifest_path)

    assert manifest["app_dir"] == str(app_dir.resolve())
    assert manifest["cwd"] == str(app_dir.resolve())


def test_resolve_input_path_uses_invocation_root_for_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MODULE, "INVOCATION_ROOT", tmp_path)
    assert (
        MODULE._resolve_input_path("relative/file.txt")
        == (tmp_path / "relative" / "file.txt").resolve()
    )


def test_build_launch_env_adds_runtime_defaults() -> None:
    manifest = {
        "runtime_dir": "/tmp/agent-runtime",
        "env": {"EXTRA_FLAG": "1"},
    }
    env = MODULE.build_launch_env(manifest, {"CUSTOM": "yes"})

    assert env["XDG_CONFIG_HOME"] == "/tmp/agent-runtime/xdg-config"
    assert env["EXTRA_FLAG"] == "1"
    assert env["CUSTOM"] == "yes"


def test_resolve_actions_path_uses_manifest_scenario_dir(tmp_path: Path) -> None:
    scenario_dir = tmp_path / "scenarios"
    scenario_dir.mkdir()
    target = scenario_dir / "app-launch.json"
    target.write_text(json.dumps({"actions": []}), encoding="utf-8")

    path = MODULE.resolve_actions_path(
        {"scenario_dir": str(scenario_dir)}, "app-launch"
    )
    assert path == target.resolve()


def test_parse_screen_size_accepts_valid_value() -> None:
    assert MODULE._parse_screen_size("1440x960") == (1440, 960)


@pytest.mark.parametrize("value", ["", "1440", "x900", "0x900", "foo"])
def test_parse_screen_size_rejects_invalid_values(value: str) -> None:
    with pytest.raises(MODULE.DriverError):
        MODULE._parse_screen_size(value)


def test_find_free_display_uses_requested_value_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MODULE, "DISPLAY_SOCKET_DIR", tmp_path / "sockets")
    monkeypatch.setattr(MODULE, "DISPLAY_LOCK_DIR", tmp_path / "locks")
    assert MODULE._find_free_display(":77") == ":77"


def test_find_free_display_rejects_requested_value_when_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket_dir = tmp_path / "sockets"
    socket_dir.mkdir()
    (socket_dir / "X77").write_text("", encoding="utf-8")
    monkeypatch.setattr(MODULE, "DISPLAY_SOCKET_DIR", socket_dir)
    monkeypatch.setattr(MODULE, "DISPLAY_LOCK_DIR", tmp_path / "locks")

    with pytest.raises(MODULE.DriverError):
        MODULE._find_free_display(":77")


def test_find_free_display_picks_first_open_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket_dir = tmp_path / "sockets"
    lock_dir = tmp_path / "locks"
    socket_dir.mkdir()
    lock_dir.mkdir()
    (socket_dir / "X90").write_text("", encoding="utf-8")
    (lock_dir / ".X91-lock").write_text("", encoding="utf-8")
    monkeypatch.setattr(MODULE, "DISPLAY_SOCKET_DIR", socket_dir)
    monkeypatch.setattr(MODULE, "DISPLAY_LOCK_DIR", lock_dir)
    monkeypatch.setattr(MODULE, "DEFAULT_DISPLAY_RANGE", range(90, 94))

    assert MODULE._find_free_display("") == ":92"
