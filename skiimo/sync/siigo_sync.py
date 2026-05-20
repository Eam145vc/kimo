"""Sync incremental Siigo -> SQLite.

Estrategia:
  - Primera corrida: baja todo desde page 1.
  - Siguientes: usa modified_start (clientes/productos) y created_start (facturas/compras).
  - Upsert por id.

Uso:
  python -m skiimo.sync.siigo_sync --full       # baja todo
  python -m skiimo.sync.siigo_sync               # incremental
  python -m skiimo.sync.siigo_sync --only customers products
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

# Permitir ejecucion como modulo desde root del proyecto
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skiimo.db.schema import get_conn, init_db
from siigo_client import SiigoClient


PAGE_SIZE = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_state(conn, entity: str) -> dict | None:
    row = conn.execute("SELECT * FROM sync_state WHERE entity = ?", (entity,)).fetchone()
    return dict(row) if row else None


def _set_state(conn, entity: str, cursor: str, items: int) -> None:
    conn.execute(
        """
        INSERT INTO sync_state (entity, last_sync_at, last_cursor, items_synced)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(entity) DO UPDATE SET
            last_sync_at = excluded.last_sync_at,
            last_cursor  = excluded.last_cursor,
            items_synced = sync_state.items_synced + excluded.items_synced
        """,
        (entity, _now(), cursor, items),
    )


def _paginate(
    siigo: SiigoClient,
    path: str,
    params: dict | None = None,
    max_pages: int = 500,
) -> list[dict]:
    out: list[dict] = []
    params = dict(params or {})
    params["page_size"] = PAGE_SIZE
    page = 1
    while page <= max_pages:
        params["page"] = page
        data = siigo.get(path, params=params)
        results = data.get("results", []) if isinstance(data, dict) else data
        if not results:
            break
        out.extend(results)
        pag = data.get("pagination") if isinstance(data, dict) else None
        if pag:
            total = pag.get("total_results", 0)
            if len(out) >= total:
                break
        if len(results) < PAGE_SIZE:
            break
        page += 1
    return out


# =============================================================================
# CUSTOMERS
# =============================================================================

def sync_customers(siigo: SiigoClient, conn, *, full: bool) -> int:
    print("[sync] customers...")
    state = _get_state(conn, "customers")
    params: dict[str, Any] = {}
    if not full and state and state.get("last_cursor"):
        params["modified_start"] = state["last_cursor"]

    items = _paginate(siigo, "/v1/customers", params=params)
    cursor = _now().split("+")[0]  # yyyy-mm-ddThh:mm:ss
    n = 0
    for c in items:
        name_parts = c.get("name") or []
        name = " ".join(p for p in name_parts if p).strip() if isinstance(name_parts, list) else str(name_parts or "")
        email = ""
        contacts = c.get("contacts") or []
        if contacts:
            email = contacts[0].get("email", "") or ""
        phone = ""
        phones = c.get("phones") or []
        if phones:
            p = phones[0]
            phone = f"{p.get('indicative', '')}{p.get('number', '')}".strip()
        addr = c.get("address") or {}
        addr_str = addr.get("address", "") if isinstance(addr, dict) else ""

        conn.execute(
            """
            INSERT INTO siigo_customers (
                id, type, identification, name, commercial_name, person_type,
                active, email, phone, address, raw, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                type=excluded.type,
                identification=excluded.identification,
                name=excluded.name,
                commercial_name=excluded.commercial_name,
                person_type=excluded.person_type,
                active=excluded.active,
                email=excluded.email,
                phone=excluded.phone,
                address=excluded.address,
                raw=excluded.raw,
                updated_at=excluded.updated_at
            """,
            (
                c["id"],
                c.get("type", "Customer"),
                c.get("identification", ""),
                name,
                c.get("commercial_name", ""),
                c.get("person_type", ""),
                1 if c.get("active", True) else 0,
                email,
                phone,
                addr_str,
                json.dumps(c, ensure_ascii=False),
                _now(),
            ),
        )
        n += 1
    _set_state(conn, "customers", cursor, n)
    conn.commit()
    print(f"[sync] customers OK ({n} upserts)")
    return n


# =============================================================================
# PRODUCTS
# =============================================================================

def sync_products(siigo: SiigoClient, conn, *, full: bool) -> int:
    print("[sync] products...")
    state = _get_state(conn, "products")
    params: dict[str, Any] = {}
    if not full and state and state.get("last_cursor"):
        params["modified_start"] = state["last_cursor"]

    items = _paginate(siigo, "/v1/products", params=params)
    cursor = _now().split("+")[0]
    n = 0
    for p in items:
        taxes = p.get("taxes") or []
        iva_id = None
        iva_pct = None
        for t in taxes:
            if t.get("type") == "IVA":
                iva_id = t.get("id")
                iva_pct = t.get("percentage")
                break
        prices = p.get("prices") or []
        price_default = None
        if prices:
            pl = (prices[0].get("price_list") or [{}])[0]
            price_default = pl.get("value")
        ag = p.get("account_group") or {}
        conn.execute(
            """
            INSERT INTO siigo_products (
                id, code, name, account_group_id, account_group_name, type,
                active, tax_classification, tax_included, iva_tax_id, iva_percentage,
                unit_label, price_default, available_quantity, reference, description,
                raw, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                code=excluded.code, name=excluded.name,
                account_group_id=excluded.account_group_id, account_group_name=excluded.account_group_name,
                type=excluded.type, active=excluded.active,
                tax_classification=excluded.tax_classification, tax_included=excluded.tax_included,
                iva_tax_id=excluded.iva_tax_id, iva_percentage=excluded.iva_percentage,
                unit_label=excluded.unit_label, price_default=excluded.price_default,
                available_quantity=excluded.available_quantity, reference=excluded.reference,
                description=excluded.description, raw=excluded.raw, updated_at=excluded.updated_at
            """,
            (
                p["id"],
                p.get("code", ""),
                p.get("name", ""),
                ag.get("id"),
                ag.get("name", ""),
                p.get("type", ""),
                1 if p.get("active", True) else 0,
                p.get("tax_classification", ""),
                1 if p.get("tax_included") else 0,
                iva_id,
                iva_pct,
                p.get("unit_label", ""),
                price_default,
                p.get("available_quantity"),
                p.get("reference", ""),
                p.get("description", ""),
                json.dumps(p, ensure_ascii=False),
                _now(),
            ),
        )
        n += 1
    _set_state(conn, "products", cursor, n)
    conn.commit()
    print(f"[sync] products OK ({n} upserts)")
    return n


# =============================================================================
# INVOICES
# =============================================================================

def sync_invoices(siigo: SiigoClient, conn, *, full: bool, days_back: int = 90) -> int:
    """En full=False usa created_start desde last_cursor.
    En full=True baja desde days_back atras (default 90 dias) para no traer 5600 historicas.
    """
    print("[sync] invoices...")
    state = _get_state(conn, "invoices")
    params: dict[str, Any] = {}
    if not full and state and state.get("last_cursor"):
        params["created_start"] = state["last_cursor"]
    else:
        # full: traemos desde days_back atras
        start = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        params["created_start"] = start

    items = _paginate(siigo, "/v1/invoices", params=params)
    cursor = _now().split("+")[0]
    n = 0
    for inv in items:
        cust = inv.get("customer") or {}
        stamp = inv.get("stamp") or {}
        conn.execute(
            """
            INSERT INTO siigo_invoices (
                id, name, number, prefix, document_id, date, customer_id, customer_ident,
                seller_id, total, balance, stamp_status, public_url, observations,
                items_json, payments_json, raw, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                total=excluded.total, balance=excluded.balance,
                stamp_status=excluded.stamp_status, public_url=excluded.public_url,
                items_json=excluded.items_json, payments_json=excluded.payments_json,
                raw=excluded.raw, updated_at=excluded.updated_at
            """,
            (
                inv["id"],
                inv.get("name", ""),
                inv.get("number"),
                inv.get("prefix", ""),
                (inv.get("document") or {}).get("id"),
                inv.get("date", ""),
                cust.get("id"),
                cust.get("identification"),
                inv.get("seller"),
                float(inv.get("total", 0) or 0),
                float(inv.get("balance", 0) or 0) if inv.get("balance") is not None else None,
                stamp.get("status") if isinstance(stamp, dict) else None,
                inv.get("public_url", ""),
                inv.get("observations", ""),
                json.dumps(inv.get("items") or [], ensure_ascii=False),
                json.dumps(inv.get("payments") or [], ensure_ascii=False),
                json.dumps(inv, ensure_ascii=False),
                (inv.get("metadata") or {}).get("created", _now()),
                _now(),
            ),
        )
        n += 1
    _set_state(conn, "invoices", cursor, n)
    conn.commit()
    print(f"[sync] invoices OK ({n} upserts)")
    return n


# =============================================================================
# PURCHASES
# =============================================================================

def sync_purchases(siigo: SiigoClient, conn, *, full: bool, days_back: int = 180) -> int:
    print("[sync] purchases...")
    state = _get_state(conn, "purchases")
    params: dict[str, Any] = {}
    if not full and state and state.get("last_cursor"):
        params["created_start"] = state["last_cursor"]
    else:
        start = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        params["created_start"] = start

    items = _paginate(siigo, "/v1/purchases", params=params)
    cursor = _now().split("+")[0]
    n = 0
    for pur in items:
        sup = pur.get("supplier") or {}
        prov = pur.get("provider_invoice") or {}
        conn.execute(
            """
            INSERT INTO siigo_purchases (
                id, name, number, document_id, date, supplier_id, supplier_ident,
                total, balance, provider_inv_prefix, provider_inv_number, observations,
                items_json, payments_json, raw, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                total=excluded.total, balance=excluded.balance,
                items_json=excluded.items_json, payments_json=excluded.payments_json,
                raw=excluded.raw, updated_at=excluded.updated_at
            """,
            (
                pur["id"],
                pur.get("name", ""),
                pur.get("number"),
                (pur.get("document") or {}).get("id"),
                pur.get("date", ""),
                sup.get("id"),
                sup.get("identification"),
                float(pur.get("total", 0) or 0),
                float(pur.get("balance", 0) or 0) if pur.get("balance") is not None else None,
                prov.get("prefix", "") if isinstance(prov, dict) else "",
                prov.get("number", "") if isinstance(prov, dict) else "",
                pur.get("observations", ""),
                json.dumps(pur.get("items") or [], ensure_ascii=False),
                json.dumps(pur.get("payments") or [], ensure_ascii=False),
                json.dumps(pur, ensure_ascii=False),
                (pur.get("metadata") or {}).get("created", _now()),
                _now(),
            ),
        )
        n += 1
    _set_state(conn, "purchases", cursor, n)
    conn.commit()
    print(f"[sync] purchases OK ({n} upserts)")
    return n


# =============================================================================
# MAIN
# =============================================================================

ENTITIES: dict[str, Callable] = {
    "customers": sync_customers,
    "products": sync_products,
    "invoices": sync_invoices,
    "purchases": sync_purchases,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="Reset cursor y baja todo (limitado a window)")
    ap.add_argument("--only", nargs="+", choices=list(ENTITIES), help="Entidades especificas")
    args = ap.parse_args()

    init_db()
    started = time.time()
    with SiigoClient() as siigo:
        siigo._authenticate()
        conn = get_conn()
        try:
            entities = args.only or list(ENTITIES)
            totals: dict[str, int] = {}
            for e in entities:
                totals[e] = ENTITIES[e](siigo, conn, full=args.full)
        finally:
            conn.close()

    elapsed = time.time() - started
    print(f"\nSync terminado en {elapsed:.1f}s")
    print("Totales:")
    for k, v in totals.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
