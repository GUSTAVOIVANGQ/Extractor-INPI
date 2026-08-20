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

## Uso general

1. Crear y activar un entorno virtual.
2. Instalar dependencias.
3. Ejecutar la versión recomendada:

```powershell
py inpi_scraper_api.py
```

Para pruebas rápidas:

```powershell
py inpi_scraper_api.py --max-pages 1
```

## Estructura de salidas

Los resultados se guardan en `salida_inpi/`:

- `inpi_api.sqlite3`: base de trabajo y checkpoint.
- `registros_inpi.csv`: exportación tabular.
- `registros_inpi.jsonl.gz`: exportación estructurada.

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

## Notas de versión

Sugerencia para este repositorio:

- **v1.0**: prototipo inicial con Playwright.
- **v2.0**: versión optimizada por API directa, con mayor rendimiento y estabilidad.

## Licencia y uso

Si este repositorio se comparte fuera del equipo, conviene agregar aquí la política interna de uso, distribución y resguardo de datos.
