"""
ERP Connector Stub - placeholder for future Phase 7 integration.

When the ERP connector framework is built, this module will be replaced
with actual connector logic that pulls orders, items, and inventory
from upstream systems (NetSuite, QuickBooks, SAP, etc.).
"""


def enrich_order(so_barcode, warehouse_id):
    """
    Future: attempt to pull a missing SO from the connected ERP.

    Called when a barcode scan doesn't match any SO in the local database.
    If a connector is configured and the order exists in the ERP, it will
    be synced into PostgreSQL and this function returns the new SO record.

    Returns None for now - no connector configured.
    """
    return None
