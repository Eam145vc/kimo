"""Smoke test del OCR de facturas con normalizacion g/ml.

Corre:
  1. Validacion sintetica del schema FacturaProveedorItem.
  2. OCR real con una factura PDF de explorations/ (requiere GEMINI_API_KEY).

Uso: python smoke_ocr_unidades.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from skiimo.llm.schemas import FacturaProveedor, FacturaProveedorItem


def ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")
    raise SystemExit(1)


def test_schema_sintetico() -> None:
    print("\n[1] Schema sintetico")

    # Caso 1: 5 kg azucar a $4.000/kg -> 5000 g a $4/g, subtotal $20.000
    item_kg = FacturaProveedorItem(
        descripcion="Azucar refinada",
        cantidad=5000,
        unidad="g",
        cantidad_original=5,
        unidad_original="kg",
        precio_unitario=4.0,
        iva_pct=19,
    )
    subtotal = item_kg.cantidad * item_kg.precio_unitario
    if abs(subtotal - 20000) < 0.01:
        ok(f"5 kg -> 5000 g, subtotal preservado ${subtotal:,.0f}")
    else:
        fail(f"subtotal esperado 20000, dio {subtotal}")

    # Caso 2: 2 garrafas de 20 L acido a $80.000/garrafa -> 40000 ml a $4/ml
    item_garrafa = FacturaProveedorItem(
        descripcion="Acido citrico garrafa 20L",
        cantidad=40000,
        unidad="ml",
        cantidad_original=2,
        unidad_original="garrafa 20L",
        precio_unitario=4.0,
    )
    subtotal2 = item_garrafa.cantidad * item_garrafa.precio_unitario
    if abs(subtotal2 - 160000) < 0.01:
        ok(f"2 garrafas 20L -> 40000 ml, subtotal ${subtotal2:,.0f}")
    else:
        fail(f"subtotal esperado 160000, dio {subtotal2}")

    # Caso 3: servicio (und sin masa)
    item_und = FacturaProveedorItem(
        descripcion="Servicio mensajeria",
        cantidad=1,
        unidad="und",
        precio_unitario=50000,
    )
    if item_und.unidad == "und":
        ok("servicio queda en und")
    else:
        fail("servicio no debio convertirse")

    # Caso 4: unidad invalida debe fallar
    try:
        FacturaProveedorItem(
            descripcion="x", cantidad=1, unidad="kg", precio_unitario=1
        )
        fail("acepto unidad='kg' (deberia ser g/ml/und)")
    except Exception:
        ok("rechaza unidad='kg' (solo g/ml/und)")


def test_ocr_real() -> None:
    print("\n[2] OCR real (Gemini)")
    pdfs = list((ROOT / "explorations").glob("*.pdf"))
    if not pdfs:
        print("  [SKIP] no hay PDFs en explorations/")
        return

    pdf = pdfs[0]
    print(f"  PDF: {pdf.name}")
    try:
        from skiimo.llm.gemini import extract_factura_proveedor
    except Exception as e:
        print(f"  [SKIP] no pude importar gemini: {e}")
        return

    try:
        data = pdf.read_bytes()
        factura = extract_factura_proveedor(data, mime_type="application/pdf")
    except Exception as e:
        print(f"  [SKIP] llamada Gemini fallo: {e}")
        return

    print(f"  Proveedor: {factura.proveedor_nombre} (NIT {factura.proveedor_nit})")
    print(f"  Factura:   {factura.prefijo_factura}{factura.numero_factura}  fecha {factura.fecha}")
    print(f"  Total:     ${factura.total:,.0f}" if factura.total else "  Total:     -")
    print(f"  Items:     {len(factura.items)}")
    print(f"  Confidence:{factura.confidence}")

    unidades_validas = {"g", "ml", "und"}
    problemas = 0
    for i, it in enumerate(factura.items, 1):
        unidad_ok = it.unidad in unidades_validas
        subtotal = it.cantidad * it.precio_unitario
        print(
            f"    {i}. {it.descripcion[:50]:50s} "
            f"cant={it.cantidad:>10,.2f} {it.unidad:3s} "
            f"@ ${it.precio_unitario:>10,.4f} "
            f"= ${subtotal:>12,.2f} "
            f"(orig: {it.cantidad_original} {it.unidad_original})"
        )
        if not unidad_ok:
            print(f"       ^^ unidad invalida: {it.unidad}")
            problemas += 1

    # Heuristica: si la factura tiene insumos (azucar, acido, etc.) NO deberian
    # estar todos en "und". Solo es una advertencia.
    if factura.items:
        units = [it.unidad for it in factura.items]
        if all(u == "und" for u in units) and factura.categoria == "materias_primas":
            print("  [WARN] todos los items en 'und' siendo materias_primas; revisar prompt")

    if problemas:
        fail(f"{problemas} item(s) con unidad invalida")
    ok("todos los items con unidad valida g/ml/und")


if __name__ == "__main__":
    test_schema_sintetico()
    test_ocr_real()
    print("\nSmoke test OK")
