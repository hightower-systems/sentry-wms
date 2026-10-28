"""
Status constants used across the Sentry WMS API.
"""

import os

# Purchase Order statuses
PO_OPEN = "OPEN"
PO_PARTIAL = "PARTIAL"
PO_RECEIVED = "RECEIVED"
PO_CLOSED = "CLOSED"
# ARCHIVED: terminal status for POs the operator wants out of the
# active list. Hidden from /admin/purchase-orders by default; only
# reachable via the status dropdown when the PO is already CLOSED.
PO_ARCHIVED = "ARCHIVED"

# Purchase Order Line statuses
POL_PENDING = "PENDING"
POL_PARTIAL = "PARTIAL"
POL_RECEIVED = "RECEIVED"

# Sales Order statuses
SO_OPEN = "OPEN"
SO_PICKED = "PICKED"
SO_PACKED = "PACKED"
SO_SHIPPED = "SHIPPED"
SO_CANCELLED = "CANCELLED"
# SOs held for fraud review (auto-flagged at ingest when the
# billing/shipping heuristic is enabled, or set manually). Excluded
# from the picking queue until a CSR clears via the Fraud page.
SO_FRAUD_REVIEW = "FRAUD_REVIEW"

# A fully-refunded sale (or a cancel issued because the customer was
# refunded). Distinct from CANCELLED so refunds read apart from plain
# cancellations in the operator views and reporting. App-enforced -- no
# DB CHECK on sales_orders.status. Pure status label: the money + the
# inventory move in the refund flow, never on this status transition.
SO_REFUNDED = "REFUNDED"

# mig 067: backorder-only off-ramp. A BO SO created via
# /partial-fulfill starts here and flips to OPEN when receipt.completed
# makes all its lines satisfiable. Hidden from picker queues by the
# existing status != OPEN gate in create_pick_batch / wave_create.
SO_WAITING_STOCK = "WAITING_STOCK"
# Return-SO (RMA) receiving lifecycle. A return SO (order_type=return, the
# <orig>-RMA goods-in) starts at SO_OPEN when created, advances as goods come
# back against it, and is CLOSED when CS finalises the case. App-enforced; the
# status column has no DB CHECK (mig 070/072 family).
RMA_STATUS_PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
RMA_STATUS_RECEIVED = "RECEIVED"
RMA_STATUS_CLOSED = "CLOSED"

# Sales Order order_type (mig 056 + mig 067 + mig 070 CHECK expansion)
ORDER_TYPE_SALE      = "sale"
ORDER_TYPE_REFUND    = "refund"
ORDER_TYPE_BACKORDER = "backorder"
# Post-fulfillment types (mig 070). Each is a typed child SO linked to the
# original via parent_so_id with a readable so_number suffix:
#   replacement -> <orig>-REPLACEMENT (same SKU, $0; POS Replacement mode)
#   exchange    -> <orig>-EXCHANGE   (different item + price delta; POS Exchange mode)
#   return      -> <orig>-RMA        (goods-in record; the RMA page receives against it)
ORDER_TYPE_REPLACEMENT = "replacement"
ORDER_TYPE_EXCHANGE    = "exchange"
ORDER_TYPE_RETURN      = "return"
# Readable so_number suffix per post-fulfillment order_type (mig 070). A child
# SO's number is "<original so_number>-<SUFFIX>" for the first child of a given
# (parent, order_type), and "<...>-<SUFFIX>-N" for subsequent ones. sale and
# backorder keep their own POS-<id> numbering and are not in this map.
ORDER_TYPE_SO_SUFFIX = {
    ORDER_TYPE_REPLACEMENT: "REPLACEMENT",
    ORDER_TYPE_EXCHANGE:    "EXCHANGE",
    ORDER_TYPE_RETURN:      "RMA",
    ORDER_TYPE_REFUND:      "REFUND",
}


def order_type_allows_fulfillment_ops(order_type):
    """Single source of truth for whether an SO's order_type may undergo
    an outbound fulfillment operation -- admin pick, release picked qty,
    or partial fulfill. Every order_type is eligible EXCEPT return: a
    return SO (the <orig>-RMA goods-in) is inbound and runs a separate
    status lifecycle (OPEN -> PARTIALLY_RECEIVED -> RECEIVED), so the
    outbound OPEN/PICKED/PACKED/SHIPPED ladder these ops assume does not
    apply to it.

    Partial fulfill layers one extra exclusion on top of this (backorder,
    to keep the no-chaining cap); see partial_fulfill_sales_order.
    """
    return order_type != ORDER_TYPE_RETURN

