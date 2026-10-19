from __future__ import annotations

import pytest

from symbiosis_brain.adapters.codex.tool_normalizer import (
    normalize_apply_patch,
    normalize_tool_call,
    stable_redacted_json,
)


def test_apply_patch_uses_paths_and_added_text_only():
    patch = """*** Begin Patch
*** Update File: src/a.py
*** Move to: src/c.py
@@
-old
+new value
*** Add File: src/b.py
+created value
*** Delete File: src/d.py
*** End Patch
"""
    query = normalize_apply_patch(patch, max_chars=500)

    assert "src/a.py" in query
    assert "src/b.py" in query
    assert "src/c.py" in query
    assert "src/d.py" in query
    assert "new value" in query
    assert "created value" in query
    assert "old" not in query


def test_apply_patch_update_move_keeps_destination_hunks_and_additions():
    patch = """*** Begin Patch
*** Update File: src/old.py
*** Move to: src/new.py
@@
-old value
+new value
*** End Patch
"""

    assert normalize_apply_patch(patch, max_chars=500) == (
        "src/old.py src/new.py new value"
    )


def test_apply_patch_delete_rejects_hunks_and_additions():
    patch = """*** Begin Patch
*** Delete File: secrets.env
@@
-old
+OPENAI_API_KEY=must-not-survive
*** End Patch
"""

    assert normalize_apply_patch(patch, max_chars=500) == ""


def test_apply_patch_malformed_transitions_fail_closed():
    cases = [
        """*** Begin Patch
*** Move to: orphan.py
*** End Patch
""",
        """*** Begin Patch
*** Add File: created.py
*** Move to: moved.py
+content
*** End Patch
""",
        """*** Begin Patch
*** Update File: a.py
*** Update File: b.py
+content
*** End Patch
""",
        """*** Begin Patch
*** Delete File: gone.py
+content
*** End Patch
""",
    ]

    assert [normalize_apply_patch(case, max_chars=500) for case in cases] == [
        "",
        "",
        "",
        "",
    ]


def test_apply_patch_end_of_file_closes_update_change_block():
    patch = """*** Begin Patch
*** Update File: src/example.py
@@
-old
+new
*** End of File
*** End Patch
"""

    assert normalize_apply_patch(patch, max_chars=500) == "src/example.py new"


def test_apply_patch_end_of_file_closes_update_move_before_next_file():
    patch = """*** Begin Patch
*** Update File: src/old.py
*** Move to: src/new.py
@@
-old
+new
*** End of File
*** Add File: src/created.py
+created
*** End Patch
"""

    assert normalize_apply_patch(patch, max_chars=500) == (
        "src/old.py src/new.py new src/created.py created"
    )


@pytest.mark.parametrize("kind", ["Add", "Delete"])
def test_apply_patch_end_of_file_marker_fails_closed_outside_update(kind: str):
    body = "+created\n" if kind == "Add" else ""
    patch = (
        "*** Begin Patch\n"
        f"*** {kind} File: src/example.py\n"
        f"{body}"
        "*** End of File\n"
        "*** End Patch\n"
    )

    assert normalize_apply_patch(patch, max_chars=500) == ""


@pytest.mark.parametrize("following", ["+later", "@@", " context"])
def test_apply_patch_rejects_changes_after_end_of_file_marker(following: str):
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/example.py\n"
        "@@\n"
        "-old\n"
        "+new\n"
        "*** End of File\n"
        f"{following}\n"
        "*** End Patch\n"
    )

    assert normalize_apply_patch(patch, max_chars=500) == ""


def test_apply_patch_oversized_envelope_fails_closed():
    patch = (
        "*** Begin Patch\n"
        "*** Add File: huge.txt\n"
        f"+{'x' * 1_100_000}\n"
        "*** End Patch\n"
    )

    assert normalize_apply_patch(patch, max_chars=500) == ""


def test_apply_patch_accepts_crlf_and_literal_marker_in_added_text():
    patch = (
        "*** Begin Patch\r\n"
        "*** Add File: notes.txt\r\n"
        "+literal *** marker\r\n"
        "*** End Patch\r\n"
    )

    assert normalize_apply_patch(patch, max_chars=500) == (
        "notes.txt literal *** marker"
    )


