"""Calculo de horas trabajadas y horas extra segun ley colombiana.

Fuentes:
  - Codigo Sustantivo del Trabajo (CST), articulos 158-179
  - Decreto 1072/2015 (compendio reglamentario laboral)
  - Ley 2101/2021 (jornada maxima legal 44h/semana, vigente plena 2026)

Bandas de tiempo segun art. 160 CST (vigente):
  - DIURNA:   06:00 - 21:00
  - NOCTURNA: 21:00 - 06:00

Recargos sobre el salario hora ordinario:
  - Hora ordinaria diurna:                +0%   (1.00x)
  - Recargo nocturno (hora ord. de noche): +35% (1.35x)  CST art. 168
  - Hora extra diurna:                    +25% (1.25x)  CST art. 168
  - Hora extra nocturna:                  +75% (1.75x)
  - Hora dominical/festivo ordinaria:     +75% (1.75x)  CST art. 179
  - Hora dominical/festivo extra diurna:  +100% (2.00x)
  - Hora dominical/festivo extra nocturna: +150% (2.50x)

Jornada ordinaria maxima:
  - 7.33 h/dia si trabajan 6 dias (44h/6)
  - 8.8  h/dia si trabajan 5 dias (44h/5)
  - Lo que exceda esto en un dia = hora extra
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from skiimo.asistencia.festivos import es_festivo

# Limites diurno/nocturno
DIURNA_INICIO = time(6, 0)
DIURNA_FIN = time(21, 0)

# Jornada ordinaria diaria por defecto (44h/6 dias ~ 7.33h, redondeamos a 8h porque
# en fabrica es lo comun y el sexto dia se hace medio dia)
JORNADA_ORDINARIA_DIARIA_HORAS = 8.0

# Recargos (multiplicadores sobre la hora ordinaria)
RECARGOS = {
    "ordinaria_diurna": 1.00,
    "ordinaria_nocturna": 1.35,
    "extra_diurna": 1.25,
    "extra_nocturna": 1.75,
    "dom_fest_ord_diurna": 1.75,
    "dom_fest_ord_nocturna": 2.10,  # 1.75 + 0.35 recargo noct
    "dom_fest_extra_diurna": 2.00,
    "dom_fest_extra_nocturna": 2.50,
}


@dataclass
class TramoHoras:
    """Cantidad de horas en cada categoria para un dia."""

    ordinarias_diurnas: float = 0.0
    ordinarias_nocturnas: float = 0.0  # recargo +35%
    extra_diurnas: float = 0.0
    extra_nocturnas: float = 0.0
    dom_fest_ord_diurnas: float = 0.0
    dom_fest_ord_nocturnas: float = 0.0
    dom_fest_extra_diurnas: float = 0.0
    dom_fest_extra_nocturnas: float = 0.0
    minutos_almuerzo: int = 0
    minutos_tarde: int = 0
    primera_entrada: time | None = None
    ultima_salida: time | None = None

    def total_horas(self) -> float:
        return (
            self.ordinarias_diurnas
            + self.ordinarias_nocturnas
            + self.extra_diurnas
            + self.extra_nocturnas
            + self.dom_fest_ord_diurnas
            + self.dom_fest_ord_nocturnas
            + self.dom_fest_extra_diurnas
            + self.dom_fest_extra_nocturnas
        )

    def valorizar(self, valor_hora_ord: float) -> float:
        """Calcula el monto total a pagar segun los recargos."""
        return (
            self.ordinarias_diurnas * valor_hora_ord * RECARGOS["ordinaria_diurna"]
            + self.ordinarias_nocturnas * valor_hora_ord * RECARGOS["ordinaria_nocturna"]
            + self.extra_diurnas * valor_hora_ord * RECARGOS["extra_diurna"]
            + self.extra_nocturnas * valor_hora_ord * RECARGOS["extra_nocturna"]
            + self.dom_fest_ord_diurnas * valor_hora_ord * RECARGOS["dom_fest_ord_diurna"]
            + self.dom_fest_ord_nocturnas * valor_hora_ord * RECARGOS["dom_fest_ord_nocturna"]
            + self.dom_fest_extra_diurnas * valor_hora_ord * RECARGOS["dom_fest_extra_diurna"]
            + self.dom_fest_extra_nocturnas * valor_hora_ord * RECARGOS["dom_fest_extra_nocturna"]
        )


def es_nocturna(t: time) -> bool:
    """True si la hora cae en franja nocturna (21:00 - 06:00)."""
    return t >= DIURNA_FIN or t < DIURNA_INICIO


def _split_diurna_nocturna(start: datetime, end: datetime) -> tuple[float, float]:
    """Divide un intervalo [start, end) en (horas_diurnas, horas_nocturnas).

    Funciona cruzando medianoche. Resuelve minuto a minuto -> redondeo simple.
    """
    if end <= start:
        return 0.0, 0.0
    diurna_segs = 0
    nocturna_segs = 0
    cursor = start
    # Iteramos por tramos de hasta una hora para limitar el costo
    while cursor < end:
        siguiente = min(cursor + timedelta(minutes=15), end)
        # Tomamos la mitad del tramo como representativa
        mid = cursor + (siguiente - cursor) / 2
        secs = (siguiente - cursor).total_seconds()
        if es_nocturna(mid.time()):
            nocturna_segs += secs
        else:
            diurna_segs += secs
        cursor = siguiente
    return diurna_segs / 3600.0, nocturna_segs / 3600.0


def calcular_dia(
    fecha: date,
    marcajes_ordenados: list[datetime],
    *,
    jornada_ordinaria_h: float = JORNADA_ORDINARIA_DIARIA_HORAS,
    almuerzo_descuento_min: int = 60,
    hora_entrada_esperada: time | None = None,
    tolerancia_min: int = 10,
) -> TramoHoras:
    """Calcula tramo de horas para un dia.

    Reglas:
      - Pares de marcajes: (entrada, salida), (entrada2, salida2), ...
      - Si solo hay 2 marcajes -> entrada y salida del dia
      - Si hay 4 marcajes -> entrada, almuerzo_out, almuerzo_in, salida
      - Si hay numero impar -> el ultimo queda colgando y se ignora (warning)
      - Si NO marcaron almuerzo y trabajaron mas de 6h corridas -> descuento auto
      - Si el dia es domingo o festivo -> todo el tiempo va a dom_fest_*
      - Lo que excede `jornada_ordinaria_h` en el dia -> horas extra
    """
    t = TramoHoras()
    if not marcajes_ordenados:
        return t

    t.primera_entrada = marcajes_ordenados[0].time()
    t.ultima_salida = marcajes_ordenados[-1].time()

    # Minutos tarde (vs hora esperada)
    if hora_entrada_esperada:
        dt_esperado = datetime.combine(fecha, hora_entrada_esperada)
        dt_real = marcajes_ordenados[0]
        delta = dt_real - dt_esperado.replace(tzinfo=dt_real.tzinfo)
        minutos = int(delta.total_seconds() / 60)
        if minutos > tolerancia_min:
            t.minutos_tarde = minutos

    # Emparejar marcajes consecutivos. Si es impar, descartamos el ultimo.
    pares: list[tuple[datetime, datetime]] = []
    i = 0
    while i + 1 < len(marcajes_ordenados):
        pares.append((marcajes_ordenados[i], marcajes_ordenados[i + 1]))
        i += 2

    # Sumar horas brutas, separando diurnas/nocturnas
    bruto_diurno = 0.0
    bruto_nocturno = 0.0
    for entrada, salida in pares:
        d, n = _split_diurna_nocturna(entrada, salida)
        bruto_diurno += d
        bruto_nocturno += n

    # Si hubo 1 solo par y trabajaron > 6h corridas sin almuerzo marcado -> descontar
    if len(pares) == 1 and (bruto_diurno + bruto_nocturno) > 6.0:
        descuento_h = almuerzo_descuento_min / 60.0
        # Descontamos del bruto diurno (el almuerzo cae en franja diurna normalmente)
        if bruto_diurno >= descuento_h:
            bruto_diurno -= descuento_h
        else:
            bruto_diurno = 0.0
            bruto_nocturno -= (descuento_h - bruto_diurno)
        t.minutos_almuerzo = almuerzo_descuento_min
    elif len(pares) >= 2:
        # Marcaron almuerzo: el tiempo entre par1 y par2 es el almuerzo
        gap = pares[1][0] - pares[0][1]
        t.minutos_almuerzo = max(0, int(gap.total_seconds() / 60))

    total_h = bruto_diurno + bruto_nocturno

    # Es domingo? es festivo?
    es_dom = fecha.weekday() == 6
    es_dom_o_fest = es_dom or es_festivo(fecha)

    if es_dom_o_fest:
        # TODO el tiempo va a dominical/festivo (no hay ordinarias)
        # Y lo que excede la jornada legal del dia es extra
        if total_h <= jornada_ordinaria_h:
            t.dom_fest_ord_diurnas = bruto_diurno
            t.dom_fest_ord_nocturnas = bruto_nocturno
        else:
            # Distribuimos: las primeras `jornada_ordinaria_h` como ord. dom/fest,
            # el resto como extra dom/fest. Proporcional segun proporcion diurno/nocturno.
            if total_h > 0:
                prop_d = bruto_diurno / total_h
                prop_n = bruto_nocturno / total_h
            else:
                prop_d, prop_n = 1.0, 0.0
            ord_total = min(jornada_ordinaria_h, total_h)
            extra_total = max(0.0, total_h - jornada_ordinaria_h)
            t.dom_fest_ord_diurnas = ord_total * prop_d
            t.dom_fest_ord_nocturnas = ord_total * prop_n
            t.dom_fest_extra_diurnas = extra_total * prop_d
            t.dom_fest_extra_nocturnas = extra_total * prop_n
    else:
        # Dia laboral normal: las primeras `jornada` son ordinarias, el resto extra.
        # Mantenemos la separacion diurna/nocturna en cada categoria.
        if total_h <= jornada_ordinaria_h:
            t.ordinarias_diurnas = bruto_diurno
            t.ordinarias_nocturnas = bruto_nocturno
        else:
            if total_h > 0:
                prop_d = bruto_diurno / total_h
                prop_n = bruto_nocturno / total_h
            else:
                prop_d, prop_n = 1.0, 0.0
            ord_total = jornada_ordinaria_h
            extra_total = total_h - jornada_ordinaria_h
            t.ordinarias_diurnas = ord_total * prop_d
            t.ordinarias_nocturnas = ord_total * prop_n
            t.extra_diurnas = extra_total * prop_d
            t.extra_nocturnas = extra_total * prop_n

    return t
