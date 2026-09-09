"""
sorbulak-evap — расчёт и прогноз испарения с водной поверхности.

Метеорология: ERA5 через Open-Meteo Archive API (без Earth Engine).
Температура воды: CSV, выгруженные gee/export_data.js через веб-редактор.
"""

from .config import Config, Paths
from . import (physics, openmeteo, lswt, ingest, waterbody,
               pipeline, validate, webmap, plots, forecast)

__version__ = "0.2.0"
__all__ = [
    "Config", "Paths",
    "physics", "openmeteo", "lswt", "ingest", "waterbody",
    "pipeline", "validate", "webmap", "plots", "forecast",
]
