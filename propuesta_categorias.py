"""Genera propuesta de categorias para que el dueno apruebe.

Para cada cliente con 3+ compras en bolsas 6L, calcula:
  - Total comprado en bolsas 6L (cantidad y plata)
  - Precio promedio pagado (vs los 3 niveles oficiales)
  - Frecuencia (cuantas facturas)
  - Categoria sugerida basada en cual precio paga
  - Lo etiqueta para que el dueno confirme con SI/NO
"""
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from skiimo.db.schema import get_conn


# Umbrales (con IVA)
PRECIO_DETAL = 26000.0
PRECIO_MAYORISTA = 23000.0
PRECIO_DISTRIBUIDOR = 20000.0

# Pre-IVA (lo que va en la factura)
PRECIO_DETAL_PRE = 21848.74
PRECIO_MAYORISTA_PRE = 19327.73
PRECIO_DISTRIBUIDOR_PRE = 16806.72

# Ya confirmados por el dueño - no proponer
YA_CONFIRMADOS = {
    "307a83aa-f601-4bde-bcbe-9d94d4ad54ed",  # Hugo
    "4c77ba58-b73f-4869-8b75-1ab4ecdc8ee2",  # David Zuniga
    "440ef191-6bae-4a24-b83c-51f863a05afe",  # Myriam
    "406be39e-a490-436f-9722-56f0aba6626d",  # ZZZ TEST BOT (no proponer)
}


def categoria_por_precio(precio_promedio_pre_iva: float) -> str:
    """Asigna categoria por proximidad al precio promedio pagado."""
    if precio_promedio_pre_iva <= (PRECIO_DISTRIBUIDOR_PRE + PRECIO_MAYORISTA_PRE) / 2:
        return "DISTRIBUIDOR"
    if precio_promedio_pre_iva <= (PRECIO_MAYORISTA_PRE + PRECIO_DETAL_PRE) / 2:
        return "MAYORISTA"
    return "DETAL"


