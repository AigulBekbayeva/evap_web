"""
Температура поверхности воды (LSWT).

Единственная величина, которую реанализ не знает и которую даёт только
дистанционное зондирование. Входит в расчёт дважды:

  1. в длинноволновое излучение вверх ε·σ·Tw⁴ (радиационный баланс);
  2. в теплозапас G = ρw·cw·h·dTw/dt.

ОТКУДА БЕРУТСЯ ДАННЫЕ. Из CSV, выгруженных скриптом gee/export_data.js
через веб-редактор Earth Engine. Python-клиент GEE здесь намеренно не
используется: getInfo() считает граф синхронно и упирается в лимит памяти
уже на нескольких годах, тогда как Export.table.toDrive ставит задачу в
очередь на серверах Google, где лимиты на порядки выше.

Источники в CSV:
  Landsat 8/9 TIRS — 30 м, раз в 8 суток при двух аппаратах, точнее
  MODIS Terra/Aqua — 1 км, 4 срока в сутки, чаще, но грубее

Ночные значения MODIS предпочтительнее дневных: нет прогрева тонкой
поверхностной плёнки (skin effect), и Tw ближе к температуре перемешанного
слоя.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def harmonise(landsat: pd.DataFrame, modis: pd.DataFrame,
              min_overlap: int = 10) -> tuple[pd.DataFrame, dict]:
    """
    Сведение MODIS к шкале Landsat по совпадающим датам.

    Landsat принимается за опорный: 30 м против 1 км, меньше смешанных
    пикселей. Регрессия строится на днях, когда сняли оба сенсора.

    Возвращает объединённый ряд и параметры приведения — их надо смотреть:
    наклон, сильно отличный от 1, означает проблему с одним из сенсоров.
    """
    if landsat.empty:
        return modis.assign(adjusted=False), {"n_overlap": 0}
    if modis.empty:
        return landsat.assign(adjusted=False), {"n_overlap": 0}

    merged = pd.merge(landsat[["date", "tw"]], modis[["date", "tw"]],
                      on="date", suffixes=("_ls", "_md"))

    info = {"n_overlap": len(merged)}

    if len(merged) < min_overlap:
        info["note"] = "мало совпадений, MODIS взят без приведения"
        combined = pd.concat([landsat, modis], ignore_index=True)
    else:
        slope, intercept = np.polyfit(merged["tw_md"], merged["tw_ls"], 1)
        resid = merged["tw_ls"] - (slope * merged["tw_md"] + intercept)
        info.update({
            "slope": round(float(slope), 4),
            "intercept": round(float(intercept), 4),
            "rmse_k": round(float(np.sqrt((resid ** 2).mean())), 3),
            "r": round(float(np.corrcoef(merged["tw_md"], merged["tw_ls"])[0, 1]), 4),
        })
        md = modis.copy()
        md["tw"] = slope * md["tw"] + intercept
        combined = pd.concat([landsat, md], ignore_index=True)

    combined = (combined.sort_values("date")
                .groupby("date", as_index=False)
                .agg(tw=("tw", "mean"), source=("source", "first")))
    return combined, info


def to_daily(obs: pd.DataFrame, dates: pd.Series,
             method: str = "harmonic") -> pd.DataFrame:
    """
    Восстановление непрерывного суточного ряда Tw из редких наблюдений.

    Безоблачных снимков 40–60 %, поэтому интерполяция обязательна — от неё
    напрямую зависит теплозапас G.

    method:
      'harmonic' — годовая + полугодовая гармоника, остатки восстанавливаются
                   линейно. Устойчиво, не даёт выбросов в длинных пропусках.
      'linear'   — простая линейная интерполяция. Годится при плотном ряде.

    Для итогового расчёта интерполяцию лучше заменить выходом 1D-модели
    (Simstrat / GLM / FLake) — это физически согласованное заполнение.
    """
    base = pd.DataFrame({"date": pd.to_datetime(pd.Series(dates).unique())})
    base = base.sort_values("date").reset_index(drop=True)
    df = base.merge(obs[["date", "tw"]], on="date", how="left")

    if method == "linear":
        df["tw"] = df["tw"].interpolate(limit_direction="both")
        df["tw_filled"] = df["tw"].isna()
        return df

    # гармоническая модель сезонного хода
    doy = df["date"].dt.dayofyear.to_numpy(dtype=float)
    w = 2 * np.pi * doy / 365.25
    X = np.column_stack([np.ones_like(w), np.sin(w), np.cos(w),
                         np.sin(2 * w), np.cos(2 * w)])

    have = df["tw"].notna().to_numpy()
    if have.sum() < 20:
        raise ValueError(
            f"Всего {have.sum()} наблюдений Tw — недостаточно для гармонической "
            "модели. Расширьте период или используйте method='linear'."
        )

    coef, *_ = np.linalg.lstsq(X[have], df.loc[have, "tw"].to_numpy(), rcond=None)
    seasonal = X @ coef

    resid = np.full(len(df), np.nan)
    resid[have] = df.loc[have, "tw"].to_numpy() - seasonal[have]
    resid = pd.Series(resid).interpolate(limit_direction="both").to_numpy()

    out = df.copy()
    out["tw_obs"] = df["tw"]
    out["tw"] = seasonal + resid
    out["tw_filled"] = ~have

    # вода не бывает холоднее точки замерзания
    out["tw"] = out["tw"].clip(lower=0.0)
    return out


def ice_flag(tw: pd.Series, ta: pd.Series, threshold: float = 1.0) -> pd.Series:
    """
    Грубый признак ледостава: холодная вода при отрицательном воздухе.

    Для точных дат замерзания и вскрытия нужен Sentinel-1 — радар не зависит
    от облачности. Здесь заглушка, чтобы обнулять испарение подо льдом.
    """
    return (tw < threshold) & (ta.rolling(5, min_periods=1).mean() < 0)
