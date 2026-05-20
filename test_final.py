"""Pruebas finales: email correcto + topics + endpoint OAuth de stamp."""
from __future__ import annotations

import httpx
from siigo_client import SiigoClient

INV_ID = "538e3537-4f76-4500-a008-1377da429f52"  # FV-1-5171 ya creada


def t(name, fn):
    try:
        r = fn()
        print(f"[OK] {name}")
        return r
    except httpx.HTTPStatusError as e:
        print(f"[FAIL] {name} -> HTTP {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:
        print(f"[FAIL] {name} -> {e}")


def main():
    with SiigoClient() as s:
        s._authenticate()

        # 1. PDF con base64 decodificado (verificar tamaño)
        print("\n--- F1: PDF de factura ---")
        pdf = t("GET /v1/invoices/{id}/pdf", lambda: s.get(f"/v1/invoices/{INV_ID}/pdf"))
        if pdf:
            import base64
            data = pdf.get("base64", "")
            decoded = base64.b64decode(data) if data else b""
            print(f"   base64 length: {len(data)} chars, decoded: {len(decoded)} bytes ({len(decoded)//1024} KB)")
            from pathlib import Path
            out = Path(__file__).parent / "explorations" / f"invoice_{INV_ID[:8]}.pdf"
            out.write_bytes(decoded)
            print(f"   PDF guardado en {out}")

        # 2. Mail con mail_to
        print("\n--- F2: enviar factura por email ---")
        # SOLO si los mail_to son emails de prueba (no reales)
        # Probamos solo con un email que no existe para no enviar nada real
        t(
            "POST /v1/invoices/{id}/mail con mail_to",
            lambda: s.post(
                f"/v1/invoices/{INV_ID}/mail",
                {"mail_to": ["test-noreal@skiimo.local"]},
            ),
        )

        # 3. Buscar topics validos en /v1/webhook-topics (si existe)
        print("\n--- F3: catalogos de webhook ---")
        for p in ["/v1/webhook-topics", "/v1/webhooks/topics", "/v1/topics"]:
            t(f"GET {p}", lambda path=p: s.get(path))

        # 4. /v1/stamp endpoint (DIAN)
        print("\n--- F4: stamp DIAN ---")
        for p in ["/v1/stamp", "/v1/stamps", "/v1/electronic-invoices"]:
            t(f"GET {p}", lambda path=p: s.get(path))


if __name__ == "__main__":
    main()
