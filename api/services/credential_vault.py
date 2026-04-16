"""Encrypted credential storage for connector secrets.

Credentials (API keys, OAuth tokens, consumer secrets) are encrypted at
rest using Fernet symmetric encryption. The master key comes from the
SENTRY_ENCRYPTION_KEY environment variable and MUST be set explicitly;
a missing key is a fatal startup error.

Credentials are scoped per connector + warehouse so that multi-warehouse
deployments can have separate API keys for each location.

SECURITY: Plaintext values are never returned in API responses or
written to application logs. Only the vault service has access to
decrypted values.
"""

import os

from cryptography.fernet import Fernet
from flask import g
from sqlalchemy import text

_fernet = None


def _get_fernet():
    """Get or create the Fernet cipher instance.

    Reads SENTRY_ENCRYPTION_KEY from the environment. Raises RuntimeError
    if the variable is unset or empty. We deliberately do NOT auto-generate
    a key: a silently-generated key would rotate on every process restart,
    silently making previously-stored credentials undecryptable, and would
    risk being logged to stdout.
    """
    global _fernet
    if _fernet is not None:
        return _fernet

    key = os.environ.get("SENTRY_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError(
            "SENTRY_ENCRYPTION_KEY environment variable is required. "
            "Generate with: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )

    _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def _encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string and return the ciphertext as a string."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def _decrypt(ciphertext: str) -> str:
    """Decrypt a ciphertext string and return the plaintext."""
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def store_credential(connector_name: str, warehouse_id: int, key: str, plaintext_value: str) -> None:
    """Encrypt and store a credential. Upserts on conflict.

    Args:
        connector_name: Connector identifier (e.g. "netsuite").
        warehouse_id: Warehouse this credential is scoped to.
        key: Credential key (e.g. "api_key", "consumer_secret").
        plaintext_value: The secret value to encrypt and store.
    """
    encrypted = _encrypt(plaintext_value)
    g.db.execute(
        text("""
            INSERT INTO connector_credentials (connector_name, warehouse_id, credential_key, encrypted_value)
            VALUES (:name, :wid, :key, :val)
            ON CONFLICT (connector_name, warehouse_id, credential_key)
            DO UPDATE SET encrypted_value = :val, updated_at = NOW()
        """),
        {"name": connector_name, "wid": warehouse_id, "key": key, "val": encrypted},
    )


def get_credential(connector_name: str, warehouse_id: int, key: str) -> str | None:
    """Decrypt and return a single credential value.

    Args:
        connector_name: Connector identifier.
        warehouse_id: Warehouse scope.
        key: Credential key.

    Returns:
        The decrypted plaintext value, or None if not found.
    """
    row = g.db.execute(
        text("""
            SELECT encrypted_value FROM connector_credentials
            WHERE connector_name = :name AND warehouse_id = :wid AND credential_key = :key
        """),
        {"name": connector_name, "wid": warehouse_id, "key": key},
    ).fetchone()
    if not row:
        return None
    return _decrypt(row.encrypted_value)


def get_all_credentials(connector_name: str, warehouse_id: int) -> dict:
    """Decrypt and return all credential key-value pairs for a connector+warehouse.

    Args:
        connector_name: Connector identifier.
        warehouse_id: Warehouse scope.

    Returns:
        Dict mapping credential keys to their decrypted plaintext values.
    """
    rows = g.db.execute(
        text("""
            SELECT credential_key, encrypted_value FROM connector_credentials
            WHERE connector_name = :name AND warehouse_id = :wid
        """),
        {"name": connector_name, "wid": warehouse_id},
    ).fetchall()
    return {row.credential_key: _decrypt(row.encrypted_value) for row in rows}


def delete_credential(connector_name: str, warehouse_id: int, key: str) -> None:
    """Remove a single credential.

    Args:
        connector_name: Connector identifier.
        warehouse_id: Warehouse scope.
        key: Credential key to remove.
    """
    g.db.execute(
        text("""
            DELETE FROM connector_credentials
            WHERE connector_name = :name AND warehouse_id = :wid AND credential_key = :key
        """),
        {"name": connector_name, "wid": warehouse_id, "key": key},
    )


def delete_all_credentials(connector_name: str, warehouse_id: int) -> None:
    """Remove all credentials for a connector+warehouse.

    Args:
        connector_name: Connector identifier.
        warehouse_id: Warehouse scope.
    """
    g.db.execute(
        text("""
            DELETE FROM connector_credentials
            WHERE connector_name = :name AND warehouse_id = :wid
        """),
        {"name": connector_name, "wid": warehouse_id},
    )


# ---------------------------------------------------------------------------
# Standalone functions for use outside Flask request context (e.g. Celery)
# ---------------------------------------------------------------------------


def get_all_credentials_standalone(connector_name: str, warehouse_id: int) -> dict:
    """Same as get_all_credentials but creates its own DB session.

    Used by Celery tasks which run outside Flask's request context
    and don't have access to g.db.
    """
    import models.database as db

    session = db.SessionLocal()
    try:
        rows = session.execute(
            text("""
                SELECT credential_key, encrypted_value FROM connector_credentials
                WHERE connector_name = :name AND warehouse_id = :wid
            """),
            {"name": connector_name, "wid": warehouse_id},
        ).fetchall()
        return {row.credential_key: _decrypt(row.encrypted_value) for row in rows}
    finally:
        session.close()
