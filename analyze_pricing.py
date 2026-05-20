"""Analisis profundo de patrones de precios.

Preguntas a responder:
  1. Cuantas listas/escalas de precio se observan en la realidad?
  2. Los precios dependen de la cantidad? (descuento por volumen)
  3. Hay clientes recurrentes con precios fijos? (clientes mayoristas)
  4. Cual es la dispersion real por producto?
  5. Hay productos con precio 100% estable?
  6. Cual es el "precio modal" (mas frecuente) por producto?
"""
import json
import statistics
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from skiimo.db.schema import get_conn

conn = get_conn()

# Cargar todas las ventas con item-cliente-cantidad-precio
rows = conn.execute(
    """SELECT i.date, i.customer_id, i.customer_ident, c.name as cname,
              i.items_json, i.total
       FROM siigo_invoices i LEFT JOIN siigo_customers c ON c.id = i.customer_id
       WHERE i.items_json IS NOT NULL"""
).fetchall()

# Estructura: por producto, lista de (precio, cantidad, cliente_id, cliente_nombre, fecha)
ventas_por_producto: dict = defaultdict(list)
nombres = {}
for r in rows:
    items = json.loads(r["items_json"])
    for it in items:
        code = it.get("code")
        if not code:
            continue
        precio = float(it.get("price") or 0)
        cant = float(it.get("quantity") or 0)
        if precio == 0 or cant == 0:
            continue
        ventas_por_producto[code].append({
            "precio": precio,
            "cantidad": cant,
            "cliente_id": r["customer_id"],
            "cliente": r["cname"] or r["customer_ident"],
            "fecha": r["date"],
        })
        nombres[code] = it.get("description") or ""

print(f"Productos con ventas: {len(ventas_por_producto)}")
print(f"Total registros venta-item: {sum(len(v) for v in ventas_por_producto.values())}")

# =============================================================================
# 1) DISPERSION POR PRODUCTO
# =============================================================================
print("\n" + "=" * 70)
print("1) DISPERSION DE PRECIOS POR PRODUCTO (top vendidos)")
print("=" * 70)

stats_productos = []
for code, ventas in ventas_por_producto.items():
    precios = [v["precio"] for v in ventas]
    if len(precios) < 5:
        continue
    p_unique = sorted(set(round(p, 0) for p in precios))
    mode_price = Counter(round(p, 0) for p in precios).most_common(1)[0]
    stats_productos.append({
        "code": code,
        "name": nombres[code][:35],
        "ventas": len(ventas),
        "precios_distintos": len(p_unique),
        "min": min(precios),
        "max": max(precios),
        "mean": statistics.mean(precios),
        "median": statistics.median(precios),
        "mode_price": mode_price[0],
        "mode_freq": mode_price[1],
        "mode_pct": mode_price[1] / len(precios) * 100,
        "std": statistics.stdev(precios) if len(precios) > 1 else 0,
    })

stats_productos.sort(key=lambda x: x["ventas"], reverse=True)

print(f"\n{'CODE':<6} {'NAME':<35} {'VENTAS':>6} {'PRECIOS':>7} {'MIN':>10} {'MAX':>10} {'MODA':>10} {'MODA%':>6} {'CV%':>6}")
print("-" * 110)
for s in stats_productos[:20]:
    cv = (s["std"] / s["mean"] * 100) if s["mean"] else 0
    print(f"{s['code']:<6} {s['name']:<35} {s['ventas']:>6} {s['precios_distintos']:>7} "
          f"{s['min']:>10,.0f} {s['max']:>10,.0f} {s['mode_price']:>10,.0f} {s['mode_pct']:>5.0f}% {cv:>5.1f}%")

# =============================================================================
# 2) CONSISTENCIA: cuantos productos tienen un precio dominante?
# =============================================================================
print("\n" + "=" * 70)
print("2) CONSISTENCIA DE PRECIOS (moda >= 70% es 'estable')")
print("=" * 70)

estables = [s for s in stats_productos if s["mode_pct"] >= 70]
variables = [s for s in stats_productos if 40 <= s["mode_pct"] < 70]
caoticos = [s for s in stats_productos if s["mode_pct"] < 40]
print(f"\nEstables (moda >= 70%): {len(estables)} productos")
print(f"Variables (40-70%):     {len(variables)} productos")
print(f"Caoticos (<40%):        {len(caoticos)} productos")
print(f"\nProductos caoticos (top 10 por volumen):")
for s in sorted(caoticos, key=lambda x: x["ventas"], reverse=True)[:10]:
    print(f"  {s['code']:<6} {s['name']:<35} ventas={s['ventas']:>4} moda=${s['mode_price']:,.0f} ({s['mode_pct']:.0f}%)")

# =============================================================================
# 3) DESCUENTO POR VOLUMEN: precio cae con cantidad mayor?
# =============================================================================
print("\n" + "=" * 70)
print("3) CORRELACION CANTIDAD vs PRECIO (productos top)")
print("=" * 70)

def correl(xs, ys):
    """Pearson manual."""
    n = len(xs)
    if n < 3:
        return 0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = (sum((x - mx) ** 2 for x in xs)) ** 0.5
    dy = (sum((y - my) ** 2 for y in ys)) ** 0.5
    if dx * dy == 0:
        return 0
    return num / (dx * dy)

