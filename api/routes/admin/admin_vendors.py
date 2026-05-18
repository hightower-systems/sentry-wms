"""Admin CRUD for the canonical vendors table.

The upstream vendors table ships without an admin-facing endpoint;
avid-overhaul-mk1 P5.1 adds one so operators can manage the roster
without round-tripping through the inbound pipe. The items.vendor_id
linkage (originally PR #13 mig 058) is deferred — soft-archive via
is_active=false remains the supported way to retire a vendor.
"""

import math

from flask import g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_admin_or_page_permission, require_auth
from middleware.db import with_db
from routes.admin import admin_bp
from schemas.vendors import CreateVendorRequest, UpdateVendorRequest
from utils.validation import validate_body


def _vendor_row_to_dict(row):
    return {
        "canonical_id": str(row.canonical_id),
        "external_id": str(row.external_id),
        "vendor_name": row.vendor_name,
        "contact_name": row.contact_name,
        "email": row.email,
        "phone": row.phone,
        "billing_address": row.billing_address,
        "remit_to_address": row.remit_to_address,
        "tax_id": row.tax_id,
        "payment_terms": row.payment_terms,
        "is_active": row.is_active,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@admin_bp.route("/vendors", methods=["GET"])
@require_auth
@require_admin_or_page_permission("vendors")
@with_db
def list_vendors():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 1000)
    search = (request.args.get("q") or "").strip()
    # Same active-vs-archived axis the Items page uses. Default 'active'
    # so retired vendors do not clutter the picker.
    active = request.args.get("active")

    where_clauses, params = [], {}
    if search:
        where_clauses.append(
            "(vendor_name ILIKE :q OR contact_name ILIKE :q OR email ILIKE :q)"
        )
        params["q"] = f"%{search}%"
    if active is not None:
        where_clauses.append("is_active = :active")
        params["active"] = active.lower() == "true"

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    total = g.db.execute(
        text(f"SELECT COUNT(*) FROM vendors {where_sql}"),
        params,
    ).scalar()
    pages = max(1, math.ceil(total / per_page)) if total else 1

    params["limit"] = per_page
    params["offset"] = (page - 1) * per_page
    rows = g.db.execute(
        text(
            f"""
            SELECT canonical_id, external_id, vendor_name, contact_name,
                   email, phone, billing_address, remit_to_address,
                   tax_id, payment_terms, is_active, created_at, updated_at
              FROM vendors {where_sql}
             ORDER BY vendor_name NULLS LAST, canonical_id
             LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).fetchall()

    return jsonify({
        "vendors": [_vendor_row_to_dict(r) for r in rows],
        "total": total, "page": page, "per_page": per_page, "pages": pages,
    })


@admin_bp.route("/vendors/<uuid:canonical_id>", methods=["GET"])
@require_auth
@require_admin_or_page_permission("vendors")
@with_db
def get_vendor(canonical_id):
    row = g.db.execute(
        text(
            """
            SELECT canonical_id, external_id, vendor_name, contact_name,
                   email, phone, billing_address, remit_to_address,
                   tax_id, payment_terms, is_active, created_at, updated_at
              FROM vendors WHERE canonical_id = :cid
            """
        ),
        {"cid": str(canonical_id)},
    ).fetchone()
    if not row:
        return jsonify({"error": "Vendor not found"}), 404
    return jsonify({"vendor": _vendor_row_to_dict(row)})


@admin_bp.route("/vendors", methods=["POST"])
@require_auth
@require_admin_or_page_permission("vendors")
@validate_body(CreateVendorRequest)
@with_db
def create_vendor(validated):
    data = validated.model_dump()
    row = g.db.execute(
        text(
            """
            INSERT INTO vendors (
                vendor_name, contact_name, email, phone,
                billing_address, remit_to_address, tax_id,
                payment_terms, is_active
            ) VALUES (
                :vendor_name, :contact_name, :email, :phone,
                :billing_address, :remit_to_address, :tax_id,
                :payment_terms, :is_active
            )
            RETURNING canonical_id, external_id, vendor_name, contact_name,
                      email, phone, billing_address, remit_to_address,
                      tax_id, payment_terms, is_active, created_at, updated_at
            """
        ),
        data,
    ).fetchone()
    g.db.commit()
    return jsonify({"vendor": _vendor_row_to_dict(row)}), 201


@admin_bp.route("/vendors/<uuid:canonical_id>", methods=["PUT"])
@require_auth
@require_admin_or_page_permission("vendors")
@validate_body(UpdateVendorRequest)
@with_db
def update_vendor(canonical_id, validated):
    data = validated.model_dump(exclude_unset=True)
    if not data:
        return jsonify({"error": "No valid fields provided"}), 400

    existing = g.db.execute(
        text("SELECT canonical_id FROM vendors WHERE canonical_id = :cid"),
        {"cid": str(canonical_id)},
    ).fetchone()
    if not existing:
        return jsonify({"error": "Vendor not found"}), 404

    set_fragments = [f"{col} = :{col}" for col in data]
    set_fragments.append("updated_at = NOW()")
    params = {**data, "cid": str(canonical_id)}
    row = g.db.execute(
        text(
            f"""
            UPDATE vendors SET {', '.join(set_fragments)}
             WHERE canonical_id = :cid
            RETURNING canonical_id, external_id, vendor_name, contact_name,
                      email, phone, billing_address, remit_to_address,
                      tax_id, payment_terms, is_active, created_at, updated_at
            """
        ),
        params,
    ).fetchone()
    g.db.commit()
    return jsonify({"vendor": _vendor_row_to_dict(row)})


@admin_bp.route("/vendors/<uuid:canonical_id>", methods=["DELETE"])
@require_auth
@require_admin_or_page_permission("vendors")
@with_db
def delete_vendor(canonical_id):
    existing = g.db.execute(
        text("SELECT canonical_id FROM vendors WHERE canonical_id = :cid"),
        {"cid": str(canonical_id)},
    ).fetchone()
    if not existing:
        return jsonify({"error": "Vendor not found"}), 404

    g.db.execute(
        text("DELETE FROM vendors WHERE canonical_id = :cid"),
        {"cid": str(canonical_id)},
    )
    g.db.commit()
    return jsonify({"deleted": True})
