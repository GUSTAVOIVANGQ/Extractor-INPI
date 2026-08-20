# Changelog

## [2.0.0] - 2026-08-20

### Added
- Versión por API directa con concurrencia controlada.
- Reanudación granular con SQLite.
- Exportación a CSV y JSONL comprimido.
- Validación de inventario y reconciliación de paginación.
- Conservación opcional del HTML bruto de detalle.

### Changed
- Se prioriza `inpi_scraper_api.py` como versión recomendada.
- El proyecto queda documentado con enfoque de operación, mantenimiento y contribución.

## [1.0.0] - 2026-08-20

### Added
- Prototipo inicial con Playwright en `inpi_scraper.py`.
- Extracción de tabla principal, modal de información y pestaña Datos Generales.
- Persistencia en SQLite y exportación de resultados.
