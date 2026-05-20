"""Cliente IMAP para Gmail: lee correos no procesados con adjuntos PDF.

Flujo:
  1. Conectar a IMAP (Gmail o cualquier proveedor).
  2. Buscar correos no leidos del INBOX con adjuntos.
  3. Por cada correo nuevo (message_id no visto):
     a. Descargar todos los adjuntos PDF / imagen.
     b. Por cada adjunto, calcular hash. Si ya esta en DB, skip.
     c. Llamar a Gemini OCR -> FacturaProveedor.
     d. Persistir en facturas_correo con estado 'pendiente'.
  4. Devolver lista de facturas pendientes para que el bot las muestre.

NO crea facturas en Siigo automaticamente. Eso lo hace el bot cuando vos confirmas.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from imap_tools import MailBox, AND

from skiimo.config import (
    IMAP_APP_PASSWORD,
    IMAP_ENABLED,
    IMAP_FOLDER,
    IMAP_HOST,
    IMAP_PORT,
    IMAP_USER,
)
from skiimo.db.schema import get_conn


log = logging.getLogger("skiimo.gmail_imap")

PDF_MIMES = {"application/pdf"}
IMAGE_MIMES = {"image/jpeg", "image/png", "image/jpg", "image/webp"}


@dataclass(slots=True)
class FacturaCorreo:
    """Resultado de procesar un adjunto."""
    id: int                              # PK en facturas_correo
    correo_id: int                       # FK a correos_procesados
    adjunto_hash: str
    nombre_archivo: str
    proveedor_nit: str | None
    proveedor_nombre: str | None
    numero_factura: str | None
    total: float | None
    confidence: float
    payload: dict
    remitente: str | None
    asunto: str | None
    estado: str = "pendiente"


@dataclass(slots=True)
class ResultadoLectura:
    """Resumen de una corrida de lectura de correo."""
    correos_leidos: int = 0
    correos_nuevos: int = 0
    adjuntos_procesados: int = 0
    facturas_extraidas: list[FacturaCorreo] = field(default_factory=list)
    errores: list[str] = field(default_factory=list)


# =============================================================================
# DB HELPERS
# =============================================================================

def _correo_ya_visto(message_id: str) -> int | None:
    """Devuelve el id local si ya procesamos ese message_id."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM correos_procesados WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def _adjunto_ya_visto(adjunto_hash: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM facturas_correo WHERE adjunto_hash = ? ORDER BY id DESC LIMIT 1",
            (adjunto_hash,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _factura_ya_existe(nit: str | None, numero: str | None) -> dict | None:
    """Busca si ya existe una factura del proveedor con ese numero, en Siigo (espejo) o en el bot."""
    if not nit or not numero:
        return None
    conn = get_conn()
    try:
        # Buscar en espejo de Siigo (purchases)
        row = conn.execute(
            """SELECT id, name as siigo_name, total FROM siigo_purchases
               WHERE supplier_ident = ?
                 AND (provider_inv_number = ? OR provider_inv_number = ?)
               ORDER BY date DESC LIMIT 1""",
            (nit, numero, str(numero).lstrip("0")),
        ).fetchone()
        if row:
            return {"fuente": "siigo", **dict(row)}
        # Buscar en facturas_correo (ya procesadas por bot)
        row = conn.execute(
            """SELECT id, siigo_purchase_name, total FROM facturas_correo
               WHERE proveedor_nit = ? AND numero_factura = ?
                 AND estado IN ('aprobada', 'enviada')
               ORDER BY id DESC LIMIT 1""",
            (nit, numero),
        ).fetchone()
        if row:
            return {"fuente": "bot", **dict(row)}
    finally:
        conn.close()
    return None


def _crear_correo_procesado(msg, conn: sqlite3.Connection) -> int:
    """Inserta o reutiliza un correo en la tabla. Devuelve su id."""
    now = datetime.now().isoformat(timespec="seconds")
    fecha_correo = ""
    if msg.date:
        try:
            fecha_correo = msg.date.isoformat()
        except Exception:
            fecha_correo = str(msg.date)
    cur = conn.execute(
        """INSERT INTO correos_procesados
           (message_id, remitente, asunto, fecha_correo, adjuntos_count, estado, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'pendiente', ?, ?)""",
        (
            msg.uid + "@" + (msg.from_ or "unknown"),  # fallback si no hay Message-ID estandar
            (msg.from_ or "")[:200],
            (msg.subject or "")[:300],
            fecha_correo,
            len(list(msg.attachments)),
            now, now,
        ),
    )
    return cur.lastrowid


def _persistir_factura_correo(
    conn: sqlite3.Connection,
    correo_id: int,
    adjunto_hash: str,
    nombre_archivo: str,
    factura: dict,
    confidence: float,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    total = factura.get("total")
    cur = conn.execute(
        """INSERT INTO facturas_correo
           (correo_id, adjunto_hash, nombre_archivo, proveedor_nit, proveedor_nombre,
            numero_factura, total, payload_extraido, confidence, estado,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pendiente', ?, ?)""",
        (
            correo_id,
            adjunto_hash,
            nombre_archivo[:200],
            factura.get("proveedor_nit"),
            (factura.get("proveedor_nombre") or "")[:200],
            factura.get("numero_factura"),
            float(total) if total is not None else None,
            json.dumps(factura, ensure_ascii=False, default=str),
            float(confidence),
            now, now,
        ),
    )
    return cur.lastrowid


def _actualizar_estado_correo(conn: sqlite3.Connection, correo_id: int,
                                facturas_creadas: int, estado: str) -> None:
    conn.execute(
        """UPDATE correos_procesados
           SET facturas_creadas = ?, estado = ?, updated_at = ?
           WHERE id = ?""",
        (facturas_creadas, estado, datetime.now().isoformat(timespec="seconds"), correo_id),
    )


# =============================================================================
# LECTURA DE CORREO
# =============================================================================

def _es_factura_potencial(asunto: str, filename: str) -> bool:
    """Heuristica simple para filtrar PDFs que parezcan facturas."""
    asunto_l = (asunto or "").lower()
    file_l = (filename or "").lower()
    if not file_l.endswith(".pdf") and not any(file_l.endswith(e) for e in (".jpg", ".jpeg", ".png", ".webp")):
        return False
    keywords = (
        "factura", "invoice", "fe-", "fc-", "fv-", "remision",
        "comprobante", "cuenta de cobro", "documento", "boleta",
    )
    if any(k in asunto_l for k in keywords) or any(k in file_l for k in keywords):
        return True
    # Si tiene PDF y el asunto es cortito, dejarlo pasar (proveedores informales)
    if file_l.endswith(".pdf") and len(asunto_l) < 80:
        return True
    return False


def leer_correos(*, limit: int = 20, marcar_leidos: bool = True) -> ResultadoLectura:
    """Lee los correos no leidos del INBOX, procesa adjuntos PDF/imagen como facturas."""
    from skiimo.llm.gemini import extract_factura_proveedor

    resultado = ResultadoLectura()

    if not IMAP_ENABLED:
        resultado.errores.append("IMAP no esta configurado (falta IMAP_USER e IMAP_APP_PASSWORD en .env)")
        return resultado

    try:
        with MailBox(IMAP_HOST, port=IMAP_PORT).login(IMAP_USER, IMAP_APP_PASSWORD, IMAP_FOLDER) as mailbox:
            # Solo no leidos
            criteria = AND(seen=False)
            msgs = list(mailbox.fetch(criteria, limit=limit, mark_seen=False, bulk=True))
            resultado.correos_leidos = len(msgs)
            log.info("IMAP: %d correos no leidos", len(msgs))

            for msg in msgs:
                # Saltar si no tiene adjuntos
                adjuntos = list(msg.attachments)
                if not adjuntos:
                    if marcar_leidos:
                        try:
                            mailbox.flag([msg.uid], "\\Seen", True)
                        except Exception:
                            pass
                    continue

                # Filtrar adjuntos que parezcan facturas
                adjuntos_validos = []
                for att in adjuntos:
                    mime = att.content_type or ""
                    if mime not in PDF_MIMES and mime not in IMAGE_MIMES:
                        continue
                    if not _es_factura_potencial(msg.subject or "", att.filename or ""):
                        continue
                    adjuntos_validos.append(att)

                if not adjuntos_validos:
                    if marcar_leidos:
                        try:
                            mailbox.flag([msg.uid], "\\Seen", True)
                        except Exception:
                            pass
                    continue

                # Verificar duplicado por message_id (Gmail provee headers['Message-ID'])
                message_id = msg.headers.get("message-id", (None,))[0] if isinstance(msg.headers, dict) else None
                if not message_id:
                    message_id = msg.uid + "@" + (msg.from_ or "unknown")

                if _correo_ya_visto(message_id):
                    log.info("IMAP: correo %s ya procesado, skip", message_id[:30])
                    if marcar_leidos:
                        try:
                            mailbox.flag([msg.uid], "\\Seen", True)
                        except Exception:
                            pass
                    continue

                # Procesar
                resultado.correos_nuevos += 1
                conn = get_conn()
                try:
                    now = datetime.now().isoformat(timespec="seconds")
                    # Insertar correo
                    fecha_correo = ""
                    if msg.date:
                        try:
                            fecha_correo = msg.date.isoformat()
                        except Exception:
                            fecha_correo = str(msg.date)
                    cur = conn.execute(
                        """INSERT INTO correos_procesados
                           (message_id, remitente, asunto, fecha_correo, adjuntos_count, estado, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, 'pendiente', ?, ?)""",
                        (
                            message_id,
                            (msg.from_ or "")[:200],
                            (msg.subject or "")[:300],
                            fecha_correo,
                            len(adjuntos_validos),
                            now, now,
                        ),
                    )
                    correo_id = cur.lastrowid
                    conn.commit()

                    facturas_ok = 0
                    for att in adjuntos_validos:
                        try:
                            data = att.payload
                            adj_hash = hashlib.sha256(data).hexdigest()

                            # Skip si ya procesamos este adjunto exacto
                            if _adjunto_ya_visto(adj_hash):
                                log.info("IMAP: adjunto %s ya procesado antes, skip", adj_hash[:10])
                                continue

                            mime = att.content_type or "application/pdf"
                            factura_pyd = extract_factura_proveedor(data, mime_type=mime)
                            factura = factura_pyd.model_dump()

                            # Skip si ya existe en Siigo (mismo NIT+numero)
                            existente = _factura_ya_existe(
                                factura.get("proveedor_nit"),
                                factura.get("numero_factura"),
                            )
                            if existente:
                                log.info("Factura %s del NIT %s ya existe en %s",
                                          factura.get("numero_factura"),
                                          factura.get("proveedor_nit"),
                                          existente.get("fuente"))
                                # Aun asi la guardamos como informativa con marca de duplicada
                                fid = _persistir_factura_correo(
                                    conn, correo_id, adj_hash, att.filename or "?",
                                    factura, factura_pyd.confidence,
                                )
                                conn.execute(
                                    "UPDATE facturas_correo SET estado = 'duplicada', error = ? WHERE id = ?",
                                    (f"Ya existe en {existente.get('fuente')}: {existente.get('siigo_name') or existente.get('siigo_purchase_name')}", fid),
                                )
                                conn.commit()
                                continue

                            fid = _persistir_factura_correo(
                                conn, correo_id, adj_hash, att.filename or "?",
                                factura, factura_pyd.confidence,
                            )
                            conn.commit()

                            resultado.facturas_extraidas.append(FacturaCorreo(
                                id=fid,
                                correo_id=correo_id,
                                adjunto_hash=adj_hash,
                                nombre_archivo=att.filename or "?",
                                proveedor_nit=factura.get("proveedor_nit"),
                                proveedor_nombre=factura.get("proveedor_nombre"),
                                numero_factura=factura.get("numero_factura"),
                                total=factura.get("total"),
                                confidence=factura_pyd.confidence,
                                payload=factura,
                                remitente=msg.from_,
                                asunto=msg.subject,
                            ))
                            facturas_ok += 1
                            resultado.adjuntos_procesados += 1
                        except Exception as e:
                            log.exception("Error procesando adjunto %s", att.filename)
                            resultado.errores.append(f"{att.filename}: {e}")

                    # Estado del correo
                    if facturas_ok == 0:
                        estado_correo = "sin_facturas"
                    elif facturas_ok < len(adjuntos_validos):
                        estado_correo = "parcial"
                    else:
                        estado_correo = "pendiente"  # listo para que usuario revise
                    _actualizar_estado_correo(conn, correo_id, facturas_ok, estado_correo)
                    conn.commit()

                    if marcar_leidos:
                        try:
                            mailbox.flag([msg.uid], "\\Seen", True)
                        except Exception:
                            pass
                finally:
                    conn.close()

    except Exception as e:
        log.exception("Error IMAP")
        resultado.errores.append(f"Error IMAP: {e}")

    return resultado


def facturas_correo_pendientes(limit: int = 20) -> list[dict]:
    """Lista facturas extraidas de correo que esperan revision."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT fc.*, co.remitente, co.asunto, co.fecha_correo
               FROM facturas_correo fc
               JOIN correos_procesados co ON co.id = fc.correo_id
               WHERE fc.estado = 'pendiente'
               ORDER BY fc.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_factura_correo(factura_id: int) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT fc.*, co.remitente, co.asunto FROM facturas_correo fc
               LEFT JOIN correos_procesados co ON co.id = fc.correo_id
               WHERE fc.id = ?""",
            (factura_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def marcar_factura_correo(factura_id: int, estado: str, **extras: Any) -> None:
    """Actualiza estado de una factura de correo (aprobada, enviada, error, descartada)."""
    fields = ["estado = ?", "updated_at = ?"]
    values: list = [estado, datetime.now().isoformat(timespec="seconds")]
    for k, v in extras.items():
        if k in ("siigo_purchase_id", "siigo_purchase_name", "error"):
            fields.append(f"{k} = ?")
            values.append(v)
    values.append(factura_id)
    conn = get_conn()
    try:
        conn.execute(f"UPDATE facturas_correo SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    r = leer_correos(limit=10, marcar_leidos=False)
    print(f"\nCorreos leidos: {r.correos_leidos}")
    print(f"Correos nuevos procesados: {r.correos_nuevos}")
    print(f"Adjuntos OCR: {r.adjuntos_procesados}")
    print(f"Facturas extraidas: {len(r.facturas_extraidas)}")
    for f in r.facturas_extraidas[:5]:
        print(f"  - {f.proveedor_nombre} ({f.proveedor_nit}) #{f.numero_factura} ${f.total or 0:,.0f}")
    if r.errores:
        print("\nErrores:")
        for e in r.errores:
            print(f"  ! {e}")
