"""Explora la cuenta Siigo: autentica, descarga catalogos y muestras de documentos.

Salidas:
  - explorations/<endpoint>.json  -> datos completos
  - explorations/summary.md       -> resumen humano legible
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from siigo_client import SiigoClient


OUT_DIR = Path(__file__).parent / "explorations"
OUT_DIR.mkdir(exist_ok=True)


def save_json(name: str, data: Any) -> Path:
    path = OUT_DIR / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def safe_call(label: str, fn):
    print(f"\n=== {label} ===", flush=True)
    try:
        out = fn()
        if isinstance(out, list):
            print(f"  OK -> {len(out)} items", flush=True)
        elif isinstance(out, dict):
            keys = list(out.keys())[:6]
            print(f"  OK -> dict keys: {keys}", flush=True)
        else:
            print(f"  OK -> {type(out).__name__}", flush=True)
        return out
    except httpx.HTTPStatusError as e:
        print(f"  HTTP {e.response.status_code}: {e.response.text[:300]}", flush=True)
        return {"_error": "http", "status": e.response.status_code, "body": e.response.text[:1000]}
    except Exception as e:
        print(f"  ERR: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        return {"_error": "exception", "type": type(e).__name__, "message": str(e)}


def main() -> int:
    print(f"Inicio: {datetime.now().isoformat()}", flush=True)
    with SiigoClient() as siigo:
        print(f"Base URL: {siigo.base_url}", flush=True)
        print(f"Username: {siigo.username}", flush=True)
        print(f"Partner-Id: {siigo.partner_id}", flush=True)

        token = safe_call("AUTH", siigo._authenticate)
        if isinstance(token, dict) and token.get("_error"):
            print("\nNo se pudo autenticar. Abortando.", flush=True)
            return 1
        print(f"  Token (primeros 30): {str(token)[:30]}...", flush=True)

        # Catalogos clave para crear documentos
        endpoints: dict[str, str] = {
            "document_types_FV": "/v1/document-types?type=FV",
            "document_types_FC": "/v1/document-types?type=FC",
            "document_types_RC": "/v1/document-types?type=RC",
            "document_types_DS": "/v1/document-types?type=DS",
            "document_types_NC": "/v1/document-types?type=NC",
            "document_types_ND": "/v1/document-types?type=ND",
            "taxes": "/v1/taxes",
            "payment_types_FV": "/v1/payment-types?document_type=FV",
            "payment_types_FC": "/v1/payment-types?document_type=FC",
            "users": "/v1/users",
            "cost_centers": "/v1/cost-centers",
            "warehouses": "/v1/warehouses",
            "fixed_assets": "/v1/fixed-assets",
            "account_groups": "/v1/account-groups",
            "accounting_periods": "/v1/accounting-periods",
        }

        catalogs: dict[str, Any] = {}
        for name, path in endpoints.items():
            catalogs[name] = safe_call(name, lambda p=path: siigo.get(p))
            save_json(name, catalogs[name])

        # Customers - primeros 50
        customers = safe_call(
            "customers (page 1, 50)",
            lambda: siigo.get("/v1/customers", params={"page_size": 50, "page": 1}),
        )
        save_json("customers_sample", customers)

        # Products - primeros 50
        products = safe_call(
            "products (page 1, 50)",
            lambda: siigo.get("/v1/products", params={"page_size": 50, "page": 1}),
        )
        save_json("products_sample", products)

        # Invoices - ultimas 50
        invoices = safe_call(
            "invoices (page 1, 50)",
            lambda: siigo.get("/v1/invoices", params={"page_size": 50, "page": 1}),
        )
        save_json("invoices_sample", invoices)

        # Purchases - ultimas 50
        purchases = safe_call(
            "purchases (page 1, 50)",
            lambda: siigo.get("/v1/purchases", params={"page_size": 50, "page": 1}),
        )
        save_json("purchases_sample", purchases)

        # Credit notes
        credit_notes = safe_call(
            "credit_notes (page 1, 20)",
            lambda: siigo.get("/v1/credit-notes", params={"page_size": 20, "page": 1}),
        )
        save_json("credit_notes_sample", credit_notes)

        # Vouchers (recibos de caja, comprobantes de pago)
        vouchers = safe_call(
            "vouchers (page 1, 20)",
            lambda: siigo.get("/v1/vouchers", params={"page_size": 20, "page": 1}),
        )
        save_json("vouchers_sample", vouchers)

        # Journals (asientos contables)
        journals = safe_call(
            "journals (page 1, 10)",
            lambda: siigo.get("/v1/journals", params={"page_size": 10, "page": 1}),
        )
        save_json("journals_sample", journals)

        # Resumen
        write_summary(
            siigo=siigo,
            catalogs=catalogs,
            customers=customers,
            products=products,
            invoices=invoices,
            purchases=purchases,
            credit_notes=credit_notes,
            vouchers=vouchers,
            journals=journals,
        )

    print("\nListo. Revisa explorations/ y explorations/summary.md", flush=True)
    return 0


def _count(d: Any) -> str:
    if isinstance(d, dict) and d.get("_error"):
        return f"ERROR: {d.get('status') or d.get('type')}"
    if isinstance(d, list):
        return f"{len(d)} items"
    if isinstance(d, dict):
        if "results" in d and isinstance(d["results"], list):
            pag = d.get("pagination") or {}
            total = pag.get("total_results")
            return f"{len(d['results'])} en pagina (total {total})" if total is not None else f"{len(d['results'])} en pagina"
        return f"dict keys={list(d.keys())[:5]}"
    return str(type(d).__name__)


def _first_item(d: Any) -> Any:
    if isinstance(d, list) and d:
        return d[0]
    if isinstance(d, dict) and isinstance(d.get("results"), list) and d["results"]:
        return d["results"][0]
    return None


def write_summary(*, siigo: SiigoClient, **payloads: Any) -> None:
    lines: list[str] = []
    lines.append(f"# Exploracion cuenta Siigo — {datetime.now().isoformat(timespec='seconds')}")
    lines.append("")
    lines.append(f"- Base URL: `{siigo.base_url}`")
    lines.append(f"- Usuario: `{siigo.username}`")
    lines.append(f"- Partner-Id: `{siigo.partner_id}`")
    lines.append("")
    lines.append("## Conteos por endpoint")
    lines.append("")
    lines.append("| Endpoint | Resultado |")
    lines.append("|---|---|")
    for k, v in payloads.items():
        lines.append(f"| `{k}` | {_count(v)} |")

    lines.append("")
    lines.append("## Muestra del primer item de cada coleccion")
    lines.append("")
    for k, v in payloads.items():
        item = _first_item(v)
        lines.append(f"### {k}")
        if item is None:
            lines.append("_vacio o error_")
        else:
            snippet = json.dumps(item, indent=2, ensure_ascii=False, default=str)
            if len(snippet) > 2500:
                snippet = snippet[:2500] + "\n... [truncado]"
            lines.append("```json")
            lines.append(snippet)
            lines.append("```")
        lines.append("")

    (OUT_DIR / "summary.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
