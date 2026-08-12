#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extractor INPI v2 por API directa — concurrente, validado y reanudable.

Extrae todos los registros de:
  https://cedulas.inpi.gob.mx/consulta/

usando los endpoints REST que la propia página consume internamente:
  - GET /Consulta/GetCedulas  →  JSON paginado con lista de registros
  - GET /Consulta/Details/{id} →  HTML del modal de detalle

Ventajas sobre el prototipo basado en Playwright:
  - HTTP directo con concurrencia real y tasa global limitada
  - Sin dependencia de Chromium (~2 MB vs. ~500 MB)
  - Mucho más estable (no hay modales, pestañas ni selectores CSS frágiles)
  - Checkpoint SQLite por registro (reanudación granular)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import json
import os
import random
import re
import sqlite3
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Iterable

# Forzar UTF-8 en la consola de Windows
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import httpx
from bs4 import BeautifulSoup, Tag

# Parser HTML: lxml es más rápido (C) pero requiere instalación aparte;
# si no está disponible o está instalado de forma incompleta (puede
# importarse `lxml` pero fallar `lxml.etree`, que es lo que bs4 usa
# realmente), usamos html.parser (incluido en la librería estándar de
# Python) para no depender de un paquete externo. Se prueba con una
# construcción real de BeautifulSoup, no solo `import lxml`, porque eso
# no detecta instalaciones parciales/rotas del binding C.
try:
    BeautifulSoup("<html></html>", "lxml")
    HTML_PARSER = "lxml"
except Exception as _lxml_exc:
    HTML_PARSER = "html.parser"
    _LXML_ERROR = f"{type(_lxml_exc).__name__}: {_lxml_exc}"
else:
    _LXML_ERROR = None

# ─── Configuración por defecto ──────────────────────────────────────────────

BASE_URL = "https://cedulas.inpi.gob.mx"
LIST_ENDPOINT = "/Consulta/GetCedulas"
DETAIL_ENDPOINT = "/Consulta/Details/{id}"
PAGE_SIZE = 10  # Tamaño de página que usa el sitio

# Encabezados para parecer un navegador normal
DEFAULT_HEADERS = {
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
    "Referer": "https://cedulas.inpi.gob.mx/consulta/",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}

# Columnas del CSV de salida
CSV_COLUMNS = [
    "id_registro",
    "numero_registro",
    "cedula",
    "pueblo_indigena",
    "nombre_comunidad",
    "entidad_federativa",
    "cve_estado",
    "municipio",
    "cve_municipio",
    "id_ccdi",
    "region",
    # Encabezado del detalle
    "pueblo",
    "region_nombre",
    "localidad",
    "unidad_administrativa",
    # Datos Generales
    "nombre_lengua_indigena",
    "significado_nombres",
    "pueblos_conforman",
    "autodenominacion_pueblo",
    "poblacion_total_estimada",
    "asociacion_regional",
    "latitud_sede",
    "longitud_sede",
    "altitud_sede",
    # JSON completos
    "encabezado_json",
    "datos_generales_json",
]


