"""`serve --vault` on a path that is not there.

Creating the vault here is the wrong answer: setup is what creates vaults, and a
path that has gone missing is far more likely to be an unmounted drive or a typo
than a first run. Conjuring an empty tree in its place hides that.
"""

import pytest

from symbiosis_brain import server


def test_serve_refuses_a_missing_vault_instead_of_creating_one(tmp_path, monkeypatch):
    missing = tmp_path / "not-mounted" / "vault"
    started = []
    def _capture(coro, *a, **k):
        coro.close()  # never awaited — closing it keeps the run warning-free
        started.append(coro)

    monkeypatch.setattr(server.asyncio, "run", _capture)
    monkeypatch.setattr("sys.argv", ["symbiosis-brain", "--vault", str(missing)])

    with pytest.raises(SystemExit) as exc:
        server.main()

    assert exc.value.code != 0
    assert not missing.exists()
    assert started == []


def test_serve_starts_against_an_existing_vault(tmp_path, monkeypatch):
    started = []
    def _capture(coro, *a, **k):
        coro.close()  # never awaited — closing it keeps the run warning-free
        started.append(coro)

    monkeypatch.setattr(server.asyncio, "run", _capture)
    monkeypatch.setattr("sys.argv", ["symbiosis-brain", "--vault", str(tmp_path)])

    server.main()

    assert started, "an existing vault must still start the server"
