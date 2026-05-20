"""Descubrir topics de webhook validos probando variantes."""
from __future__ import annotations

import httpx
from siigo_client import SiigoClient

# Topics candidatos basados en convenciones
TOPIC_CANDIDATES = [
    # Schema "public.siigoapi.<resource>.<event>"
    "public.siigoapi.products.create",
    "public.siigoapi.products.update",
    "public.siigoapi.products.delete",
    "public.siigoapi.invoice.create",
    "public.siigoapi.invoice.update",
    "public.siigoapi.invoice.created",
    "public.siigoapi.invoices.created",
    "public.siigoapi.invoices.updated",
    "public.siigoapi.customer.create",
    "public.siigoapi.customers.create",
    "public.siigoapi.customer.created",
    "public.siigoapi.purchase.create",
    "public.siigoapi.purchases.create",
    "public.siigoapi.purchase.created",
    "public.siigoapi.voucher.create",
    "public.siigoapi.vouchers.create",
    "public.siigoapi.credit-note.create",
    "public.siigoapi.credit-notes.create",
    "public.siigoapi.payment-receipt.create",
    "public.siigoapi.quotation.create",
    "public.siigoapi.stamp.created",
    "public.siigoapi.stamp.accepted",
    "public.siigoapi.stamp.rejected",
    # Otros prefijos
    "siigoapi.products.create",
    "products.create",
]


def main():
    valid_topics: list[str] = []
    with SiigoClient() as s:
        s._authenticate()
        for topic in TOPIC_CANDIDATES:
            payload = {
                "application_id": "Skiimo",
                "url": "https://webhook.site/test-skiimo-discovery",
                "topic": topic,
            }
            try:
                r = s.post("/v1/webhooks", payload)
                if r.get("id"):
                    valid_topics.append(topic)
                    print(f"[VALID] {topic}")
                    try:
                        s.delete(f"/v1/webhooks/{r['id']}")
                    except Exception:
                        pass
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 400:
                    body = e.response.text
                    if "invalid_reference" in body or "doesn't exist" in body:
                        print(f"[INVALID] {topic}")
                    else:
                        print(f"[ERR400] {topic} -> {body[:150]}")
                else:
                    print(f"[ERR{e.response.status_code}] {topic}")
            except Exception as e:
                print(f"[EXC] {topic} -> {e}")

    print(f"\n=== Topics validos: {len(valid_topics)} ===")
    for t in valid_topics:
        print(f"  - {t}")


if __name__ == "__main__":
    main()
