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

# Переменные, без которых расчёт бессмысленен: радиация и ветер — это оба
# слагаемых Пенмана, радиационное и адвективное. Подставлять вместо них
# значения по умолчанию нельзя — расчёт пройдёт и будет неверен втрое.
ESSENTIAL_VARS = ["temperature_2m", "dew_point_2m",
                  "shortwave_radiation", "wind_speed_10m"]

# Open-Meteo Archive: era5_land (9 км) содержит только приземные метеополя.
# Радиации, ветра и давления в нём НЕТ — они есть лишь в era5 (31 км).
# Поэтому базовый запрос идёт к era5, а era5_land накладывается сверху там,
# где даёт лучшее разрешение.
BASE_MODEL = "era5"
REFINE_MODEL = "era5_land"
REFINE_VARS = ["temperature_2m", "dew_point_2m", "precipitation"]


def fetch_archive(lat: float, lon: float, start: str, end: str,
                  model: str | None = None,
                  chunk_years: int = 5,
                  pause_s: float = 1.0) -> pd.DataFrame:
    """
    Забирает почасовой ряд ERA5 и агрегирует до суток.

    Базовый запрос идёт к era5 (полный набор переменных), затем температура и
    осадки уточняются из era5_land, где разрешение втрое лучше. Параметр
    model оставлен для совместимости: если задан явно, используется только он.
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

        base_model = model or BASE_MODEL
        print(f"  Open-Meteo ({base_model}): {s} … {e}")
        base = _request_hourly(lat, lon, s, e, base_model, HOURLY_VARS)

        if base is None:
            raise RuntimeError(
                f"Open-Meteo не вернул пригодных данных за {s} … {e}")

        if model is None:
            try:
                fine = _request_hourly(lat, lon, s, e, REFINE_MODEL, REFINE_VARS)
                if fine is not None and len(fine) == len(base):
                    for v in REFINE_VARS:
                        if v in fine and fine[v].notna().any():
                            base[v] = fine[v].to_numpy()
            except Exception:
                pass    # уточнение необязательно

        frames.append(base)
        time.sleep(pause_s)

    if not frames:
        raise RuntimeError("Open-Meteo не вернул данных — проверьте координаты и даты")

    hourly = pd.concat(frames, ignore_index=True).drop_duplicates(subset="time")
    daily = _aggregate_daily(hourly)
    _validate_essentials(daily)
    return daily


def _request_hourly(lat, lon, start, end, model, variables):
    """Один запрос к Archive API. None, если ключевых переменных нет."""
    import requests

    params = {
        "latitude": lat, "longitude": lon,
        "start_date": start, "end_date": end,
        "hourly": ",".join(variables),
        "models": model,
        "timezone": "UTC",
    }
    r = requests.get(ARCHIVE_URL, params=params, timeout=180)
    if not r.ok:
        reason = r.text[:200]
        try:
            reason = r.json().get("reason", reason)
        except Exception:
            pass
        raise RuntimeError(f"Open-Meteo отклонил запрос ({model}): {reason}")

    hourly = r.json().get("hourly")
    if not hourly or "time" not in hourly:
        return None

    # API может добавлять суффикс модели к именам полей
    df = pd.DataFrame({"time": pd.to_datetime(hourly["time"])})
    for v in variables:
        col = None
        for cand in (v, f"{v}_{model}"):
            if cand in hourly:
                col = cand
                break
        if col is None:
            col = next((k for k in hourly if k.startswith(v)), None)
        df[v] = hourly[col] if col else np.nan

    for v in variables:
        if v in ESSENTIAL_VARS and df[v].isna().all():
            return None
    return df


def _validate_essentials(daily: pd.DataFrame) -> None:
    """
    Ключевые переменные обязаны СОДЕРЖАТЬ СИГНАЛ, а не просто присутствовать.

    Нулевая радиация или постоянный ветер выглядят как нормальные числа и
    проходят любую проверку на конечность. Но радиация, равная нулю круглый
    год, — это не «мало солнца», а отсутствующие данные, и расчёт по ним
    занижает испарение втрое.
    """
    problems = []

    rs_per_year = daily["rs_down"].mean() * 365
    if rs_per_year < 1000:
        problems.append(
            f"солнечная радиация {rs_per_year:.0f} МДж/м²/год "
            "(норма 3000–7000) — данных фактически нет")

    if daily["u10"].round(3).nunique() <= 2:
        problems.append("скорость ветра постоянна — данных нет")

    if daily["ta"].max() - daily["ta"].min() < 5:
        problems.append(f"размах температуры всего "
                        f"{daily['ta'].max() - daily['ta'].min():.1f} °C")

    if problems:
        raise RuntimeError(
            "Полученные данные непригодны для расчёта: "
            + "; ".join(problems)
            + ". Возможно, Open-Meteo временно недоступен или для этой точки "
              "нет покрытия.")


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