# mig 067: sales_orders.cancellation_reason allowed values.
# App-enforced enum (no DB CHECK). Set by /cancel-backorder
# (operator-supplied) or by the parent-cancel cascade in
# sales_order_service.cancel_sales_order.
CANCEL_REASON_FOUND            = "found"
CANCEL_REASON_CUSTOMER_ASKED   = "customer_asked"
CANCEL_REASON_REFUNDED         = "refunded"
CANCEL_REASON_PARENT_CANCELLED = "parent_cancelled"
CANCEL_REASON_OTHER            = "other"
CANCEL_REASONS = (
    CANCEL_REASON_FOUND,
    CANCEL_REASON_CUSTOMER_ASKED,
    CANCEL_REASON_REFUNDED,
    CANCEL_REASON_PARENT_CANCELLED,
    CANCEL_REASON_OTHER,
)
# Company-wide timezone for operator-facing calendar dates (e.g. the
# Shipped Date on the SO modal). Timestamps are stored in UTC; this is
# the single zone used to convert them to the local calendar date
# operators reason about, and to anchor manually-entered dates.
# Deployment-configurable; defaults to UTC. Single-zone assumption;
# switch to per-row warehouses.timezone if that ever changes.
COMPANY_TIMEZONE = os.environ.get("SENTRY_COMPANY_TIMEZONE", "UTC")


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
# so-refinement: an operator-reverted PICKED task. Distinct from
# PENDING so a subsequent revert prompt does not re-offer the same
# task, and from PICKED so dashboards do not double-count.
TASK_RELEASED = "RELEASED"

# Cycle Count statuses
COUNT_PENDING = "PENDING"
COUNT_IN_PROGRESS = "IN_PROGRESS"
COUNT_COMPLETED = "COMPLETED"
COUNT_VARIANCE = "VARIANCE"

# Inventory Adjustment statuses
ADJ_PENDING = "PENDING"
ADJ_APPROVED = "APPROVED"
ADJ_REJECTED = "REJECTED"

# Inventory Adjustment reason codes (mig 067 added SHORT). App-enforced
# enum (no DB CHECK). SHORT is written by /partial-fulfill when on-hand
# stock exists for a shorted line; reason_detail carries the BO SO #.
ADJ_REASON_CYCLE_COUNT = "CYCLE_COUNT"
ADJ_REASON_DAMAGE      = "DAMAGE"
ADJ_REASON_FOUND       = "FOUND"
ADJ_REASON_LOST        = "LOST"
ADJ_REASON_CORRECTION  = "CORRECTION"
ADJ_REASON_SHORT       = "SHORT"

# Audit Log action types
ACTION_RECEIVE = "RECEIVE"
ACTION_RECEIVE_CANCEL = "RECEIVE_CANCEL"
# Goods received back against a return SO (the <orig>-RMA). entity_type='SO';
# distinct from ACTION_RECEIVE (PO receiving) so return receipts are auditable
# apart from PO receipts.
ACTION_RETURN_RECEIVE = "RETURN_RECEIVE"
# Operator soft-deleted a mistakenly created return SO (RMA). entity_type='SO';
# distinct from ACTION_CANCEL (which unwinds outbound allocation/picking) -- a
# void stamps sales_orders.voided_at and touches no inventory (mig 076).
ACTION_RETURN_VOID = "RETURN_VOID"
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
# Reference-SO ingest: a minimal historical SO stubbed from a
# source-system-only original so a post-fulfillment child (replacement/exchange/RMA) can link
# parent_so_id. Records lines as fully shipped but touches no inventory and
# emits no events. One audit_log row per ingest.
ACTION_POS_REFERENCE_INGEST = "POS_REFERENCE_INGEST"
# v1.9.0: SO cancellation. Initiated by ERP via inbound or by an admin
# operator. Pre-PICKED states release allocation; PICKED/PACKED states
# revert inventory to the default receiving bin.
ACTION_CANCEL = "CANCEL"
# Partial-fulfill / backorders. Two rows per partial-fulfill
# call: one on the original SO (shrink summary), one on the new BO SO
# (creation). BACKORDER_CANCELLED is emitted by /cancel-backorder
# alongside the existing CANCEL row that cancel_sales_order writes;
# kept distinct so dashboards can split BO-only cancels from regular
# CANCEL volume.
ACTION_PARTIAL_FULFILL     = "PARTIAL_FULFILL"
ACTION_BACKORDER_CANCELLED = "BACKORDER_CANCELLED"
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

