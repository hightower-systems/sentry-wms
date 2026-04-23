"""
Status constants used across the Sentry WMS API.
"""

# Purchase Order statuses
PO_OPEN = "OPEN"
PO_PARTIAL = "PARTIAL"
PO_RECEIVED = "RECEIVED"
PO_CLOSED = "CLOSED"

# Purchase Order Line statuses
POL_PENDING = "PENDING"
POL_PARTIAL = "PARTIAL"
POL_RECEIVED = "RECEIVED"

# Sales Order statuses
SO_OPEN = "OPEN"
SO_PICKING = "PICKING"
SO_PICKED = "PICKED"
SO_PACKING = "PACKING"
SO_PACKED = "PACKED"
SO_SHIPPED = "SHIPPED"
SO_CANCELLED = "CANCELLED"

# Pick Batch statuses
BATCH_OPEN = "OPEN"
BATCH_IN_PROGRESS = "IN_PROGRESS"
BATCH_COMPLETED = "COMPLETED"
BATCH_CANCELLED = "CANCELLED"

# Pick Task statuses
TASK_PENDING = "PENDING"
TASK_PICKED = "PICKED"
TASK_SHORT = "SHORT"
TASK_SKIPPED = "SKIPPED"

# Cycle Count statuses
COUNT_PENDING = "PENDING"
COUNT_IN_PROGRESS = "IN_PROGRESS"
COUNT_COMPLETED = "COMPLETED"
COUNT_VARIANCE = "VARIANCE"

# Inventory Adjustment statuses
ADJ_PENDING = "PENDING"
ADJ_APPROVED = "APPROVED"
ADJ_REJECTED = "REJECTED"

# Audit Log action types
ACTION_RECEIVE = "RECEIVE"
ACTION_RECEIVE_CANCEL = "RECEIVE_CANCEL"
ACTION_PUTAWAY = "PUTAWAY"
ACTION_PICK = "PICK"
ACTION_PACK = "PACK"
ACTION_SHIP = "SHIP"
ACTION_TRANSFER = "TRANSFER"
ACTION_ADJUST = "ADJUST"
ACTION_COUNT = "COUNT"

# v1.5.1 V-208 (#141): wms_tokens lifecycle actions. Admin token CRUD
# (issue, rotate, revoke, delete) writes one audit_log row per call
# so post-incident forensics can reconstruct "who issued what and
# when" even if the DB row itself is later deleted. The v1.4 hash
# chain trigger on audit_log makes the trail tamper-evident.
# Plaintext token values NEVER appear in `details`; scope snapshots
# do, so delete can be audited after the row is gone.
ACTION_TOKEN_ISSUE = "TOKEN_ISSUE"
ACTION_TOKEN_ROTATE = "TOKEN_ROTATE"
ACTION_TOKEN_REVOKE = "TOKEN_REVOKE"
ACTION_TOKEN_DELETE = "TOKEN_DELETE"

# Bin types
BIN_STAGING = "Staging"
BIN_PICKABLE_STAGING = "PickableStaging"
BIN_PICKABLE = "Pickable"

# User roles
ROLE_ADMIN = "ADMIN"
ROLE_USER = "USER"