# ─── Utilidades ─────────────────────────────────────────────────────────────


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def norm_space(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def clean_html_text(s: str | None) -> str:
    """Normaliza texto extraído de HTML: decodifica entidades y colapsa espacios."""
    if not s:
        return ""
    s = unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def norm_key(s: str | None) -> str:
    """Normaliza una etiqueta a una clave estable ASCII."""
    s = norm_space(s).lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or "campo"


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class AsyncRateLimiter:
    """Limita el inicio GLOBAL de solicitudes HTTP sin eliminar concurrencia."""

    def __init__(self, requests_per_second: float):
        self.interval = 0.0 if requests_per_second <= 0 else 1.0 / requests_per_second
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def wait(self) -> None:
        if self.interval <= 0:
            return
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            if self._next_at > now:
                await asyncio.sleep(self._next_at - now)
                now = loop.time()
            self._next_at = max(now, self._next_at) + self.interval


# ─── Modelo de datos ────────────────────────────────────────────────────────


@dataclass
class ListItem:
    """Un registro de la lista paginada."""
    id_registro: int
    pueblo_indigena: str
    cedula: str
    nombre_comunidad: str
    cve_estado: str
    entidad_federativa: str
    cve_municipio: str
    municipio: str
    id_ccdi: int
    region: int


@dataclass
class DetailHeader:
    """Datos del encabezado del modal de detalle."""
    nombre_comunidad: str = ""
    pueblo: str = ""
    region: str = ""
    numero_registro: str = ""
    entidad_federativa: str = ""
    municipio: str = ""
    localidad: str = ""
    unidad_administrativa: str = ""
    raw: dict[str, str] = field(default_factory=dict)


@dataclass
class DatosGenerales:
    """Datos extraídos de la pestaña Datos Generales."""
    nombre_lengua_indigena: str = ""
    significado_nombres: str = ""
    pueblos_conforman: str = ""
    autodenominacion_pueblo: str = ""
    poblacion_total_estimada: str = ""
    asociacion_regional: str = ""
    latitud_sede: str = ""
    longitud_sede: str = ""
    altitud_sede: str = ""
    tipo_comunidad: list[dict[str, str]] = field(default_factory=list)
    localidad_sede: list[dict[str, str]] = field(default_factory=list)
    asentamientos_cantidad: list[dict[str, str]] = field(default_factory=list)
    listado_asentamientos: list[dict[str, str]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


# ─── Store SQLite ────────────────────────────────────────────────────────────


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._create_tables()

    def _create_tables(self) -> None:
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS registros (
                id_registro INTEGER PRIMARY KEY,
                pueblo_indigena TEXT NOT NULL,
                cedula TEXT NOT NULL,
                nombre_comunidad TEXT NOT NULL,
                cve_estado TEXT NOT NULL,
                entidad_federativa TEXT NOT NULL,
                cve_municipio TEXT NOT NULL,
                municipio TEXT NOT NULL,
                id_ccdi INTEGER NOT NULL,
                region INTEGER NOT NULL,
                indexed_at TEXT NOT NULL
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS detalles (
                id_registro INTEGER PRIMARY KEY,
                encabezado_json TEXT NOT NULL,
                datos_generales_json TEXT NOT NULL,
                detail_html TEXT,
                scraped_at TEXT NOT NULL,
                FOREIGN KEY (id_registro) REFERENCES registros(id_registro)
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS errores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                id_registro INTEGER,
                fase TEXT NOT NULL,
                error_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS current_index (
                id_registro INTEGER PRIMARY KEY
            )
        """)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ── Meta ──

    def set_meta(self, k: str, v: Any) -> None:
        self.db.execute(
            "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
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

    # ── Registros (índice) ──

    def upsert_registro(self, item: ListItem) -> None:
        self.db.execute(
            """
            INSERT INTO registros (
                id_registro, pueblo_indigena, cedula, nombre_comunidad,
                cve_estado, entidad_federativa, cve_municipio, municipio,
                id_ccdi, region, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id_registro) DO UPDATE SET
                pueblo_indigena=excluded.pueblo_indigena,
                cedula=excluded.cedula,
                nombre_comunidad=excluded.nombre_comunidad,
                cve_estado=excluded.cve_estado,
                entidad_federativa=excluded.entidad_federativa,
                cve_municipio=excluded.cve_municipio,
                municipio=excluded.municipio,
                id_ccdi=excluded.id_ccdi,
                region=excluded.region,
                indexed_at=excluded.indexed_at
            """,
            (
                item.id_registro,
                item.pueblo_indigena,
                item.cedula,
                item.nombre_comunidad,
                item.cve_estado,
                item.entidad_federativa,
                item.cve_municipio,
                item.municipio,
                item.id_ccdi,
                item.region,
                now_iso(),
            ),
        )

    def commit_registros(self) -> None:
        self.db.commit()

    def count_registros(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM registros").fetchone()[0]

    def begin_current_index(self) -> None:
        """Inicia un inventario nuevo de los IDs visibles en esta ejecución."""
        self.db.execute("DELETE FROM current_index")
        self.db.commit()

    def mark_current(self, id_registro: int) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO current_index(id_registro) VALUES(?)",
            (id_registro,),
        )

    def count_current(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM current_index").fetchone()[0]

    def ids_sin_detalle(self) -> list[int]:
        """IDs del inventario ACTUAL que aún no tienen detalle extraído."""
        cur = self.db.execute(
            """
            SELECT r.id_registro
            FROM current_index c
            JOIN registros r ON r.id_registro = c.id_registro
            LEFT JOIN detalles d ON r.id_registro = d.id_registro
            WHERE d.id_registro IS NULL
            ORDER BY r.id_registro
            """
        )
        return [row[0] for row in cur]

    def count_detalles(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM detalles").fetchone()[0]

    def count_detalles_current(self) -> int:
        return self.db.execute(
            """
            SELECT COUNT(*) FROM detalles d
            JOIN current_index c ON c.id_registro = d.id_registro
            """
        ).fetchone()[0]

    # ── Detalles ──

    def upsert_detalle(
        self,
        id_registro: int,
        header: DetailHeader,
        general: DatosGenerales,
        html: str | None,
    ) -> None:
        header_dict = {
            "nombre_comunidad": header.nombre_comunidad,
            "pueblo": header.pueblo,
            "region": header.region,
            "numero_registro": header.numero_registro,
            "entidad_federativa": header.entidad_federativa,
            "municipio": header.municipio,
            "localidad": header.localidad,
            "unidad_administrativa": header.unidad_administrativa,
            **header.raw,
        }
        general_dict = {
            "nombre_lengua_indigena": general.nombre_lengua_indigena,
            "significado_nombres": general.significado_nombres,
            "pueblos_conforman": general.pueblos_conforman,
            "autodenominacion_pueblo": general.autodenominacion_pueblo,
            "poblacion_total_estimada": general.poblacion_total_estimada,
            "asociacion_regional": general.asociacion_regional,
            "latitud_sede": general.latitud_sede,
            "longitud_sede": general.longitud_sede,
            "altitud_sede": general.altitud_sede,
            "tipo_comunidad": general.tipo_comunidad,
            "localidad_sede": general.localidad_sede,
            "asentamientos_cantidad": general.asentamientos_cantidad,
            "listado_asentamientos": general.listado_asentamientos,
            **general.raw,
        }
        self.db.execute(
            """
            INSERT INTO detalles (id_registro, encabezado_json, datos_generales_json, detail_html, scraped_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id_registro) DO UPDATE SET
                encabezado_json=excluded.encabezado_json,
                datos_generales_json=excluded.datos_generales_json,
                detail_html=excluded.detail_html,
                scraped_at=excluded.scraped_at
            """,
            (
                id_registro,
                json_dumps(header_dict),
                json_dumps(general_dict),
                html,
                now_iso(),
            ),
        )
        self.db.commit()

    # ── Errores ──

    def add_error(self, id_registro: int | None, fase: str, error_text: str) -> None:
        self.db.execute(
            "INSERT INTO errores(id_registro, fase, error_text, created_at) VALUES(?,?,?,?)",
            (id_registro, fase, error_text, now_iso()),
        )
        self.db.commit()

    # ── Iteración para exportar ──

    def iter_all(self) -> Iterable[dict[str, Any]]:
        cur = self.db.execute(
            """
            SELECT r.id_registro, r.pueblo_indigena, r.cedula, r.nombre_comunidad,
                   r.cve_estado, r.entidad_federativa, r.cve_municipio, r.municipio,
                   r.id_ccdi, r.region,
                   d.encabezado_json, d.datos_generales_json
            FROM current_index c
            JOIN registros r ON r.id_registro = c.id_registro
            LEFT JOIN detalles d ON r.id_registro = d.id_registro
            ORDER BY r.id_registro
            """
        )
        for row in cur:
            header = json.loads(row[10]) if row[10] else {}
            general = json.loads(row[11]) if row[11] else {}
            yield {
                "id_registro": row[0],
                "pueblo_indigena": row[1],
                "cedula": row[2],
                "nombre_comunidad": row[3],
                "cve_estado": row[4],
                "entidad_federativa": row[5],
                "cve_municipio": row[6],
                "municipio": row[7],
                "id_ccdi": row[8],
                "region": row[9],
                "header": header,
                "general": general,
            }


# ─── HTML Parser para el modal de detalle ────────────────────────────────────


def _extract_h6_value_pairs(container: Tag) -> dict[str, str]:
    """
    Extrae pares h6 → p del HTML del detalle del INPI.

    La estructura típica es:
      <div class="row">
        <div class="col-..."><h6>Etiqueta</h6></div>
        <div class="col-..."><p>Valor</p></div>
      </div>
    """
    pairs: dict[str, str] = {}
    for h6 in container.find_all("h6"):
        text = clean_html_text(h6.get_text())
        if not text:
            continue
        # Busca el <p> hermano o en la misma fila
        parent_row = h6.find_parent(class_=re.compile(r"row"))
        if parent_row:
            p_tags = parent_row.find_all("p")
            for p in p_tags:
                val = clean_html_text(p.get_text())
                if val:
                    pairs[text] = val
                    break
    return pairs


def _parse_html_table(table: Tag) -> list[dict[str, str]]:
    """Convierte una tabla HTML a lista de dicts con encabezados como claves."""
    headers: list[str] = []
    thead = table.find("thead")
    if thead:
        # Toma la última fila del thead (por si hay encabezados multi-línea)
        header_rows = thead.find_all("tr")
        if header_rows:
            last_header = header_rows[-1]
            headers = [clean_html_text(th.get_text()) for th in last_header.find_all(["th", "td"])]

    rows: list[dict[str, str]] = []
    tbody = table.find("tbody")
    data_rows = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]
    for tr in data_rows:
        cells = [clean_html_text(td.get_text()) for td in tr.find_all(["td", "th"])]
        if not any(cells):
            continue
        row_dict: dict[str, str] = {}
        for i, cell in enumerate(cells):
            key = headers[i] if i < len(headers) else f"columna_{i + 1}"
            row_dict[key] = cell
        rows.append(row_dict)
    return rows


def parse_detail_header(soup: BeautifulSoup) -> DetailHeader:
    """Parsea el encabezado del modal (datos arriba de los tabs)."""
    header = DetailHeader()

    # El encabezado está en el div.fixed-div antes de las pestañas
    fixed = soup.find("div", class_="fixed-div")
    if not fixed:
        # Fallback: buscar antes del card-header
        card_header = soup.find("div", class_="card-header")
        if card_header:
            fixed = card_header
        else:
            return header

    # Extraer pares h6 → valor (p)
    all_h6 = fixed.find_all("h6") if isinstance(fixed, Tag) else []
    all_p = fixed.find_all("p") if isinstance(fixed, Tag) else []

    # Mapear por texto normalizado
    h6_texts = [clean_html_text(h.get_text()) for h in all_h6]
    p_texts = [clean_html_text(p.get_text()) for p in all_p]

    # La estructura tiene h6 como etiquetas y p como valores,
    # emparejados por posición dentro de sus contenedores row.
    # NOTA: A veces el h6 está en un row y el p en el siguiente row
    # (ej: "Nombre de la comunidad" seguido de un row con el valor).
    pairs: dict[str, str] = {}
    if isinstance(fixed, Tag):
        rows = fixed.find_all("div", class_="row", recursive=False)
        pending_labels: list[str] = []
        for row in rows:
            labels = [clean_html_text(h.get_text()) for h in row.find_all("h6")]
            values = [clean_html_text(p.get_text()) for p in row.find_all("p")]

            if labels and values:
                # Caso normal: h6 y p en el mismo row
                for i, label in enumerate(labels):
                    if i < len(values) and values[i]:
                        pairs[label] = values[i]
                pending_labels = []  # reset
            elif labels and not values:
                # Solo labels, sin valores: guardar para emparejar con el próximo row
                pending_labels = labels
            elif not labels and values and pending_labels:
                # Solo valores, sin labels: emparejar con labels pendientes
                for i, label in enumerate(pending_labels):
                    if i < len(values) and values[i]:
                        pairs[label] = values[i]
                pending_labels = []

    # Fallback si la maqueta cambia y los .row están anidados.
    if not pairs and isinstance(fixed, Tag):
        pairs = _extract_h6_value_pairs(fixed)

    # Mapear campos conocidos
    for label, value in pairs.items():
        key = norm_key(label)
        if "nombre" in key and "comunidad" in key:
            header.nombre_comunidad = value
        elif key == "pueblo":
            header.pueblo = value
        elif key == "region":
            header.region = value
        elif "numero" in key and "registro" in key:
            header.numero_registro = value
        elif "entidad" in key and "federativa" in key:
            header.entidad_federativa = value
        elif key == "municipio":
            header.municipio = value
        elif key == "localidad":
            header.localidad = value
        elif "unidad" in key and "administrativa" in key:
            header.unidad_administrativa = value
        else:
            header.raw[key] = value

    return header


def parse_datos_generales(soup: BeautifulSoup) -> DatosGenerales:
    """Parsea la pestaña Datos Generales del modal de detalle."""
    dg = DatosGenerales()

    # Buscar el tab-pane de Datos Generales (id="tabDescripcion")
    tab = soup.find("div", id="tabDescripcion")
    if not tab:
        # Fallback: buscar el primer tab-pane activo
        tab = soup.find("div", class_=re.compile(r"tab-pane.*active"))
    if not tab or not isinstance(tab, Tag):
        return dg

    # Extraer pares h6 → p
    rows = tab.find_all("div", class_="row", recursive=False)

    for row in rows:
        if not isinstance(row, Tag):
            continue
        labels = [clean_html_text(h.get_text()) for h in row.find_all("h6")]
        values = [clean_html_text(p.get_text()) for p in row.find_all("p")]

        for i, label in enumerate(labels):
            key = norm_key(label)
            value = values[i] if i < len(values) else ""

            if "nombre" in key and "lengua" in key and "indigena" in key:
                dg.nombre_lengua_indigena = value
            elif "significado" in key and "nombre" in key:
                dg.significado_nombres = value
            elif "pueblos" in key and "conforman" in key:
                dg.pueblos_conforman = value
            elif "autodenominacion" in key:
                dg.autodenominacion_pueblo = value
            elif "estimacion" in key and "poblacion" in key:
                # El valor está incrustado en el propio h6
                m = re.search(r":\s*(.+)", label)
                if m:
                    dg.poblacion_total_estimada = clean_html_text(m.group(1))
            elif "asociacion" in key or "congregacion" in key or ("pertenece" in key and "asociacion" in key):
                # El valor suele estar como <span> dentro del h6
                span = row.find("span")
                if span:
                    dg.asociacion_regional = clean_html_text(span.get_text())
                elif value:
                    dg.asociacion_regional = value
            else:
                if value:
                    dg.raw[key] = value

        # Buscar la estimación de población directamente en h6
        for h6 in row.find_all("h6"):
            h6_text = clean_html_text(h6.get_text())
            if "estimación" in h6_text.lower() or "estimacion" in norm_key(h6_text):
                m = re.search(r":\s*([0-9,.]+)", h6_text)
                if m:
                    dg.poblacion_total_estimada = m.group(1).strip()

        # Buscar la asociación regional en h6 con span
        for h6 in row.find_all("h6"):
            h6_text = clean_html_text(h6.get_text())
            if "pertenece" in h6_text.lower() and "asociaci" in h6_text.lower():
                span = h6.find("span")
                if span:
                    dg.asociacion_regional = clean_html_text(span.get_text())

    # Fallback para pares h6→p si los rows están anidados en una versión nueva del sitio.
    flexible_pairs = _extract_h6_value_pairs(tab)
    for label, value in flexible_pairs.items():
        key = norm_key(label)
        if not dg.nombre_lengua_indigena and "nombre" in key and "lengua" in key and "indigena" in key:
            dg.nombre_lengua_indigena = value
        elif not dg.significado_nombres and "significado" in key and "nombre" in key:
            dg.significado_nombres = value
        elif not dg.pueblos_conforman and "pueblos" in key and "conforman" in key:
            dg.pueblos_conforman = value
        elif not dg.autodenominacion_pueblo and "autodenominacion" in key:
            dg.autodenominacion_pueblo = value

    # Extraer tablas
    tables = tab.find_all("table")
    for table in tables:
        if not isinstance(table, Tag):
            continue
        parsed = _parse_html_table(table)
        if not parsed:
            continue

        # Identificar la tabla por sus encabezados
        thead = table.find("thead")
        header_text = clean_html_text(thead.get_text()).lower() if thead else ""

        # Título antes de la tabla
        title_el = table.find_previous(["h6"])
        title = clean_html_text(title_el.get_text()).lower() if title_el else ""

        if "según el pueblo" in header_text or "tipo de comunidad" in title:
            dg.tipo_comunidad = parsed
        elif "localidad sede" in title:
            dg.localidad_sede = parsed
            # Extraer coordenadas
            if parsed:
                first = parsed[0]
                for k, v in first.items():
                    if norm_key(k) == "latitud":
                        dg.latitud_sede = v
                    elif norm_key(k) == "longitud":
                        dg.longitud_sede = v
                    elif norm_key(k) == "altitud":
                        dg.altitud_sede = v
        elif "número de asentamientos" in title or "numero_de_asentamientos" in norm_key(title):
            dg.asentamientos_cantidad = parsed
        elif "listado de asentamientos" in title or "listado_de_asentamientos" in norm_key(title):
            dg.listado_asentamientos = parsed

    return dg


# ─── Cliente HTTP asíncrono ──────────────────────────────────────────────────


async def _request_with_retries(
    client: httpx.AsyncClient,
    url: str,
    *,
    sem: asyncio.Semaphore,
    limiter: AsyncRateLimiter,
    retries: int,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """GET robusto: TLS normal, rate-limit global, Retry-After y backoff."""
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            await limiter.wait()
            async with sem:
                resp = await client.get(url, params=params)
            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt == retries:
                    resp.raise_for_status()
                retry_after = resp.headers.get("Retry-After", "").strip()
                try:
                    wait = float(retry_after)
                except ValueError:
                    wait = min(2 ** attempt + random.uniform(0, 1), 30)
                print(f"  HTTP {resp.status_code} en {url}; reintento en {wait:.1f}s")
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except Exception as exc:
            last_exc = exc
            if attempt == retries:
                raise
            wait = min(2 ** attempt + random.uniform(0, 1), 30)
            await asyncio.sleep(wait)
    assert last_exc is not None
    raise last_exc


async def fetch_page(
    client: httpx.AsyncClient,
    page: int,
    sem: asyncio.Semaphore,
    limiter: AsyncRateLimiter,
    retries: int = 3,
    page_size: int = PAGE_SIZE,
) -> dict[str, Any]:
    params = {
        "parameterOne": "0",
        "parameterTwo": "0",
        "parameterThree": "0",
        "parameterFour": "",
        "parameterFive": "0",
        "parameterSix": "0",
        "page": str(page),
        "pageSize": str(page_size),
    }
    resp = await _request_with_retries(
        client, LIST_ENDPOINT, sem=sem, limiter=limiter, retries=retries, params=params
    )
    data = resp.json()
    if not isinstance(data, dict) or not data.get("success"):
        raise ValueError(f"Respuesta de índice inválida en página {page}")
    if not isinstance(data.get("data"), list):
        raise ValueError(f"La página {page} no contiene una lista 'data'")
    return data


async def fetch_detail(
    client: httpx.AsyncClient,
    id_registro: int,
    sem: asyncio.Semaphore,
    limiter: AsyncRateLimiter,
    retries: int = 3,
) -> str:
    url = DETAIL_ENDPOINT.format(id=id_registro)
    resp = await _request_with_retries(
        client, url, sem=sem, limiter=limiter, retries=retries
    )
    html = resp.text
    if len(html.strip()) < 100:
        raise ValueError(f"Detalle {id_registro}: HTML demasiado corto")
    return html


def parse_list_item(item_data: dict[str, Any]) -> ListItem:
    if "idRegistro" not in item_data:
        raise ValueError("Registro de índice sin idRegistro")
    return ListItem(
        id_registro=int(item_data["idRegistro"]),
        pueblo_indigena=item_data.get("descripcionPuebloIndigena", "") or "",
        cedula=item_data.get("gralCedula", "") or "",
        nombre_comunidad=item_data.get("gralNombreComunidad", "") or "",
        cve_estado=item_data.get("cveEstado", "") or "",
        entidad_federativa=item_data.get("gralEntidadFederativa", "") or "",
        cve_municipio=item_data.get("cveMunicipio", "") or "",
        municipio=item_data.get("gralMunicipio", "") or "",
        id_ccdi=int(item_data.get("idCcdi", 0) or 0),
        region=int(item_data.get("region", 0) or 0),
    )


def parse_and_validate_detail(id_registro: int, html: str) -> tuple[DetailHeader, DatosGenerales]:
    soup = BeautifulSoup(html, HTML_PARSER)
    has_header = soup.find("div", class_="fixed-div") or soup.find("div", class_="card-header")
    has_general = soup.find("div", id="tabDescripcion") or soup.find(
        "div", class_=re.compile(r"tab-pane.*active")
    )
    if not has_header or not has_general:
        raise ValueError(
            f"Detalle {id_registro}: HTML 200 pero no contiene la estructura esperada "
            "de Información/Datos Generales"
        )
    header = parse_detail_header(soup)
    general = parse_datos_generales(soup)
    header_signal = any((
        header.nombre_comunidad, header.numero_registro, header.pueblo,
        header.entidad_federativa, header.municipio,
    ))
    general_signal = any((
        general.nombre_lengua_indigena, general.significado_nombres,
        general.pueblos_conforman, general.autodenominacion_pueblo,
        general.poblacion_total_estimada, general.tipo_comunidad,
        general.localidad_sede, general.listado_asentamientos,
    ))
    if not header_signal and not general_signal:
        raise ValueError(f"Detalle {id_registro}: parser produjo un registro vacío")
    return header, general


# ─── Fases del scraping ─────────────────────────────────────────────────────


async def _fetch_full_index(
    client: httpx.AsyncClient,
    store: Store,
    sem: asyncio.Semaphore,
    limiter: AsyncRateLimiter,
    retries: int,
    page_size: int,
    max_pages: int | None,
    prefix: str,
) -> tuple[int, int, int]:
    """
    Recorre el catálogo completo (o hasta max_pages) con el page_size dado,
    fusionando los IDs encontrados en el inventario actual. `mark_current`
    usa INSERT OR IGNORE, así que fusionar el resultado de varias pasadas
    (incluso con page_size distinto) es seguro y solo puede sumar IDs.
    Devuelve (total_items reportado, total_pages reportado, páginas recorridas).
    """
    first = await fetch_page(client, 1, sem, limiter, retries, page_size)
    total_items = int(first.get("totalItems", 0) or 0)
    total_pages = int(first.get("totalPages", 0) or 0)
    if total_items <= 0 or total_pages <= 0:
        raise RuntimeError(
            f"El sitio devolvió totales inválidos: {total_items=} {total_pages=}"
        )

    pages_to_do = total_pages if max_pages is None else min(total_pages, max_pages)

    def store_page(data: dict[str, Any]) -> int:
        items = data.get("data", [])
        for item_data in items:
            item = parse_list_item(item_data)
            store.upsert_registro(item)
            store.mark_current(item.id_registro)
        store.commit_registros()
        return len(items)

    store_page(first)

    failed: list[str] = []
    completed = 1
    if pages_to_do > 1:
        async def get_one(page_num: int) -> tuple[int, dict[str, Any]]:
            try:
                data = await fetch_page(
                    client, page_num, sem, limiter, retries, page_size
                )
                return page_num, data
            except Exception as exc:
                raise RuntimeError(
                    f"Página {page_num}: {type(exc).__name__}: {exc}"
                ) from exc

        tasks = [asyncio.create_task(get_one(p)) for p in range(2, pages_to_do + 1)]
        for fut in asyncio.as_completed(tasks):
            try:
                _, data = await fut
                store_page(data)
                completed += 1
                if completed % 50 == 0 or completed == pages_to_do:
                    print(
                        f"  {prefix} {completed:,}/{pages_to_do:,}; "
                        f"IDs únicos {store.count_current():,}"
                    )
            except Exception as exc:
                failed.append(f"{type(exc).__name__}: {exc}")

    if failed:
        for err in failed[:10]:
            store.add_error(None, "index", err)
        raise RuntimeError(
            f"Fallaron {len(failed)} páginas del índice ({prefix.strip()}). "
            "Ejecuta nuevamente."
        )

    return total_items, total_pages, pages_to_do


async def phase1_index(
    client: httpx.AsyncClient,
    store: Store,
    sem: asyncio.Semaphore,
    limiter: AsyncRateLimiter,
    max_pages: int | None,
    retries: int,
    page_size: int,
    reconcile_rounds: int = 5,
) -> tuple[int, int]:
    """
    Construye un inventario actual y exige completitud antes de continuar.

    NOTA sobre el desfase de 1 ID: el backend de /Consulta/GetCedulas no
    garantiza un ORDER BY con clave única — cada página se resuelve con su
    propia consulta, así que cuando hay registros empatados en la clave de
    orden, el límite entre una página y la siguiente puede desplazarse
    ligeramente entre una solicitud y otra. El síntoma típico es que
    totalItems no coincide con los IDs únicos recolectados aunque ninguna
    página haya fallado (ej. 16,730 reportados vs 16,729 obtenidos). Esto NO
    se arregla con más reintentos de red porque no es un error de red.
    En vez de fallar de inmediato, si faltan IDs se repite el recorrido
    completo variando pageSize (desplazando los límites de página) para dar
    otra oportunidad de capturar el/los ID(s) que cayeron en la "grieta"
    entre dos páginas, fusionando lo ya encontrado. Solo se declara fallo si
    tras `reconcile_rounds` intentos sigue sin cuadrar.
    """
    print("\n=== FASE 1: Indexando registros ===")
    if HTML_PARSER != "lxml":
        print(
            "  Aviso: 'lxml' no funciona en este entorno "
            f"({_LXML_ERROR}); usando 'html.parser' (más lento pero funcional). "
            "Para diagnosticar: python3 -c \"import lxml.etree\""
        )
    store.begin_current_index()

    total_items, total_pages, _ = await _fetch_full_index(
        client, store, sem, limiter, retries, page_size, max_pages, prefix="Páginas"
    )
    print(f"El sitio reporta: {total_items:,} registros en {total_pages:,} páginas")

    current = store.count_current()

    if max_pages is None:
        round_no = 0
        while current != total_items and round_no < reconcile_rounds:
            round_no += 1
            faltan = total_items - current
            # pageSize distinto en cada ronda → los límites de página caen en
            # otro sitio, dando otra oportunidad de capturar el ID perdido.
            variant_page_size = max(2, page_size + round_no * 3)
            print(
                f"  Reconciliación {round_no}/{reconcile_rounds}: faltan {faltan:,} "
                f"IDs; repasando el catálogo con pageSize={variant_page_size}..."
            )
            try:
                await _fetch_full_index(
                    client, store, sem, limiter, retries, variant_page_size,
                    max_pages=None, prefix=f"Reconciliación {round_no}: página",
                )
            except Exception as exc:
                print(f"  Aviso: reconciliación {round_no} incompleta: {exc}")
            current = store.count_current()
            print(
                f"  Tras reconciliación {round_no}: IDs únicos "
                f"{current:,}/{total_items:,}"
            )

        if current != total_items:
            raise RuntimeError(
                f"VALIDACIÓN DE ÍNDICE FALLÓ: API reporta {total_items:,}, pero se "
                f"obtuvieron {current:,} IDs únicos tras {reconcile_rounds} rondas de "
                "reconciliación. El sitio podría no garantizar un orden estable en la "
                "paginación; prueba con --index-reconcile-rounds más alto o revisa "
                "manualmente los IDs faltantes. No se continúa."
            )

        # Detectar si el catálogo cambió mientras recorríamos las páginas.
        check = await fetch_page(client, 1, sem, limiter, retries, page_size)
        end_items = int(check.get("totalItems", 0) or 0)
        end_pages = int(check.get("totalPages", 0) or 0)
        if (end_items, end_pages) != (total_items, total_pages):
            raise RuntimeError(
                "El catálogo cambió durante la ejecución: "
                f"inicio=({total_items},{total_pages}), fin=({end_items},{end_pages}). "
                "Repite para obtener una fotografía consistente."
            )

    store.set_meta("site_totals", {
        "total_items": total_items,
        "total_pages": total_pages,
        "page_size": page_size,
        "current_ids": current,
        "observed_at": now_iso(),
        "partial_test": max_pages is not None,
    })
    print(f"  Índice validado: {current:,} IDs únicos")
    return total_items, total_pages


async def phase2_details(
    client: httpx.AsyncClient,
    store: Store,
    sem: asyncio.Semaphore,
    limiter: AsyncRateLimiter,
    save_html: bool,
    retries: int,
    continue_on_error: bool,
    max_details: int | None,
    workers: int,
) -> None:
    """Extrae detalles concurrentemente con límite GLOBAL de tasa y checkpoint por ID."""
    print("\n=== FASE 2: Extrayendo detalles ===")
    pending = store.ids_sin_detalle()
    already = store.count_detalles_current()
    if not pending:
        print(f"  Todos los {already:,} registros del inventario actual ya tienen detalle.")
        return
    if max_details is not None:
        pending = pending[:max_details]
    total = len(pending)
    print(f"  Pendientes: {total:,}; ya extraídos del inventario actual: {already:,}")

    queue: asyncio.Queue[int | None] = asyncio.Queue()
    for id_reg in pending:
        queue.put_nowait(id_reg)
    for _ in range(workers):
        queue.put_nowait(None)

    stats = {"ok": 0, "err": 0, "processed": 0}
    fatal_errors: list[str] = []
    stats_lock = asyncio.Lock()
    t0 = time.monotonic()

    async def worker(worker_no: int) -> None:
        while True:
            id_reg = await queue.get()
            if id_reg is None:
                queue.task_done()
                return
            try:
                html = await fetch_detail(client, id_reg, sem, limiter, retries)
                header, general = parse_and_validate_detail(id_reg, html)
                store.upsert_detalle(
                    id_reg, header, general, html if save_html else None
                )
                async with stats_lock:
                    stats["ok"] += 1
            except Exception as exc:
                store.add_error(id_reg, "detail", f"{type(exc).__name__}: {exc}")
                async with stats_lock:
                    stats["err"] += 1
                print(f"  ERROR id={id_reg}: {type(exc).__name__}: {exc}")
                if not continue_on_error:
                    fatal_errors.append(f"id={id_reg}: {type(exc).__name__}: {exc}")
            finally:
                async with stats_lock:
                    stats["processed"] += 1
                    p = stats["processed"]
                    if p % 100 == 0 or p == total:
                        elapsed = max(time.monotonic() - t0, 0.001)
                        rate = p / elapsed
                        eta = (total - p) / rate if rate else 0
                        print(
                            f"  [{p:,}/{total:,}] OK={stats['ok']:,} "
                            f"ERR={stats['err']:,} ({rate:.2f} reg/s, ETA {eta/60:.0f} min)"
                        )
                queue.task_done()

    tasks = [asyncio.create_task(worker(i + 1)) for i in range(workers)]
    try:
        await queue.join()
        await asyncio.gather(*tasks)
        if fatal_errors:
            raise RuntimeError(fatal_errors[0])
    except Exception:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    print(
        f"\n  Detalles actuales: {store.count_detalles_current():,}; "
        f"nuevos OK={stats['ok']:,}; errores={stats['err']:,}"
    )


async def phase3_export(store: Store, out_dir: Path) -> None:
    """Fase 3: exportar CSV y JSONL.gz."""
    print("\n=== FASE 3: Exportando ===")

    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_gz = out_dir / "registros_inpi.jsonl.gz"
    csv_path = out_dir / "registros_inpi.csv"

    count = 0
    with gzip.open(jsonl_gz, "wt", encoding="utf-8") as gz, \
         csv_path.open("w", newline="", encoding="utf-8-sig") as cf:

        writer = csv.DictWriter(cf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()

        for rec in store.iter_all():
            header = rec["header"]
            general = rec["general"]

            full = {
                "id_registro": rec["id_registro"],
                "pueblo_indigena": rec["pueblo_indigena"],
                "cedula": rec["cedula"],
                "nombre_comunidad": rec["nombre_comunidad"],
                "cve_estado": rec["cve_estado"],
                "entidad_federativa": rec["entidad_federativa"],
                "cve_municipio": rec["cve_municipio"],
                "municipio": rec["municipio"],
                "id_ccdi": rec["id_ccdi"],
                "region": rec["region"],
                "informacion": header,
                "datos_generales": general,
            }
            gz.write(json.dumps(full, ensure_ascii=False) + "\n")

            csv_row = {
                "id_registro": rec["id_registro"],
                "numero_registro": header.get("numero_registro", ""),
                "cedula": rec["cedula"],
                "pueblo_indigena": rec["pueblo_indigena"],
                "nombre_comunidad": rec["nombre_comunidad"],
                "entidad_federativa": rec["entidad_federativa"],
                "cve_estado": rec["cve_estado"],
                "municipio": rec["municipio"],
                "cve_municipio": rec["cve_municipio"],
                "id_ccdi": rec["id_ccdi"],
                "region": rec["region"],
                "pueblo": header.get("pueblo", ""),
                "region_nombre": header.get("region", ""),
                "localidad": header.get("localidad", ""),
                "unidad_administrativa": header.get("unidad_administrativa", ""),
                "nombre_lengua_indigena": general.get("nombre_lengua_indigena", ""),
                "significado_nombres": general.get("significado_nombres", ""),
                "pueblos_conforman": general.get("pueblos_conforman", ""),
                "autodenominacion_pueblo": general.get("autodenominacion_pueblo", ""),
                "poblacion_total_estimada": general.get("poblacion_total_estimada", ""),
                "asociacion_regional": general.get("asociacion_regional", ""),
                "latitud_sede": general.get("latitud_sede", ""),
                "longitud_sede": general.get("longitud_sede", ""),
                "altitud_sede": general.get("altitud_sede", ""),
                "encabezado_json": json_dumps(header),
                "datos_generales_json": json_dumps(general),
            }
            writer.writerow(csv_row)
            count += 1

    print(f"  {csv_path} — {count:,} filas")
    print(f"  {jsonl_gz}")


# ─── Programa principal ─────────────────────────────────────────────────────


async def scrape(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "inpi_api.sqlite3"

    store = Store(db_path)
    sem = asyncio.Semaphore(args.concurrency)
    limiter = AsyncRateLimiter(args.requests_per_second)

    async with httpx.AsyncClient(
        base_url=BASE_URL,
        headers=DEFAULT_HEADERS,
        timeout=httpx.Timeout(args.timeout, connect=15),
        follow_redirects=True,
        limits=httpx.Limits(
            max_connections=args.concurrency + 2,
            max_keepalive_connections=args.concurrency,
        ),
    ) as client:

        # Fase 1: Indexar
        total_items, total_pages = await phase1_index(
            client, store, sem, limiter,
            max_pages=args.max_pages,
            retries=args.retries,
            page_size=args.page_size,
            reconcile_rounds=args.index_reconcile_rounds,
        )

        # Fase 2: Detalles
        await phase2_details(
            client, store, sem, limiter,
            save_html=args.save_html,
            retries=args.retries,
            continue_on_error=args.continue_on_error,
            max_details=args.max_details,
            workers=args.concurrency,
        )

    # Fase 3: Exportar
    await phase3_export(store, out_dir)

    # Validación final
    n_registros = store.count_current()
    n_detalles = store.count_detalles_current()
    print(f"\n=== RESUMEN ===")
    print(f"  Registros indexados: {n_registros:,}")
    print(f"  Detalles extraídos:  {n_detalles:,}")
    if total_items:
        print(f"  Total del sitio:     {total_items:,}")
        if n_detalles == total_items:
            print("  OK VALIDACION: conteo coincide exactamente con el sitio.")
        elif n_registros == total_items and n_detalles < total_items:
            print(
                f"  AVISO: Indexacion completa pero faltan {total_items - n_detalles:,} detalles. "
                "Ejecuta de nuevo para reintentar los faltantes."
            )
        else:
            print(
                f"  AVISO: Indexados {n_registros:,} / {total_items:,}. "
                "Ejecuta de nuevo para continuar."
            )

    store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=(
            "Extrae el catálogo completo de cédulas del INPI usando la API directa "
            "(sin navegador). Mucho más rápido y estable que el prototipo con Playwright."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  # Prueba rápida: indexar 1 página (10 registros) y extraer sus detalles
  py inpi_scraper_api_v2.py --max-pages 1

  # Ejecutar todo el catálogo (~16,730 registros)
  py inpi_scraper_api_v2.py

  # Reanudar una ejecución interrumpida (automático vía SQLite)
  py inpi_scraper_api_v2.py

  # Aumentar concurrencia si el servidor lo permite
  py inpi_scraper_api_v2.py --concurrency 5
        """,
    )
    ap.add_argument("--out", default="salida_inpi", help="Directorio de salida.")
    ap.add_argument(
        "--max-pages", type=int, default=None,
        help="Limitar páginas a indexar (para pruebas). Ej: --max-pages 1",
    )
    ap.add_argument(
        "--max-details", type=int, default=None,
        help="Limitar detalles a extraer (para pruebas). Ej: --max-details 5",
    )
    ap.add_argument(
        "--concurrency", type=int, default=3,
        help="Workers/solicitudes simultáneas máximas. Default seguro: 3",
    )
    ap.add_argument(
        "--requests-per-second", type=float, default=3.0,
        help="Límite GLOBAL de inicio de solicitudes por segundo. Default: 3.0",
    )
    ap.add_argument(
        "--page-size", type=int, default=10,
        help="Tamaño de página de la API. Default conservador: 10",
    )
    ap.add_argument(
        "--index-reconcile-rounds", type=int, default=5,
        help=(
            "Si tras indexar faltan IDs (paginación con orden inestable en el "
            "sitio), número de rondas de reconciliación (repasar el catálogo "
            "completo con pageSize distinto) antes de fallar. Default: 5"
        ),
    )
    ap.add_argument("--timeout", type=float, default=30, help="Timeout HTTP en segundos.")
    ap.add_argument("--retries", type=int, default=3, help="Reintentos por solicitud.")
    ap.add_argument(
        "--continue-on-error",
        action=argparse.BooleanOptionalAction, default=True,
        help="Continuar tras agotar reintentos de un registro.",
    )
    ap.add_argument(
        "--save-html",
        action=argparse.BooleanOptionalAction, default=True,
        help="Conservar HTML bruto del detalle en SQLite.",
    )
    return ap


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(scrape(args))
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario. Los datos guardados permanecen en SQLite.")
        return 130
    except Exception as exc:
        print(f"\nERROR FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
