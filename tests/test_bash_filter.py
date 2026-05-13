"""Tests for bash_filter (B1 hook)."""
from symbiosis_brain.bash_filter import matches_whitelist
from symbiosis_brain.pre_action_config import PreActionConfig


def _default_whitelist() -> list[str]:
    return PreActionConfig().bash_whitelist


def test_git_commit_matches():
    assert matches_whitelist("git commit -m 'feat: x'", _default_whitelist()) is True


def test_git_push_matches():
    assert matches_whitelist("git push origin feat/x", _default_whitelist()) is True


def test_git_status_does_not_match():
    assert matches_whitelist("git status", _default_whitelist()) is False


def test_git_log_does_not_match():
    assert matches_whitelist("git log --oneline -5", _default_whitelist()) is False


def test_ls_does_not_match():
    assert matches_whitelist("ls -la", _default_whitelist()) is False


def test_pip_install_matches():
    assert matches_whitelist("pip install requests", _default_whitelist()) is True


def test_uv_add_matches():
    assert matches_whitelist("uv add pydantic", _default_whitelist()) is True


def test_npm_install_matches():
    assert matches_whitelist("npm install lodash", _default_whitelist()) is True


def test_script_execution_dot_slash_matches():
    assert matches_whitelist("./deploy.sh", _default_whitelist()) is True


def test_script_execution_no_dot_matches():
    assert matches_whitelist("/usr/local/bin/build.py", _default_whitelist()) is True


def test_grep_does_not_match():
    assert matches_whitelist("grep -r 'foo' src/", _default_whitelist()) is False


def test_empty_whitelist_matches_nothing():
    assert matches_whitelist("git commit -m x", []) is False
    assert matches_whitelist("anything", []) is False


def test_custom_whitelist_works():
    custom = [r"^terraform (apply|destroy)"]
    assert matches_whitelist("terraform apply -auto-approve", custom) is True
    assert matches_whitelist("terraform plan", custom) is False


def test_case_insensitive():
    assert matches_whitelist("GIT COMMIT -m x", _default_whitelist()) is True
