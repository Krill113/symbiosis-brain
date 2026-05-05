"""Tests for install_statusline.py (idempotent, auto-detect, reversible)."""
import json
from pathlib import Path

import pytest


@pytest.fixture
def fake_settings_path(tmp_path: Path) -> Path:
    return tmp_path / ".claude" / "settings.json"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_install_no_existing_statusline(fake_settings_path: Path):
    from scripts import install_statusline
    _write(fake_settings_path, {"hooks": {}})
    install_statusline.install(fake_settings_path, hook_path="/path/to/sb-statusline.sh")
    settings = _read(fake_settings_path)
    assert "sb-statusline.sh" in settings["statusLine"]["command"]
    assert "SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD" not in settings.get("env", {})


def test_install_with_user_statusline(fake_settings_path: Path):
    from scripts import install_statusline
    _write(fake_settings_path, {
        "statusLine": {"type": "command", "command": "bash /custom/user-line.sh", "refreshInterval": 10}
    })
    install_statusline.install(fake_settings_path, hook_path="/path/to/sb-statusline.sh")
    settings = _read(fake_settings_path)
    assert "sb-statusline.sh" in settings["statusLine"]["command"]
    assert settings["env"]["SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD"] == "bash /custom/user-line.sh"


def test_install_idempotent(fake_settings_path: Path):
    from scripts import install_statusline
    _write(fake_settings_path, {"hooks": {}})
    install_statusline.install(fake_settings_path, hook_path="/path/to/sb-statusline.sh")
    first = _read(fake_settings_path)
    install_statusline.install(fake_settings_path, hook_path="/path/to/sb-statusline.sh")
    second = _read(fake_settings_path)
    assert first == second  # second call is no-op


def test_install_creates_backup(fake_settings_path: Path):
    from scripts import install_statusline
    _write(fake_settings_path, {"foo": "bar"})
    install_statusline.install(fake_settings_path, hook_path="/path/to/sb-statusline.sh")
    backups = list(fake_settings_path.parent.glob("settings.json.bak.*"))
    assert len(backups) == 1


def test_uninstall_restores_user_cmd(fake_settings_path: Path):
    from scripts import install_statusline, uninstall_statusline
    _write(fake_settings_path, {
        "statusLine": {"type": "command", "command": "bash /custom/user-line.sh", "refreshInterval": 10}
    })
    install_statusline.install(fake_settings_path, hook_path="/path/to/sb-statusline.sh")
    uninstall_statusline.uninstall(fake_settings_path)
    settings = _read(fake_settings_path)
    assert settings["statusLine"]["command"] == "bash /custom/user-line.sh"
    assert "SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD" not in settings.get("env", {})


def test_uninstall_when_no_user_cmd_removes_statusline(fake_settings_path: Path):
    from scripts import install_statusline, uninstall_statusline
    _write(fake_settings_path, {"hooks": {}})
    install_statusline.install(fake_settings_path, hook_path="/path/to/sb-statusline.sh")
    uninstall_statusline.uninstall(fake_settings_path)
    settings = _read(fake_settings_path)
    assert "statusLine" not in settings
