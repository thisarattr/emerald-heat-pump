import json
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.storage import Store


class VaultError(Exception):
    pass


class CredentialVault:
    def __init__(self, store: Store, key_file: Path | None):
        self.store = store
        self.key_file = key_file

    def _cipher(self):
        try:
            if not self.key_file:
                raise ValueError()
            path = self.key_file.resolve()
            directory = self.store.path.parent.resolve()
            if path.is_relative_to(directory) or self.key_file.absolute().is_relative_to(directory):
                raise ValueError()
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise ValueError()
            if metadata.st_size > 128:
                raise ValueError()
            return Fernet(path.read_bytes().strip())
        except (OSError, ValueError, TypeError):
            raise VaultError(
                "Encrypted storage is unavailable. Check the separate key file."
            ) from None

    @property
    def available(self):
        try:
            self._cipher()
            return True
        except VaultError:
            return False

    @property
    def saved(self):
        return self.store.encrypted_login() is not None

    def save(self, email: str, password: str):
        payload = json.dumps({"version": 1, "email": email, "password": password}).encode()
        self.store.save_encrypted_login(self._cipher().encrypt(payload))

    def load(self):
        encrypted = self.store.encrypted_login()
        if encrypted is None:
            return None
        try:
            payload = json.loads(self._cipher().decrypt(encrypted))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError()
            email, password = payload["email"], payload["password"]
            if (
                not isinstance(email, str)
                or not isinstance(password, str)
                or not email
                or not password
            ):
                raise ValueError()
            return email, password
        except (InvalidToken, ValueError, TypeError, KeyError):
            raise VaultError(
                "Saved login cannot be decrypted. Sign in again or disable saving."
            ) from None

    def forget(self):
        self.store.forget_login()
