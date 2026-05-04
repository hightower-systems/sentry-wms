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

# v1.5.1 V-221 (#154): consumer_groups + connector-registry admin
# actions. Structurally identical to the V-208 token CRUD audit
# coverage but lower severity -- consumer_groups do not hold auth
# material, so a compromise here causes data-flow misdirection
# (V-207 replay, V-204 subscription tampering) rather than an auth
# bypass. Worth filing for forensic symmetry: without these writes,
# an attacker could delete + recreate a consumer_group with a
# tampered subscription (V-204) and leave no audit trace.
#
# Entity-id convention: consumer_group_id and connector_id are
# VARCHAR so they cannot fit audit_log.entity_id (INT NOT NULL).
# Writes use entity_id=0 as a sentinel and carry the real string
# id in details so investigators can still bind actions to rows.
ACTION_CONNECTOR_REGISTRY_CREATE = "CONNECTOR_REGISTRY_CREATE"
ACTION_CONNECTOR_REGISTRY_UPDATE = "CONNECTOR_REGISTRY_UPDATE"
ACTION_CONNECTOR_REGISTRY_DELETE = "CONNECTOR_REGISTRY_DELETE"
ACTION_CONSUMER_GROUP_CREATE = "CONSUMER_GROUP_CREATE"
ACTION_CONSUMER_GROUP_UPDATE = "CONSUMER_GROUP_UPDATE"
ACTION_CONSUMER_GROUP_DELETE = "CONSUMER_GROUP_DELETE"

# v1.6.0 outbound webhook subscription CRUD audit coverage. Same
# shape as the v1.5 token CRUD writes: one audit row per mutation,
# scope snapshot in details so post-incident forensics survive a
# hard delete. Plaintext webhook secrets NEVER appear in details;
# the row carries display_name + delivery_url + filter + ceilings
# + rate so an investigator can reconstruct what was created and
# under what bounds. entity_id holds a stable surrogate (the audit
# table column is INT; subscription_id is UUID, so writes use a
# sentinel and carry the UUID under details.subscription_id).
ACTION_WEBHOOK_SUBSCRIPTION_CREATE = "WEBHOOK_SUBSCRIPTION_CREATE"
ACTION_WEBHOOK_SUBSCRIPTION_UPDATE = "WEBHOOK_SUBSCRIPTION_UPDATE"
ACTION_WEBHOOK_SUBSCRIPTION_DELETE_SOFT = "WEBHOOK_SUBSCRIPTION_DELETE_SOFT"
ACTION_WEBHOOK_SUBSCRIPTION_DELETE_HARD = "WEBHOOK_SUBSCRIPTION_DELETE_HARD"
ACTION_WEBHOOK_SECRET_ROTATE = "WEBHOOK_SECRET_ROTATE"
ACTION_WEBHOOK_DELIVERY_REPLAY_SINGLE = "WEBHOOK_DELIVERY_REPLAY_SINGLE"
ACTION_WEBHOOK_DELIVERY_REPLAY_BATCH = "WEBHOOK_DELIVERY_REPLAY_BATCH"
# #232: dispatcher auto-pause when subscription_filter fails
# Pydantic validation. user_id is the daemon's identity ("system");
# details.subscription_id + details.parse_error capture the
# offending row + the recoverable error so an operator can find
# the bad column in audit_log without grepping daemon logs.
ACTION_WEBHOOK_SUBSCRIPTION_AUTO_PAUSE = "WEBHOOK_SUBSCRIPTION_AUTO_PAUSE"

# Bin types
BIN_STAGING = "Staging"
BIN_PICKABLE_STAGING = "PickableStaging"
BIN_PICKABLE = "Pickable"

# User roles
ROLE_ADMIN = "ADMIN"
ROLE_USER = "USER"
