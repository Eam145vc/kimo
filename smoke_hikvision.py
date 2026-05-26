r"""Smoke test del Hikvision DS-K1T321MFWX via skiimo.hikvision.

Solo LEE del equipo. No modifica nada. Confirma:
  1. device_info() trae modelo + serial + firmware
  2. device_time() trae la hora actual
  3. count_persons() y iter_persons() listan personas
  4. iter_events() para los ultimos 7 dias

Uso: .\.venv\Scripts\python.exe smoke_hikvision.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from skiimo.hikvision import TZ_BOGOTA, HikClient


def main() -> None:
    print("\nHikvision smoke test")
    print("=" * 60)

    with HikClient() as hik:
        # 1. Device info
        print("\n[1] device_info()")
        di = hik.device_info()
        print(f"  modelo:    {di.model}")
        print(f"  serial:    {di.serial}")
        print(f"  firmware:  {di.firmware}")
        print(f"  mac:       {di.mac}")
        print(f"  name:      {di.name}")

        # 2. Time
        print("\n[2] device_time()")
        t = hik.device_time()
        print(f"  localTime: {t.get('localTime')}")
        print(f"  timeZone:  {t.get('timeZone')}")
        if t.get("timeZone") and "GMT-5" not in (t.get("timeZone") or "") and "-5:00" not in (t.get("timeZone") or ""):
            print(f"  [WARN] timezone parece NO Colombia. Ajustar en panel web.")

        # 3. Personas
        print("\n[3] count_persons() + iter_persons()")
        n = hik.count_persons()
        print(f"  total: {n}")
        for p in list(hik.iter_persons())[:5]:
            print(f"   - id={p.employee_no}  name={p.name!r}  type={p.user_type}")

        # 4. Eventos
        print("\n[4] iter_events() ultimos 7 dias")
        end = datetime.now(TZ_BOGOTA)
        start = end - timedelta(days=7)
        events = list(hik.iter_events(start, end))
        print(f"  total: {len(events)}")
        for e in events[:5]:
            print(
                f"   - {e.timestamp.isoformat()} emp={e.employee_no} "
                f"name={e.name!r} verify={e.verify_mode} att={e.attendance_status}"
            )

    print("\nSmoke OK")


if __name__ == "__main__":
    main()