# v1.30.0 Pipe C channel-availability config CRUD audit coverage. Same
# shape as the webhook subscription writes: one row per mutation, config
# snapshot in details (channel_id, scope, transform, ceilings, rate) so
# forensics survive a delete. entity_id=0 sentinel; channel_id (VARCHAR)
# lives in details.
ACTION_CHANNEL_CREATE = "CHANNEL_CREATE"
ACTION_CHANNEL_UPDATE = "CHANNEL_UPDATE"
ACTION_CHANNEL_DELETE = "CHANNEL_DELETE"
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

# Fraud queue. The auto-flag at ingest does NOT get its own audit row
# -- the existing inbound CREATE row plus the resulting
# status='FRAUD_REVIEW' already capture it forensically. These two
# cover the CSR-driven mutations on the Fraud page.
ACTION_SO_FRAUD_CLEARED = "SO_FRAUD_CLEARED"
ACTION_SO_MEMO_EDITED = "SO_MEMO_EDITED"

# PO line edit + status surfaces. One audit row per mutation so the
# historical record of what was ordered survives later edits. Status
# transitions get their own action so the audit query "when did this
# PO move from OPEN to RECEIVED" stays answerable. Line CRUD carries
# {sku, quantity_ordered, quantity_received} in details so a deleted
# line can still be reconstructed.
ACTION_PO_STATUS_CHANGED = "PO_STATUS_CHANGED"
ACTION_PO_LINE_ADDED = "PO_LINE_ADDED"
ACTION_PO_LINE_UPDATED = "PO_LINE_UPDATED"
ACTION_PO_LINE_REMOVED = "PO_LINE_REMOVED"

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
ROLE_SHIPYARD_MANAGER = "SHIPYARD_MANAGER"
ROLE_RECEIVER = "RECEIVER"
ROLE_OUTBOUND = "OUTBOUND"
ROLE_VIEWER = "VIEWER"

USER_ROLES = (
    ROLE_ADMIN,
    ROLE_USER,
    ROLE_SHIPYARD_MANAGER,
    ROLE_RECEIVER,
    ROLE_OUTBOUND,
    ROLE_VIEWER,
)

ALL_MOBILE_FUNCTIONS = (
    "receive",
    "putaway",
    "pick",
    "pack",
    "ship",
    "count",
    "transfer",
    "shipyard_receive",
    "shipyard_outbound",
)

# Shipyard domain status and audit actions. Shipyard values are namespaced
# separately from the e-commerce order states so the two domains cannot drift
# into each other accidentally.
SHIPYARD_STATUS_ACTIVE = "ACTIVE"
SHIPYARD_STATUS_INACTIVE = "INACTIVE"

SHIPYARD_MANIFEST_PARSED = "PARSED"
SHIPYARD_MANIFEST_CONFIRMED = "CONFIRMED"
SHIPYARD_MANIFEST_PARTIAL = "PARTIAL"
SHIPYARD_MANIFEST_CLOSED = "CLOSED"
SHIPYARD_MANIFEST_VOID = "VOID"

SHIPYARD_LINE_PENDING = "PENDING"
SHIPYARD_LINE_PARTIAL = "PARTIAL"
SHIPYARD_LINE_RECEIVED = "RECEIVED"

SHIPYARD_TICKET_EXPECTED = "EXPECTED"
SHIPYARD_TICKET_PARTIAL = "PARTIAL"
SHIPYARD_TICKET_IN_STOCK = "IN_STOCK"
SHIPYARD_TICKET_ALLOCATED = "ALLOCATED"
SHIPYARD_TICKET_OUTBOUND = "OUTBOUND"

