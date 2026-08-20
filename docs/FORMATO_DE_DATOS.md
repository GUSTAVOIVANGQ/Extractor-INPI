# Formato de datos

## Salida versión API

### SQLite
La base `inpi_api.sqlite3` guarda:
- inventario de registros
- detalles por registro
- errores
- metadatos de ejecución

### CSV
`registros_inpi.csv` contiene campos planos de consulta rápida.

### JSONL comprimido
`registros_inpi.jsonl.gz` conserva la estructura completa por registro.

## Salida versión Playwright

### SQLite
La base `inpi.sqlite3` guarda:
- registros procesados
- información extraída del modal
- errores
- metadatos

### CSV
`registros_inpi.csv` contiene columnas resumidas y JSON serializado.

### JSONL comprimido
`registros_inpi.jsonl.gz` conserva los objetos completos.
