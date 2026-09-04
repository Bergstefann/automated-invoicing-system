"""Unit tests for the OAuth token cache's pickle-to-JSON migration.

`_load_cached_token` is pure local file I/O — no network call happens
regardless of which branch it takes — so unlike the rest of
`providers/google.py` it's safe to exercise directly. The token file used
to be written with `pickle.dump` despite its documented `.json` extension;
this covers both reading an existing pickle (migrating it in place) and
reading an already-migrated JSON file.
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path

from google.oauth2.credentials import Credentials

from invoicing.providers.google import OAUTH_SCOPES, _load_cached_token


def _fake_credentials() -> Credentials:
    return Credentials(
        token="fake-access-token",
        refresh_token="fake-refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="fake-client-id",
        client_secret="fake-client-secret",
        scopes=OAUTH_SCOPES,
    )


def test_loads_and_migrates_a_legacy_pickle_token_in_place(tmp_path: Path) -> None:
    token_file = tmp_path / "token.json"
    with open(token_file, "wb") as f:
        pickle.dump(_fake_credentials(), f)

    creds = _load_cached_token(token_file, OAUTH_SCOPES)

    assert creds.refresh_token == "fake-refresh-token"
    # The file on disk is now real JSON, not a pickle.
    on_disk = json.loads(token_file.read_text())
    assert on_disk["refresh_token"] == "fake-refresh-token"
    assert on_disk["client_id"] == "fake-client-id"


def test_loads_an_already_migrated_json_token_without_touching_it(tmp_path: Path) -> None:
    token_file = tmp_path / "token.json"
    token_file.write_text(_fake_credentials().to_json())
    original_contents = token_file.read_text()

    creds = _load_cached_token(token_file, OAUTH_SCOPES)

    assert creds.refresh_token == "fake-refresh-token"
    assert token_file.read_text() == original_contents
