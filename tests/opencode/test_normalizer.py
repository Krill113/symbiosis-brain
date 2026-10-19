from symbiosis_brain.adapters.opencode.normalizer import HostContext, normalize_action


def test_normalizes_edit_and_patch_without_mutating_arguments():
    args = {"filePath": "src/alpha.py", "oldString": "old", "newString": "new safe text"}
    before = args.copy()
    action = normalize_action("edit", args, HostContext(shell="powershell", cwd="/work", boundary="/work"))
    assert action is not None and action.tool == "edit"
    assert "alpha.py" in action.query and "new safe text" in action.query
    assert args == before
    patch = normalize_action("apply_patch", {"patchText": "*** Begin Patch\n*** Add File: a.py\n+hello\n+world\n*** End Patch"},
                             HostContext(shell=None, cwd="/work", boundary="/work"))
    assert patch is not None and "hello" in patch.query and "world" in patch.query


def test_secret_scan_precedes_truncation_and_redacts_patch_lines():
    host = HostContext(shell="powershell", cwd="/work", boundary="/work")
    cases = [
        "x" * 1000 + " Authorization: Bearer synthetic-secret",
        "setx API_KEY synthetic-secret",
        "-----BEGIN PRIVATE KEY-----\nsynthetic-secret",
        "postgres://user:password@host/db",
    ]
    for command in cases:
        action = normalize_action("edit", {"filePath": "alpha.txt", "newString": command}, host)
        assert action is not None and "synthetic-secret" not in action.query
        assert "REDACTED" in action.query or "[OVERSIZE" in action.query
    patch = normalize_action("apply_patch", {"patchText": "*** Begin Patch\n*** Add File: x.txt\n+added API_TOKEN=synthetic-secret\n*** End Patch"}, host)
    assert patch is not None and "synthetic-secret" not in patch.query and "REDACTED PATCH LINE" in patch.query
    shell = normalize_action("bash", {"command": "git push Authorization: Bearer synthetic-secret"}, host)
    assert shell is not None and shell.command and "synthetic-secret" not in shell.command
    multi = normalize_action("apply_patch", {"patchText": "*** Begin Patch\n*** Add File: first.py\n+one\n*** Add File: second.py\n+two\n*** End Patch"}, host)
    assert multi is not None and "first.py" in multi.query and "second.py" in multi.query


def test_quoted_structured_credentials_and_admitted_chained_command_are_whole_line_redacted():
    host = HostContext(shell="powershell", cwd="/work", boundary="/work")
    structured = normalize_action("edit", {
        "filePath": "src/secret.txt",
        "newString": 'keep before\n{"api_key":"first SYNTHETIC_SECRET"}\nkeep after',
    }, host)
    assert structured is not None
    assert structured.query == "secret.txt keep before\n[REDACTED]\nkeep after"
    vectors = [
        'setx API_KEY "first SYNTHETIC_SECRET"',
        '$env:API_KEY = "first SYNTHETIC_SECRET"',
        'Server=example;User Id=demo;Password="first SYNTHETIC_SECRET";',
        'git push; setx API_KEY "first SYNTHETIC_SECRET"',
    ]
    for value in vectors:
        result = normalize_action("bash", {"command": value}, host) if value.startswith("git ") else normalize_action(
            "edit", {"filePath": "secret.txt", "newString": value}, host
        )
        assert result is not None and "SYNTHETIC_SECRET" not in result.query
        if result.command:
            assert "SYNTHETIC_SECRET" not in result.command
    patch = normalize_action("apply_patch", {
        "patchText": '*** Begin Patch\n*** Add File: secret.json\n+{"api_key":"first SYNTHETIC_SECRET"}\n*** End Patch'
    }, host)
    assert patch is not None and patch.query == "secret.json:\n+[REDACTED PATCH LINE]"


def test_raw_oversize_patch_counts_removed_context_and_later_files():
    host = HostContext(shell="bash", cwd="/work", boundary="/work")
    raw = "*** Begin Patch\n*** Update File: first.py\n+retained\n" + "-deleted context " * 1000 + \
        "*** Add File: seventh.py\n+later body\n*** End Patch"
    result = normalize_action("apply_patch", {"patchText": raw}, host)
    assert result is not None
    assert "oversize" in result.query.lower()
    assert "retained" not in result.query and "later body" not in result.query


def test_setx_access_key_value_is_suppressed_without_secret_word_markers():
    host = HostContext(shell="powershell", cwd="/work", boundary="/work")
    edit = normalize_action("edit", {
        "filePath": "src/settings.txt",
        "newString": "safe before\nsetx ACCESS_KEY SYNTHETIC_VALUE\nsafe after",
    }, host)
    assert edit is not None
    assert edit.query == "settings.txt safe before\n[REDACTED]\nsafe after"
    quoted = normalize_action("write", {
        "filePath": "src/settings.txt",
        "content": 'setx ACCESS_KEY "first SYNTHETIC_VALUE"',
    }, host)
    assert quoted is not None and "first SYNTHETIC_VALUE" not in quoted.query
    command = normalize_action("bash", {
        "command": 'git push; setx ACCESS_KEY "first SYNTHETIC_VALUE"',
    }, host)
    assert command is not None
    assert command.query == "[REDACTED]" and command.command == "[REDACTED]"
    patch = normalize_action("apply_patch", {
        "patchText": "*** Begin Patch\n*** Add File: setup.ps1\n+setx ACCESS_KEY SYNTHETIC_VALUE\n*** End Patch",
    }, host)
    assert patch is not None and patch.query == "setup.ps1:\n+[REDACTED PATCH LINE]"


def test_unknown_shell_is_not_guessed_and_whitelisted_shell_only():
    assert normalize_action("bash", {"command": "git status"}, HostContext(None, "/w", "/w")) is None
    assert normalize_action("bash", {"command": "git status"}, HostContext("bash", "/w", "/w")) is not None
    assert normalize_action("bash", {"command": "Remove-Item x"}, HostContext("bash", "/w", "/w")) is None
    assert normalize_action("grep", {"pattern": "needle", "path": "x.txt"}, HostContext(None, "/w", "/w")) is not None
