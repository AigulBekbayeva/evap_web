"""
Оперативный прогноз испарения на 10 суток.

Схема:

    прогноз погоды (ECMWF IFS / GFS через Open-Meteo)
            │
            ├──► Пенман ─────────► E(t), мм/сут
            │
            └──► инерция Tw ─────► температура воды, теплозапас

Начальное условие для Tw — последнее наблюдённое спутниковое значение.
Простейшее усвоение данных: прямая подстановка с последующей релаксацией к
равновесной температуре. Для рабочей системы это место заменяется 1D-моделью
(Simstrat / GLM / FLake), инициализированной тем же наблюдением.

Ошибка прогноза наследуется от прогноза погоды и составляет порядка 15–20 %
на суточных значениях.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import physics as ph

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

HOURLY_VARS = [
    "temperature_2m",
    "dew_point_2m",
    "surface_pressure",
    "wind_speed_10m",
    "shortwave_radiation",
    "terrestrial_radiation",
    "precipitation",
]


def fetch_forecast(lat: float, lon: float, days: int = 10,
                   model: str = "ecmwf_ifs025") -> pd.DataFrame:
    """
    Забирает прогноз погоды и приводит к суточным величинам в единицах physics.

    model: 'ecmwf_ifs025' (рекомендуется), 'gfs_seamless', 'best_match'.
    Open-Meteo не требует ключа для некоммерческого использования.
    """
    import requests

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(HOURLY_VARS),
        "forecast_days": days,
        "models": model,
        "timezone": "UTC",
    }
    r = requests.get(OPEN_METEO_URL, params=params, timeout=60)
    r.raise_for_status()
    h = pd.DataFrame(r.json()["hourly"])
    h["time"] = pd.to_datetime(h["time"])
    h["date"] = h["time"].dt.floor("D")

    daily = h.groupby("date").agg(
        ta=("temperature_2m", "mean"),
        ta_max=("temperature_2m", "max"),
        tdew=("dew_point_2m", "mean"),
        p_hpa=("surface_pressure", "mean"),
        u10=("wind_speed_10m", "mean"),
        u10_max=("wind_speed_10m", "max"),
        rs_wm2=("shortwave_radiation", "mean"),
        prcp=("precipitation", "sum"),
    ).reset_index()

    daily["p_kpa"] = daily["p_hpa"] / 10.0
    # Вт/м² среднесуточное → МДж/м²/сут
    daily["rs_down"] = daily["rs_wm2"] * 0.0864
    daily["u10"] = daily["u10"] / 3.6 if daily["u10"].max() > 40 else daily["u10"]

    # Open-Meteo не отдаёт нисходящее длинноволновое напрямую —
    # оцениваем по формуле Брунта через температуру и влажность
    daily["rl_down"] = _estimate_rl_down(daily["ta"], daily["tdew"],
                                         daily["rs_down"])
    return daily


def _estimate_rl_down(ta_c, tdew_c, rs_down, clear_sky_factor=0.75):
    """
    Нисходящее длинноволновое излучение, МДж/м²/сут.

    Прогноз Open-Meteo не отдаёт облачность в том же наборе, поэтому она
    оценивается по отношению приходящей радиации к ясному небу. Дальше —
    та же формула Брунта, что в openmeteo.estimate_rl_down; см. там же
    предупреждение о константах.
    """
    from .openmeteo import estimate_rl_down

    rs = np.asarray(rs_down, dtype=float)
    rs_clear = np.maximum(np.nanmax(rs) * clear_sky_factor, 1e-6)
    cloud_pct = np.clip(1.0 - rs / rs_clear, 0.0, 1.0) * 100.0

    return estimate_rl_down(ta_c, tdew_c, cloud_pct)


def propagate_tw(tw0: float, ta_series, u10_series,
                 depth_m: float, relax_days: float | None = None):
    """
    Инерционная экстраполяция температуры воды вперёд.

    Простейшая модель релаксации к равновесной температуре с постоянной
    времени, пропорциональной глубине:

        dTw/dt = (Ta_eq − Tw) / τ,   τ ≈ depth / 2  [сут]

    Это заглушка, а не физика. Для рабочей системы использовать 1D-модель.
    Однако на горизонте 10 суток инерция глубокого водоёма настолько велика,
    что даже такая оценка даёт разумный результат: Tw меняется медленно.
    """
    ta = np.asarray(ta_series, dtype=float)
    tau = relax_days if relax_days else max(depth_m / 2.0, 3.0)

    tw = np.empty(len(ta))
    cur = float(tw0)
    for i, t_air in enumerate(ta):
        cur = cur + (t_air - cur) / tau
        tw[i] = max(cur, 0.0)
    return tw


def forecast_evaporation(lat: float, lon: float, tw_last: float,
                         depth_m: float, days: int = 10,
                         model: str = "ecmwf_ifs025") -> pd.DataFrame:
    """
    Полный прогноз испарения. Возвращает суточный ряд с E и Tw.

    tw_last — последнее наблюдённое спутниковое значение температуры воды.
    Чем свежее наблюдение, тем точнее прогноз.
    """
    fc = fetch_forecast(lat, lon, days=days, model=model)

    fc["tw"] = propagate_tw(tw_last, fc["ta"], fc["u10"], depth_m)

    u2 = ph.wind_at_2m(fc["u10"])
    h_mix = ph.mixed_layer_depth(fc["tw"], depth_m, u2)
    fc["h_mix"] = h_mix
    fc["G"] = ph.heat_storage(fc["tw"], h_mix, dt_days=1.0)

    fc["E"] = ph.penman_openwater(
        fc["ta"], fc["tdew"], fc["u10"], fc["p_kpa"],
        fc["rs_down"], fc["rl_down"], tw_c=fc["tw"], g=fc["G"])
    fc["E"] = fc["E"].clip(lower=0.0)

    fc["E_cum"] = fc["E"].cumsum()
    return fc[["date", "ta", "tdew", "u10", "u10_max", "rs_down",
               "prcp", "tw", "h_mix", "G", "E", "E_cum"]]
