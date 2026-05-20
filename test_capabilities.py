"""Suite exhaustiva de capacidades Siigo API.

Estrategia:
  - Cada prueba registra los IDs creados en `cleanup_registry`.
  - Al final (try/finally) se intenta DELETE de todo.
  - Si algun DELETE falla, se reporta para limpieza manual.

Salida: explorations/CAPACIDADES.md + explorations/capabilities_raw.json
"""
from __future__ import annotations

import json
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import httpx

from siigo_client import SiigoClient


OUT_DIR = Path(__file__).parent / "explorations"
OUT_DIR.mkdir(exist_ok=True)

# IDs descubiertos en exploracion previa
IDS = {
    "doc_type_fv_electronica": 27703,
    "doc_type_fv_tradicional": 13214,
    "doc_type_fc_materias": 13219,
    "doc_type_fc_gasto": 27394,
    "iva_19": 7108,
    "iva_0": 13999,
    "payment_efectivo": 3043,
    "payment_credito": 3044,
    "payment_banco_ahorros": 8104,
    "seller_admin": 341,
}

# Registry de cleanup: (label, callable_que_borra)
cleanup_registry: list[tuple[str, Callable[[], Any]]] = []

# Resultados por prueba
results: list[dict[str, Any]] = []


def record(name: str, status: str, detail: Any = None, *, capability: str | None = None) -> None:
    results.append(
        {
            "name": name,
            "capability": capability or name,
            "status": status,
            "detail": detail,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
    )
    flag = {"OK": "[OK]", "FAIL": "[FAIL]", "SKIP": "[SKIP]", "PARTIAL": "[~~]"}.get(status, "[??]")
    print(f"{flag} {name}", flush=True)
    if status != "OK" and detail is not None:
        snip = str(detail)
        if len(snip) > 300:
            snip = snip[:300] + "..."
        print(f"     -> {snip}", flush=True)


def try_call(name: str, fn: Callable[[], Any], *, capability: str | None = None) -> Any:
    try:
        out = fn()
        record(name, "OK", detail=None, capability=capability)
        return out
    except httpx.HTTPStatusError as e:
        body = e.response.text[:500]
        record(name, "FAIL", detail=f"HTTP {e.response.status_code}: {body}", capability=capability)
        return None
    except Exception as e:
        record(name, "FAIL", detail=f"{type(e).__name__}: {e}", capability=capability)
        traceback.print_exc()
        return None


# =============================================================================
# BLOQUE 1 - LECTURAS AVANZADAS
# =============================================================================

def test_read_capabilities(siigo: SiigoClient) -> None:
    print("\n" + "=" * 70)
    print("BLOQUE 1: LECTURAS AVANZADAS")
    print("=" * 70)

    # 1.1 Filtro por fecha created_start en invoices
    last_30d = (date.today() - timedelta(days=30)).isoformat()
    out = try_call(
        "1.1 invoices filtrado por created_start (ultimos 30 dias)",
        lambda: siigo.get("/v1/invoices", params={"created_start": last_30d, "page_size": 5}),
        capability="filter_invoices_by_date",
    )
    if out:
        n = len(out.get("results", []))
        total = out.get("pagination", {}).get("total_results")
        print(f"     -> {n} en pagina, total {total}")

    # 1.2 Filtro por fecha date_start
    out = try_call(
        "1.2 invoices filtrado por date_start",
        lambda: siigo.get("/v1/invoices", params={"date_start": last_30d, "page_size": 5}),
        capability="filter_invoices_by_doc_date",
    )

    # 1.3 Busqueda de cliente por identificacion
    out = try_call(
        "1.3 customers filtrado por identification",
        lambda: siigo.get("/v1/customers", params={"identification": "32160242"}),
        capability="search_customer_by_id",
    )
    if out and out.get("results"):
        print(f"     -> encontrado: {out['results'][0]['name']}")

    # 1.4 Busqueda de producto por code
    out = try_call(
        "1.4 products filtrado por code",
        lambda: siigo.get("/v1/products", params={"code": "P23"}),
        capability="search_product_by_code",
    )
    if out and out.get("results"):
        print(f"     -> encontrado: {out['results'][0]['name']}")

    # 1.5 Detalle de factura por id (la primera de la muestra)
    sample = OUT_DIR / "invoices_sample.json"
    if sample.exists():
        first = json.loads(sample.read_text(encoding="utf-8"))["results"][0]
        inv_id = first["id"]
        out = try_call(
            f"1.5 invoice detalle by id ({inv_id[:8]}...)",
            lambda: siigo.get(f"/v1/invoices/{inv_id}"),
            capability="get_invoice_by_id",
        )

    # 1.6 PDF de factura (si endpoint existe)
    if sample.exists():
        first = json.loads(sample.read_text(encoding="utf-8"))["results"][0]
        inv_id = first["id"]
        try_call(
            f"1.6 invoice PDF ({inv_id[:8]}...)",
            lambda: siigo.get(f"/v1/invoices/{inv_id}/pdf"),
            capability="get_invoice_pdf",
        )

    # 1.7 Page-size grande
    try_call(
        "1.7 customers page_size=100",
        lambda: siigo.get("/v1/customers", params={"page_size": 100, "page": 1}),
        capability="pagination_100",
    )

    # 1.8 modified_start (para sync incremental)
    try_call(
        "1.8 customers con modified_start",
        lambda: siigo.get("/v1/customers", params={"modified_start": last_30d, "page_size": 5}),
        capability="filter_by_modified_start",
    )

    # 1.9 Listar webhooks subscritos
    try_call(
        "1.9 GET /v1/webhooks",
        lambda: siigo.get("/v1/webhooks"),
        capability="list_webhooks",
    )

    # 1.10 GET de una compra existente
    sample = OUT_DIR / "purchases_sample.json"
    if sample.exists():
        first = json.loads(sample.read_text(encoding="utf-8"))["results"][0]
        pid = first["id"]
        try_call(
            f"1.10 purchase detalle by id ({pid[:8]}...)",
            lambda: siigo.get(f"/v1/purchases/{pid}"),
            capability="get_purchase_by_id",
        )


# =============================================================================
# BLOQUE 2 - CLIENTE: CRUD
# =============================================================================

def test_customer_crud(siigo: SiigoClient) -> str | None:
    print("\n" + "=" * 70)
    print("BLOQUE 2: CLIENTE - CRUD")
    print("=" * 70)

    # 2.1 Crear cliente
    suffix = int(time.time())
    fake_nit = f"9{suffix % 100000000:08d}"  # NIT ficticio 9-prefijado
    payload = {
        "type": "Customer",
        "person_type": "Person",
        "id_type": "13",  # cedula
        "identification": fake_nit,
        "name": ["TEST BOT", "CLIENTE PRUEBA"],
        "commercial_name": "TEST BOT CLIENTE",
        "branch_office": 0,
        "active": True,
        "vat_responsible": False,
        "fiscal_responsibilities": [{"code": "R-99-PN"}],
        "address": {
            "address": "CALLE TEST 123",
            "city": {"country_code": "Co", "state_code": "05", "city_code": "05001"},
            "postal_code": "050001",
        },
        "phones": [{"indicative": "57", "number": "3001234567"}],
        "contacts": [
            {
                "first_name": "TEST",
                "last_name": "BOT",
                "email": "test-bot@skiimo.local",
                "phone": {"indicative": "57", "number": "3001234567"},
            }
        ],
    }
    created = try_call("2.1 POST /v1/customers (crear)", lambda: siigo.post("/v1/customers", payload), capability="create_customer")
    if not created or not created.get("id"):
        return None
    cid = created["id"]
    print(f"     -> id: {cid}, identification: {created.get('identification')}")
    cleanup_registry.append((f"customer {cid}", lambda: siigo.delete(f"/v1/customers/{cid}")))

    # 2.2 GET del recien creado
    try_call(
        "2.2 GET /v1/customers/{id}",
        lambda: siigo.get(f"/v1/customers/{cid}"),
        capability="get_customer_by_id",
    )

    # 2.3 PUT (update) -> cambiar nombre comercial
    updated = dict(created)
    updated["commercial_name"] = "TEST BOT CLIENTE - ACTUALIZADO"
    # PUT en Siigo a veces requiere solo el delta; probamos
    try_call(
        "2.3 PUT /v1/customers/{id} (actualizar)",
        lambda: siigo.put(f"/v1/customers/{cid}", {"commercial_name": "TEST BOT CLIENTE - ACTUALIZADO"}),
        capability="update_customer",
    )

    return cid


# =============================================================================
# BLOQUE 3 - PRODUCTO: CRUD
# =============================================================================

def test_product_crud(siigo: SiigoClient) -> str | None:
    print("\n" + "=" * 70)
    print("BLOQUE 3: PRODUCTO - CRUD")
    print("=" * 70)

    code = f"TST{int(time.time()) % 100000}"
    payload = {
        "code": code,
        "name": "PRODUCTO TEST BOT",
        "account_group": 1755,  # Materias Primas (existente)
        "type": "Product",
        "stock_control": False,
        "active": True,
        "tax_classification": "Taxed",
        "tax_included": False,
        "taxes": [{"id": IDS["iva_19"]}],
        "unit_label": "unidad",
        "reference": "TEST",
        "description": "Producto creado por suite de pruebas",
        "prices": [
            {
                "currency_code": "COP",
                "price_list": [{"position": 1, "value": 1000.0}],
            }
        ],
    }
    created = try_call(
        "3.1 POST /v1/products (crear)",
        lambda: siigo.post("/v1/products", payload),
        capability="create_product",
    )
    if not created or not created.get("id"):
        return None
    pid = created["id"]
    print(f"     -> id: {pid}, code: {created.get('code')}")
    cleanup_registry.append((f"product {pid}", lambda: siigo.delete(f"/v1/products/{pid}")))

    # GET
    try_call(
        "3.2 GET /v1/products/{id}",
        lambda: siigo.get(f"/v1/products/{pid}"),
        capability="get_product_by_id",
    )

    return pid


# =============================================================================
# BLOQUE 4 - FACTURA DE VENTA (FV)
# =============================================================================

def test_create_invoice(siigo: SiigoClient, customer_id: str | None, product_code: str | None) -> str | None:
    print("\n" + "=" * 70)
    print("BLOQUE 4: FACTURA DE VENTA")
    print("=" * 70)

    if not customer_id:
        record("4.1 POST /v1/invoices", "SKIP", detail="Sin cliente de prueba", capability="create_invoice")
        return None

    # Obtener identification del cliente
    cust = siigo.get(f"/v1/customers/{customer_id}")
    cust_ident = cust["identification"]

    # Producto: usamos uno existente real para evitar dependencias
    product_code_real = product_code or "P23"  # existe en la cuenta

    # Probamos primero con factura tradicional (id 13214) que NO dispara DIAN inmediatamente
    payload = {
        "document": {"id": IDS["doc_type_fv_tradicional"]},
        "date": date.today().isoformat(),
        "customer": {"identification": cust_ident, "branch_office": 0},
        "seller": IDS["seller_admin"],
        "observations": "FACTURA DE PRUEBA - SUITE BOT - IGNORAR",
        "items": [
            {
                "code": product_code_real,
                "description": "ITEM DE PRUEBA",
                "quantity": 1.0,
                "price": 100.0,
                "taxes": [{"id": IDS["iva_19"]}],
            }
        ],
        "payments": [{"id": IDS["payment_efectivo"], "value": 119.0}],
    }
    created = try_call(
        "4.1 POST /v1/invoices (factura tradicional)",
        lambda: siigo.post("/v1/invoices", payload),
        capability="create_invoice_fv_tradicional",
    )
    if not created or not created.get("id"):
        return None
    iid = created["id"]
    print(f"     -> id: {iid}, name: {created.get('name')}, total: {created.get('total')}")
    print(f"     -> PDF: {created.get('public_url', '(sin url)')}")
    cleanup_registry.append((f"invoice {iid}", lambda: siigo.delete(f"/v1/invoices/{iid}")))
    return iid


def test_credit_note(siigo: SiigoClient, invoice_id: str | None, customer_id: str | None) -> str | None:
    print("\nBLOQUE 4b: NOTA CREDITO")
    if not invoice_id:
        record("4b POST /v1/credit-notes", "SKIP", detail="Sin factura de prueba", capability="create_credit_note")
        return None

    # Buscar tipo de nota credito
    nc_types = siigo.get("/v1/document-types", params={"type": "NC"})
    if not nc_types:
        record("4b POST /v1/credit-notes", "SKIP", detail="No hay document_types NC")
        return None
    nc_doc_id = nc_types[0]["id"]

    cust = siigo.get(f"/v1/customers/{customer_id}")
    payload = {
        "document": {"id": nc_doc_id},
        "date": date.today().isoformat(),
        "invoice": invoice_id,
        "cause": "1",
        "customer": {"identification": cust["identification"], "branch_office": 0},
        "items": [
            {
                "code": "P23",
                "description": "ITEM DEVOLUCION PRUEBA",
                "quantity": 1.0,
                "price": 100.0,
                "taxes": [{"id": IDS["iva_19"]}],
            }
        ],
        "payments": [{"id": IDS["payment_efectivo"], "value": 119.0}],
        "observations": "NOTA CREDITO DE PRUEBA - SUITE BOT - IGNORAR",
    }
    created = try_call(
        "4b POST /v1/credit-notes",
        lambda: siigo.post("/v1/credit-notes", payload),
        capability="create_credit_note",
    )
    if created and created.get("id"):
        ncid = created["id"]
        print(f"     -> id: {ncid}, name: {created.get('name')}")
        cleanup_registry.append((f"credit_note {ncid}", lambda: siigo.delete(f"/v1/credit-notes/{ncid}")))
        return ncid
    return None


# =============================================================================
# BLOQUE 5 - FACTURA DE COMPRA (FC)
# =============================================================================

def test_create_purchase(siigo: SiigoClient, supplier_id: str | None) -> str | None:
    print("\n" + "=" * 70)
    print("BLOQUE 5: FACTURA DE COMPRA")
    print("=" * 70)

    if not supplier_id:
        # Buscar un proveedor existente
        suppliers_resp = siigo.get("/v1/customers", params={"type": "Supplier", "page_size": 5})
        results_list = suppliers_resp.get("results", [])
        if not results_list:
            record("5.1 POST /v1/purchases", "SKIP", detail="No hay proveedores en la cuenta", capability="create_purchase")
            return None
        supplier = results_list[0]
        sup_ident = supplier["identification"]
    else:
        cust = siigo.get(f"/v1/customers/{supplier_id}")
        sup_ident = cust["identification"]

    suffix = int(time.time()) % 1000000
    payload = {
        "document": {"id": IDS["doc_type_fc_gasto"]},
        "date": date.today().isoformat(),
        "supplier": {"identification": sup_ident, "branch_office": 0},
        "provider_invoice": {"prefix": "TST", "number": str(suffix)},
        "discount_type": "Value",
        "supplier_by_item": False,
        "observations": "COMPRA DE PRUEBA - SUITE BOT - IGNORAR",
        "items": [
            {
                "type": "Product",
                "code": "AC1",  # ACIDO CITRICO, existente
                "description": "GASTO DE PRUEBA",
                "quantity": 1.0,
                "price": 100.0,
                "discount": 0.0,
                "taxes": [{"id": IDS["iva_19"]}],
            }
        ],
        "payments": [{"id": IDS["payment_efectivo"], "value": 119.0}],
    }
    created = try_call(
        "5.1 POST /v1/purchases (factura compra GASTO)",
        lambda: siigo.post("/v1/purchases", payload),
        capability="create_purchase_gasto",
    )
    if not created or not created.get("id"):
        return None
    pid = created["id"]
    print(f"     -> id: {pid}, name: {created.get('name')}")
    cleanup_registry.append((f"purchase {pid}", lambda: siigo.delete(f"/v1/purchases/{pid}")))
    return pid


# =============================================================================
# BLOQUE 6 - EXTRAS
# =============================================================================

def test_extras(siigo: SiigoClient) -> None:
    print("\n" + "=" * 70)
    print("BLOQUE 6: EXTRAS (webhooks, vouchers POST, journals)")
    print("=" * 70)

    # 6.1 POST recibo de caja (voucher)
    try_call(
        "6.1 GET /v1/document-types?type=RC",
        lambda: siigo.get("/v1/document-types", params={"type": "RC"}),
        capability="list_rc_doc_types",
    )

    # 6.2 Listar webhooks
    try_call(
        "6.2 GET /v1/webhooks (capabilidad ya verificada en bloque 1.9)",
        lambda: siigo.get("/v1/webhooks"),
        capability="webhooks_supported",
    )

    # 6.3 POST webhook (intento de suscripcion)
    webhook_payload = {
        "url": "https://example.com/webhook-test-skiimo",
        "events": ["invoice.created"],
    }
    out = try_call(
        "6.3 POST /v1/webhooks (test suscripcion)",
        lambda: siigo.post("/v1/webhooks", webhook_payload),
        capability="create_webhook",
    )
    if out and out.get("id"):
        wid = out["id"]
        cleanup_registry.append((f"webhook {wid}", lambda: siigo.delete(f"/v1/webhooks/{wid}")))


# =============================================================================
# CLEANUP
# =============================================================================

def cleanup(siigo: SiigoClient) -> None:
    print("\n" + "=" * 70)
    print(f"CLEANUP: borrando {len(cleanup_registry)} recursos creados")
    print("=" * 70)

    # Orden inverso (lo mas reciente primero, ej: nota credito antes que factura)
    fails: list[str] = []
    for label, fn in reversed(cleanup_registry):
        try:
            fn()
            print(f"  [OK] borrado: {label}")
        except httpx.HTTPStatusError as e:
            msg = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            print(f"  [FAIL] {label}: {msg}")
            fails.append(f"{label} -> {msg}")
        except Exception as e:
            print(f"  [FAIL] {label}: {e}")
            fails.append(f"{label} -> {e}")

    if fails:
        print("\nLIMPIEZA MANUAL PENDIENTE:")
        for f in fails:
            print(f"  - {f}")
    return None


# =============================================================================
# REPORTE
# =============================================================================

def write_report() -> None:
    raw_path = OUT_DIR / "capabilities_raw.json"
    raw_path.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    md = ["# Matriz de capacidades — Siigo API (Skiimo)"]
    md.append("")
    md.append(f"Ejecutado: {datetime.now().isoformat(timespec='seconds')}")
    md.append("")
    md.append("Leyenda: OK = funciona; FAIL = no funciona o endpoint no existe; SKIP = no probado por dependencia.")
    md.append("")
    md.append("| # | Prueba | Capacidad | Resultado | Detalle |")
    md.append("|---|---|---|---|---|")
    for i, r in enumerate(results, 1):
        det = r.get("detail") or ""
        if det:
            det = str(det).replace("|", "\\|").replace("\n", " ")
            if len(det) > 120:
                det = det[:120] + "..."
        md.append(f"| {i} | {r['name']} | `{r['capability']}` | **{r['status']}** | {det} |")

    md.append("")
    md.append("## Resumen por bloque")
    md.append("")
    ok = sum(1 for r in results if r["status"] == "OK")
    fail = sum(1 for r in results if r["status"] == "FAIL")
    skip = sum(1 for r in results if r["status"] == "SKIP")
    md.append(f"- OK: {ok}")
    md.append(f"- FAIL: {fail}")
    md.append(f"- SKIP: {skip}")
    md.append(f"- Total: {len(results)}")

    (OUT_DIR / "CAPACIDADES.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\nReporte: {OUT_DIR / 'CAPACIDADES.md'}")


# =============================================================================
# MAIN
# =============================================================================

def main() -> int:
    with SiigoClient() as siigo:
        siigo._authenticate()
        print("Autenticacion OK\n")

        cust_id: str | None = None
        prod_id: str | None = None
        inv_id: str | None = None
        try:
            test_read_capabilities(siigo)
            cust_id = test_customer_crud(siigo)
            prod_id = test_product_crud(siigo)
            inv_id = test_create_invoice(siigo, cust_id, None)
            test_credit_note(siigo, inv_id, cust_id)
            test_create_purchase(siigo, None)
            test_extras(siigo)
        finally:
            cleanup(siigo)
            write_report()

    return 0


if __name__ == "__main__":
    main()