def test_apply_patch_malformed_or_empty_is_safe():
    assert normalize_apply_patch("*** Update File: x.py\n+secret", max_chars=100) == ""
    assert normalize_apply_patch("", max_chars=100) == ""


def test_apply_patch_caps_multi_file_added_text_after_redaction():
    patch = "*** Begin Patch\n" + "".join(
        f"*** Add File: src/{i}.txt\n+value-{i} {'x' * 100}\n" for i in range(30)
    ) + "*** End Patch\n"

    query = normalize_apply_patch(patch, max_chars=180)

    assert len(query) <= 180
    assert query.startswith("src/0.txt value-0")
    assert "src/29.txt" not in query


def test_mcp_arguments_are_stable_and_redacted():
    query = normalize_tool_call(
        "mcp__service__call",
        {
            "query": "widgets",
            "authorization": "Bearer private",
            "nested": {"token": "x"},
        },
        max_chars=500,
    )

    assert query is not None
    assert query.startswith("mcp__service__call ")
    assert "widgets" in query
    assert "private" not in query
    assert '"authorization":"[REDACTED]"' in query
    assert '"token":"[REDACTED]"' in query


def test_headers_cookies_and_case_variants_are_redacted():
    rendered = stable_redacted_json(
        {
            "Headers": {"Authorization": "Bearer sentinel"},
            "cookies": {"session": "sentinel-cookie"},
            "API_KEY": "sentinel-key",
        },
        max_chars=500,
    )

    assert "sentinel" not in rendered
    assert rendered == (
        '{"API_KEY":"[REDACTED]","Headers":"[REDACTED]",'
        '"cookies":"[REDACTED]"}'
    )


def test_secret_key_segments_prefixes_suffixes_and_separators_are_redacted():
    sentinel = "SENTINEL-PRIVATE-42"
    values = {
        "OPENAI_API_KEY": sentinel,
        "github_token": sentinel,
        "client_secret": sentinel,
        "proxy_authorization": sentinel,
        "x-api-key": sentinel,
        "access-token": sentinel,
        "request.cookies": sentinel,
        "HTTP_HEADERS": sentinel,
        "not_secretive": "safe",
        "tokenizer": "safe",
    }

    rendered = stable_redacted_json(values, max_chars=2000)

    assert sentinel not in rendered
    for key in (
        "OPENAI_API_KEY",
        "github_token",
        "client_secret",
        "proxy_authorization",
        "x-api-key",
        "access-token",
        "request.cookies",
        "HTTP_HEADERS",
    ):
        assert f'"{key}":"[REDACTED]"' in rendered
    assert '"not_secretive":"safe"' in rendered
    assert '"tokenizer":"safe"' in rendered


def test_secret_syntax_inside_non_secret_string_is_redacted():
    sentinel = "SENTINEL-PRIVATE-42"

    rendered = stable_redacted_json(
        {
            "query": (
                f'curl --header "Authorization: Bearer {sentinel}" '
                f"--cookie session={sentinel}"
            )
        },
        max_chars=500,
    )

    assert sentinel not in rendered
    assert rendered.count("[REDACTED]") == 2


def test_bash_and_powershell_secret_forms_are_redacted_before_query():
    sentinel = "SENTINEL-PRIVATE-42"
    command = "\n".join(
        [
            f"OPENAI_API_KEY={sentinel} run",
            f"export github_token='{sentinel}'",
            f'$env:CLIENT_SECRET = "{sentinel}"',
            f'$access_token = "{sentinel}"',
            f'curl -H "Proxy-Authorization: Basic {sentinel}"',
            f'curl -H "X-API-Key: {sentinel}"',
            f'Invoke-WebRequest -Headers @{{ Authorization = "Bearer {sentinel}" }}',
            f'Invoke-WebRequest -Headers @{{ Cookie = "session={sentinel}" }}',
        ]
    )

    for tool_name in ("Bash", "PowerShell"):
        query = normalize_tool_call(
            tool_name,
            {"command": command},
            max_chars=4000,
        )

        assert query == "secret-bearing command [REDACTED]"
        assert sentinel not in query


