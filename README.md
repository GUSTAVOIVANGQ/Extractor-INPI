# Extractor INPI — Cédulas por comunidad

Este proyecto extrae y consolida datos del **Catálogo Nacional de Pueblos y Comunidades Indígenas y Afromexicanas** del INPI:

`https://catalogo.inpi.gob.mx/cedulas/`

## Qué obtiene

Para cada comunidad, el sistema puede extraer:

1. Las columnas visibles de la tabla principal.
2. La información del encabezado del modal **Información**.
3. La pestaña **Datos Generales**.
4. El HTML bruto de las secciones procesadas para auditoría y reproceso.
5. Archivos de salida en CSV, JSONL comprimido y una base SQLite de trabajo/reanudación.

## Versiones del proyecto

El repositorio contiene dos implementaciones:

### 1) `inpi_scraper_api.py` — versión recomendada

- Usa la API interna del sitio mediante HTTP directo.
- Es más rápida y más estable.
- No requiere Chromium para funcionar.
- Incluye concurrencia controlada, reintentos, validación y reanudación por SQLite.

### 2) `inpi_scraper.py` — versión prototipo

- Usa Playwright y navegador automatizado.
- Es útil como alternativa si la API cambia.
- Mantiene una lógica más conservadora basada en la interfaz del sitio.

## Estado y enfoque

Este programa está diseñado para:

- extraer información de forma reproducible,
- permitir reinicios sin perder avance,
- guardar evidencia procesable para auditoría,
- facilitar mantenimiento y futuras mejoras.

## Dependencias

Las dependencias se encuentran en `requirements.txt`.

## Instalación

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
```

## Requisitos opcionales

- Para la versión por API, normalmente basta con `httpx`, `beautifulsoup4` y `lxml`.
- Para la versión con navegador, además se requiere Playwright y su navegador correspondiente.

## Uso general

### Versión recomendada: API directa

Prueba rápida:

```powershell
py inpi_scraper_api.py --max-pages 1
```

Ejecución completa:

```powershell
py inpi_scraper_api.py
```

### Versión alternativa: Playwright

```powershell
py -m playwright install chromium
py inpi_scraper.py --max-pages 1
```

## Parámetros útiles

### `inpi_scraper_api.py`

- `--out`: carpeta de salida.
- `--max-pages`: limita páginas para pruebas.
- `--max-details`: limita detalles para pruebas.
- `--concurrency`: número de solicitudes simultáneas.
- `--requests-per-second`: límite global de solicitud.
- `--page-size`: tamaño de página usado por la API.
- `--index-reconcile-rounds`: rondas de reconciliación si la paginación deja huecos.
- `--continue-on-error`: continúa ante errores de un registro.
- `--save-html`: guarda HTML bruto en SQLite.

### `inpi_scraper.py`

- `--url`: URL del catálogo.
- `--out`: carpeta de salida.
- `--start-page`: página inicial manual.
- `--max-pages`: límite para pruebas.
- `--headless` / `--no-headless`: ejecutar con o sin ventana.
- `--channel`: canal de navegador, por ejemplo `chrome`.
- `--timeout-ms`: tiempo máximo por operación.
- `--retries`: reintentos por fila.
- `--continue-on-error`: continúa ante fallos individuales.
- `--save-html`: conserva HTML bruto.
- `--capture-network`: guarda tráfico XHR/fetch.
- `--delay-min`, `--delay-max`: pausas entre filas.
- `--page-delay-min`, `--page-delay-max`: pausas entre páginas.

## Estructura de salidas

Los resultados se guardan en `salida_inpi/`.

### Versión API

- `inpi_api.sqlite3`: base de trabajo y checkpoint.
- `registros_inpi.csv`: exportación tabular.
- `registros_inpi.jsonl.gz`: exportación estructurada.
- `errors/`: capturas y trazas de errores.
- `network.jsonl`: solo si se activa captura de red.

### Versión Playwright

- `inpi.sqlite3`: base de trabajo y checkpoint.
- `registros_inpi.csv`: exportación tabular.
- `registros_inpi.jsonl.gz`: exportación estructurada.
- `errors/`: capturas de errores.
- `network.jsonl`: solo si se activa captura de red.

## Estructura de datos

### Versión API

Cada registro incluye, entre otros:

- `id_registro`
- `pueblo_indigena`
- `cedula`
- `nombre_comunidad`
- `entidad_federativa`
- `municipio`
- `informacion`
- `datos_generales`

### Versión Playwright

Cada registro incluye, entre otros:

- `record_key`
- `numero_registro`
- `page_number`
- `row_number`
- `summary`
- `informacion`
- `datos_generales`

## Trazabilidad y reanudación

Ambas versiones usan SQLite para:

- evitar reprocesar registros ya guardados,
- reanudar ejecuciones interrumpidas,
- conservar metadatos del proceso,
- almacenar errores para diagnóstico.

## Notas técnicas

- La versión API prioriza estabilidad y velocidad.
- La versión Playwright actúa como respaldo si cambian los endpoints internos.
- Se recomienda mantener límites de concurrencia conservadores para respetar el sitio.

## Contribución

### Desarrollo principal

- **Ivan Paredes** — `ivan.paredes@crt.gob.mx`

### Enlace de apoyo

- **Gustavo García** — `gustavo.garcia@crt.gob.mx`

### Lineamientos de contribución

- Mantener compatibilidad con la versión recomendada por API.
- Conservar la capacidad de reanudación mediante SQLite.
- Documentar cambios funcionales, dependencias y ajustes de ejecución.
- Agregar notas de versión cuando se introduzcan mejoras o correcciones.

## Historial de versiones

- **v1.0**: prototipo inicial con Playwright.
- **v2.0**: versión optimizada por API directa, con mayor rendimiento y estabilidad.

## Sugerencia de documentación adicional

Si este repositorio va a mantenerse por más tiempo, también conviene agregar:

- `CHANGELOG.md`
- `CONTRIBUTING.md`
- `LICENSE` o aviso de uso interno
- `docs/USO.md` con ejemplos de ejecución y diagnóstico
- `docs/FORMATO_DE_DATOS.md` con el detalle de columnas

## Licencia y uso

Si este repositorio se comparte fuera del equipo, conviene agregar aquí la política interna de uso, distribución y resguardo de datos.
