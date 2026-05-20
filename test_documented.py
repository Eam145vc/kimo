"""Probar endpoints descubiertos en documentacion oficial Siigo.

Endpoints nuevos a verificar:
  - GET  /v1/invoices/{id}/pdf
  - POST /v1/invoices/{id}/annul        (no /void)
  - POST /v1/invoices/{id}/stamp        (timbrado DIAN)
  - POST /v1/invoices/{id}/mail         (envio por email)
  - GET  /v1/quotations
  - POST /v1/quotations
  - GET  /v1/payment-receipts
  - POST /v1/payment-receipts
  - POST /v1/vouchers
  - GET  /v1/purchase-support-documents
  - POST /v1/debit-notes (existe?)
  - GET  /v1/journals (POST?)
  - POST /v1/webhooks (con application_id correcto y topic correcto)
"""
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
        d = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
        results.append({"name": name, "capability": capability or name, "status": "FAIL", "detail": d})
        print(f"[FAIL] {name}\n   -> {d}")
        return None
    except Exception as e:
        results.append({"name": name, "capability": capability or name, "status": "FAIL", "detail": f"{type(e).__name__}: {e}"})
        print(f"[FAIL] {name}\n   -> {e}")
        traceback.print_exc()
        return None


def main():
    with SiigoClient() as s:
        s._authenticate()

        # === GET de listados nuevos ===
        print("\n--- D1: GET endpoints nuevos ---")
        t("GET /v1/quotations", lambda: s.get("/v1/quotations", params={"page_size": 3}), capability="list_quotations")
        t("GET /v1/payment-receipts", lambda: s.get("/v1/payment-receipts", params={"page_size": 3}), capability="list_payment_receipts")
        t("GET /v1/purchase-support-documents", lambda: s.get("/v1/purchase-support-documents", params={"page_size": 3}), capability="list_support_docs")
        t("GET /v1/debit-notes", lambda: s.get("/v1/debit-notes", params={"page_size": 3}), capability="list_debit_notes")

        # === Acciones sobre factura existente (la que dejamos creada antes) ===
        # FV-1-5171 id: 538e3537-4f76-4500-a008-1377da429f52
        INV_ID = "538e3537-4f76-4500-a008-1377da429f52"
        print(f"\n--- D2: acciones sobre invoice {INV_ID[:8]}... ---")

        # D2.1 PDF correcto path
        pdf = t(
            "GET /v1/invoices/{id}/pdf",
            lambda: s.get(f"/v1/invoices/{INV_ID}/pdf"),
            capability="get_invoice_pdf",
        )
        if pdf:
            # quitamos el contenido binario si esta inline
            keys = list(pdf.keys()) if isinstance(pdf, dict) else None
            print(f"     -> keys: {keys}")

        # D2.2 mail
        t(
            "POST /v1/invoices/{id}/mail",
            lambda: s.post(f"/v1/invoices/{INV_ID}/mail", {"emails": ["test@example.com"]}),
            capability="send_invoice_email",
        )

        # D2.3 annul (anular factura) -- atencion: esto podria realmente anularla
        # No la anulamos porque ya tiene NC. Solo probamos que el endpoint exista
        # mediante POST con payload deliberadamente vacio -> deberia dar 400 (campos requeridos)
        # mejor que 404 (endpoint no existe)
        print("\n--- D2.3: SKIP annul (factura ya anulada por NC, no provocar mas movimientos) ---")
        results.append({
            "name": "POST /v1/invoices/{id}/annul",
            "capability": "annul_invoice",
            "status": "SKIP",
            "detail": "endpoint conocido por SDK oficial; no lo probamos en cuenta real para no generar mas movimientos contables",
        })

        # D2.4 stamp (timbrado DIAN) -- solo aplica a factura electronica, FV-1-5171 es tradicional
        # Mismo principio: lo dejamos documentado
        results.append({
            "name": "POST /v1/invoices/{id}/stamp",
            "capability": "stamp_dian",
            "status": "SKIP",
            "detail": "endpoint conocido por SDK oficial; aplica solo a documento electronico 27703",
        })

        # === Quotations POST + DELETE ===
        print("\n--- D3: cotizaciones CRUD ---")
        # primero buscar un cliente
        cust = s.get("/v1/customers", params={"page_size": 1, "page": 1})
        cust_ident = cust["results"][0]["identification"]
        prod_code = "P23"  # producto existente

        quote_payload = {
            "document": {"id": None},  # se completara
            "date": date.today().isoformat(),
            "customer": {"identification": cust_ident, "branch_office": 0},
            "items": [{
                "code": prod_code,
                "description": "ITEM COTIZACION TEST",
                "quantity": 1.0,
                "price": 100.0,
                "taxes": [{"id": 7108}],
            }],
            "observations": "COTIZACION DE PRUEBA - IGNORAR",
        }
        # Buscar document type para cotizacion: puede ser tipo CT, CO o COT
        for dtype in ["CT", "CO", "COT", "QUO"]:
            try:
                dtypes = s.get("/v1/document-types", params={"type": dtype})
                if dtypes:
                    print(f"     -> doc-type {dtype}: {[(d['id'], d['name']) for d in dtypes]}")
                    quote_payload["document"]["id"] = dtypes[0]["id"]
                    break
            except Exception:
                pass

        if quote_payload["document"]["id"]:
            quote = t(
                "POST /v1/quotations",
                lambda: s.post("/v1/quotations", quote_payload),
                capability="create_quotation",
            )
            if quote and quote.get("id"):
                qid = quote["id"]
                print(f"     -> quotation id: {qid}, name: {quote.get('name')}")
                # Intentar borrarla inmediatamente
                t(
                    "DELETE /v1/quotations/{id}",
                    lambda: s.delete(f"/v1/quotations/{qid}"),
                    capability="delete_quotation",
                )
        else:
            results.append({
                "name": "POST /v1/quotations",
                "capability": "create_quotation",
                "status": "SKIP",
                "detail": "no se encontro document-type de cotizacion (CT/CO/COT/QUO)",
            })

        # === Payment receipts ===
        print("\n--- D4: payment-receipts ---")
        # Solo GET, no creamos para no afectar cartera real
        # ya verificado en D1

        # === Webhooks con topic correcto ===
        print("\n--- D5: webhooks con application_id y topic correctos ---")
        webhook_payload = {
            "application_id": "Skiimo",  # nombre libre segun docs
            "url": "https://webhook.site/test-skiimo-no-real",
            "topic": "public.siigoapi.invoices.create",
        }
        wh = t(
            "POST /v1/webhooks (topic invoices.create)",
            lambda: s.post("/v1/webhooks", webhook_payload),
            capability="create_webhook_invoices",
        )
        if wh and wh.get("id"):
            wid = wh["id"]
            print(f"     -> webhook id: {wid}, topic: {wh.get('topic')}")
            t(
                "DELETE /v1/webhooks/{id}",
                lambda: s.delete(f"/v1/webhooks/{wid}"),
                capability="delete_webhook",
            )

        # === Topics adicionales para mapeo ===
        for topic in [
            "public.siigoapi.products.create",
            "public.siigoapi.customers.create",
            "public.siigoapi.purchases.create",
            "public.siigoapi.invoices.update",
            "public.siigoapi.payments.create",
        ]:
            payload = {
                "application_id": "Skiimo",
                "url": "https://webhook.site/test-skiimo-no-real",
                "topic": topic,
            }
            r = t(
                f"POST /v1/webhooks (topic {topic.split('.')[-2]}.{topic.split('.')[-1]})",
                lambda p=payload: s.post("/v1/webhooks", p),
                capability=f"webhook_topic_{topic.split('.')[-2]}_{topic.split('.')[-1]}",
            )
            if r and r.get("id"):
                try:
                    s.delete(f"/v1/webhooks/{r['id']}")
                except Exception:
                    pass

    # Reporte
    md = ["# Endpoints documentados — verificacion en cuenta real", ""]
    md.append(f"Ejecutado: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    md.append("")
    md.append("| Prueba | Capacidad | Resultado | Detalle |")
    md.append("|---|---|---|---|")
    for r in results:
        det = r.get("detail") or ""
        if det:
            det = str(det).replace("|", "\\|").replace("\n", " ")
            if len(det) > 200:
                det = det[:200] + "..."
        md.append(f"| {r['name']} | `{r['capability']}` | **{r['status']}** | {det} |")

    (OUT / "CAPACIDADES_DOCUMENTADAS.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\nReporte: explorations/CAPACIDADES_DOCUMENTADAS.md")


if __name__ == "__main__":
    main()
