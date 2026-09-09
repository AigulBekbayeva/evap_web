"""
Метеорологический вход: ERA5 через Open-Meteo Archive API.

ЕДИНСТВЕННЫЙ источник метеоданных в проекте. Archive API раздаёт тот же
ERA5 и ERA5-Land, что лежит в каталоге Earth Engine, но без регистрации,
без лимитов памяти и без ключа. Для некоммерческого использования бесплатно.

Earth Engine из Python-кода не вызывается вообще: он нужен только один раз,
через веб-редактор, чтобы выгрузить температуру воды (gee/export_data.js).

ЧТО ЭТО МЕНЯЕТ. Главная проблема ERA5-Land в GEE — маскирование крупных
водоёмов: над озером данных попросту нет. Open-Meteo интерполирует к точке и
всегда возвращает значение, беря его с ближайшей суши. Строго говоря, это
та же самая подстановка сухопутных условий, только выполненная за вас — то
есть систематическое завышение испарения никуда не девается, но пропусков в
ряду не будет.

ЧЕГО ЗДЕСЬ НЕТ. Open-Meteo не отдаёт нисходящее длинноволновое излучение
(Rl↓). Оно оценивается по формуле Брунта через температуру, влажность и
облачность — точность около 10 %, что для месячных и годовых сумм приемлемо,
но заметно хуже прямого поля из ERA5. Если Rl↓ критично, берите метео
через GEE.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

HOURLY_VARS = [
    "temperature_2m",
    "dew_point_2m",
    "surface_pressure",
    "wind_speed_10m",
    "shortwave_radiation",
    "cloud_cover",
    "precipitation",
]


def fetch_archive(lat: float, lon: float, start: str, end: str,
                  model: str = "era5_land",
                  chunk_years: int = 5,
                  pause_s: float = 1.0) -> pd.DataFrame:
    """
    Забирает почасовой ряд ERA5 и агрегирует до суток.

    model: 'era5_land' (9 км, рекомендуется) или 'era5' (31 км).
    Разбивка на чанки — вежливость к бесплатному API, а не техническая
    необходимость: за 25 лет это 219 000 значений на переменную.
    """
    import requests

    start_year, end_year = int(start[:4]), int(end[:4])
    frames = []

    for y0 in range(start_year, end_year + 1, chunk_years):
        y1 = min(y0 + chunk_years - 1, end_year)
        s = max(f"{y0}-01-01", start)
        e = min(f"{y1}-12-31", end)
        if s > e:
            continue

        print(f"  Open-Meteo ({model}): {s} … {e}")

        params = {
            "latitude": lat, "longitude": lon,
            "start_date": s, "end_date": e,
            "hourly": ",".join(HOURLY_VARS),
            "models": model,
            "timezone": "UTC",
        }
        r = requests.get(ARCHIVE_URL, params=params, timeout=180)

        if r.status_code == 400 and model == "era5_land":
            print("    era5_land недоступен для этой точки, перехожу на era5")
            return fetch_archive(lat, lon, start, end, model="era5",
                                 chunk_years=chunk_years, pause_s=pause_s)
        r.raise_for_status()

        h = pd.DataFrame(r.json()["hourly"])
        h["time"] = pd.to_datetime(h["time"])
        frames.append(h)
        time.sleep(pause_s)

    if not frames:
        raise RuntimeError("Open-Meteo не вернул данных — проверьте координаты и даты")

    hourly = pd.concat(frames, ignore_index=True).drop_duplicates(subset="time")
    return _aggregate_daily(hourly)


def _aggregate_daily(h: pd.DataFrame) -> pd.DataFrame:
    """
    Почасовые значения → суточные, сразу в единицах evap.physics.

    Радиация приходит в Вт/м² как среднее за час, поэтому сумма за сутки
    умножается на 3600 с и переводится в МДж.
    """
    h = h.copy()
    h["date"] = h["time"].dt.floor("D")

    d = h.groupby("date").agg(
        ta=("temperature_2m", "mean"),
        ta_min=("temperature_2m", "min"),
        ta_max=("temperature_2m", "max"),
        tdew=("dew_point_2m", "mean"),
        p_hpa=("surface_pressure", "mean"),
        u10=("wind_speed_10m", "mean"),
        u10_max=("wind_speed_10m", "max"),
        rs_wm2=("shortwave_radiation", "sum"),
        cloud=("cloud_cover", "mean"),
        prcp=("precipitation", "sum"),
        n_hours=("temperature_2m", "size"),
    ).reset_index()

    d = d[d["n_hours"] >= 20]        # отбрасываем неполные сутки

    d["p_kpa"] = d["p_hpa"] / 10.0
    # Вт/м² × 3600 с → Дж/м², далее → МДж/м²
    d["rs_down"] = d["rs_wm2"] * 3600.0 / 1e6
    # Open-Meteo отдаёт скорость ветра в км/ч
    d["u10"] = d["u10"] / 3.6
    d["u10_max"] = d["u10_max"] / 3.6
    d["rl_down"] = estimate_rl_down(d["ta"], d["tdew"], d["cloud"])

    return d[["date", "ta", "ta_min", "ta_max", "tdew", "p_kpa",
              "u10", "u10_max", "rs_down", "rl_down", "prcp", "cloud"]]


def estimate_rl_down(ta_c, tdew_c, cloud_pct) -> np.ndarray:
    """
    Нисходящее длинноволновое излучение по Брунту, МДж/м²/сут.

        Rl↓ = ε_sky · σ · Ta⁴
        ε_clear = 0.605 + 0.048 · √(ea в гПа)        [Brunt 1932]
        ε_sky   = ε_clear · (1 + 0.22 · c²)          [Bolz 1949]

    ВНИМАНИЕ на константы. В литературе рядом ходит выражение
    (0.34 − 0.14·√ea), но это коэффициент для ЧИСТОГО длинноволнового баланса
    (FAO-56, ур. 39), а не излучательная способность неба. Подстановка его
    сюда занижает Rl↓ примерно на треть, радиационный баланс уходит в минус,
    и в модели теплового баланса температура воды схлопывается к нулю.

    Ориентир: при Ta = 25 °C, Tdew = 10 °C и половинной облачности Rl↓
    должно быть около 30–33 МДж/м²/сут.
    """
    from .physics import SIGMA, sat_vapour_pressure

    ta = np.asarray(ta_c, dtype=float)
    ea_hpa = sat_vapour_pressure(tdew_c) * 10.0        # кПа → гПа
    cloud = np.clip(np.asarray(cloud_pct, dtype=float) / 100.0, 0.0, 1.0)

    eps_clear = 0.605 + 0.048 * np.sqrt(np.maximum(ea_hpa, 0.0))
    eps_sky = np.minimum(eps_clear * (1.0 + 0.22 * cloud ** 2), 1.0)

    return eps_sky * SIGMA * (ta + 273.15) ** 4


def sanity_check(df: pd.DataFrame) -> list[str]:
    """
    Контроль правдоподобия входных данных.

    Пустой список — данные выглядят нормально. Ловит перепутанные единицы,
    дыры в ряду и нетипичные соотношения потоков.
    """
    warn = []

    na = df["ta"].isna().mean()
    if na > 0.05:
        warn.append(f"Пропусков в температуре {na:.0%}")

    if df["ta"].max() > 55 or df["ta"].min() < -60:
        warn.append(f"Температура вне диапазона: "
                    f"{df['ta'].min():.1f}…{df['ta'].max():.1f} °C")

    if (df["tdew"] > df["ta"] + 0.5).mean() > 0.02:
        warn.append("Точка росы систематически выше температуры воздуха — "
                    "проверьте, не перепутаны ли поля")

    if df["rs_down"].max() > 45:
        warn.append(f"Подозрительно высокая радиация "
                    f"{df['rs_down'].max():.1f} МДж/м²/сут — проверьте единицы")

    if df["p_kpa"].median() < 60 or df["p_kpa"].median() > 105:
        warn.append(f"Давление {df['p_kpa'].median():.1f} кПа выглядит странно")

    ann_p = df.set_index("date")["prcp"].resample("YS").sum()
    ann_p = ann_p[ann_p > 0]
    if len(ann_p) and (ann_p.mean() < 50 or ann_p.mean() > 2000):
        warn.append(f"Годовые осадки {ann_p.mean():.0f} мм — проверьте единицы")

    gaps = df["date"].diff().dt.days
    if (gaps > 1).any():
        warn.append(f"В ряду {int((gaps > 1).sum())} разрывов по датам")

    rl_ratio = df["rl_down"].mean() / df["rs_down"].mean()
    if not 0.8 < rl_ratio < 4.0:
        warn.append(
            f"Отношение Rl↓/Rs↓ = {rl_ratio:.2f} выглядит нетипично. "
            "Rl↓ здесь оценено по Брунту, а не измерено — если результат "
            "важен, берите метео через GEE с прямым полем."
        )
    return warn