print(f"\n{'CODE':<6} {'NAME':<35} {'N':>4} {'CORREL':>8} {'INTERPRETACION'}")
print("-" * 100)
for s in stats_productos[:20]:
    ventas = ventas_por_producto[s["code"]]
    qs = [v["cantidad"] for v in ventas]
    ps = [v["precio"] for v in ventas]
    c = correl(qs, ps)
    if c < -0.3:
        interp = "DESCUENTO por volumen (mas cant -> menos precio)"
    elif c > 0.3:
        interp = "Inverso (raro)"
    else:
        interp = "Sin correlacion fuerte"
    print(f"{s['code']:<6} {s['name']:<35} {len(ventas):>4} {c:>8.3f} {interp}")

# =============================================================================
# 4) UMBRALES DE CANTIDAD: a partir de cuanto cae el precio?
# =============================================================================
print("\n" + "=" * 70)
print("4) PATRONES DE PRECIO POR RANGO DE CANTIDAD")
print("=" * 70)

# Para los 5 mas vendidos, agrupar por bucket de cantidad
for s in stats_productos[:5]:
    ventas = ventas_por_producto[s["code"]]
    buckets: dict = {"1": [], "2-5": [], "6-10": [], "11-30": [], "31-99": [], "100+": []}
    for v in ventas:
        q = v["cantidad"]
        if q == 1: buckets["1"].append(v["precio"])
        elif q <= 5: buckets["2-5"].append(v["precio"])
        elif q <= 10: buckets["6-10"].append(v["precio"])
        elif q <= 30: buckets["11-30"].append(v["precio"])
        elif q <= 99: buckets["31-99"].append(v["precio"])
        else: buckets["100+"].append(v["precio"])
    print(f"\n  {s['code']} {s['name']}:")
    for rango, ps in buckets.items():
        if not ps:
            continue
        avg = statistics.mean(ps)
        print(f"    {rango:>6}: n={len(ps):>3} precio_promedio=${avg:>10,.0f} (min ${min(ps):,.0f} - max ${max(ps):,.0f})")

# =============================================================================
# 5) CLIENTES MAYORISTAS (recurrentes con precio < promedio)
# =============================================================================
print("\n" + "=" * 70)
print("5) CLIENTES MAYORISTAS (3+ compras, precio promedio < promedio general)")
print("=" * 70)

# Para los 10 productos top: por cliente, contar compras y precio promedio
ventajas: dict = defaultdict(list)  # cliente -> list of (precio_relativo,)
clientes_recurrentes: dict = defaultdict(int)
for code in [s["code"] for s in stats_productos[:30]]:
    ventas = ventas_por_producto[code]
    if len(ventas) < 5:
        continue
    avg_global = statistics.mean(v["precio"] for v in ventas)
    por_cliente: dict = defaultdict(list)
    for v in ventas:
        por_cliente[v["cliente"]].append(v["precio"])
    for cliente, ps in por_cliente.items():
        if len(ps) >= 2:
            ratio = (statistics.mean(ps) / avg_global - 1) * 100
            ventajas[cliente].append(ratio)
            clientes_recurrentes[cliente] += len(ps)

# Clientes que aparecen en >=3 productos top y con precio promedio menor
mayoristas = []
for cliente, ratios in ventajas.items():
    if len(ratios) >= 3:
        avg_ratio = statistics.mean(ratios)
        mayoristas.append((cliente, avg_ratio, clientes_recurrentes[cliente], len(ratios)))
mayoristas.sort(key=lambda x: x[1])  # mas descuento primero
print(f"\n{'CLIENTE':<40} {'COMPRAS':>8} {'PRODS':>6} {'DESCUENTO_PROM':>15}")
print("-" * 80)
for cliente, ratio, compras, prods in mayoristas[:20]:
    sign = "menos" if ratio < 0 else "mas"
    print(f"{cliente[:40]:<40} {compras:>8} {prods:>6} {ratio:>+13.1f}% ({sign})")

# =============================================================================
# 6) RESUMEN EJECUTIVO
# =============================================================================
print("\n" + "=" * 70)
print("RESUMEN EJECUTIVO")
print("=" * 70)
print(f"""
- Productos con ventas: {len(ventas_por_producto)}
- De los top-20 vendidos:
  - Precio estable (moda >=70%): {len([s for s in stats_productos[:20] if s['mode_pct'] >= 70])}
  - Precio variable (40-70%):    {len([s for s in stats_productos[:20] if 40 <= s['mode_pct'] < 70])}
  - Precio caotico (<40%):       {len([s for s in stats_productos[:20] if s['mode_pct'] < 40])}
- Productos con correlacion cantidad-precio negativa (descuento por volumen):
  {sum(1 for s in stats_productos[:30] if correl([v['cantidad'] for v in ventas_por_producto[s['code']]], [v['precio'] for v in ventas_por_producto[s['code']]]) < -0.3)} de top-30
- Clientes que aparecen como recurrentes con precio menor al promedio: {len([m for m in mayoristas if m[1] < -2])}
""")

conn.close()
