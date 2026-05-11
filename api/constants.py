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
# v1.9.0 dockd: void of a previously-shipped order. Reverts the SO and
# the matching item_fulfillment row to pre_ship_status (PICKED or PACKED).
ACTION_SHIP_VOID = "SHIP_VOID"
# v1.10.0 POS endpoint surface. Counter sale (atomic SO-create +
# inventory decrement) and refund (credit-memo SO + inventory re-
# increment). One audit_log row per successful checkout / refund.
ACTION_POS_CHECKOUT = "POS_CHECKOUT"
ACTION_POS_REFUND = "POS_REFUND"
# v1.9.0: SO cancellation. Initiated by ERP via inbound or by an admin
# operator. Pre-PICKED states release allocation; PICKED/PACKED states
# revert inventory to the default receiving bin.
ACTION_CANCEL = "CANCEL"
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

# v1.8.0 (#288) sales_order address edits. One audit row per edited
# field carrying {field_changed, old_value, new_value} in details so
# investigators can reconstruct who changed what without scanning the
# 16-column row diff. PII-careful: only changed fields are recorded,
# not the full address.
ACTION_SO_ADDRESS_EDITED = "SO_ADDRESS_EDITED"

# v1.8.0 (#290) transfer order lifecycle. Same audit shape as the
# cycle count adjustment surface: one row per state transition;
# details JSONB carries the surrounding context. entity_type is 'TO'
# for header actions, 'TO_LINE' for line actions, 'TO_APPROVAL' for
# approval actions. The audit_log V-025 hash chain extends through
# every TO surface so post-incident forensics can reconstruct the
# full lifecycle.
ACTION_TO_CREATED           = "TO_CREATED"
ACTION_TO_LINE_PICKED       = "TO_LINE_PICKED"
ACTION_TO_SUBMITTED         = "TO_SUBMITTED"
ACTION_TO_APPROVED          = "TO_APPROVED"
ACTION_TO_REJECTED          = "TO_REJECTED"
ACTION_TO_LINE_SHORT_CLOSED = "TO_LINE_SHORT_CLOSED"
ACTION_TO_CANCELLED         = "TO_CANCELLED"
ACTION_TO_DELETED           = "TO_DELETED"
ACTION_TO_CLOSED            = "TO_CLOSED"

# Bin types
BIN_STAGING = "Staging"
BIN_PICKABLE_STAGING = "PickableStaging"
BIN_PICKABLE = "Pickable"

# User roles
ROLE_ADMIN = "ADMIN"
ROLE_USER = "USER"
