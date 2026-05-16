"""
Audit logging helper - used by all warehouse workflows.
"""

import json

from sqlalchemy import text


def write_audit_log(db, action_type, entity_type, entity_id, user_id, warehouse_id, details=None, device_id=None):
    """Write one audit_log row; returns the assigned log_id.

    The hash chain trigger (mig 016 / 047) populates row_hash and the
    chain head sentinel automatically on INSERT, so callers do not need
    to thread anything else.

    The return value is unused by most callers; the v1.9 dockd ship /
    void-ship surfaces use it to populate audit_log_id in the response
    body so dockd can deep-link operators to the audit row.
    """
    result = db.execute(
        text(
            """
            INSERT INTO audit_log (action_type, entity_type, entity_id, user_id, warehouse_id, details, device_id)
            VALUES (:action_type, :entity_type, :entity_id, :user_id, :warehouse_id, :details, :device_id)
            RETURNING log_id
            """
        ),
        {
            "action_type": action_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "user_id": user_id,
            "warehouse_id": warehouse_id,
            "details": json.dumps(details) if details else None,
            "device_id": device_id,
        },
    )
    return result.fetchone()[0]
