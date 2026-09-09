#!/usr/bin/env python3
"""
Подбор средней глубины водоёма по спутниковой температуре воды.

ЗАЧЕМ. Глубина Сорбулака в открытых источниках приводится противоречиво, а
теплозапас G = ρw·cw·h·dTw/dt к ней прямо пропорционален. Это главный
свободный параметр расчёта.

ИДЕЯ. Глубину не нужно знать заранее — её можно ВОССТАНОВИТЬ. Простая
модель теплового баланса водной толщи с заданной глубиной даёт свой ход
температуры воды; сравнивая его с наблюдённым спутниковым ходом, подбираем
глубину, при которой они совпадают.

    ρw·cw·h · dTw/dt = Rn − H − LE

Глубокий водоём инерционен: летний пик температуры сдвинут и срезан.
Мелкий — быстро следует за воздухом. Именно по фазовому сдвигу и амплитуде
глубина определяется устойчиво, даже когда абсолютные значения смещены.

ЧТО ЭТО НЕ ДАЁТ. Восстанавливается «эффективная термическая глубина» —
характеристика теплового отклика, а не батиметрическая средняя глубина.
Для расчёта испарения нужна именно она, но подставлять её в расчёт объёма
водоёма нельзя.

Использование:
    python scripts/calibrate_depth.py [--min 2 --max 30 --step 0.5]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evap import physics as ph  # noqa: E402
from evap.config import Config  # noqa: E402


def simulate_tw(df: pd.DataFrame, depth_m: float, tw_init: float) -> np.ndarray:
    """
    Прямое интегрирование теплового баланса водной толщи.

    Явная схема Эйлера с суточным шагом. Для инерционного водоёма устойчива
    при h > 1 м; при меньших глубинах шаг надо дробить.
    """
    n = len(df)
    tw = np.empty(n)
    cur = float(tw_init)

    ta = df["ta"].to_numpy()
    tdew = df["tdew"].to_numpy()
    u10 = df["u10"].to_numpy()
    p = df["p_kpa"].to_numpy()
    rs = df["rs_down"].to_numpy()
    rl = df["rl_down"].to_numpy()

    heat_capacity = ph.RHO_CW * depth_m      # МДж м⁻² К⁻¹

    for i in range(n):
        rn = ph.net_radiation(rs[i], rl[i], cur)

        # Скрытый поток через МАССОПЕРЕНОС, а не через Пенмана.
        # Пенман берёт дефицит по es(Ta) и почти не реагирует на температуру
        # воды; в модели теплового баланса это даёт положительную обратную
        # связь и схлопывание Tw к нулю. Массоперенос использует es(Tw) —
        # давление насыщения при температуре поверхности, — и баланс
        # замыкается на равновесной температуре.
        e_mm = max(float(ph.mass_transfer(cur, tdew[i], u10[i], a=0.0, b=1.4)), 0.0)
        le = e_mm * ph.latent_heat(ta[i])     # МДж м⁻² сут⁻¹

        # явный поток через отношение Боуэна
        gamma = ph.psychrometric_constant(p[i])
        de = ph.sat_vapour_pressure(cur) - ph.sat_vapour_pressure(tdew[i])
        beta = gamma * (cur - ta[i]) / de if abs(de) > 0.05 else 0.0
        beta = float(np.clip(beta, -2.0, 3.0))
        h_sens = beta * le

        cur = cur + (rn - le - h_sens) / heat_capacity
        cur = max(cur, 0.0)                   # лёд не моделируем
        tw[i] = cur

    return tw


def objective(observed: pd.Series, simulated: np.ndarray) -> dict:
    """Метрики согласия там, где есть наблюдения."""
    mask = observed.notna().to_numpy()
    if mask.sum() < 20:
        return {"rmse": np.inf, "bias": np.nan, "r": np.nan, "n": int(mask.sum())}

    obs = observed.to_numpy()[mask]
    sim = simulated[mask]
    resid = sim - obs

    return {
        "rmse": float(np.sqrt((resid ** 2).mean())),
        "bias": float(resid.mean()),
        "r": float(np.corrcoef(obs, sim)[0, 1]),
        "amp_ratio": float(sim.std() / obs.std()),
        "n": int(mask.sum()),
    }


def phase_lag_days(ta: pd.Series, tw: pd.Series, max_lag: int = 60) -> int:
    """Лаг температуры воды за воздухом — самая информативная характеристика."""
    corrs = [ta.shift(k).corr(tw) for k in range(max_lag)]
    return int(np.nanargmax(corrs))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--min", type=float, default=2.0, dest="dmin")
    ap.add_argument("--max", type=float, default=30.0, dest="dmax")
    ap.add_argument("--step", type=float, default=0.5)
    ap.add_argument("--spinup-days", type=int, default=365,
                    help="период раскрутки, исключаемый из оценки")
    args = ap.parse_args()

    cfg = Config.from_yaml(args.config) if Path(args.config).exists() else Config()

    cache = Path(cfg.paths.processed) / "inputs_daily.parquet"
    if not cache.exists():
        sys.exit(f"Нет {cache}. Сначала выполните `evap run`, "
                 "чтобы выгрузить входные данные.")

    df = pd.read_parquet(cache).sort_values("date").reset_index(drop=True)
    need = ["ta", "tdew", "u10", "p_kpa", "rs_down", "rl_down", "tw_obs"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        sys.exit(f"В данных нет колонок: {missing}")

    n_obs = int(df["tw_obs"].notna().sum())
    print(f"Наблюдений Tw: {n_obs} за {len(df)} суток "
          f"({100 * n_obs / len(df):.1f} %)")
    if n_obs < 50:
        print("! Мало наблюдений — результат калибровки будет неустойчив.")

    tw_init = float(df["tw_obs"].dropna().iloc[0])
    depths = np.arange(args.dmin, args.dmax + 1e-9, args.step)

    print(f"\nПеребор глубин {args.dmin}–{args.dmax} м с шагом {args.step} м")
    print(f"Раскрутка: первые {args.spinup_days} сут исключены из оценки\n")
    print(f"{'h, м':>6} {'RMSE, K':>9} {'смещение':>10} "
          f"{'r':>7} {'ампл.':>7} {'лаг, сут':>9}")
    print("-" * 54)

    results = []
    eval_slice = slice(args.spinup_days, None)

    for h in depths:
        sim = simulate_tw(df, h, tw_init)
        m = objective(df["tw_obs"].iloc[eval_slice],
                      sim[eval_slice])
        lag = phase_lag_days(df["ta"].iloc[eval_slice],
                             pd.Series(sim[eval_slice]))
        m["depth"] = float(h)
        m["lag"] = lag
        results.append(m)

        if abs(h - round(h)) < 1e-9 or h == depths[0]:
            print(f"{h:6.1f} {m['rmse']:9.3f} {m['bias']:+10.3f} "
                  f"{m['r']:7.3f} {m['amp_ratio']:7.3f} {lag:9d}")

    res = pd.DataFrame(results)
    best = res.loc[res["rmse"].idxmin()]

    print("\n" + "=" * 54)
    print(f"Лучшая глубина: {best['depth']:.1f} м")
    print(f"  RMSE      {best['rmse']:.3f} K")
    print(f"  смещение  {best['bias']:+.3f} K")
    print(f"  r         {best['r']:.3f}")
    print(f"  амплитуда модель/наблюдения  {best['amp_ratio']:.3f}")
    print(f"  лаг за воздухом  {int(best['lag'])} сут")
    print("=" * 54)

    # насколько остро выражен минимум — мера надёжности калибровки
    within = res[res["rmse"] < best["rmse"] * 1.05]
    print(f"\nГлубины в пределах 5 % от лучшего RMSE: "
          f"{within['depth'].min():.1f}–{within['depth'].max():.1f} м")
    if within["depth"].max() - within["depth"].min() > 8:
        print("! Минимум пологий — глубина определяется плохо. Испарение к ней\n"
              "  малочувствительно по годовой сумме, но сезонный ход остаётся\n"
              "  неопределённым. Ищите независимую оценку глубины.")

    if best["rmse"] > 3.0:
        print("! RMSE выше 3 K. Вероятные причины: смешанные пиксели у берега\n"
              "  (увеличьте erode_metres), ошибка в маске льда, или ячейка\n"
              "  ERA5-Land не репрезентативна для акватории.")

    if abs(best["amp_ratio"] - 1.0) > 0.25:
        direction = "завышает" if best["amp_ratio"] > 1 else "занижает"
        print(f"! Модель {direction} амплитуду годового хода в "
              f"{best['amp_ratio']:.2f} раза.\n"
              "  Проверьте радиационный баланс и альбедо.")

    out = Path(cfg.paths.outputs) / "depth_calibration.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    res.to_csv(out, index=False)
    print(f"\nПолная кривая сохранена: {out}")
    print(f"Внесите в config/config.yaml:  mean_depth_m: {best['depth']:.1f}")


if __name__ == "__main__":
    main()
