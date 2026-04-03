"""
Audit logging helper - used by all warehouse workflows.
"""

import json

from sqlalchemy import text


def write_audit_log(db, action_type, entity_type, entity_id, user_id, warehouse_id, details=None, device_id=None):
    db.execute(
        text(
            """
            INSERT INTO audit_log (action_type, entity_type, entity_id, user_id, warehouse_id, details, device_id)
            VALUES (:action_type, :entity_type, :entity_id, :user_id, :warehouse_id, :details, :device_id)
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
