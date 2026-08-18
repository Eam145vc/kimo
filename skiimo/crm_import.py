"""Importa el historial de WhatsApp (iOS ChatStorage.sqlite) al CRM.

Uso:
    python -m skiimo.crm_import /ruta/ChatStorage.sqlite --linea 1 [--desde 2024-01-01] [--dry-run]

De donde sale ChatStorage.sqlite:
    Backup local del iPhone (iTunes / app Dispositivos Apple) -> extraer el
    contenedor del app group de WhatsApp Business:
        group.net.whatsapp.WhatsAppSMB.shared/ChatStorage.sqlite
    (WhatsApp normal: group.net.whatsapp.WhatsApp.shared)

Importa solo chats individuales (los grupos @g.us se saltan). Idempotente:
re-correrlo no duplica mensajes (dedup por stanza id de WhatsApp).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from skiimo.db.schema import get_conn

TZ = ZoneInfo("America/Bogota")
APPLE_EPOCH = 978307200  # 2001-01-01 UTC en unix seconds

# ZMESSAGETYPE de iOS -> tipo del CRM
TIPOS = {
    0: "text", 1: "image", 2: "video", 3: "audio", 4: "contact",
    5: "location", 7: "url", 8: "document", 11: "gif", 15: "sticker",
}
TIPOS_SISTEMA = {6, 10, 12}  # eventos de grupo / sistema: se saltan


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _apple_ts_to_iso(val: float | None) -> str | None:
    if val is None:
        return None
    try:
        return (
            datetime.fromtimestamp(APPLE_EPOCH + float(val), tz=timezone.utc)
            .astimezone(TZ)
            .isoformat(timespec="seconds")
        )
    except Exception:
        return None


def importar(chatstorage: Path, linea_id: int, desde: str | None, dry_run: bool) -> None:
    src = sqlite3.connect(f"file:{chatstorage}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    dst = get_conn()

    linea = dst.execute("SELECT id, nombre FROM wa_lineas WHERE id = ?", (linea_id,)).fetchone()
    if not linea:
        sys.exit(f"No existe la linea {linea_id}. Crea/mira las lineas en el panel (/crm/usuarios).")

    sesiones = src.execute(
        """SELECT Z_PK, ZCONTACTJID, ZPARTNERNAME
           FROM ZWACHATSESSION
           WHERE ZCONTACTJID LIKE '%@s.whatsapp.net'"""
    ).fetchall()
    print(f"Chats individuales en el backup: {len(sesiones)}")

    total_msgs = 0
    total_nuevos = 0
    now = _now()

    for ses in sesiones:
        jid = ses["ZCONTACTJID"] or ""
        telefono = jid.split("@")[0]
        if not telefono.isdigit():
            continue
        nombre = (ses["ZPARTNERNAME"] or "").strip() or None

        rows = src.execute(
            """SELECT Z_PK, ZISFROMME, ZMESSAGETYPE, ZTEXT, ZMESSAGEDATE, ZSTANZAID
               FROM ZWAMESSAGE
               WHERE ZCHATSESSION = ?
               ORDER BY ZMESSAGEDATE""",
            (ses["Z_PK"],),
        ).fetchall()
        if not rows:
            continue

        msgs = []
        for m in rows:
            mtype = m["ZMESSAGETYPE"]
            if mtype in TIPOS_SISTEMA:
                continue
            ts_iso = _apple_ts_to_iso(m["ZMESSAGEDATE"])
            if not ts_iso:
                continue
            if desde and ts_iso[:10] < desde:
                continue
            tipo = TIPOS.get(mtype, "otro")
            body = (m["ZTEXT"] or "").strip() or None
            if tipo != "text" and not body:
                body = None  # media sin caption
            stanza = (m["ZSTANZAID"] or "").strip()
            wa_msg_id = stanza if stanza else f"ios:{linea_id}:{m['Z_PK']}"
            msgs.append((wa_msg_id, "out" if m["ZISFROMME"] else "in", tipo, body, ts_iso))

        if not msgs:
            continue
        total_msgs += len(msgs)

        if dry_run:
            print(f"  +{telefono} ({nombre or 'sin nombre'}): {len(msgs)} mensajes")
            continue

        # Contacto
        row = dst.execute("SELECT id FROM wa_contactos WHERE telefono = ?", (telefono,)).fetchone()
        if row:
            contacto_id = row["id"]
            if nombre:
                dst.execute(
                    "UPDATE wa_contactos SET nombre_wa = COALESCE(nombre_wa, ?), updated_at = ? WHERE id = ?",
                    (nombre, now, contacto_id),
                )
        else:
            digits = telefono[-10:]
            match = dst.execute(
                """SELECT id FROM siigo_customers
                   WHERE active = 1 AND phone IS NOT NULL AND phone != ''
                     AND REPLACE(REPLACE(REPLACE(REPLACE(phone,' ',''),'-',''),'(',''),')','') LIKE ?
                   LIMIT 1""",
                (f"%{digits}",),
            ).fetchone()
            cur = dst.execute(
                """INSERT INTO wa_contactos (telefono, nombre_wa, siigo_customer_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (telefono, nombre, match["id"] if match else None, now, now),
            )
            contacto_id = cur.lastrowid

        # Chat
        row = dst.execute(
            "SELECT id FROM wa_chats WHERE linea_id = ? AND contacto_id = ?",
            (linea_id, contacto_id),
        ).fetchone()
        if row:
            chat_id = row["id"]
        else:
            etapa = dst.execute("SELECT id FROM crm_etapas ORDER BY orden LIMIT 1").fetchone()
            cur = dst.execute(
                "INSERT INTO wa_chats (linea_id, contacto_id, etapa_id, created_at) VALUES (?, ?, ?, ?)",
                (linea_id, contacto_id, etapa["id"] if etapa else None, now),
            )
            chat_id = cur.lastrowid

        # Mensajes (dedup por wa_msg_id UNIQUE)
        nuevos = 0
        for wa_msg_id, direccion, tipo, body, ts_iso in msgs:
            try:
                dst.execute(
                    """INSERT INTO wa_mensajes
                       (chat_id, wa_msg_id, direccion, tipo, body, ts, origen, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'import', ?)""",
                    (chat_id, wa_msg_id, direccion, tipo, body, ts_iso, now),
                )
                nuevos += 1
            except sqlite3.IntegrityError:
                pass  # ya importado
        total_nuevos += nuevos

        # last_msg del chat
        last = dst.execute(
            "SELECT body, tipo, ts FROM wa_mensajes WHERE chat_id = ? ORDER BY ts DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if last:
            preview = last["body"] if last["tipo"] == "text" else f"📎 {last['tipo']}"
            dst.execute(
                """UPDATE wa_chats
                   SET last_msg_at = MAX(COALESCE(last_msg_at,''), ?), last_msg_preview = ?
                   WHERE id = ?""",
                (last["ts"], (preview or "")[:120], chat_id),
            )
        dst.commit()
        print(f"  ✓ +{telefono} ({nombre or 'sin nombre'}): {nuevos}/{len(msgs)} nuevos")

    src.close()
    dst.close()
    print(f"\nListo. {total_msgs} mensajes leidos, {total_nuevos} importados a la linea '{linea['nombre']}'.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Importar ChatStorage.sqlite (iOS) al CRM")
    ap.add_argument("chatstorage", type=Path, help="ruta a ChatStorage.sqlite del backup")
    ap.add_argument("--linea", type=int, required=True, help="id de wa_lineas destino")
    ap.add_argument("--desde", default=None, help="YYYY-MM-DD: ignorar mensajes anteriores")
    ap.add_argument("--dry-run", action="store_true", help="solo contar, no escribir")
    args = ap.parse_args()
    if not args.chatstorage.exists():
        sys.exit(f"No existe {args.chatstorage}")
    importar(args.chatstorage, args.linea, args.desde, args.dry_run)


if __name__ == "__main__":
    main()
