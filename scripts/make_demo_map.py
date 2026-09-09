#!/usr/bin/env python3
"""
Демонстрационная карта на синтетических данных.

Позволяет посмотреть, как выглядит результат, до того как получены реальные
данные и настроен Earth Engine. Контур водоёма — условный, погода
сгенерирована по климатическим параметрам юго-востока Казахстана.

    python scripts/make_demo_map.py
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evap import pipeline, webmap                      # noqa: E402
from evap.config import Config                         # noqa: E402

LON0, LAT0 = 76.745, 43.862
AREA_KM2 = 52.4


def synthetic_weather(start="2015-01-01", end="2024-12-31", seed=11):
    """Правдоподобная погода семиаридной зоны на 43.9° с.ш."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start, end, freq="D")
    n = len(dates)
    w = 2 * np.pi * dates.dayofyear.to_numpy(float) / 365.25

    ta = 10.5 + 16.0 * np.sin(w - 1.9) + rng.normal(0, 3.2, n)
    tdew = ta - (7.0 + 7.0 * np.clip(np.sin(w - 1.9), 0, 1)) + rng.normal(0, 1.6, n)
    rs = np.clip(14.5 + 11.5 * np.sin(w - 1.9), 1.5, None) * rng.uniform(0.55, 1.0, n)
    rl = 22.5 + 8.5 * np.sin(w - 1.9) + rng.normal(0, 1.0, n)
    u10 = np.abs(rng.gamma(4.2, 0.95, n))

    # осадки: весенне-осенний максимум, годовая сумма около 350 мм
    wet_prob = 0.26 + 0.10 * np.cos(w - 0.6)
    prcp = rng.gamma(0.85, 4.4, n) * (rng.random(n) < wet_prob)

    tw = np.clip(11.5 + 13.0 * np.sin(w - 2.25), 0.0, None)

    return pd.DataFrame({
        "date": dates, "ta": ta, "tdew": tdew, "p_kpa": 92.0, "u10": u10,
        "rs_down": rs, "rl_down": rl, "prcp": prcp, "tw": tw,
        "tw_obs": np.where(np.arange(n) % 8 == 0, tw + rng.normal(0, 0.6, n), np.nan),
        "tw_filled": np.arange(n) % 8 != 0,
    })


def synthetic_outline(lon0=LON0, lat0=LAT0, n=60):
    """Условный контур водоёма — неправильный многоугольник."""
    th = np.linspace(0, 2 * np.pi, n)
    r_lon = 0.085 * (1 + 0.28 * np.sin(3 * th) + 0.12 * np.cos(5 * th))
    r_lat = 0.055 * (1 + 0.22 * np.cos(2 * th) + 0.10 * np.sin(4 * th))
    ring = [[round(lon0 + r_lon[i] * np.cos(th[i]), 5),
             round(lat0 + r_lat[i] * np.sin(th[i]), 5)] for i in range(n)]
    ring.append(ring[0])
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"name": "водоём (демо)"},
        "geometry": {"type": "Polygon", "coordinates": [ring]}}]}


def main():
    df = synthetic_weather()
    res = pipeline.compute_evaporation(df, Config(mean_depth_m=11.0))
    clim = pipeline.monthly_climatology(res)
    annual = pipeline.annual_summary(res)

    out = webmap.build_map(
        synthetic_outline(), res, clim, annual,
        "data/outputs/map_demo.html", ee_layers=[],
        title="Испарение — демонстрация на синтетике",
        centroid=(LON0, LAT0), area_km2=AREA_KM2)

    print(f"Карта: {Path(out).resolve()}  ({Path(out).stat().st_size / 1024:.0f} КБ)")
    print(f"Испарение {annual['E_penman'].mean():.0f} мм/год, "
          f"осадки {annual['P'].mean():.0f} мм/год, "
          f"дефицит {annual['E_minus_P'].mean():.0f} мм/год")
    print("\nДанные СИНТЕТИЧЕСКИЕ — для проверки внешнего вида, не для выводов.")
    print("Открыть: python -m http.server -d data/outputs 8000 "
          "→ http://localhost:8000/map_demo.html")


if __name__ == "__main__":
    main()