@pytest.mark.parametrize(
    "command",
    [
        (
            'echo before; export OPENAI_API_KEY="SENTINEL-PRIVATE-42; | &"; '  # gitleaks:allow (synthetic redaction sentinel)
            "echo after"
        ),
        (
            "Set-Item -Value 'SENTINEL-PRIVATE-42; | &' "
            "-LiteralPath 'Env:OPENAI_API_KEY'"
        ),
        (
            "Set-Item `\n"
            "  -Value 'SENTINEL-PRIVATE-42; | &' `\n"
            "  -Path 'Env:OPENAI_API_KEY'"
        ),
        'setx /P:"SENTINEL-PRIVATE-42; | &" PUBLIC_NAME public-value',
        'run --connection-string "Server=db;Password=SENTINEL-PRIVATE-42"',  # gitleaks:allow (synthetic redaction sentinel)
        'curl -H "Authorization: Basic SENTINEL-PRIVATE-42" example.invalid',
        'curl -HAuthorization:SENTINEL-PRIVATE-42 example.invalid',
    ],
)
@pytest.mark.parametrize("tool_name", ["Bash", "PowerShell"])
def test_secret_marker_anywhere_replaces_entire_command(
    tool_name: str,
    command: str,
):
    query = normalize_tool_call(
        tool_name,
        {"command": command},
        max_chars=1000,
    )

    assert query == "secret-bearing command [REDACTED]"
    assert "SENTINEL-PRIVATE-42" not in query
    assert "echo after" not in query
    assert "example.invalid" not in query


@pytest.mark.parametrize(
    "command",
    [
        'Set-Item Env:OPENAI_API_KEY "SENTINEL-PRIVATE-42"',
        "Set-Item -LiteralPath Env:OPENAI_API_KEY -Value 'SENTINEL-PRIVATE-42'",
        "Set-Item -Path 'Env:OPENAI_API_KEY' -Value 'SENTINEL-PRIVATE-42'",
        "Set-Item -Value 'SENTINEL-PRIVATE-42' -Path 'Env:OPENAI_API_KEY'",
        "setx OPENAI_API_KEY SENTINEL-PRIVATE-42",
        "PRIVATE_KEY=SENTINEL-PRIVATE-42",  # gitleaks:allow (synthetic redaction sentinel)
        "AWS_CREDENTIALS=SENTINEL-PRIVATE-42",  # gitleaks:allow (synthetic redaction sentinel)
        "DATABASE_URL=postgres://u:SENTINEL-PRIVATE-42@example.invalid/db",
        "curl -HAuthorization:Bearer SENTINEL-PRIVATE-42 example.invalid",
    ],
)
@pytest.mark.parametrize("tool_name", ["Bash", "PowerShell"])
def test_windows_and_connection_secret_forms_are_redacted(
    tool_name: str,
    command: str,
):
    query = normalize_tool_call(
        tool_name,
        {"command": command},
        max_chars=1000,
    )

    assert query == "secret-bearing command [REDACTED]"
    assert "SENTINEL-PRIVATE-42" not in query


@pytest.mark.parametrize("tool_name", ["Bash", "PowerShell"])
def test_setx_redacts_remote_password_and_secret_key_value(tool_name: str):
    query = normalize_tool_call(
        tool_name,
        {
            "command": (
                r"setx /S remote /U domain\\user /P SENTINEL-PRIVATE-42 "
                "OPENAI_API_KEY publicvalue"
            )
        },
        max_chars=1000,
    )

    assert query == "secret-bearing command [REDACTED]"
    assert "SENTINEL-PRIVATE-42" not in query
    assert "publicvalue" not in query


def test_private_key_credentials_and_connection_keys_are_redacted_in_json():
    sentinel = "SENTINEL-PRIVATE-42"

    rendered = stable_redacted_json(
        {
            "private_key": sentinel,
            "aws_credentials": sentinel,
            "database_url": f"postgres://u:{sentinel}@example.invalid/db",
            "connection_string": f"Server=db;Password={sentinel}",
            "connectionStrings": sentinel,
        },
        max_chars=2000,
    )

    assert sentinel not in rendered
    assert rendered.count("[REDACTED]") == 5


def test_stable_json_sorts_keys_and_caps_only_after_redaction():
    left = stable_redacted_json(
        {"z": 1, "password": "p" * 1000, "a": 2},
        max_chars=60,
    )
    right = stable_redacted_json(
        {"a": 2, "password": "different", "z": 1},
        max_chars=60,
    )

    assert left == right == '{"a":2,"password":"[REDACTED]","z":1}'