SHIPYARD_PALLET_RECEIVED = "RECEIVED"
SHIPYARD_PALLET_STORED = "STORED"
SHIPYARD_PALLET_OUTBOUND = "OUTBOUND"

SHIPYARD_SHORTAGE_NONE = "NONE"
SHIPYARD_SHORTAGE_PENDING_REPORT = "PENDING_REPORT"
SHIPYARD_SHORTAGE_REPORTED = "REPORTED"
SHIPYARD_SHORTAGE_IN_TRANSIT = "IN_TRANSIT"
SHIPYARD_SHORTAGE_PARTIAL_REPLENISHMENT = "PARTIAL_REPLENISHMENT"
SHIPYARD_SHORTAGE_REPLENISHED = "REPLENISHED"

SHIPYARD_PHOTO_PALLET = "PALLET"
SHIPYARD_PHOTO_GROUP = "GROUP"
SHIPYARD_PHOTO_AREA = "AREA"
SHIPYARD_PHOTO_DIFFERENCE = "DIFFERENCE"

SHIPYARD_LEDGER_RECEIPT = "RECEIPT"
SHIPYARD_LEDGER_OUTBOUND = "OUTBOUND"

SHIPYARD_DISTRIBUTION_PARSED = "PARSED"
SHIPYARD_DISTRIBUTION_CONFIRMED = "CONFIRMED"
SHIPYARD_DISTRIBUTION_VOID = "VOID"

SHIPYARD_DISPATCH_CONFIRMED = "CONFIRMED"
SHIPYARD_DISPATCH_CANCELLED = "CANCELLED"
SHIPYARD_DISPATCH_SOURCE_MANUAL = "MANUAL"
SHIPYARD_DISPATCH_SOURCE_EXCEL = "EXCEL"
SHIPYARD_DISPATCH_SOURCE_IMAGE_OCR = "IMAGE_OCR"

SHIPYARD_MATCH_MATCHED = "MATCHED"
SHIPYARD_MATCH_MANUAL = "MANUAL"
SHIPYARD_MATCH_UNMATCHED = "UNMATCHED"
SHIPYARD_MATCH_AMBIGUOUS = "AMBIGUOUS"

SHIPYARD_LABEL_RECOGNITION_MATCHED = "MATCHED"
SHIPYARD_LABEL_RECOGNITION_NEEDS_REVIEW = "NEEDS_REVIEW"
SHIPYARD_LABEL_RECOGNITION_FAILED = "FAILED"

SHIPYARD_DELIVERY_ISSUED = "ISSUED"
SHIPYARD_DELIVERY_CANCELLED = "CANCELLED"
SHIPYARD_OUTBOUND_MANIFEST_PARSED = "PARSED"
SHIPYARD_OUTBOUND_MANIFEST_CONFIRMED = "CONFIRMED"
SHIPYARD_OUTBOUND_MANIFEST_VOID = "VOID"
SHIPYARD_OUTBOUND_TASK_PENDING = "PENDING"
SHIPYARD_OUTBOUND_TASK_OUTBOUND = "OUTBOUND"
SHIPYARD_OUTBOUND_ORDER_PENDING = "PENDING"
SHIPYARD_OUTBOUND_ORDER_IN_PROGRESS = "IN_PROGRESS"
SHIPYARD_OUTBOUND_ORDER_PARTIAL = "PARTIAL"
SHIPYARD_OUTBOUND_ORDER_OUTBOUND = "OUTBOUND"
SHIPYARD_OUTBOUND_ORDER_LOADED = "LOADED"
SHIPYARD_OUTBOUND_LINE_PENDING = "PENDING"
SHIPYARD_OUTBOUND_LINE_SHORTAGE = "SHORTAGE"
SHIPYARD_OUTBOUND_LINE_ALLOCATED = "ALLOCATED"
SHIPYARD_OUTBOUND_LINE_OUTBOUND = "OUTBOUND"

SHIPYARD_BINDING_MATCHED = "MATCHED"
SHIPYARD_BINDING_MISMATCH = "MISMATCH"
SHIPYARD_BINDING_NOT_CONFIGURED = "NOT_CONFIGURED"
SHIPYARD_BINDING_AMBIGUOUS = "AMBIGUOUS"

