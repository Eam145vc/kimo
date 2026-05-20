"""Limpieza residual: lo que DELETE no permite, lo desactivamos.

Recursos remanentes del test_capabilities.py:
  - customer 406be39e-a490-436f-9722-56f0aba6626d (TEST BOT) -> PUT active=false
  - invoice 538e3537-4f76-4500-a008-1377da429f52 (FV-1-5171, ya anulada por NC)
  - credit_note 80abc1f9-cdcd-494f-be73-769ce731a603 (NC-1-1)
"""
from __future__ import annotations

import httpx
from siigo_client import SiigoClient

CUSTOMER_ID = "406be39e-a490-436f-9722-56f0aba6626d"
INVOICE_ID = "538e3537-4f76-4500-a008-1377da429f52"
CREDIT_NOTE_ID = "80abc1f9-cdcd-494f-be73-769ce731a603"


def try_action(label, fn):
    try:
        out = fn()
        print(f"[OK] {label}")
        return out
    except httpx.HTTPStatusError as e:
        print(f"[FAIL] {label} -> HTTP {e.response.status_code}: {e.response.text[:250]}")
    except Exception as e:
        print(f"[FAIL] {label} -> {e}")


def main():
    with SiigoClient() as siigo:
        siigo._authenticate()

        # 1. Recuperar cliente y desactivarlo
        cust = try_action(f"GET cliente {CUSTOMER_ID[:8]}...", lambda: siigo.get(f"/v1/customers/{CUSTOMER_ID}"))
        if cust:
            # PUT con todos los campos requeridos + active=false + nombre marcado
            payload = {
                "type": cust.get("type", "Customer"),
                "person_type": cust.get("person_type", "Person"),
                "id_type": cust.get("id_type", {}).get("code", "13"),
                "identification": cust["identification"],
                "name": ["ZZZ TEST BOT - INACTIVO", "NO USAR"],
                "commercial_name": "ZZZ TEST BOT - INACTIVO",
                "branch_office": 0,
                "active": False,
                "vat_responsible": cust.get("vat_responsible", False),
                "fiscal_responsibilities": cust.get("fiscal_responsibilities", [{"code": "R-99-PN"}]),
                "address": cust.get("address"),
                "phones": cust.get("phones", []),
                "contacts": cust.get("contacts", []),
            }
            try_action(
                f"PUT cliente active=false",
                lambda: siigo.put(f"/v1/customers/{CUSTOMER_ID}", payload),
            )

        # 2. Verificar estado de factura (probablemente anulada via NC)
        inv = try_action(f"GET invoice {INVOICE_ID[:8]}...", lambda: siigo.get(f"/v1/invoices/{INVOICE_ID}"))
        if inv:
            print(f"     invoice balance={inv.get('balance')}, total={inv.get('total')}, observations='{inv.get('observations')}'")
            # Intentar anularla via endpoint void si existe
            try_action(
                "POST /v1/invoices/{id}/void (intentar anular)",
                lambda: siigo.post(f"/v1/invoices/{INVOICE_ID}/void", {}),
            )

        # 3. Verificar credit note
        cn = try_action(f"GET credit-note {CREDIT_NOTE_ID[:8]}...", lambda: siigo.get(f"/v1/credit-notes/{CREDIT_NOTE_ID}"))
        if cn:
            print(f"     credit_note total={cn.get('total')}")


if __name__ == "__main__":
    main()
