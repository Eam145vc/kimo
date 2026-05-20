"""Test end-to-end SIN Telegram: simula un mensaje y crea factura real en Siigo.

Esto valida que el flujo completo funciona: extraccion -> matching -> creacion.
La factura queda en Siigo. Idempotencia evita duplicados al re-correr.
"""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from skiimo.db.schema import init_db
from skiimo.llm.gemini import extract_pedido
from skiimo.matcher import Matcher
from skiimo.pipeline import format_summary, resolve_pedido
from skiimo.siigo_writer import crear_factura_venta, get_invoice_pdf


def main() -> int:
    init_db()
    matcher = Matcher()

    # Mensaje de prueba: usa codigo de producto exacto (match 100) para que no necesite confirmacion
    mensaje = "Necesito 1 P23 a $100 pesos para entregar hoy en efectivo"
    print(f"Mensaje: {mensaje}\n")

    pedido = extract_pedido(mensaje)
    print(f"Extraido:\n{pedido.model_dump_json(indent=2)}\n")

    rp = resolve_pedido(pedido, matcher)
    print("Resolved:")
    print(format_summary(rp))
    print()
    print(f"idempotency_key: {rp.idempotency_key}")

    if rp.necesita_input_humano:
        print("\nNo se puede enviar automaticamente, requiere confirmacion humana:")
        for p in rp.necesita_input_humano:
            print(f"  - {p}")
        # Forzamos el precio a 100 si Gemini no lo capturo
        for item in rp.items:
            if item.elegido and item.precio_unitario is None:
                item.precio_unitario = 100.0
                print(f"  -> forzando precio_unitario=100 para item {item.raw.descripcion}")
        rp.necesita_input_humano = []

    print("\n=== ENVIANDO A SIIGO ===")
    result = crear_factura_venta(rp, actor="test_full_pipeline")
    print(f"OK: {result.ok}")
    if result.ok:
        print(f"  Factura: {result.siigo_name}")
        print(f"  ID: {result.siigo_id}")
        print(f"  Total: ${result.total}")
        print(f"  PDF: {result.public_url}")

        # Bajamos PDF
        pdf_bytes = get_invoice_pdf(result.siigo_id) if result.siigo_id else None
        if pdf_bytes:
            from pathlib import Path
            out = Path("explorations") / f"test_pipeline_{result.siigo_name}.pdf"
            out.write_bytes(pdf_bytes)
            print(f"  PDF guardado en {out} ({len(pdf_bytes)} bytes)")
    else:
        print(f"  Error: {result.error}")

    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