ACTION_SHIPYARD_PROJECT_BINDING_CREATED = "SHIPYARD_PROJECT_BINDING_CREATED"
ACTION_SHIPYARD_PROJECT_BINDING_UPDATED = "SHIPYARD_PROJECT_BINDING_UPDATED"
ACTION_SHIPYARD_ZONE_SHIP_MAPPING_CREATED = "SHIPYARD_ZONE_SHIP_MAPPING_CREATED"
ACTION_SHIPYARD_ZONE_SHIP_MAPPING_UPDATED = "SHIPYARD_ZONE_SHIP_MAPPING_UPDATED"
ACTION_SHIPYARD_INBOUND_MANIFEST_PARSED = "SHIPYARD_INBOUND_MANIFEST_PARSED"
ACTION_SHIPYARD_INBOUND_MANIFEST_CONFIRMED = "SHIPYARD_INBOUND_MANIFEST_CONFIRMED"
ACTION_SHIPYARD_INBOUND_MANIFEST_VOIDED = "SHIPYARD_INBOUND_MANIFEST_VOIDED"
ACTION_SHIPYARD_TICKET_RECEIVED = "SHIPYARD_TICKET_RECEIVED"
ACTION_SHIPYARD_SHORTAGE_UPDATED = "SHIPYARD_SHORTAGE_UPDATED"
ACTION_SHIPYARD_DISTRIBUTION_IMPORTED = "SHIPYARD_DISTRIBUTION_IMPORTED"
ACTION_SHIPYARD_DISTRIBUTION_CONFIRMED = "SHIPYARD_DISTRIBUTION_CONFIRMED"
ACTION_SHIPYARD_DISTRIBUTION_VOIDED = "SHIPYARD_DISTRIBUTION_VOIDED"
ACTION_SHIPYARD_DISPATCH_CREATED = "SHIPYARD_DISPATCH_CREATED"
ACTION_SHIPYARD_DISPATCH_LINE_MATCHED = "SHIPYARD_DISPATCH_LINE_MATCHED"
ACTION_SHIPYARD_DELIVERY_NOTE_CREATED = "SHIPYARD_DELIVERY_NOTE_CREATED"
ACTION_SHIPYARD_OUTBOUND_MANIFEST_PARSED = "SHIPYARD_OUTBOUND_MANIFEST_PARSED"
ACTION_SHIPYARD_OUTBOUND_MANIFEST_CONFIRMED = (
    "SHIPYARD_OUTBOUND_MANIFEST_CONFIRMED"
)
ACTION_SHIPYARD_OUTBOUND_MANIFEST_VOIDED = "SHIPYARD_OUTBOUND_MANIFEST_VOIDED"
ACTION_SHIPYARD_TICKET_OUTBOUND = "SHIPYARD_TICKET_OUTBOUND"
ACTION_SHIPYARD_OUTBOUND_LOADED = "SHIPYARD_OUTBOUND_LOADED"

# mig 061: source of truth for the per-page web-admin
# permission grid. Keys MATCH the React admin URL slugs so the sidebar
# filter and the @require_admin_or_page_permission decorator can both
# read straight off this list. Adding a new admin page is a two-step
# change: append the key here and grant it to any users who need it.
ALL_PAGE_KEYS = (
    "dashboard",
    "inventory", "cycle-counts", "count-approvals",
    "purchase-orders", "receiving", "putaway",
    "sales-orders",
    # Partial-fulfill / backorders (Outbound). Holders see the
    # /backorders dashboard plus the cancel-backorder action. The
    # partial-fulfill action itself is gated by 'sales-orders' since it
    # lives on the SO edit modal; this key only gates the dedicated
    # dashboard.
    "backorders",
    # Fraud (Outbound): the review queue for SOs held at FRAUD_REVIEW.
    # Holders see the /fraud page and can edit the CSR memo or push an
    # order back into the picking queue.
    "fraud",
    # Picking Tickets (Outbound): the printable packing-slip queue and
    # the per-SO / Print All print views. Holders see the /picking-tickets
    # page and can mark tickets printed.
    "picking-tickets",
    # Picking Batches (Outbound): lists the active pick batches that hold
    # the cross-pick lock and lets an admin release a stuck one. Holders
    # see the /picking-batches page plus the SO batch-release action.
    "picking-batches",
    # 'picking', 'packing', 'shipping' retired: the mobile (handheld)
    # flow is the canonical surface and the admin-side mirrors
    # duplicated supervisor state. Migration 065 prunes any stragglers
    # in user_page_permissions.
    "items", "vendors",
    "adjustments", "inter-warehouse-transfers", "transfer-orders",
    "warehouses", "bins", "zones", "preferred-bins",
    "users", "api-tokens", "inbound", "shipyard-outbound", "shipyard-settings",
    "consumer-groups",
    # Channels (Pipe C): per-channel availability config + the publish
    # health view. Holders see the /channels page and can CRUD channels,
    # pause/resume, and inspect the DLQ.
    "channels",
    # Notifications: per-warehouse Teams notification webhooks for the
    # backorder.* event family. Holders see the /notifications page and
    # can CRUD + test-send destinations. Distinct from 'webhooks'
    # (signed-envelope HTTP subscribers).
    "webhooks", "notifications", "audit-log", "imports", "integrations", "settings",
)

