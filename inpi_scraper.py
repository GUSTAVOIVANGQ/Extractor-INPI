#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extractor conservador para:
https://catalogo.inpi.gob.mx/cedulas/

Objetivo:
- Recorrer todas las páginas de la tabla.
- Abrir "Ver Detalle" de cada comunidad.
- Extraer:
  1) las columnas de la fila principal;
  2) la información superior de la ventana "Información";
  3) únicamente la pestaña "Datos Generales".
- Guardar además el HTML bruto de esas dos zonas para poder re-procesar sin
  volver a consultar el sitio.
- Reanudar después de interrupciones mediante SQLite.

No usa selectores rígidos de ids generados: localiza la tabla, botones y
pestañas principalmente por sus textos visibles.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import hashlib
import json
import random
import re
import sqlite3
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

DEFAULT_URL = "https://catalogo.inpi.gob.mx/cedulas/"
FOOTER_RE = re.compile(
    r"Página\s+([\d,.]+)\s+de\s+([\d,.]+)\s*-\s*Registros\s+([\d,.]+)",
    re.IGNORECASE,
)

DETAIL_TEXT = "Ver Detalle"
GENERAL_TAB_TEXT = "Datos Generales"

# El CSV incluye campos de fácil consulta y conserva los objetos completos
# como JSON en columnas adicionales.
CSV_BASE_COLUMNS = [
    "record_key",
    "numero_registro",
    "page_number",
    "row_number",
    "pueblo_indigena",
    "nombre_comunidad",
    "entidad_federativa",
    "municipio",
    "encabezado_texto",
    "datos_generales_texto",
    "encabezado_json",
    "datos_generales_json",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def norm_space(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def norm_key(s: str | None) -> str:
    """Normaliza una etiqueta a una clave estable ASCII-ish."""
    s = norm_space(s).lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or "campo"


def parse_int_text(s: str) -> int:
    # Acepta 1,673 / 16,730 / 1.673 (solo separadores de millares)
    digits = re.sub(r"\D", "", s)
    return int(digits) if digits else 0


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


@dataclass
class Totals:
    current_page: int
    total_pages: int
    total_records: int


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS records (
                record_key TEXT PRIMARY KEY,
                numero_registro TEXT,
                page_number INTEGER NOT NULL,
                row_number INTEGER NOT NULL,
                summary_json TEXT NOT NULL,
                header_json TEXT NOT NULL,
                general_json TEXT NOT NULL,
                header_text TEXT NOT NULL,
                general_text TEXT NOT NULL,
                header_html TEXT,
                general_html TEXT,
                scraped_at TEXT NOT NULL
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_number INTEGER,
                row_number INTEGER,
                summary_json TEXT,
                error_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL
            )
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM records").fetchone()[0])

    def has_key(self, key: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM records WHERE record_key=? LIMIT 1", (key,)
        ).fetchone() is not None

    def has_numero_registro(self, numero: str) -> bool:
        if not numero:
            return False
        return self.db.execute(
            "SELECT 1 FROM records WHERE numero_registro=? LIMIT 1", (numero,)
        ).fetchone() is not None

    def upsert(self, rec: dict[str, Any], save_html: bool) -> None:
        self.db.execute(
            """
            INSERT INTO records (
                record_key, numero_registro, page_number, row_number,
                summary_json, header_json, general_json,
                header_text, general_text, header_html, general_html, scraped_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(record_key) DO UPDATE SET
                numero_registro=excluded.numero_registro,
                page_number=excluded.page_number,
                row_number=excluded.row_number,
                summary_json=excluded.summary_json,
                header_json=excluded.header_json,
                general_json=excluded.general_json,
                header_text=excluded.header_text,
                general_text=excluded.general_text,
                header_html=excluded.header_html,
                general_html=excluded.general_html,
                scraped_at=excluded.scraped_at
            """,
            (
                rec["record_key"],
                rec.get("numero_registro", ""),
                rec["page_number"],
                rec["row_number"],
                json_dumps(rec["summary"]),
                json_dumps(rec["header"]),
                json_dumps(rec["general"]),
                rec["header"].get("text", ""),
                rec["general"].get("text", ""),
                rec["header"].get("html", "") if save_html else None,
                rec["general"].get("html", "") if save_html else None,
                rec["scraped_at"],
            ),
        )
        self.db.commit()

    def add_error(
        self,
        page_number: int,
        row_number: int,
        summary: dict[str, Any],
        error_text: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO errors(page_number,row_number,summary_json,error_text,created_at)
            VALUES(?,?,?,?,?)
            """,
            (page_number, row_number, json_dumps(summary), error_text, now_iso()),
        )
        self.db.commit()

    def set_meta(self, k: str, v: Any) -> None:
        self.db.execute(
            "INSERT INTO meta(k,v) VALUES(?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, json_dumps(v)),
        )
        self.db.commit()

    def get_meta(self, k: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except Exception:
            return default

    def iter_records(self) -> Iterable[dict[str, Any]]:
        cur = self.db.execute(
            """
            SELECT record_key, numero_registro, page_number, row_number,
                   summary_json, header_json, general_json,
                   header_text, general_text, scraped_at
            FROM records
            ORDER BY page_number, row_number
            """
        )
        for row in cur:
            yield {
                "record_key": row[0],
                "numero_registro": row[1] or "",
                "page_number": row[2],
                "row_number": row[3],
                "summary": json.loads(row[4]),
                "header": json.loads(row[5]),
                "general": json.loads(row[6]),
                "header_text": row[7],
                "general_text": row[8],
                "scraped_at": row[9],
            }


async def sleep_polite(min_s: float, max_s: float) -> None:
    if max_s <= 0:
        return
    lo = max(0.0, min(min_s, max_s))
    hi = max(lo, max_s)
    await asyncio.sleep(random.uniform(lo, hi))


async def wait_text_stable(locator: Locator, timeout_ms: int = 15000) -> None:
    """Espera a que el texto del modal deje de cambiar durante ~600 ms."""
    deadline = time.monotonic() + timeout_ms / 1000
    previous = None
    stable_hits = 0
    while time.monotonic() < deadline:
        try:
            current = norm_space(await locator.inner_text(timeout=1500))
        except Exception:
            current = ""
        if current and current == previous:
            stable_hits += 1
            if stable_hits >= 2:
                return
        else:
            stable_hits = 0
        previous = current
        await asyncio.sleep(0.3)
    raise PlaywrightTimeoutError("El contenido del modal no se estabilizó a tiempo.")


async def locate_results_table(page: Page) -> Locator:
    tables = page.locator("table")
    count = await tables.count()
    for i in range(count):
        t = tables.nth(i)
        try:
            txt = norm_space(await t.inner_text(timeout=1000))
        except Exception:
            continue
        low = txt.lower()
        if (
            DETAIL_TEXT.lower() in low
            and "nombre comunidad" in low
            and "entidad federativa" in low
            and "municipio" in low
        ):
            return t
    raise RuntimeError(
        "No pude localizar la tabla principal. "
        "El sitio pudo cambiar su estructura o no terminó de cargar."
    )


async def read_totals(page: Page) -> Totals | None:
    text = norm_space(await page.locator("body").inner_text())
    m = FOOTER_RE.search(text)
    if not m:
        return None
    return Totals(*(parse_int_text(x) for x in m.groups()))


async def table_headers(table: Locator) -> list[str]:
    heads = [norm_space(x) for x in await table.locator("thead th").all_inner_texts()]
    if heads:
        return heads
    # Fallback para tablas sin <thead>.
    first = table.locator("tr").first
    return [norm_space(x) for x in await first.locator("th,td").all_inner_texts()]


async def row_summary(row: Locator, headers: list[str]) -> dict[str, str]:
    cells = [norm_space(x) for x in await row.locator("td").all_inner_texts()]
    result: dict[str, str] = {}
    for i, value in enumerate(cells):
        header = headers[i] if i < len(headers) else f"columna_{i+1}"
        if norm_key(header) == "accion":
            continue
        result[header or f"columna_{i+1}"] = value
    return result


def summary_value(summary: dict[str, str], *wanted: str) -> str:
    by_key = {norm_key(k): v for k, v in summary.items()}
    for w in wanted:
        if norm_key(w) in by_key:
            return by_key[norm_key(w)]
    return ""


EXTRACT_JS = r"""
(modal, generalText) => {
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();

  const visible = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    return st.display !== "none" && st.visibility !== "hidden";
  };

  const tableToObject = (table) => {
    const rows = Array.from(table.querySelectorAll("tr"));
    const matrix = rows.map(tr =>
      Array.from(tr.querySelectorAll(":scope > th, :scope > td")).map(td => norm(td.innerText))
    ).filter(r => r.some(Boolean));

    let headers = [];
    const thead = table.querySelector("thead");
    if (thead) {
      const hrs = Array.from(thead.querySelectorAll("tr"));
      if (hrs.length) {
        headers = Array.from(hrs[hrs.length - 1].querySelectorAll("th,td"))
          .map(x => norm(x.innerText));
      }
    }
    if (!headers.length && matrix.length) {
      const firstHasTH = rows[0] && rows[0].querySelector("th");
      if (firstHasTH) headers = matrix[0];
    }

    const dataRows = headers.length && matrix.length &&
      matrix[0].join("\u241f") === headers.join("\u241f")
      ? matrix.slice(1)
      : matrix;

    // Título: texto corto inmediatamente anterior a la tabla.
    let title = "";
    let p = table.previousElementSibling;
    let hops = 0;
    while (p && hops < 4) {
      const t = norm(p.innerText);
      if (t && t.length <= 220) { title = t; break; }
      p = p.previousElementSibling;
      hops++;
    }

    return {title, headers, rows: dataRows};
  };

  const makePairs = (root) => {
    const out = {};
    const add = (k, v) => {
      k = norm(k); v = norm(v);
      if (!k || !v || k === v || k.length > 180) return;
      if (out[k] === undefined) out[k] = v;
      else if (Array.isArray(out[k])) {
        if (!out[k].includes(v)) out[k].push(v);
      } else if (out[k] !== v) out[k] = [out[k], v];
    };

    // <dt>/<dd>
    root.querySelectorAll("dt").forEach(dt => {
      let dd = dt.nextElementSibling;
      if (dd && dd.tagName === "DD") add(dt.innerText, dd.innerText);
    });

    // <label> valor en hermano/celda cercana.
    root.querySelectorAll("label").forEach(lab => {
      let v = lab.nextElementSibling;
      if (v) add(lab.innerText, v.innerText);
    });

    // Contenedores donde el primer hijo parece etiqueta en negrita.
    root.querySelectorAll("div, p, li, td").forEach(el => {
      const children = Array.from(el.children).filter(visible);
      if (!children.length || children.length > 6) return;
      const first = children[0];
      const tag = first.tagName;
      const cls = (first.className || "").toString().toLowerCase();
      const weight = parseInt(window.getComputedStyle(first).fontWeight || "400", 10);
      const labelish = ["B","STRONG","LABEL","DT"].includes(tag) ||
                       weight >= 600 ||
                       cls.includes("label") || cls.includes("title");
      if (!labelish) return;
      const k = norm(first.innerText);
      const whole = norm(el.innerText);
      if (!k || !whole || whole === k) return;
      let v = norm(whole.slice(whole.indexOf(k) + k.length));
      if (v) add(k, v);
    });
    return out;
  };

  const body = modal.querySelector(".modal-body") || modal;

  // Localiza la pestaña "Datos Generales" por texto, luego su panel target.
  const candidates = Array.from(
    modal.querySelectorAll('a,button,[role="tab"]')
  ).filter(el => norm(el.innerText).toLowerCase() === generalText.toLowerCase());

  const tab = candidates[0] || null;
  let general = null;
  if (tab) {
    let target = tab.getAttribute("href") || tab.getAttribute("data-bs-target") ||
                 tab.getAttribute("data-target") || tab.getAttribute("aria-controls");
    if (target) {
      if (!target.startsWith("#") && !target.includes(" ")) target = "#" + target;
      try { general = modal.querySelector(target); } catch (_) {}
    }
  }
  if (!general) {
    general = Array.from(modal.querySelectorAll(".tab-pane,[role=tabpanel]"))
      .find(el => visible(el) && norm(el.innerText)) || null;
  }

  // Encabezado = clon del body sin navegación de tabs, paneles y pie.
  const headerClone = body.cloneNode(true);
  headerClone.querySelectorAll(
    ".nav-tabs,.nav-pills,[role=tablist],.tab-content,.tab-pane,[role=tabpanel],.modal-footer"
  ).forEach(el => el.remove());

  const header = {
    text: norm(headerClone.innerText),
    html: headerClone.innerHTML,
    pairs: makePairs(headerClone),
    tables: Array.from(headerClone.querySelectorAll("table")).map(tableToObject)
  };

  const generalObj = general ? {
    text: norm(general.innerText),
    html: general.innerHTML,
    pairs: makePairs(general),
    tables: Array.from(general.querySelectorAll("table")).map(tableToObject)
  } : {
    text: "",
    html: "",
    pairs: {},
    tables: []
  };

  return {header, general: generalObj};
}
"""


async def find_visible_modal(page: Page) -> Locator:
    selectors = [
        ".modal.show:visible",
        '[role="dialog"]:visible',
        ".modal:visible",
    ]
    for selector in selectors:
        loc = page.locator(selector)
        if await loc.count():
            return loc.last
    raise RuntimeError("No apareció una ventana modal visible de Información.")


async def click_general_tab(modal: Locator) -> None:
    # Prefiere role=tab, luego cualquier enlace/botón con texto exacto.
    candidates = [
        modal.get_by_role("tab", name=GENERAL_TAB_TEXT, exact=True),
        modal.get_by_role("link", name=GENERAL_TAB_TEXT, exact=True),
        modal.get_by_role("button", name=GENERAL_TAB_TEXT, exact=True),
        modal.get_by_text(GENERAL_TAB_TEXT, exact=True),
    ]
    for c in candidates:
        if await c.count():
            try:
                await c.first.click(timeout=3000)
                await asyncio.sleep(0.15)
                return
            except Exception:
                pass
    raise RuntimeError(f"No encontré la pestaña {GENERAL_TAB_TEXT!r}.")


async def close_modal(page: Page, modal: Locator) -> None:
    candidates = [
        modal.locator("button.btn-close"),
        modal.locator('button[aria-label="Close"]'),
        modal.locator('button[aria-label="Cerrar"]'),
        modal.locator("button.close"),
    ]
    for c in candidates:
        if await c.count():
            try:
                await c.first.click(timeout=2500)
                await modal.wait_for(state="hidden", timeout=5000)
                return
            except Exception:
                pass
    await page.keyboard.press("Escape")
    try:
        await modal.wait_for(state="hidden", timeout=5000)
    except Exception:
        # Último recurso: click fuera del modal si hay backdrop.
        backdrop = page.locator(".modal-backdrop:visible")
        if await backdrop.count():
            try:
                await backdrop.last.click(position={"x": 2, "y": 2}, timeout=1500)
            except Exception:
                pass


async def extract_one(
    page: Page,
    row: Locator,
    summary: dict[str, str],
    page_number: int,
    row_number: int,
    timeout_ms: int,
) -> dict[str, Any]:
    button = row.get_by_text(DETAIL_TEXT, exact=True)
    if not await button.count():
        # Fallback a botón/enlace que contenga el texto.
        button = row.locator("button,a").filter(has_text=DETAIL_TEXT)
    if not await button.count():
        raise RuntimeError("La fila no contiene el botón 'Ver Detalle'.")

    await button.first.scroll_into_view_if_needed()
    await button.first.click(timeout=timeout_ms)

    modal = await find_visible_modal(page)
    await modal.wait_for(state="visible", timeout=timeout_ms)

    # Confirma que sea el modal esperado y espera la carga asíncrona.
    await wait_text_stable(modal, timeout_ms=timeout_ms)

    await click_general_tab(modal)
    await wait_text_stable(modal, timeout_ms=timeout_ms)

    extracted = await modal.evaluate(EXTRACT_JS, GENERAL_TAB_TEXT)
    header = extracted["header"]
    general = extracted["general"]

    if not general.get("text"):
        raise RuntimeError("La pestaña 'Datos Generales' quedó vacía.")

    # Número registro: primero desde pares; después regex sobre encabezado.
    numero = ""
    for k, v in (header.get("pairs") or {}).items():
        if norm_key(k) in {"numero_registro", "numero_de_registro"}:
            if isinstance(v, list):
                numero = norm_space(str(v[0]))
            else:
                numero = norm_space(str(v))
            break
    if not numero:
        m = re.search(
            r"N[uú]mero\s+(?:de\s+)?registro\s*[:\-]?\s*([A-Za-z0-9._/-]+)",
            header.get("text", ""),
            re.IGNORECASE,
        )
        if m:
            numero = m.group(1).strip()

    fingerprint = "\u241f".join(
        [
            numero,
            summary_value(summary, "Pueblo Indígena"),
            summary_value(summary, "Nombre Comunidad"),
            summary_value(summary, "Entidad Federativa"),
            summary_value(summary, "Municipio"),
            header.get("text", ""),
        ]
    )
    key = f"registro:{numero}" if numero else f"sha256:{sha256_text(fingerprint)}"

    return {
        "record_key": key,
        "numero_registro": numero,
        "page_number": page_number,
        "row_number": row_number,
        "summary": summary,
        "header": header,
        "general": general,
        "scraped_at": now_iso(),
        "_modal": modal,
    }


async def page_number_from_footer(page: Page) -> int | None:
    totals = await read_totals(page)
    return totals.current_page if totals else None


async def go_next(page: Page, expected_current: int, timeout_ms: int) -> bool:
    candidates = [
        page.get_by_role("link", name="Siguiente", exact=True),
        page.get_by_role("button", name="Siguiente", exact=True),
        page.get_by_text("Siguiente", exact=True),
    ]
    nxt = None
    for c in candidates:
        if await c.count():
            nxt = c.last
            break
    if nxt is None:
        return False

    # Detecta disabled por atributo/clase.
    try:
        disabled = await nxt.get_attribute("disabled")
        aria_disabled = await nxt.get_attribute("aria-disabled")
        cls = (await nxt.get_attribute("class")) or ""
        if disabled is not None or aria_disabled == "true" or "disabled" in cls.lower():
            return False
    except Exception:
        pass

    await nxt.scroll_into_view_if_needed()
    await nxt.click(timeout=timeout_ms)

    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        n = await page_number_from_footer(page)
        if n is not None and n != expected_current:
            return True
        await asyncio.sleep(0.2)

    # Si el pie no se puede leer, al menos espera que exista la tabla.
    await locate_results_table(page)
    return True


async def navigate_to_page(
    page: Page,
    target_page: int,
    delay_min: float,
    delay_max: float,
    timeout_ms: int,
) -> None:
    if target_page <= 1:
        return
    current = await page_number_from_footer(page) or 1
    while current < target_page:
        ok = await go_next(page, current, timeout_ms)
        if not ok:
            raise RuntimeError(
                f"No pude avanzar desde página {current} hasta {target_page}."
            )
        await sleep_polite(delay_min, delay_max)
        current = await page_number_from_footer(page) or (current + 1)
        print(f"\rAvanzando al punto de reanudación: página {current}/{target_page}", end="", flush=True)
    print()


async def launch_browser(playwright, headless: bool, channel: str | None) -> Browser:
    kwargs: dict[str, Any] = {"headless": headless}
    if channel:
        kwargs["channel"] = channel
    try:
        return await playwright.chromium.launch(**kwargs)
    except PlaywrightError:
        if channel:
            raise
        # Fallback útil en Windows cuando está Chrome instalado pero no se
        # ejecutó todavía `playwright install chromium`.
        return await playwright.chromium.launch(headless=headless, channel="chrome")


async def export_outputs(store: Store, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_gz = out_dir / "registros_inpi.jsonl.gz"
    csv_path = out_dir / "registros_inpi.csv"

    with gzip.open(jsonl_gz, "wt", encoding="utf-8") as gz, csv_path.open(
        "w", newline="", encoding="utf-8-sig"
    ) as cf:
        writer = csv.DictWriter(cf, fieldnames=CSV_BASE_COLUMNS, extrasaction="ignore")
        writer.writeheader()

        for rec in store.iter_records():
            full = {
                "record_key": rec["record_key"],
                "numero_registro": rec["numero_registro"],
                "page_number": rec["page_number"],
                "row_number": rec["row_number"],
                "summary": rec["summary"],
                "informacion": rec["header"],
                "datos_generales": rec["general"],
                "scraped_at": rec["scraped_at"],
            }
            gz.write(json.dumps(full, ensure_ascii=False) + "\n")

            writer.writerow(
                {
                    "record_key": rec["record_key"],
                    "numero_registro": rec["numero_registro"],
                    "page_number": rec["page_number"],
                    "row_number": rec["row_number"],
                    "pueblo_indigena": summary_value(rec["summary"], "Pueblo Indígena"),
                    "nombre_comunidad": summary_value(rec["summary"], "Nombre Comunidad"),
                    "entidad_federativa": summary_value(rec["summary"], "Entidad Federativa"),
                    "municipio": summary_value(rec["summary"], "Municipio"),
                    "encabezado_texto": rec["header_text"],
                    "datos_generales_texto": rec["general_text"],
                    "encabezado_json": json_dumps(rec["header"]),
                    "datos_generales_json": json_dumps(rec["general"]),
                }
            )

    print(f"Exportado: {jsonl_gz}")
    print(f"Exportado: {csv_path}")


async def scrape(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    errors_dir = out_dir / "errors"
    errors_dir.mkdir(exist_ok=True)
    db_path = out_dir / "inpi.sqlite3"
    network_path = out_dir / "network.jsonl"

    store = Store(db_path)

    async with async_playwright() as p:
        browser = await launch_browser(p, args.headless, args.channel)
        context = await browser.new_context(
            locale="es-MX",
            viewport={"width": 1440, "height": 1000},
            ignore_https_errors=False,
        )
        page = await context.new_page()
        page.set_default_timeout(args.timeout_ms)

        if args.capture_network:
            network_fh = network_path.open("a", encoding="utf-8")

            async def on_response(resp):
                try:
                    req = resp.request
                    if req.resource_type in {"xhr", "fetch"}:
                        payload = {
                            "at": now_iso(),
                            "status": resp.status,
                            "method": req.method,
                            "resource_type": req.resource_type,
                            "url": resp.url,
                            "post_data": req.post_data,
                        }
                        network_fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
                        network_fh.flush()
                except Exception:
                    pass

            page.on("response", on_response)
        else:
            network_fh = None

        print(f"Abriendo {args.url}")
        await page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout_ms)
        await locate_results_table(page)

        totals = await read_totals(page)
        if totals:
            print(
                f"Sitio reporta: página {totals.current_page} de {totals.total_pages}; "
                f"{totals.total_records} registros."
            )
            store.set_meta(
                "site_totals",
                {
                    "observed_at": now_iso(),
                    "current_page": totals.current_page,
                    "total_pages": totals.total_pages,
                    "total_records": totals.total_records,
                },
            )
        else:
            print(
                "AVISO: no pude leer el total de páginas/registros del pie. "
                "Continuaré hasta que no haya 'Siguiente'."
            )

        if args.start_page is not None:
            start_page = max(1, args.start_page)
        else:
            last_completed = store.get_meta("last_completed_page", 0) or 0
            if last_completed:
                start_page = int(last_completed) + 1
                if totals and start_page > totals.total_pages:
                    # Si el recorrido anterior llegó al final pero faltan registros
                    # (por errores previos), reiniciamos desde 1 y deduplicamos.
                    if store.count() < totals.total_records:
                        print(
                            "La corrida anterior llegó a la última página, pero el "
                            "conteo no coincide. Reiniciaré desde página 1 para "
                            "reintentar posibles faltantes."
                        )
                        start_page = 1
                    else:
                        start_page = totals.total_pages
                print(f"Reanudación automática desde página {start_page}.")
            else:
                start_page = 1

        if start_page > 1:
            await navigate_to_page(
                page, start_page, args.delay_min, args.delay_max, args.timeout_ms
            )

        pages_done = 0
        current_page = await page_number_from_footer(page) or start_page

        while True:
            if args.max_pages is not None and pages_done >= args.max_pages:
                print("Límite --max-pages alcanzado.")
                break

            table = await locate_results_table(page)
            headers = await table_headers(table)
            body_rows = table.locator("tbody tr")
            row_count = await body_rows.count()
            if row_count == 0:
                # Fallback para tabla sin tbody explícito.
                all_rows = table.locator("tr")
                n_all = await all_rows.count()
                body_rows = all_rows.nth(1) if n_all == 2 else table.locator("tr").filter(has=table.locator("td"))
                row_count = await body_rows.count()

            print(
                f"\nPágina {current_page}"
                + (f"/{totals.total_pages}" if totals else "")
                + f" — {row_count} filas — guardados {store.count()}"
            )

            # IMPORTANTE: tras cerrar el modal la tabla puede re-renderizarse.
            # Re-localizamos la tabla y la fila en cada iteración.
            for idx in range(row_count):
                row_no = idx + 1
                last_error: Exception | None = None

                for attempt in range(1, args.retries + 1):
                    modal: Locator | None = None
                    summary: dict[str, str] = {}
                    try:
                        table = await locate_results_table(page)
                        rows = table.locator("tbody tr")
                        if await rows.count() == 0:
                            rows = table.locator("tr").filter(has=table.locator("td"))
                        row = rows.nth(idx)
                        summary = await row_summary(row, headers)

                        # Un fingerprint preliminar permite evitar reabrir filas
                        # ya guardadas cuando se reanuda desde una página anterior.
                        preliminary = sha256_text(
                            "\u241f".join(
                                [
                                    summary_value(summary, "Pueblo Indígena"),
                                    summary_value(summary, "Nombre Comunidad"),
                                    summary_value(summary, "Entidad Federativa"),
                                    summary_value(summary, "Municipio"),
                                ]
                            )
                        )

                        # No saltamos por hash preliminar: pueden existir homónimos.
                        rec = await extract_one(
                            page,
                            row,
                            summary,
                            current_page,
                            row_no,
                            args.timeout_ms,
                        )
                        modal = rec.pop("_modal", None)

                        # Deduplicación exacta por número de registro cuando existe.
                        if rec["numero_registro"] and store.has_numero_registro(
                            rec["numero_registro"]
                        ):
                            print(
                                f"  {row_no:02d}/{row_count}: ya existe "
                                f"{rec['numero_registro']} — omitido"
                            )
                        elif store.has_key(rec["record_key"]):
                            print(
                                f"  {row_no:02d}/{row_count}: ya existe "
                                f"{rec['record_key']} — omitido"
                            )
                        else:
                            store.upsert(rec, save_html=args.save_html)
                            name = summary_value(summary, "Nombre Comunidad")
                            print(
                                f"  {row_no:02d}/{row_count}: OK "
                                f"{rec['numero_registro'] or '(sin número)'} — {name}"
                            )

                        if modal is not None:
                            await close_modal(page, modal)
                        await sleep_polite(args.delay_min, args.delay_max)
                        last_error = None
                        break

                    except Exception as exc:
                        last_error = exc
                        try:
                            if modal is None:
                                modal = await find_visible_modal(page)
                            await close_modal(page, modal)
                        except Exception:
                            pass

                        print(
                            f"  {row_no:02d}/{row_count}: intento {attempt}/"
                            f"{args.retries} falló: {type(exc).__name__}: {exc}"
                        )
                        await asyncio.sleep(min(2 ** attempt, 12))

                if last_error is not None:
                    store.add_error(
                        current_page,
                        row_no,
                        summary,
                        f"{type(last_error).__name__}: {last_error}",
                    )
                    try:
                        await page.screenshot(
                            path=str(errors_dir / f"p{current_page:04d}_r{row_no:02d}.png"),
                            full_page=True,
                        )
                    except Exception:
                        pass
                    if not args.continue_on_error:
                        raise last_error

            pages_done += 1
            store.set_meta("last_completed_page", current_page)

            if totals and current_page >= totals.total_pages:
                break

            ok = await go_next(page, current_page, args.timeout_ms)
            if not ok:
                break
            await sleep_polite(args.page_delay_min, args.page_delay_max)
            current_page = await page_number_from_footer(page) or (current_page + 1)

        if network_fh:
            network_fh.close()
        await context.close()
        await browser.close()

    await export_outputs(store, out_dir)

    count = store.count()
    expected = totals.total_records if totals else None
    print(f"\nRegistros únicos guardados: {count}")
    if expected is not None:
        if count == expected:
            print("VALIDACIÓN: la cantidad coincide exactamente con el total reportado por el sitio.")
        else:
            print(
                f"VALIDACIÓN: el sitio reportó {expected}; la base contiene {count}. "
                "Revisa errors/ y vuelve a ejecutar: SQLite permite reanudar."
            )
    store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Extrae la tabla y Datos Generales de las cédulas del INPI."
    )
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--out", default="salida_inpi", help="Directorio de salida.")
    ap.add_argument(
        "--start-page",
        type=int,
        default=None,
        help="Página inicial manual. Si se omite, reanuda automáticamente desde SQLite.",
    )
    ap.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Para pruebas. Ej.: --max-pages 1",
    )
    ap.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Ejecutar navegador sin ventana. Recomiendo probar primero con --no-headless.",
    )
    ap.add_argument(
        "--channel",
        default=None,
        help='Canal Playwright, por ejemplo "chrome". Si se omite usa Chromium y luego Chrome como fallback.',
    )
    ap.add_argument("--timeout-ms", type=int, default=30000)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument(
        "--continue-on-error",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Continúa tras agotar reintentos de una fila.",
    )
    ap.add_argument(
        "--save-html",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Conservar HTML bruto del encabezado y Datos Generales en SQLite.",
    )
    ap.add_argument(
        "--capture-network",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Registrar XHR/fetch en network.jsonl para descubrir endpoints oficiales.",
    )
    ap.add_argument("--delay-min", type=float, default=0.45)
    ap.add_argument("--delay-max", type=float, default=0.95)
    ap.add_argument("--page-delay-min", type=float, default=0.8)
    ap.add_argument("--page-delay-max", type=float, default=1.5)
    return ap


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(scrape(args))
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario. Los registros ya guardados permanecen en SQLite.")
        return 130
    except Exception as exc:
        print(f"\nERROR FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
