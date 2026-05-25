"""Matching difuso de clientes y productos contra el espejo local.

Estrategia:
  - Carga todos los registros en memoria al iniciar (cache).
  - Usa rapidfuzz para similitud.
  - Devuelve top-k candidatos con score 0..100.
  - Match por NIT/codigo es exacto; match por nombre es difuso.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz, process

from skiimo.db.schema import get_conn


def _normalize(s: str) -> str:
    if not s:
        return ""
    s = s.lower().strip()
    # quitar acentos
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    # quitar puntuacion comun
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _prefix_token_score(query: str, name: str, **kwargs) -> float:
    """Scorer custom para rapidfuzz: premia cuando los tokens del query son
    prefijos de tokens del name. Resuelve casos como 'sebas gomez' -> 'SEBASTIAN GOMEZ MONTOYA'
    que ni token_set_ratio ni partial_ratio captan.

    Lógica:
      - Tokens del query (>=3 chars) vs tokens del name.
      - Cada token del query cuenta como match si:
          * coincide exacto con algún token del name, O
          * es prefijo de algún token del name (>=3 chars de overlap), O
          * algún token del name es prefijo del query (>=3 chars)
      - Score = (matched / total_query_tokens) * 100
      - Si query no tiene tokens validos, retorna 0.
    """
    q_tokens = [t for t in query.split() if len(t) >= 3]
    n_tokens = [t for t in name.split() if len(t) >= 3]
    if not q_tokens or not n_tokens:
        return 0.0
    matched = 0
    for qt in q_tokens:
        for nt in n_tokens:
            if qt == nt or nt.startswith(qt) or qt.startswith(nt):
                matched += 1
                break
    return (matched / len(q_tokens)) * 100.0


@dataclass(slots=True)
class CustomerHit:
    id: str
    identification: str
    name: str
    commercial_name: str
    email: str
    score: float


@dataclass(slots=True)
class ProductHit:
    id: str
    code: str
    name: str
    account_group_name: str
    price_default: float | None
    iva_tax_id: int | None
    iva_percentage: float | None
    tax_included: bool
    score: float


class Matcher:
    """Carga catalogos en memoria. Recargar tras cada sync."""

    def __init__(self) -> None:
        self._customers: list[dict] = []
        self._products: list[dict] = []
        self._customer_keys: list[str] = []
        self._product_keys: list[str] = []
        self.reload()

    def reload(self) -> None:
        from skiimo.config import SIIGO_INVOICE_TEST_MODE, SIIGO_TEST_CUSTOMER_ID
        # Invalidar cache de invoice counts (lo refrescaremos lazy en search_customer)
        self._invoice_count_cache = None
        conn = get_conn()
        try:
            # En modo test cargamos tambien el cliente de prueba aunque este inactivo
            cust_filter = "active = 1"
            params: tuple = ()
            if SIIGO_INVOICE_TEST_MODE and SIIGO_TEST_CUSTOMER_ID:
                cust_filter = "active = 1 OR id = ?"
                params = (SIIGO_TEST_CUSTOMER_ID,)
            self._customers = [
                dict(r) for r in conn.execute(
                    f"SELECT id, type, identification, name, commercial_name, email "
                    f"FROM siigo_customers WHERE {cust_filter}",
                    params,
                )
            ]
            self._products = [
                dict(r) for r in conn.execute(
                    "SELECT id, code, name, account_group_name, price_default, "
                    "iva_tax_id, iva_percentage, tax_included "
                    "FROM siigo_products WHERE active = 1"
                )
            ]
        finally:
            conn.close()
        # claves precomputadas para rapidfuzz
        self._customer_keys = [
            _normalize(f"{c['name']} {c.get('commercial_name') or ''}")
            for c in self._customers
        ]
        self._product_keys = [
            _normalize(f"{p['name']} {p['code']}")
            for p in self._products
        ]

    # --- CLIENTES ---
    def find_customer_by_identification(self, identification: str) -> CustomerHit | None:
        ident = identification.strip()
        for c in self._customers:
            if c["identification"] == ident:
                return CustomerHit(
                    id=c["id"],
                    identification=c["identification"],
                    name=c["name"],
                    commercial_name=c.get("commercial_name") or "",
                    email=c.get("email") or "",
                    score=100.0,
                )
        return None

    def _get_customer_invoice_count(self, customer_id: str) -> int:
        """Cuenta facturas historicas del cliente (cache best-effort)."""
        if not hasattr(self, "_invoice_count_cache") or self._invoice_count_cache is None:
            conn = get_conn()
            try:
                rows = conn.execute(
                    "SELECT customer_id, COUNT(*) AS n FROM siigo_invoices GROUP BY customer_id"
                ).fetchall()
                self._invoice_count_cache = {r["customer_id"]: r["n"] for r in rows}
            except Exception:
                self._invoice_count_cache = {}
            finally:
                conn.close()
        return self._invoice_count_cache.get(customer_id, 0)

    def search_customer(self, query: str, *, limit: int = 5, min_score: int = 60) -> list[CustomerHit]:
        if not query.strip():
            return []
        # primero match exacto por NIT/CC
        if re.fullmatch(r"\d{5,}", query.strip()):
            exact = self.find_customer_by_identification(query.strip())
            if exact:
                return [exact]
        q = _normalize(query)
        # Estrategia: combinar token_set_ratio (premia tokens compartidos) con WRatio (penaliza
        # ruido). token_set_ratio resuelve casos como "Daniel Bernal" -> "DANIEL ALBERTO BERNAL ACOSTA"
        # que WRatio dejaba fuera. Tomamos top de cada uno y nos quedamos con el max score por candidato.
        candidates: dict[int, float] = {}
        for scorer in (fuzz.token_set_ratio, fuzz.WRatio, _prefix_token_score):
            for _, score, idx in process.extract(
                q, self._customer_keys, scorer=scorer, limit=limit * 3, score_cutoff=min_score
            ):
                candidates[idx] = max(candidates.get(idx, 0.0), float(score))

        # Tiebreaker: cuando 2+ candidatos tienen score similar, priorizar el que tiene mas
        # facturas historicas. Esto evita elegir un cliente raro sobre uno con mucha actividad.
        # Sort key: (-score, -invoice_count) para que mayor score primero, mayor count segundo.
        def _sort_key(item):
            idx, sc = item
            cid = self._customers[idx]["id"]
            return (-sc, -self._get_customer_invoice_count(cid))
        sorted_items = sorted(candidates.items(), key=_sort_key)[:limit]
        hits: list[CustomerHit] = []
        for idx, score in sorted_items:
            c = self._customers[idx]
            hits.append(
                CustomerHit(
                    id=c["id"],
                    identification=c["identification"],
                    name=c["name"],
                    commercial_name=c.get("commercial_name") or "",
                    email=c.get("email") or "",
                    score=score,
                )
            )
        return hits

    # --- PRODUCTOS ---
    def find_product_by_code(self, code: str) -> ProductHit | None:
        code_u = code.strip().upper()
        for p in self._products:
            if p["code"].upper() == code_u:
                return self._product_to_hit(p, 100.0)
        return None

    # Grupos que NO se venden (son insumos/herramientas/servicios internos).
    # Si el LLM no asigna codigo de un producto de venta, el fuzzy NO debe sugerir
    # estos como alternativa. Mejor reportar 'no encontrado'.
    _GRUPOS_NO_VENTA = {
        "MATERIAS PRIMAS",
        "MAQUINA",
        "REPUESTOS MAQUINAS",
        "SERVICIOS",
    }

    def search_product(self, query: str, *, limit: int = 5, min_score: int = 55,
                       incluir_no_venta: bool = False) -> list[ProductHit]:
        if not query.strip():
            return []
        # match por codigo exacto
        if re.fullmatch(r"[A-Za-z]{1,4}\d{1,5}", query.strip()):
            exact = self.find_product_by_code(query)
            if exact:
                return [exact]

        q = _normalize(query)
        # Detectar intencion del query
        quiere_sin_licor = "sin licor" in q or "sin lic" in q
        quiere_sachet = "sachet" in q or " 8 oz" in q or " 08 oz" in q
        quiere_6l = "6l" in q or "6 l" in q or "bolsa 6" in q
        # Si no menciona tamano y dice "bolsa", default es 6L
        if "bolsa" in q and not quiere_sachet and not quiere_6l:
            quiere_6l = True

        # token_set_ratio: ignora orden y duplicados
        all_results = process.extract(
            q, self._product_keys, scorer=fuzz.token_set_ratio,
            limit=30, score_cutoff=min_score,  # tomar mas para filtrar despues
        )

        # Re-rankear segun intencion
        scored: list[tuple[float, int]] = []
        for _, score, idx in all_results:
            p = self._products[idx]
            grupo = (p.get("account_group_name") or "").upper().strip()
            # Filtrar grupos no-venta para pedidos (a menos que incluir_no_venta=True)
            if not incluir_no_venta and grupo in self._GRUPOS_NO_VENTA:
                continue
            name = p["name"].upper()
            is_sin = "SIN LICOR" in name or "SIN LIC" in name
            is_sachet = "SACHET" in name
            is_6l = "6L" in name or "6 L" in name

            penalty = 0.0
            # Penalizar productos que NO matchean la intencion explicita
            if quiere_sachet and not is_sachet:
                penalty += 30  # buscaste sachet, no es sachet
            if quiere_6l and not is_6l:
                penalty += 30  # buscaste 6L, no es 6L
            if quiere_sin_licor and not is_sin:
                penalty += 20  # pediste sin licor, este es con licor
            # Si NO pediste sin licor, penalizar levemente los sin licor (default = con licor)
            if not quiere_sin_licor and is_sin:
                penalty += 5
            # Si NO pediste sachet, penalizar fuerte los sachets cuando hay alternativa 6L
            if not quiere_sachet and is_sachet and quiere_6l:
                penalty += 40

            scored.append((float(score) - penalty, idx))

        scored.sort(key=lambda x: x[0], reverse=True)
        result = []
        for s, idx in scored[:limit]:
            if s < min_score - 10:  # umbral final
                break
            result.append(self._product_to_hit(self._products[idx], s))
        return result

    def _product_to_hit(self, p: dict, score: float) -> ProductHit:
        return ProductHit(
            id=p["id"],
            code=p["code"],
            name=p["name"],
            account_group_name=p.get("account_group_name") or "",
            price_default=p.get("price_default"),
            iva_tax_id=p.get("iva_tax_id"),
            iva_percentage=p.get("iva_percentage"),
            tax_included=bool(p.get("tax_included")),
            score=score,
        )

    # --- STATS ---
    def stats(self) -> dict[str, int]:
        return {
            "customers": len(self._customers),
            "products": len(self._products),
        }


if __name__ == "__main__":
    m = Matcher()
    print("Stats:", m.stats())
    print("\n--- Test cliente por NIT ---")
    for hit in m.search_customer("32160242"):
        print(f"  [{hit.score:.0f}] {hit.identification} | {hit.name}")
    print("\n--- Test cliente por nombre 'martinez dora' ---")
    for hit in m.search_customer("martinez dora"):
        print(f"  [{hit.score:.0f}] {hit.identification} | {hit.name}")
    print("\n--- Test producto 'bolsa chicle' ---")
    for hit in m.search_product("bolsa chicle"):
        print(f"  [{hit.score:.0f}] {hit.code} | {hit.name} | precio={hit.price_default}")
    print("\n--- Test producto 'perlas mango' ---")
    for hit in m.search_product("perlas mango"):
        print(f"  [{hit.score:.0f}] {hit.code} | {hit.name} | precio={hit.price_default}")
    print("\n--- Test producto codigo 'P23' ---")
    for hit in m.search_product("P23"):
        print(f"  [{hit.score:.0f}] {hit.code} | {hit.name} | precio={hit.price_default}")
