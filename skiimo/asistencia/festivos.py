"""Festivos colombianos. Genera el calendario nacional.

Colombia tiene 18 festivos al ano. La mayoria son "trasladables" al lunes
siguiente por la Ley Emiliani (Ley 51 de 1983).

Fijos (no trasladables):
  - 1 enero (Ano Nuevo)
  - 1 mayo (Trabajo)
  - 20 julio (Independencia)
  - 7 agosto (Boyaca)
  - 8 diciembre (Inmaculada)
  - 25 diciembre (Navidad)
  - Jueves Santo y Viernes Santo (movibles, antes de Pascua)
  - Ascension del Senor, Corpus Christi, Sagrado Corazon (movibles relativos a Pascua)

Trasladables al lunes:
  - 6 enero (Reyes)
  - 19 marzo (San Jose)
  - 29 junio (San Pedro y San Pablo)
  - 15 agosto (Asuncion)
  - 12 octubre (Raza)
  - 1 noviembre (Todos los Santos)
  - 11 noviembre (Independencia Cartagena)

Pascua movible: algoritmo de Butcher/Meeus para domingo de Pascua.
"""
from __future__ import annotations

from datetime import date, timedelta


def domingo_pascua(year: int) -> date:
    """Algoritmo de Butcher (Gregoriano) para el domingo de Pascua."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    L = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * L) // 451
    month = (h + L - 7 * m + 114) // 31
    day = ((h + L - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _trasladar_lunes(d: date) -> date:
    """Si la fecha no cae lunes, mueve al lunes siguiente (Ley Emiliani)."""
    if d.weekday() == 0:  # ya es lunes
        return d
    dias_hasta_lunes = (7 - d.weekday()) % 7
    return d + timedelta(days=dias_hasta_lunes)


def festivos_colombia(year: int) -> list[tuple[date, str]]:
    """Devuelve lista de (fecha, nombre) ordenada por fecha."""
    pascua = domingo_pascua(year)
    jueves_santo = pascua - timedelta(days=3)
    viernes_santo = pascua - timedelta(days=2)

    # Trasladables relativos a Pascua (al lunes siguiente)
    ascension = _trasladar_lunes(pascua + timedelta(days=39))
    corpus_christi = _trasladar_lunes(pascua + timedelta(days=60))
    sagrado_corazon = _trasladar_lunes(pascua + timedelta(days=68))

    items: list[tuple[date, str]] = [
        (date(year, 1, 1), "Ano Nuevo"),
        (_trasladar_lunes(date(year, 1, 6)), "Reyes Magos"),
        (_trasladar_lunes(date(year, 3, 19)), "San Jose"),
        (jueves_santo, "Jueves Santo"),
        (viernes_santo, "Viernes Santo"),
        (date(year, 5, 1), "Dia del Trabajo"),
        (ascension, "Ascension del Senor"),
        (corpus_christi, "Corpus Christi"),
        (sagrado_corazon, "Sagrado Corazon"),
        (_trasladar_lunes(date(year, 6, 29)), "San Pedro y San Pablo"),
        (date(year, 7, 20), "Independencia"),
        (date(year, 8, 7), "Batalla de Boyaca"),
        (_trasladar_lunes(date(year, 8, 15)), "Asuncion de la Virgen"),
        (_trasladar_lunes(date(year, 10, 12)), "Dia de la Raza"),
        (_trasladar_lunes(date(year, 11, 1)), "Todos los Santos"),
        (_trasladar_lunes(date(year, 11, 11)), "Independencia de Cartagena"),
        (date(year, 12, 8), "Inmaculada Concepcion"),
        (date(year, 12, 25), "Navidad"),
    ]
    items.sort(key=lambda t: t[0])
    return items


def cargar_en_db(years: list[int]) -> int:
    """Inserta festivos en la tabla festivos_colombia. Idempotente (ON CONFLICT IGNORE)."""
    from datetime import datetime

    from skiimo.db.schema import get_conn

    inserted = 0
    conn = get_conn()
    try:
        for y in years:
            for fecha, nombre in festivos_colombia(y):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO festivos_colombia (fecha, nombre, fuente) VALUES (?, ?, 'precargado')",
                    (fecha.isoformat(), nombre),
                )
                inserted += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return inserted


def es_festivo(d: date) -> bool:
    from skiimo.db.schema import get_conn

    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM festivos_colombia WHERE fecha = ?", (d.isoformat(),)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


if __name__ == "__main__":
    # Mostrar festivos 2026
    print("Festivos Colombia 2026:")
    for f, n in festivos_colombia(2026):
        print(f"  {f.isoformat()}  {n}")
    print(f"\nTotal: {len(festivos_colombia(2026))} festivos")
