"""
Валидация без наземных измерений.

Прямых данных по испарению нет и не будет, поэтому проверка строится на
внутренней согласованности. Работающие подходы:

  1. Два метода с НЕЗАВИСИМЫМИ источниками ошибок (Пенман vs массоперенос).
     Годовые суммы совпадают по построению — информативно расхождение
     ПО МЕСЯЦАМ: оно локализует проблему.
  2. Кросс-сенсорная сверка Landsat ↔ MODIS.
  3. Замыкание годового цикла теплозапаса: за полный год сумма G должна быть
     близка к нулю, иначе озеро бесконечно греется или остывает.
  4. Проверка на воспроизведение известных закономерностей.
  5. Сопоставление годовой суммы с региональными аналогами.

Ни одна из проверок не доказывает правильность абсолютных значений. Все
вместе они отсекают грубые ошибки — это максимум достижимого без проб.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def method_agreement(df: pd.DataFrame) -> pd.DataFrame:
    """
    Помесячное расхождение Пенмана и массопереноса.

    Интерпретация систематических расхождений:
      весной E_penman > E_mt   → вероятно завышен теплозапас (глубина мала)
      летом  E_penman < E_mt   → возможно занижена радиация или завышено альбедо
      зимой  большое расхождение → проверьте маску льда
    """
    d = df.copy()
    d["month"] = d["date"].dt.month

    m = (d.groupby([d["date"].dt.year.rename("year"), "month"])
         .agg(E_penman=("E_penman", "sum"),
              E_mt=("E_masstransfer", "sum"))
         .reset_index())

    clim = (m.groupby("month")
            .agg(E_penman=("E_penman", "mean"),
                 E_mt=("E_mt", "mean"))
            .reset_index())
    clim["diff_mm"] = clim["E_penman"] - clim["E_mt"]

    # Процент считается только там, где испарение существенно. Зимой E близко
    # к нулю, и относительное расхождение уходит в сотни процентов при
    # физически ничтожной абсолютной разнице — такие числа вводят в заблуждение.
    significant = clim["E_penman"] > 10.0
    clim["diff_pct"] = np.where(
        significant, 100 * clim["diff_mm"] / clim["E_penman"], np.nan)
    return clim


def heat_storage_closure(df: pd.DataFrame, tol_pct: float = 5.0) -> pd.DataFrame:
    """
    Годовая сумма G должна быть близка к нулю.

    Физически озеро за год возвращается примерно в то же тепловое состояние.
    Ненулевая сумма означает либо реальный тренд температуры воды, либо —
    гораздо чаще — артефакт интерполяции Tw или ошибку в глубине.

    Нормировка на Σ|G|, а не на ΣRn: годовой ход теплозапаса на порядок
    превышает годовой радиационный баланс по абсолютной величине, поэтому
    отношение к Rn преувеличивает незамыкание. Содержательный вопрос —
    какая доля общего оборота тепла осталась незамкнутой.
    """
    a = (df.set_index("date").resample("YS")
         .agg(G_sum=("G", "sum"),
              G_abs=("G", lambda x: x.abs().sum()),
              Rn_sum=("Rn", "sum"),
              tw_mean=("tw", "mean"),
              n=("G", "size"))
         .reset_index())
    a = a[a["n"] > 350].copy()

    a["G_over_absG_pct"] = 100 * a["G_sum"] / a["G_abs"].replace(0, np.nan)
    a["G_over_Rn_pct"] = 100 * a["G_sum"] / a["Rn_sum"].replace(0, np.nan)
    a["ok"] = a["G_over_absG_pct"].abs() < tol_pct
    return a


def physical_checks(df: pd.DataFrame) -> list[dict]:
    """
    Проверка на воспроизведение известных закономерностей.

    Если модель их не воспроизводит, она неверна независимо от формальных
    метрик согласия.
    """
    checks = []
    d = df.dropna(subset=["E_penman", "u10", "ta", "tw"])

    # 1. Испарение растёт с ветром при прочих равных
    warm = d[(d["ta"] > 15) & (~d["ice"])]
    if len(warm) > 100:
        r = float(np.corrcoef(warm["u10"], warm["E_penman"])[0, 1])
        checks.append({
            "check": "E растёт с ветром (тёплый сезон)",
            "value": round(r, 3),
            "expected": "> 0.3",
            "ok": r > 0.3,
        })

    # 2. Летний максимум испарения
    monthly = d.groupby(d["date"].dt.month)["E_penman"].mean()
    peak = int(monthly.idxmax())
    checks.append({
        "check": "месяц максимума испарения",
        "value": peak,
        "expected": "6–8",
        "ok": peak in (6, 7, 8),
    })

    # 3. Температура воды отстаёт от воздуха (тепловая инерция)
    lag_corr = [float(d["tw"].corr(d["ta"].shift(k))) for k in range(0, 45, 5)]
    best_lag = int(np.argmax(lag_corr) * 5)
    checks.append({
        "check": "лаг Tw за Ta, сут",
        "value": best_lag,
        "expected": "10–40 для водоёма 5–15 м",
        "ok": 5 <= best_lag <= 45,
    })

    # 4. Летом вода теплее воздуха ночью, но холоднее дневного максимума
    summer = d[d["date"].dt.month.isin([6, 7, 8])]
    if len(summer) > 50:
        dt = float((summer["tw"] - summer["ta"]).mean())
        checks.append({
            "check": "Tw − Ta летом, °C",
            "value": round(dt, 2),
            "expected": "−5 … +5",
            "ok": -5 <= dt <= 5,
        })

    # 5. Годовая сумма в разумных пределах для семиаридной зоны
    ann = d.set_index("date").resample("YS")["E_penman"].sum()
    ann = ann[ann > 100]
    if len(ann):
        mean_ann = float(ann.mean())
        checks.append({
            "check": "годовое испарение, мм",
            "value": round(mean_ann),
            "expected": "700–1900 (для юго-востока Казахстана ждём 900–1600)",
            "ok": 700 <= mean_ann <= 1900,
        })

    return checks


def sensor_agreement(landsat: pd.DataFrame, modis: pd.DataFrame,
                     max_days: int = 1) -> dict:
    """
    Сверка Landsat и MODIS на близких датах.

    RMSE до 1.5 K — хорошо, до 2.5 K — приемлемо, выше — есть проблема
    с маской или со смешанными пикселями.
    """
    if landsat.empty or modis.empty:
        return {"n": 0, "note": "недостаточно данных"}

    merged = pd.merge_asof(
        landsat.sort_values("date")[["date", "tw"]],
        modis.sort_values("date")[["date", "tw"]],
        on="date", suffixes=("_ls", "_md"),
        tolerance=pd.Timedelta(days=max_days), direction="nearest").dropna()

    if len(merged) < 5:
        return {"n": len(merged), "note": "мало совпадающих дат"}

    diff = merged["tw_ls"] - merged["tw_md"]
    return {
        "n": len(merged),
        "bias_k": round(float(diff.mean()), 3),
        "rmse_k": round(float(np.sqrt((diff ** 2).mean())), 3),
        "r": round(float(merged["tw_ls"].corr(merged["tw_md"])), 4),
        "ok": bool(np.sqrt((diff ** 2).mean()) < 2.5),
    }


def satellite_contribution(df: pd.DataFrame) -> dict:
    """
    Насколько спутниковая Tw меняет результат по сравнению с Tw = Ta, G = 0.

    Ожидание: годовые суммы расходятся на 5–15 %, а сезонный ход — сильнее,
    со сдвигом месяца пика. Если расхождение по месяцам мало, значит
    теплозапас не работает — проверьте глубину и интерполяцию Tw.

    Все величины по МЕСЯЧНЫМ СУММАМ (мм/мес), а не по суточным средним:
    иначе цифры выглядят обманчиво малыми.
    """
    d = df.dropna(subset=["E_penman", "E_nosat"]).set_index("date")

    ann = d.resample("YS").agg(p=("E_penman", "sum"), n=("E_nosat", "sum"),
                               days=("E_penman", "size"))
    ann = ann[ann["days"] > 350]

    mon = d.resample("MS").agg(p=("E_penman", "sum"), n=("E_nosat", "sum"))
    mon["month"] = mon.index.month
    clim = mon.groupby("month").agg(p=("p", "mean"), n=("n", "mean"))
    diff = clim["p"] - clim["n"]

    return {
        "annual_penman_mm": round(float(ann["p"].mean()), 1),
        "annual_nosat_mm": round(float(ann["n"].mean()), 1),
        "annual_diff_pct": round(float(
            100 * (ann["p"].mean() - ann["n"].mean()) / ann["n"].mean()), 2),
        "max_monthly_diff_mm": round(float(diff.abs().max()), 1),
        "month_of_max_diff": int(diff.abs().idxmax()),
        "peak_month_penman": int(clim["p"].idxmax()),
        "peak_month_nosat": int(clim["n"].idxmax()),
    }


def report(df: pd.DataFrame, landsat: pd.DataFrame | None = None,
           modis: pd.DataFrame | None = None) -> str:
    """Сводный текстовый отчёт по всем проверкам."""
    lines = ["=" * 66, "ВАЛИДАЦИЯ (без наземных данных)", "=" * 66, ""]

    lines.append("1. Физическая осмысленность")
    for c in physical_checks(df):
        mark = "OK " if c["ok"] else "!! "
        lines.append(f"   {mark}{c['check']}: {c['value']} "
                     f"(ожидается {c['expected']})")

    lines.append("")
    lines.append("2. Замыкание годового цикла теплозапаса")
    hs = heat_storage_closure(df)
    for _, r in hs.iterrows():
        mark = "OK " if r["ok"] else "!! "
        lines.append(f"   {mark}{r['date'].year}: ΣG = {r['G_sum']:+.0f} МДж/м², "
                     f"{r['G_over_absG_pct']:+.1f} % от оборота тепла, "
                     f"{r['G_over_Rn_pct']:+.1f} % от Rn")

    lines.append("")
    lines.append("3. Расхождение Пенман − массоперенос по месяцам")
    ma = method_agreement(df)
    for _, r in ma.iterrows():
        pct = ("     —" if not np.isfinite(r["diff_pct"])
               else f"{r['diff_pct']:+5.1f} %")
        lines.append(f"   м{int(r['month']):02d}: E={r['E_penman']:6.1f} мм, "
                     f"расхождение {r['diff_mm']:+6.1f} мм ({pct})")

    lines.append("")
    lines.append("4. Вклад спутниковой температуры воды")
    sc = satellite_contribution(df)
    lines.append(f"   со спутником {sc['annual_penman_mm']:.0f} мм/год, "
                 f"без него {sc['annual_nosat_mm']:.0f} мм/год "
                 f"({sc['annual_diff_pct']:+.1f} %)")
    lines.append(f"   макс. расхождение по месяцам: {sc['max_monthly_diff_mm']:.1f} мм/мес "
                 f"(месяц {sc['month_of_max_diff']})")
    lines.append(f"   месяц пика: со спутником {sc['peak_month_penman']}, "
                 f"без него {sc['peak_month_nosat']}")

    if landsat is not None and modis is not None:
        lines.append("")
        lines.append("5. Сверка сенсоров Landsat ↔ MODIS")
        sa = sensor_agreement(landsat, modis)
        lines.append(f"   {sa}")

    lines.append("")
    lines.append("=" * 66)
    return "\n".join(lines)
