"""CLI keyring backend scoped to one explicitly selected account directory.

The official CLI imports this in its own Python environment (which supplies
keyring). The pool server itself does not need to install keyring.
"""

from __future__ import annotations

import os

from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from .oauth_store import oauth_token_path, write_oauth_token


class AccountKeyring(KeyringBackend):
    priority = 1

    @staticmethod
    def _home(service: str, username: str) -> str:
        if (service, username) != ("google-colab-cli", "oauth_token"):
            raise ValueError("This backend only stores Colab OAuth credentials.")
        home = os.environ.get("COLAB_POOL_ACCOUNT_HOME")
        if not home:
            raise ValueError("COLAB_POOL_ACCOUNT_HOME must select an isolated account.")
        return home

    def get_password(self, service: str, username: str) -> str | None:
        path = oauth_token_path(self._home(service, username))
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def set_password(self, service: str, username: str, password: str) -> None:
        write_oauth_token(self._home(service, username), password)

    def delete_password(self, service: str, username: str) -> None:
        path = oauth_token_path(self._home(service, username))
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise PasswordDeleteError("No OAuth credentials for this account.") from exc
