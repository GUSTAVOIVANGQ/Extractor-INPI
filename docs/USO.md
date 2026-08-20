# Uso del Extractor INPI

## 1. Instalación

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
```

## 2. Versión recomendada

Prueba rápida:

```powershell
py inpi_scraper_api.py --max-pages 1
```

Ejecución completa:

```powershell
py inpi_scraper_api.py
```

## 3. Versión alternativa

```powershell
py -m playwright install chromium
py inpi_scraper.py --max-pages 1
```

## 4. Salidas

Los archivos quedan en `salida_inpi/`.

## 5. Diagnóstico rápido

Si hay errores:

- revisar `salida_inpi/errors/`
- revisar `salida_inpi/network.jsonl` si se activó captura de red
- reintentar la ejecución, ya que SQLite conserva el avance
