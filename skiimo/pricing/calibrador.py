"""Calibrador: analiza el historico y propone precios/categorias/pronto-pago.

Genera 3 archivos para revision humana:
  - data/precios_sugeridos.csv      -> dueno revisa y aprueba
  - data/clientes_categorias.csv    -> dueno revisa y aprueba
  - data/pronto_pago_sugerido.csv   -> dueno completa los plazos negociados

Tambien puede aplicar las sugerencias a las tablas (--apply).

Uso:
  python -m skiimo.pricing.calibrador           # solo genera CSVs
  python -m skiimo.pricing.calibrador --apply   # aplica sugerencias a la DB
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from skiimo.db.schema import get_conn


OUT_DIR = Path(__file__).resolve().parents[2] / "data"
OUT_DIR.mkdir(exist_ok=True)


# =============================================================================
# 1) PRECIOS POR LISTA
# =============================================================================

def calcular_precios_sugeridos() -> list[dict]:
    """Para cada producto activo, sugerir DETAL, MAYORISTA, DISTRIBUIDOR.

    Estrategia:
      - DETAL: precio MODA del top 60% de las ventas (mayoritario).
      - MAYORISTA: percentil 25 del precio (clientes que pagan menos).
      - DISTRIBUIDOR: precio MINIMO recurrente (al menos 3 ventas iguales).
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT i.items_json FROM siigo_invoices i
               WHERE i.date >= date('now', '-180 days')"""
        ).fetchall()
        prods = {p["code"]: dict(p) for p in conn.execute(
            "SELECT id, code, name, iva_percentage, tax_included, raw FROM siigo_products WHERE active = 1"
        )}
    finally:
        conn.close()

    # Recolectar precios reales por producto
    precios_por_prod: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        try:
            items = json.loads(r["items_json"])
        except Exception:
            continue
        for it in items:
            code = it.get("code")
            if not code or code not in prods:
                continue
            precio = float(it.get("price") or 0)
            if precio > 0:
                precios_por_prod[code].append(precio)

    sugerencias: list[dict] = []
    for code, prod in prods.items():
        precios = precios_por_prod.get(code, [])
        iva = prod.get("iva_percentage") or 19.0
        tax_inc = bool(prod.get("tax_included"))

        # Default: leer del catalogo Siigo
        catalogo_detal = catalogo_mayor = None
        try:
            raw = json.loads(prod["raw"])
            for pr in raw.get("prices") or []:
                for entry in pr.get("price_list") or []:
                    if entry.get("position") == 1:
                        catalogo_detal = float(entry.get("value") or 0)
                    elif entry.get("position") == 2:
                        catalogo_mayor = float(entry.get("value") or 0)
        except Exception:
            pass

        if not precios:
            # Sin ventas: usar catalogo
            if catalogo_detal:
                if tax_inc:
                    detal_pre = catalogo_detal / (1 + iva / 100)
                else:
                    detal_pre = catalogo_detal
                sugerencias.append({
                    "product_code": code,
                    "product_name": prod["name"][:50],
                    "ventas_180d": 0,
                    "precio_detal": round(detal_pre, 2),
                    "precio_mayorista": round(detal_pre * 0.90, 2),
                    "precio_distribuidor": round(detal_pre * 0.80, 2),
                    "fuente_detal": "catalogo",
                    "fuente_mayor": "estimado_-10%",
                    "fuente_distri": "estimado_-20%",
                    "respaldo_detal": "",
                    "respaldo_distri": "",
                })
            continue

        # MODA del precio
        contador = Counter(round(p, 2) for p in precios)
        moda_precio, moda_freq = contador.most_common(1)[0]
        moda_pct = moda_freq / len(precios) * 100

        # MINIMO recurrente (al menos 3 ventas al mismo precio bajo)
        sorted_precios = sorted(precios)
        # buscar el precio mas bajo que se repite >=3 veces
        precio_distri = None
        for p, count in sorted(contador.items()):
            if count >= 3 and p < moda_precio:
                precio_distri = p
                break

        # Precio mayorista: segundo grupo mas comun por debajo de la moda
        precios_bajo_moda = [(p, c) for p, c in contador.items() if p < moda_precio]
        precios_bajo_moda.sort(key=lambda x: x[1], reverse=True)
        precio_mayor = precios_bajo_moda[0][0] if precios_bajo_moda else None

        # Si no hay precios bajo moda, usar catalogo o estimaciones
        if precio_mayor is None:
            precio_mayor = round(moda_precio * 0.90, 2)
        if precio_distri is None:
            precio_distri = round(moda_precio * 0.80, 2)

        sugerencias.append({
            "product_code": code,
            "product_name": prod["name"][:50],
            "ventas_180d": len(precios),
            "precio_detal": moda_precio,
            "precio_mayorista": precio_mayor,
            "precio_distribuidor": precio_distri,
            "fuente_detal": f"moda_{moda_pct:.0f}%",
            "fuente_mayor": "2do_mas_comun" if precios_bajo_moda else "estimado_-10%",
            "fuente_distri": "min_recurrente_3+" if any(c >= 3 for p, c in contador.items() if p < moda_precio) else "estimado_-20%",
            "respaldo_detal": f"{moda_freq}/{len(precios)} ventas",
            "respaldo_distri": "",
        })

    return sugerencias


def escribir_csv_precios(sugerencias: list[dict]) -> Path:
    path = OUT_DIR / "precios_sugeridos.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "product_code", "product_name", "ventas_180d",
            "precio_detal", "precio_mayorista", "precio_distribuidor",
            "fuente_detal", "fuente_mayor", "fuente_distri",
            "respaldo_detal", "respaldo_distri",
        ])
        w.writeheader()
        for s in sorted(sugerencias, key=lambda x: x["ventas_180d"], reverse=True):
            w.writerow(s)
    return path


# =============================================================================
# 2) CATEGORIAS DE CLIENTE
# =============================================================================

def calcular_categorias() -> list[dict]:
    """Sugiere categoria para cada cliente con 3+ compras.

    Reglas:
      - 20+ facturas Y descuento promedio menor a -10% del promedio -> DISTRIBUIDOR
      - 5-20 facturas con descuento menor a -3% -> MAYORISTA
      - Resto: DETAL
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT i.customer_id, i.customer_ident, c.name, i.items_json
               FROM siigo_invoices i LEFT JOIN siigo_customers c ON c.id = i.customer_id
               WHERE i.customer_id IS NOT NULL AND i.date >= date('now', '-365 days')"""
        ).fetchall()
    finally:
        conn.close()

    # Promedio global por producto
    precios_globales: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        for it in json.loads(r["items_json"] or "[]"):
            code = it.get("code")
            precio = float(it.get("price") or 0)
            if code and precio > 0:
                precios_globales[code].append(precio)
    promedio_global = {k: statistics.mean(v) for k, v in precios_globales.items() if v}

    # Por cliente: compras y descuento relativo
    por_cliente: dict[str, dict] = {}
    for r in rows:
        cid = r["customer_id"]
        if cid not in por_cliente:
            por_cliente[cid] = {
                "customer_id": cid,
                "customer_ident": r["customer_ident"],
                "name": r["name"] or "(sin nombre)",
                "facturas": 0,
                "ratios": [],
            }
        por_cliente[cid]["facturas"] += 1
        for it in json.loads(r["items_json"] or "[]"):
            code = it.get("code")
            precio = float(it.get("price") or 0)
            if code in promedio_global and precio > 0:
                ratio = precio / promedio_global[code]
                por_cliente[cid]["ratios"].append(ratio)

    sugerencias: list[dict] = []
    for cid, d in por_cliente.items():
        if d["facturas"] < 3:
            continue
        if not d["ratios"]:
            continue
        descuento_pct = (1 - statistics.mean(d["ratios"])) * 100  # >0 = paga menos
        if d["facturas"] >= 20 and descuento_pct >= 10:
            categoria = "DISTRIBUIDOR"
        elif d["facturas"] >= 5 and descuento_pct >= 3:
            categoria = "MAYORISTA"
        else:
            categoria = "DETAL"
        sugerencias.append({
            "customer_id": cid,
            "customer_ident": d["customer_ident"],
            "name": d["name"][:50],
            "facturas_12m": d["facturas"],
            "descuento_promedio_pct": round(descuento_pct, 1),
            "categoria_sugerida": categoria,
            "categoria_aprobada": "",  # dueno llena
            "notas": "",
        })
    return sugerencias


def escribir_csv_categorias(sugerencias: list[dict]) -> Path:
    # Solo escribimos clientes interesantes (no DETAL puro a menos que tengan volumen)
    relevantes = [s for s in sugerencias if s["categoria_sugerida"] != "DETAL" or s["facturas_12m"] >= 10]
    relevantes.sort(key=lambda x: (x["categoria_sugerida"] != "DISTRIBUIDOR", -x["facturas_12m"]))
    path = OUT_DIR / "clientes_categorias.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(relevantes[0].keys()) if relevantes else
                           ["customer_id", "customer_ident", "name", "facturas_12m",
                            "descuento_promedio_pct", "categoria_sugerida", "categoria_aprobada", "notas"])
        w.writeheader()
        for s in relevantes:
            w.writerow(s)
    return path


# =============================================================================
# 3) PRONTO PAGO (plantilla vacia para llenar)
# =============================================================================

def escribir_csv_pronto_pago(categorias: list[dict]) -> Path:
    """Genera plantilla con clientes MAYORISTA/DISTRIBUIDOR para que dueno llene los plazos."""
    candidatos = [c for c in categorias if c["categoria_sugerida"] in ("MAYORISTA", "DISTRIBUIDOR")]
    candidatos.sort(key=lambda x: (x["categoria_sugerida"] != "DISTRIBUIDOR", -x["facturas_12m"]))
    path = OUT_DIR / "pronto_pago_sugerido.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        # Una fila por cada escalon. Damos plantilla 3 escalones por cliente para llenar.
        w = csv.DictWriter(f, fieldnames=[
            "customer_id", "customer_ident", "name", "categoria",
            "escalon_1_dias", "escalon_1_descuento_pct",
            "escalon_2_dias", "escalon_2_descuento_pct",
            "escalon_3_dias", "escalon_3_descuento_pct",
            "notas",
        ])
        w.writeheader()
        for c in candidatos:
            w.writerow({
                "customer_id": c["customer_id"],
                "customer_ident": c["customer_ident"],
                "name": c["name"],
                "categoria": c["categoria_sugerida"],
                # Plantilla en blanco: el dueno llena
                "escalon_1_dias": "", "escalon_1_descuento_pct": "",
                "escalon_2_dias": "", "escalon_2_descuento_pct": "",
                "escalon_3_dias": "", "escalon_3_descuento_pct": "",
                "notas": "",
            })
    return path


# =============================================================================
# 4) APLICAR (precarga la DB con sugerencias - se pueden ajustar despues)
# =============================================================================

def aplicar_sugerencias(precios: list[dict], categorias: list[dict]) -> tuple[int, int]:
    conn = get_conn()
    try:
        # Precios
        n_p = 0
        for s in precios:
            row = conn.execute("SELECT id FROM siigo_products WHERE code = ?", (s["product_code"],)).fetchone()
            if not row:
                continue
            for lista, key in (("DETAL", "precio_detal"), ("MAYORISTA", "precio_mayorista"), ("DISTRIBUIDOR", "precio_distribuidor")):
                val = float(s[key])
                if val <= 0:
                    continue
                con_iva = round(val * 1.19, 2)
                conn.execute(
                    """INSERT INTO precios_oficiales
                       (product_id, product_code, lista, precio_pre_iva, precio_con_iva, fuente, ventas_referencia, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(product_id, lista) DO UPDATE SET
                         precio_pre_iva = excluded.precio_pre_iva,
                         precio_con_iva = excluded.precio_con_iva,
                         fuente = excluded.fuente,
                         ventas_referencia = excluded.ventas_referencia,
                         updated_at = excluded.updated_at""",
                    (row["id"], s["product_code"], lista, val, con_iva,
                     s.get(f"fuente_{lista.lower()[:5]}", "moda_historica"),
                     int(s.get("ventas_180d", 0)),
                     datetime.now().isoformat(timespec="seconds")),
                )
                n_p += 1
        # Categorias
        n_c = 0
        for c in categorias:
            if c["categoria_sugerida"] == "DETAL":
                continue
            conn.execute(
                """INSERT INTO clientes_categoria
                   (customer_id, categoria, fuente, notas, created_at, updated_at)
                   VALUES (?, ?, 'sugerido_historia', ?, ?, ?)
                   ON CONFLICT(customer_id) DO UPDATE SET
                     categoria = excluded.categoria,
                     fuente = excluded.fuente,
                     updated_at = excluded.updated_at""",
                (c["customer_id"], c["categoria_sugerida"],
                 f"descuento promedio {c['descuento_promedio_pct']:.1f}% en {c['facturas_12m']} facturas",
                 datetime.now().isoformat(timespec="seconds"),
                 datetime.now().isoformat(timespec="seconds")),
            )
            n_c += 1
        conn.commit()
    finally:
        conn.close()
    return n_p, n_c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Aplicar sugerencias a la DB")
    args = ap.parse_args()

    print("Analizando precios...")
    precios = calcular_precios_sugeridos()
    p1 = escribir_csv_precios(precios)
    print(f"  -> {len(precios)} productos analizados. CSV: {p1}")

    print("Analizando categorias de cliente...")
    categorias = calcular_categorias()
    p2 = escribir_csv_categorias(categorias)
    no_detal = sum(1 for c in categorias if c["categoria_sugerida"] != "DETAL")
    print(f"  -> {len(categorias)} clientes activos analizados ({no_detal} con categoria > DETAL). CSV: {p2}")

    p3 = escribir_csv_pronto_pago(categorias)
    print(f"  -> plantilla pronto pago: {p3}")

    if args.apply:
        print("\nAplicando sugerencias a la DB...")
        n_p, n_c = aplicar_sugerencias(precios, categorias)
        print(f"  -> {n_p} precios cargados, {n_c} categorias asignadas")
    else:
        print("\nPara aplicar a la DB: python -m skiimo.pricing.calibrador --apply")


if __name__ == "__main__":
    main()
