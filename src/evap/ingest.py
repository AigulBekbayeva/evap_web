"""
Чтение CSV, выгруженных скриптом gee/export_data.js.

Путь через Export.table.toDrive обходит лимиты памяти Earth Engine: задача
считается асинхронно на серверах Google, а не в одном синхронном getInfo().
Взамен появляется ручной шаг — скачать файлы из Drive в data/raw/.

Ожидаемые файлы (имена задаёт JS-скрипт, суффиксы Drive вида
"era5land_daily (1).csv" распознаются):

    era5land_daily.csv      метеоряд, если брали через GEE
    landsat_lswt.csv        температура воды, Landsat 8/9
    modis_lswt_night.csv    температура воды, MODIS, ночь
    water_area_annual.csv   площадь зеркала по годам
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

# Соответствие полос суточной коллекции ERA5-Land каноническим именам
ERA5_RENAME = {
    "temperature_2m_min": "t2m_min",
    "temperature_2m_max": "t2m_max",
    "surface_solar_radiation_downwards_sum":
        "surface_solar_radiation_downwards_hourly",
    "surface_thermal_radiation_downwards_sum":
        "surface_thermal_radiation_downwards_hourly",
    "total_precipitation_sum": "total_precipitation_hourly",
}


def find_csv(raw_dir: str | Path, stem: str) -> Path | None:
    """
    Ищет файл по основе имени.

    Google Drive при повторной выгрузке добавляет суффиксы вида " (1)",
    поэтому точное совпадение имени не гарантировано. При нескольких
    кандидатах берётся самый свежий.
    """
    raw_dir = Path(raw_dir)
    if not raw_dir.exists():
        return None

    hits = sorted(raw_dir.glob(f"{stem}*.csv"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return hits[0] if hits else None


def _read_dated(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "date" not in df.columns:
        raise ValueError(f"{path.name}: нет колонки 'date'")
    df["date"] = pd.to_datetime(df["date"])
    # GEE добавляет служебные колонки
    df = df.drop(columns=[c for c in ("system:index", ".geo") if c in df.columns])
    return df.sort_values("date").reset_index(drop=True)


def load_era5(raw_dir: str | Path = "data/raw") -> pd.DataFrame | None:
    """Метеоряд из GEE. None, если файла нет — тогда берётся Open-Meteo."""
    path = find_csv(raw_dir, "era5land_daily")
    if path is None:
        return None

    df = _read_dated(path).rename(columns=ERA5_RENAME)
    n_empty = df.drop(columns=["date"]).isna().all(axis=1).sum()
    print(f"  {path.name}: {len(df)} суток"
          + (f", пустых {n_empty}" if n_empty else ""))

    if n_empty > 0.5 * len(df):
        print("  ! Больше половины строк пустые — ячейка ERA5-Land, скорее "
              "всего, замаскирована над водой.\n"
              "    Либо включите USE_LAND_BUFFER в JS-скрипте, либо перейдите "
              "на Open-Meteo:\n"
              "      evap fetch --source openmeteo")
    return df


def load_lswt(raw_dir: str | Path = "data/raw") -> dict[str, pd.DataFrame]:
    """Ряды температуры воды: Landsat и MODIS раздельно."""
    out = {}

    p = find_csv(raw_dir, "landsat_lswt")
    if p is not None:
        df = _read_dated(p).dropna(subset=["tw"])
        if "n_pix" in df.columns:
            df = df[df["n_pix"] >= 25]
        out["landsat"] = df.reset_index(drop=True)
        print(f"  {p.name}: {len(out['landsat'])} снимков")

    p = find_csv(raw_dir, "modis_lswt")
    if p is not None:
        df = _read_dated(p).dropna(subset=["tw"])
        out["modis"] = df.reset_index(drop=True)
        print(f"  {p.name}: {len(out['modis'])} суток")

    return out


def load_water_area(raw_dir: str | Path = "data/raw") -> pd.DataFrame | None:
    """Площадь зеркала по годам — для перехода мм → м³."""
    p = find_csv(raw_dir, "water_area_annual")
    if p is None:
        return None

    df = pd.read_csv(p)
    df = df.drop(columns=[c for c in ("system:index", ".geo") if c in df.columns])
    print(f"  {p.name}: {len(df)} лет, "
          f"площадь {df['area_km2'].min():.1f}–{df['area_km2'].max():.1f} км²")
    return df.sort_values("year").reset_index(drop=True)


def status(raw_dir: str | Path = "data/raw") -> dict:
    """Что уже скачано, а чего не хватает."""
    expected = {
        "era5land_daily": "метеоряд (не нужен при использовании Open-Meteo)",
        "landsat_lswt": "температура воды, Landsat — ОБЯЗАТЕЛЬНО",
        "modis_lswt": "температура воды, MODIS — желательно",
        "water_area_annual": "площадь зеркала по годам — желательно",
    }
    return {stem: {"found": find_csv(raw_dir, stem) is not None, "what": what}
            for stem, what in expected.items()}
