"""Конфигурация проекта: YAML + значения по умолчанию."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class Paths:
    shapes: str = "data/shapes"
    raw: str = "data/raw"
    processed: str = "data/processed"
    outputs: str = "data/outputs"


@dataclass
class Config:
    # --- период расчёта ---
    start: str = "2000-01-01"
    end: str = "2026-01-01"

    # --- источник метеоданных ---
    # 'openmeteo' — Archive API, без Earth Engine и регистрации. Рекомендуется:
    #     снимает проблему маскирования водоёмов в ERA5-Land, но нисходящее
    #     длинноволновое излучение оценивается по Брунту (~10 % точности).
    # 'gee'      — ERA5-Land через Earth Engine, прямое поле Rl↓.
    # 'csv'      — CSV, выгруженные скриптом gee/export_data.js в data/raw/.
    meteo_source: str = "openmeteo"

    # --- Earth Engine ---
    gee_project: str | None = None      # ID облачного проекта GEE
    era5_scale: int = 9000              # родное разрешение ERA5-Land
    landsat_sensors: tuple = ("L8", "L9")
    use_modis: bool = True

    # --- геометрия ---
    erode_metres: float = 60.0          # отступ от берега для термометрии

    # --- морфометрия ---
    # Средняя глубина — главный неизвестный параметр. Влияет на теплозапас G.
    # Подбирается так, чтобы модельный ход Tw совпал со спутниковым
    # (см. scripts/calibrate_depth.py).
    mean_depth_m: float = 10.0
    mixed_depth_source: str = "parameterised"   # 'constant' | 'parameterised'

    # --- обработка Tw ---
    tw_interp_method: str = "harmonic"  # 'harmonic' | 'linear'
    tw_smooth_days: int = 15            # сглаживание перед dTw/dt

    # --- прочее ---
    force_download: bool = False
    paths: Paths = field(default_factory=Paths)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Config":
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        paths = Paths(**raw.pop("paths", {}))
        if "landsat_sensors" in raw:
            raw["landsat_sensors"] = tuple(raw["landsat_sensors"])
        return cls(paths=paths, **raw)

    def to_dict(self) -> dict:
        return asdict(self)