def main():
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT i.customer_id, i.customer_ident, i.date, c.name, i.items_json
               FROM siigo_invoices i JOIN siigo_customers c ON c.id = i.customer_id
               WHERE i.date >= date('now', '-365 days')"""
        ).fetchall()
    finally:
        conn.close()

    # Por cliente, agregar SOLO bolsas 6L (los items que aplican a la regla del dueno)
    stats: dict = defaultdict(lambda: {
        "nombre": "", "ident": "", "facturas": set(),
        "bolsas_qty": 0.0, "bolsas_valor_pre_iva": 0.0,
        "precios": [],
    })
    for r in rows:
        cid = r["customer_id"]
        if cid in YA_CONFIRMADOS:
            continue
        stats[cid]["nombre"] = r["name"] or "(sin nombre)"
        stats[cid]["ident"] = r["customer_ident"]
        try:
            items = json.loads(r["items_json"])
        except Exception:
            continue
        for it in items:
            code = (it.get("code") or "")
            desc = (it.get("description") or "").upper()
            # Solo bolsas 6L (no sachet, no cremoso)
            if not (code.startswith("A1") or code.startswith("A2")):
                continue
            if "SACHET" in desc or "CREMOSO" in desc:
                continue
            if "6L" not in desc and "6 L" not in desc and "BOLSA" not in desc:
                continue
            qty = float(it.get("quantity") or 0)
            precio = float(it.get("price") or 0)
            if qty <= 0 or precio <= 0:
                continue
            stats[cid]["facturas"].add(r["date"])
            stats[cid]["bolsas_qty"] += qty
            stats[cid]["bolsas_valor_pre_iva"] += qty * precio
            stats[cid]["precios"].extend([precio] * int(qty))

    # Filtrar: solo clientes con 3+ facturas Y 30+ bolsas en el ultimo ano
    candidates = []
    for cid, s in stats.items():
        if len(s["facturas"]) < 3:
            continue
        if s["bolsas_qty"] < 30:
            continue
        avg_pre = sum(s["precios"]) / len(s["precios"])
        cat = categoria_por_precio(avg_pre)
        candidates.append({
            "customer_id": cid,
            "nit": s["ident"],
            "nombre": s["nombre"],
            "facturas_12m": len(s["facturas"]),
            "bolsas_compradas": int(s["bolsas_qty"]),
            "valor_total_pre_iva": s["bolsas_valor_pre_iva"],
            "valor_total_con_iva": s["bolsas_valor_pre_iva"] * 1.19,
            "precio_promedio_pre": round(avg_pre, 2),
            "precio_promedio_con_iva": round(avg_pre * 1.19, 0),
            "categoria_sugerida": cat,
        })

    # Ordenar: distribuidor primero, despues mayorista, despues por valor
    cat_orden = {"DISTRIBUIDOR": 0, "MAYORISTA": 1, "DETAL": 2}
    candidates.sort(key=lambda x: (cat_orden[x["categoria_sugerida"]], -x["valor_total_con_iva"]))

    # Imprimir tabla en consola
    print("=" * 130)
    print("PROPUESTA DE CATEGORIAS - PARA APROBAR CON EL DUENO")
    print("=" * 130)
    print(f"\nPrecios oficiales (bolsas 6L): DETAL ${PRECIO_DETAL:,.0f} | MAYORISTA ${PRECIO_MAYORISTA:,.0f} | DISTRIBUIDOR ${PRECIO_DISTRIBUIDOR:,.0f} (con IVA)\n")
    print(f"Ya confirmados por el dueno: Hugo, David Zuniga (MAYORISTA), Myriam (DISTRIBUIDOR)\n")

    print(f"{'PROPUESTA':<13} {'NOMBRE':<42} {'NIT':<13} {'FACT':>5} {'BOLSAS':>7} {'VALOR_TOTAL':>14} {'PRECIO_AVG':>11}")
    print("-" * 130)

    actual_cat = None
    for c in candidates:
        if c["categoria_sugerida"] != actual_cat:
            actual_cat = c["categoria_sugerida"]
            print(f"\n--- {actual_cat} ---")
        print(f"{c['categoria_sugerida']:<13} {c['nombre'][:42]:<42} {c['nit']:<13} "
              f"{c['facturas_12m']:>5} {c['bolsas_compradas']:>7} "
              f"${c['valor_total_con_iva']:>13,.0f} ${c['precio_promedio_con_iva']:>10,.0f}")

    # Resumen
    by_cat = defaultdict(lambda: {"clientes": 0, "valor": 0.0})
    for c in candidates:
        by_cat[c["categoria_sugerida"]]["clientes"] += 1
        by_cat[c["categoria_sugerida"]]["valor"] += c["valor_total_con_iva"]
    print("\n" + "=" * 60)
    print("RESUMEN:")
    print(f"  Total candidatos a categorizar: {len(candidates)}")
    for cat, d in sorted(by_cat.items(), key=lambda x: cat_orden[x[0]]):
        print(f"  {cat:13} clientes={d['clientes']:>3}  valor_compras_12m=${d['valor']:>14,.0f}")

    # Escribir CSV para aprobacion
    out = Path("data/propuesta_categorias_para_dueno.csv")
    out.parent.mkdir(exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "categoria_sugerida", "APROBAR_SI_NO", "categoria_final_si_cambia",
            "nombre", "nit", "facturas_12m", "bolsas_compradas_12m",
            "valor_total_con_iva", "precio_promedio_con_iva",
            "customer_id", "notas_dueno",
        ])
        for c in candidates:
            w.writerow([
                c["categoria_sugerida"], "", "",
                c["nombre"], c["nit"], c["facturas_12m"], c["bolsas_compradas"],
                round(c["valor_total_con_iva"]), int(c["precio_promedio_con_iva"]),
                c["customer_id"], "",
            ])
    print(f"\nCSV para el dueno: {out}")


if __name__ == "__main__":
    main()
