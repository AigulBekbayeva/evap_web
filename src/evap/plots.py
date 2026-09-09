"""Графики. Минимальный набор для контроля и отчёта."""

from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def _save(fig, outdir, name):
    Path(outdir).mkdir(parents=True, exist_ok=True)
    p = Path(outdir) / name
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return p


def plot_timeseries(df, outdir="data/outputs", years=None):
    """Суточный ход испарения и температуры воды."""
    d = df if years is None else df[df["date"].dt.year.isin(years)]

    fig, ax = plt.subplots(3, 1, figsize=(13, 9), sharex=True)

    ax[0].plot(d["date"], d["E_penman"], lw=0.8, label="Пенман (со спутн. Tw)")
    ax[0].plot(d["date"], d["E_masstransfer"], lw=0.7, alpha=0.7,
               label="массоперенос")
    ax[0].plot(d["date"], d["E_nosat"], lw=0.7, alpha=0.6, ls="--",
               label="Пенман без спутника")
    ax[0].set_ylabel("E, мм/сут")
    ax[0].legend(fontsize=8, ncol=3)
    ax[0].grid(alpha=0.3)

    ax[1].plot(d["date"], d["ta"], lw=0.7, alpha=0.6, label="воздух")
    ax[1].plot(d["date"], d["tw"], lw=1.0, label="вода (модель)")
    obs = d.dropna(subset=["tw_obs"])
    ax[1].scatter(obs["date"], obs["tw_obs"], s=6, zorder=5,
                  label="вода (наблюдения)")
    ax[1].set_ylabel("T, °C")
    ax[1].legend(fontsize=8, ncol=3)
    ax[1].grid(alpha=0.3)

    ax[2].plot(d["date"], d["Rn"], lw=0.8, label="Rn")
    ax[2].plot(d["date"], d["G"], lw=0.8, label="G (теплозапас)")
    ax[2].axhline(0, color="k", lw=0.5)
    ax[2].set_ylabel("МДж/м²/сут")
    ax[2].legend(fontsize=8)
    ax[2].grid(alpha=0.3)

    fig.suptitle("Испарение, температура и энергобаланс")
    return _save(fig, outdir, "timeseries.png")


def plot_climatology(clim, outdir="data/outputs"):
    """Средний годовой ход и сравнение методов."""
    fig, ax = plt.subplots(1, 2, figsize=(13, 4.5))

    m = clim["month"]
    ax[0].bar(m - 0.2, clim["E_mean"], width=0.4, label="Пенман")
    ax[0].bar(m + 0.2, clim["E_mt_mean"], width=0.4, label="массоперенос")
    ax[0].errorbar(m - 0.2, clim["E_mean"], yerr=clim["E_sd"],
                   fmt="none", ecolor="k", capsize=2, lw=0.8)
    ax[0].plot(m, clim["P_mean"], "o-", color="tab:green", label="осадки")
    ax[0].set_xlabel("месяц"); ax[0].set_ylabel("мм/мес")
    ax[0].set_title("Средний годовой ход")
    ax[0].legend(fontsize=8); ax[0].grid(alpha=0.3)

    ax[1].plot(m, clim["sat_effect_mm"], "o-",
               label="вклад спутниковой Tw")
    ax[1].plot(m, clim["method_diff_mm"], "s-",
               label="Пенман − массоперенос")
    ax[1].plot(m, clim["G_mean"] * 30, "^-", alpha=0.6,
               label="G, МДж/м²/мес × усл.")
    ax[1].axhline(0, color="k", lw=0.5)
    ax[1].set_xlabel("месяц"); ax[1].set_ylabel("мм/мес")
    ax[1].set_title("Диагностика расхождений")
    ax[1].legend(fontsize=8); ax[1].grid(alpha=0.3)

    return _save(fig, outdir, "climatology.png")


def plot_annual(annual, outdir="data/outputs"):
    """Межгодовая динамика."""
    fig, ax = plt.subplots(figsize=(11, 4.5))
    yrs = annual["date"].dt.year

    ax.plot(yrs, annual["E_penman"], "o-", label="испарение")
    ax.plot(yrs, annual["P"], "s-", label="осадки")
    ax.fill_between(yrs, annual["P"], annual["E_penman"],
                    alpha=0.15, label="дефицит")

    if len(annual) > 4:
        z = np.polyfit(yrs, annual["E_penman"], 1)
        ax.plot(yrs, np.polyval(z, yrs), "--", color="k", lw=1,
                label=f"тренд {z[0]:+.1f} мм/год")

    ax.set_xlabel("год"); ax.set_ylabel("мм/год")
    ax.set_title("Годовые суммы")
    ax.legend(fontsize=8); ax.grid(alpha=0.3)
    return _save(fig, outdir, "annual.png")


def plot_forecast(fc, outdir="data/outputs"):
    """Прогноз на 10 суток."""
    fig, ax = plt.subplots(2, 1, figsize=(10, 6), sharex=True)

    ax[0].bar(fc["date"], fc["E"], width=0.7, label="E, мм/сут")
    ax2 = ax[0].twinx()
    ax2.plot(fc["date"], fc["E_cum"], "o-", color="tab:red",
             label="накопленное")
    ax2.set_ylabel("накопл., мм")
    ax[0].set_ylabel("E, мм/сут")
    ax[0].set_title("Прогноз испарения")
    ax[0].grid(alpha=0.3)

    ax[1].plot(fc["date"], fc["ta"], "o-", label="воздух")
    ax[1].plot(fc["date"], fc["tw"], "s-", label="вода")
    ax3 = ax[1].twinx()
    ax3.bar(fc["date"], fc["u10"], alpha=0.25, color="grey")
    ax3.set_ylabel("ветер, м/с")
    ax[1].set_ylabel("T, °C")
    ax[1].legend(fontsize=8); ax[1].grid(alpha=0.3)

    return _save(fig, outdir, "forecast.png")