def test_spawn_agent_uses_message_and_text_items():
    query = normalize_tool_call(
        "spawn_agent",
        {
            "message": "inspect installer",
            "items": [{"type": "text", "text": "focus rollback"}],
        },
        max_chars=500,
    )

    assert query == "inspect installer focus rollback"


def test_agent_accepts_prompt_and_ignores_non_text_items():
    query = normalize_tool_call(
        "Agent",
        {
            "prompt": "inspect installer",
            "items": [
                {"type": "image", "text": "do not use"},
                {"type": "text", "text": "focus rollback"},
            ],
        },
        max_chars=500,
    )

    assert query == "inspect installer focus rollback"


def test_bash_uses_command_and_redacts_header_cookie_and_token_assignments():
    sentinel = "SENTINEL-PRIVATE-42"
    query = normalize_tool_call(
        "Bash",
        {
            "command": (
                f'curl -H "Authorization: Bearer {sentinel}" '
                f'--cookie "session={sentinel}" --token={sentinel} example.invalid'
            )
        },
        max_chars=500,
    )

    assert query == "secret-bearing command [REDACTED]"
    assert sentinel not in query
    assert "example.invalid" not in query


def test_apply_patch_redacts_secret_assignments_in_added_text():
    sentinel = "SENTINEL-PRIVATE-42"
    patch = (
        "*** Begin Patch\n"
        "*** Update File: config.txt\n"
        f"+authorization: Bearer {sentinel}\n"
        f"+cookie={sentinel}\n"
        "*** End Patch\n"
    )

    query = normalize_apply_patch(patch, max_chars=500)

    assert sentinel not in query
    assert query == (
        "config.txt [REDACTED SECRET LINE] [REDACTED SECRET LINE]"
    )


def test_apply_patch_redacts_composite_keys_and_auth_schemes_in_added_lines():
    sentinel = "SENTINEL-PRIVATE-42"
    patch = (
        "*** Begin Patch\n"
        "*** Update File: config.ps1\n"
        "@@\n"
        f"+$env:OPENAI_API_KEY = \"{sentinel}\"\n"
        f"+github_token={sentinel}\n"
        f"+client-secret: {sentinel}\n"
        f"+Proxy-Authorization: Basic {sentinel}\n"
        f"+X-API-Key: {sentinel}\n"
        "*** End Patch\n"
    )

    query = normalize_apply_patch(patch, max_chars=2000)

    assert sentinel not in query
    assert query == "config.ps1 " + " ".join(
        ["[REDACTED SECRET LINE]"] * 5
    )


@pytest.mark.parametrize(
    "added_line",
    [
        'export OPENAI_API_KEY="SENTINEL-PRIVATE-42; | &"',  # gitleaks:allow (synthetic redaction sentinel)
        (
            "Set-Item -Value 'SENTINEL-PRIVATE-42; | &' "
            "-Path 'Env:OPENAI_API_KEY'"
        ),
        'Authorization: Bearer "SENTINEL-PRIVATE-42; | &"',
        'connection string = "Server=db;Password=SENTINEL-PRIVATE-42"',  # gitleaks:allow (synthetic redaction sentinel)
    ],
)
def test_apply_patch_replaces_entire_secret_bearing_added_line(added_line: str):
    patch = (
        "*** Begin Patch\n"
        "*** Update File: config.txt\n"
        "@@\n"
        "+safe before\n"
        f"+{added_line}\n"
        "+safe after\n"
        "*** End Patch\n"
    )

    query = normalize_apply_patch(patch, max_chars=1000)

    assert query == (
        "config.txt safe before [REDACTED SECRET LINE] safe after"
    )
    assert "SENTINEL-PRIVATE-42" not in query
    assert added_line not in query


def test_other_local_tool_gets_redacted_stable_json():
    query = normalize_tool_call(
        "read_file",
        {"path": "src/a.py", "password": "private"},
        max_chars=500,
    )

    assert query == '{"password":"[REDACTED]","path":"src/a.py"}'


def test_empty_or_invalid_limits_do_not_leak_input():
    assert normalize_tool_call("Bash", {"command": "private"}, max_chars=0) == ""
    assert stable_redacted_json({"token": "private"}, max_chars=0) == ""