# Override grants (mig 062): feature-flag grants that ride on the same
# user_page_permissions table but are not pages in the sidebar. Granting
# one lifts the status gate on SO edits so a non-admin operator can
# repair an order past the OPEN window (e.g. fix a customer-supplied
# typo on a CLOSED PO or repoint an SO's source_system after an ERP
# mis-tag). ADMIN bypasses these checks; non-admins must hold the
# override key. Kept out of ALL_PAGE_KEYS so the sidebar permission
# grid renders them in a separate "Overrides" group.
OVERRIDE_SO_FULL_EDIT = "so-full-edit"
ALL_OVERRIDE_KEYS = (
    OVERRIDE_SO_FULL_EDIT,
)

USER_ROLE_TEMPLATES = {
    ROLE_ADMIN: {
        "page_keys": ALL_PAGE_KEYS,
        "functions": ALL_MOBILE_FUNCTIONS,
    },
    ROLE_USER: {
        "page_keys": ("dashboard",),
        "functions": (),
    },
    ROLE_SHIPYARD_MANAGER: {
        "page_keys": (
            "dashboard",
            "inventory",
            "inbound",
            "shipyard-outbound",
            "shipyard-settings",
            "audit-log",
        ),
        "functions": (
            "receive",
            "count",
            "transfer",
            "shipyard_receive",
            "shipyard_outbound",
        ),
    },
    ROLE_RECEIVER: {
        "page_keys": ("dashboard", "inbound"),
        "functions": ("shipyard_receive",),
    },
    ROLE_OUTBOUND: {
        "page_keys": ("dashboard", "inventory", "shipyard-outbound"),
        "functions": ("shipyard_outbound",),
    },
    ROLE_VIEWER: {
        "page_keys": (
            "dashboard",
            "inventory",
            "inbound",
            "shipyard-outbound",
            "audit-log",
        ),
        "functions": (),
    },
}

# SO mutation audit actions (mig 062). Mirror the PO line
# CRUD coverage so the "what was on this order before it shipped"
# question survives any post-OPEN edit. details JSONB carries the
# before/after diff so an investigator can reconstruct the change
# without scanning the row itself.
ACTION_SO_LINE_ADDED = "SO_LINE_ADDED"
ACTION_SO_LINE_UPDATED = "SO_LINE_UPDATED"
ACTION_SO_LINE_REMOVED = "SO_LINE_REMOVED"
ACTION_SO_HEADER_EDITED = "SO_HEADER_EDITED"
ACTION_SO_SOURCE_SYSTEM_REASSIGNED = "SO_SOURCE_SYSTEM_REASSIGNED"
ACTION_SO_ALLOCATION_RELEASED = "SO_ALLOCATION_RELEASED"
# so-refinement: admin reverts an SO from PICKED/PACKED/SHIPPED back
# to an earlier status. One STATUS_REVERTED row per request carries
# {from_status, to_status}; the per-effect rows below carry the line
# / pick_task detail so investigators can reconstruct the unwind.
ACTION_SO_STATUS_REVERTED = "SO_STATUS_REVERTED"
ACTION_SO_PICK_RELEASED = "SO_PICK_RELEASED"
ACTION_SO_UNPACKED = "SO_UNPACKED"
ACTION_SO_UNSHIPPED = "SO_UNSHIPPED"
