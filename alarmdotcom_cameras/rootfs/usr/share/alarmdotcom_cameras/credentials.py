"""Secure credential storage for Alarm.com login."""

import base64
import hashlib
import json
import logging
import os
import pathlib

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


def _derive_key(data_dir: str) -> bytes:
    """Derive an encryption key from the machine-id or a generated secret.

    We use /etc/machine-id if available (standard on HA OS), otherwise
    generate and persist a random secret in the data directory.
    """
    machine_id = None
    for path in ("/etc/machine-id", "/proc/sys/kernel/random/boot_id"):
        try:
            machine_id = pathlib.Path(path).read_text().strip()
            break
        except OSError:
            continue

    if not machine_id:
        # Fall back to a generated secret persisted in the data dir
        secret_path = pathlib.Path(data_dir) / "credentials" / ".secret"
        if secret_path.exists():
            machine_id = secret_path.read_text().strip()
        else:
            machine_id = base64.urlsafe_b64encode(os.urandom(32)).decode()
            secret_path.parent.mkdir(parents=True, exist_ok=True)
            secret_path.write_text(machine_id)
            # Restrict permissions
            secret_path.chmod(0o600)

    # Derive a Fernet-compatible key using PBKDF2
    salt = b"alarmdotcom_cameras_v1"
    key = hashlib.pbkdf2_hmac("sha256", machine_id.encode(), salt, 100_000)
    return base64.urlsafe_b64encode(key)


class CredentialStore:
    """Manages encrypted storage of alarm.com credentials."""

    def __init__(self, data_dir: str) -> None:
        self._data_dir = data_dir
        self._cred_path = pathlib.Path(data_dir) / "credentials" / "credentials.enc"
        self._key = _derive_key(data_dir)
        self._fernet = Fernet(self._key)
        self._cached: dict | None = None

    def is_configured(self) -> bool:
        """Check if credentials have been saved."""
        return self._cred_path.exists()

    def save(self, username: str, password: str) -> None:
        """Save credentials encrypted to disk."""
        payload = json.dumps({"username": username, "password": password})
        encrypted = self._fernet.encrypt(payload.encode())
        self._cred_path.parent.mkdir(parents=True, exist_ok=True)
        self._cred_path.write_bytes(encrypted)
        self._cred_path.chmod(0o600)
        self._cached = {"username": username, "password": password}
        logger.info("Credentials saved for %s", username)

    def load(self) -> dict | None:
        """Load and decrypt credentials. Returns {"username": ..., "password": ...} or None."""
        if self._cached is not None:
            return self._cached

        if not self._cred_path.exists():
            return None

        try:
            encrypted = self._cred_path.read_bytes()
            decrypted = self._fernet.decrypt(encrypted)
            self._cached = json.loads(decrypted.decode())
            return self._cached
        except Exception:
            logger.exception("Failed to decrypt credentials")
            return None

    def get_username(self) -> str | None:
        """Get just the username (safe to expose via API)."""
        creds = self.load()
        return creds["username"] if creds else None

    def clear(self) -> None:
        """Remove stored credentials."""
        if self._cred_path.exists():
            self._cred_path.unlink()
        self._cached = None
        logger.info("Credentials cleared")
