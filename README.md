# Extractor INPI — Cédulas por comunidad

Este proyecto extrae datos del **Catálogo Nacional de Pueblos y Comunidades Indígenas y Afromexicanas** del INPI:

`https://catalogo.inpi.gob.mx/cedulas/`

Para cada comunidad extrae:

1. Las columnas de la tabla principal (pueblo, nombre, entidad, municipio).
2. La información del encabezado del modal **Información** (número de registro, región, localidad, unidad administrativa).
3. La pestaña **Datos Generales** (nombre en lengua indígena, tipo de comunidad, asentamientos, coordenadas, población, etc.).
4. El HTML bruto del detalle se conserva en SQLite para poder auditar o re-procesar.

---

## Dos versiones

| Archivo | Técnica | Velocidad | Dependencias |
|---|---|---|---|
| **`inpi_scraper_api.py`** ⭐ Recomendado | API HTTP directa | ~1.5–2.5 horas | `httpx`, `beautifulsoup4`, `lxml` (~2 MB) |
| `inpi_scraper.py` (prototipo) | Playwright (navegador) | ~14–23 horas | `playwright` + Chromium (~500 MB) |

La versión por API fue posible porque la página del INPI usa internamente dos endpoints REST:
- `GET /Consulta/GetCedulas` — devuelve JSON paginado
- `GET /Consulta/Details/{id}` — devuelve HTML del modal de detalle

---

## Instalación

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
```

No necesitas instalar Chromium para la versión por API.

## Uso: versión por API (recomendada)

### 1. Prueba rápida (1 página = 10 registros)

```powershell
py inpi_scraper_api.py --max-pages 1
```

### 2. Ejecutar todo el catálogo (~16,730 registros)

```powershell
py inpi_scraper_api.py
```

### 3. Reanudar una ejecución interrumpida

Simplemente vuelve a ejecutar:

```powershell
py inpi_scraper_api.py
```

SQLite recuerda qué páginas se indexaron y qué detalles se extrajeron. Solo procesa los faltantes.

### Opciones útiles

```powershell
# Aumentar concurrencia (default: 3 solicitudes simultáneas)
py inpi_scraper_api.py --concurrency 5

# Limitar detalles a extraer (para pruebas)
py inpi_scraper_api.py --max-pages 1 --max-details 3

# Aumentar pausas si el servidor va lento
py inpi_scraper_api.py --detail-delay-min 0.5 --detail-delay-max 1.0

# Sin conservar HTML bruto (ahorra espacio en SQLite)
py inpi_scraper_api.py --no-save-html
```

### Archivos de salida

En `salida_inpi/`:

| Archivo | Descripción |
|---|---|
| `inpi_api.sqlite3` | Base de trabajo y checkpoint. Soporta reanudación. |
| `registros_inpi.csv` | Una fila por registro con campos planos + JSON. |
| `registros_inpi.jsonl.gz` | Formato estructurado con tablas y objetos completos. |

### Estructura de datos

En `registros_inpi.jsonl.gz`, cada registro incluye:

```json
{
  "id_registro": 18819,
  "pueblo_indigena": "Afromexicano",
  "cedula": "167-0008",
  "nombre_comunidad": "El Nacimiento de los Negros Mascogos",
  "entidad_federativa": "(05) Coahuila",
  "municipio": "(020) Múzquiz",
  "informacion": {
    "nombre_comunidad": "...",
    "pueblo": "...",
    "region": "...",
    "numero_registro": "...",
    "localidad": "...",
    "unidad_administrativa": "..."
  },
  "datos_generales": {
    "nombre_lengua_indigena": "...",
    "significado_nombres": "...",
    "pueblos_conforman": "...",
    "autodenominacion_pueblo": "...",
    "poblacion_total_estimada": "1,075",
    "latitud_sede": "16.519952000000",
    "longitud_sede": "-92.017474000000",
    "tipo_comunidad": [...],
    "localidad_sede": [...],
    "listado_asentamientos": [...]
  }
}
```

---

## Uso: prototipo con Playwright (alternativa)

Si prefieres ver el navegador en acción o si los endpoints dejan de funcionar:

```powershell
py -m playwright install chromium
py inpi_scraper.py --max-pages 1
```

Consulta la documentación original del prototipo en los comentarios de `inpi_scraper.py`.

---

## Ajustes conservadores

La versión por API ya incluye:
- Pausas aleatorias entre solicitudes
- Concurrencia limitada (default: 3)
- Reintentos con backoff exponencial
- No se recomienda paralelizar más de 5 solicitudes contra un sitio institucional
