"""Pruebas extra: cosas que olvidamos en la suite principal."""
from __future__ import annotations

import json
import time
import traceback
from datetime import date
from pathlib import Path

import httpx
from siigo_client import SiigoClient


OUT = Path(__file__).parent / "explorations"
results: list[dict] = []


def t(name, fn, capability=None):
    try:
        out = fn()
        results.append({"name": name, "capability": capability or name, "status": "OK", "detail": None})
        print(f"[OK] {name}")
        return out
    except httpx.HTTPStatusError as e:
        d = f"HTTP {e.response.status_code}: {e.response.text[:250]}"
        results.append({"name": name, "capability": capability or name, "status": "FAIL", "detail": d})
        print(f"[FAIL] {name}\n   -> {d}")
    except Exception as e:
        results.append({"name": name, "capability": capability or name, "status": "FAIL", "detail": f"{type(e).__name__}: {e}"})
        print(f"[FAIL] {name}\n   -> {e}")


def main():
    with SiigoClient() as s:
        s._authenticate()

        # E1. GET tax types - mas detallado
        print("\n--- E1: catálogos faltantes ---")
        t("GET /v1/id-types (tipos de identificacion)", lambda: s.get("/v1/id-types"))
        t("GET /v1/cities (lista de ciudades Colombia)", lambda: s.get("/v1/cities", params={"page_size": 5}))
        t("GET /v1/cities sin params", lambda: s.get("/v1/cities"))

        # E2. Filtros mas complejos en invoices
        print("\n--- E2: filtros complejos ---")
        t(
            "GET /v1/invoices filtro por NIT cliente",
            lambda: s.get("/v1/invoices", params={"customer_identification": "32160242", "page_size": 5}),
            capability="filter_invoices_by_customer_nit",
        )
        t(
            "GET /v1/invoices filtro por vendedor",
            lambda: s.get("/v1/invoices", params={"seller": 341, "page_size": 5}),
            capability="filter_invoices_by_seller",
        )

        # E3. Reportes / agregaciones (endpoint hipotetico)
        print("\n--- E3: reportes/agregados ---")
        t("GET /v1/reports (existe?)", lambda: s.get("/v1/reports"), capability="reports_endpoint")
        t("GET /v1/cash-flow (existe?)", lambda: s.get("/v1/cash-flow"), capability="cashflow_endpoint")

        # E4. Webhooks: schema y app
        print("\n--- E4: webhooks ---")
        t("GET /v1/webhooks (todos)", lambda: s.get("/v1/webhooks"))
        t("GET /v1/applications", lambda: s.get("/v1/applications"))

        # E5. Suppliers vs Customers
        print("\n--- E5: separacion suppliers ---")
        t("GET /v1/customers?type=Supplier", lambda: s.get("/v1/customers", params={"type": "Supplier", "page_size": 5}))
        t("GET /v1/suppliers (endpoint dedicado?)", lambda: s.get("/v1/suppliers"))

        # E6. Vouchers (recibos de caja) POST
        print("\n--- E6: recibo de caja POST ---")
        # Buscar RC document type
        rc_types = s.get("/v1/document-types", params={"type": "RC"})
        if rc_types:
            print(f"   RC doc types disponibles: {[(d['id'], d['name']) for d in rc_types]}")

        # E7. Detalle de notas credito
        print("\n--- E7: notas credito ---")
        ncs = t("GET /v1/credit-notes (listar)", lambda: s.get("/v1/credit-notes", params={"page_size": 3}))

        # E8. Tax classification - aplica impoconsumo?
        print("\n--- E8: tax classifications ---")
        t("GET /v1/tax-classifications", lambda: s.get("/v1/tax-classifications"))

        # E9. Crear cliente con campos minimos (entender requisitos)
        print("\n--- E9: variantes de cliente ---")
        suf = int(time.time()) % 1000000
        minimal = {
            "person_type": "Person",
            "id_type": "13",
            "identification": f"99{suf:06d}",
            "name": ["MINIMAL TEST"],
            "branch_office": 0,
        }
        out = t(
            "POST /v1/customers MINIMO",
            lambda: s.post("/v1/customers", minimal),
            capability="customer_minimal_fields",
        )
        if out and out.get("id"):
            cid = out["id"]
            # marcar inactivo inmediatamente
            try:
                s.put(f"/v1/customers/{cid}", {**out, "active": False, "commercial_name": "ZZZ MIN TEST INACTIVO"})
                print(f"   -> {cid} marcado inactivo")
            except Exception as e:
                print(f"   -> no se pudo desactivar: {e}")

        # E10. Schema de un product en detalle (mostrar campos opcionales)
        print("\n--- E10: schema producto ---")
        prods = s.get("/v1/products", params={"page_size": 1, "page": 1})
        if prods.get("results"):
            p = prods["results"][0]
            print(f"   campos en product: {list(p.keys())}")

    # Reporte
    md = ["# Pruebas extra de capacidades", ""]
    md.append("| Prueba | Capacidad | Resultado |")
    md.append("|---|---|---|")
    for r in results:
        md.append(f"| {r['name']} | `{r['capability']}` | **{r['status']}** |")
    (OUT / "CAPACIDADES_EXTRAS.md").write_text("\n".join(md), encoding="utf-8")
    print("\nReporte: explorations/CAPACIDADES_EXTRAS.md")


if __name__ == "__main__":
    main()
