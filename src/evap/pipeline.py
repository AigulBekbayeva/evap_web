"""
Сборка расчёта: геометрия → метео → температура воды → испарение → баланс.

Earth Engine здесь не вызывается. Метеорология идёт через Open-Meteo Archive
API, температура воды читается из CSV, выгруженных gee/export_data.js через
веб-редактор.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import ingest as ingest_mod
from . import lswt as lswt_mod
from . import openmeteo as om_mod
from . import physics as ph
from . import waterbody as wb


def build_dataset(cfg) -> pd.DataFrame:
    """
    Выгружает и объединяет все входные данные в один суточный DataFrame.

    Результат кэшируется в data/processed — повторные запуски не дёргают
    Open-Meteo заново.
    """
    cache = Path(cfg.paths.processed) / "inputs_daily.parquet"
    if cache.exists() and not cfg.force_download:
        print(f"Читаю кэш {cache}")
        return pd.read_parquet(cache)

    gdf = wb.load_lake(shapes_dir=cfg.paths.shapes)
    info = wb.describe(gdf)
    print(f"Озеро: площадь {info['area_km2']} км², "
          f"центроид {info['centroid_lat']:.4f}N {info['centroid_lon']:.4f}E, "
          f"частей {info['n_parts']}")

    n_cells = info["area_km2"] / 81.0
    if n_cells < 1.5:
        print(f"  ! Озеро занимает ~{n_cells:.1f} ячейки ERA5-Land (9 км).\n"
              "    Метеоданные будут частично или полностью с суши: воздух\n"
              "    там суше и теплее, поэтому испарение окажется завышенным.\n"
              "    Это свойство исходных данных, а не способа их получения.")

    # ---------- метеорология ----------
    print("Выгружаю ERA5 через Open-Meteo…")
    met = om_mod.fetch_archive(info["centroid_lat"], info["centroid_lon"],
                               cfg.start, cfg.end[:10], model=cfg.era5_model)

    for w in om_mod.sanity_check(met):
        print(f"  ! {w}")

    # ---------- температура воды ----------
    print("Читаю температуру воды из data/raw…")
    cached = ingest_mod.load_lswt(cfg.paths.raw)
    ls = cached.get("landsat", pd.DataFrame())
    md = cached.get("modis", pd.DataFrame())

    if ls.empty and md.empty:
        raise SystemExit(
            "\nНет данных о температуре воды.\n\n"
            "Без неё расчёт вырождается в вариант «Tw = Ta, G = 0», который\n"
            "не воспроизводит сезонный ход. Выгрузите её:\n\n"
            "  1. Откройте gee/export_data.js в code.earthengine.google.com\n"
            "  2. Подставьте свой контур, Run\n"
            "  3. Вкладка Tasks справа → запустить задачи\n"
            f"  4. Скачайте CSV из Google Drive в {cfg.paths.raw}/\n\n"
            "Проверить, что дошло: evap status\n")

    combined, harm_info = lswt_mod.harmonise(ls, md)
    print(f"  сведение сенсоров: {harm_info}")

    tw_daily = lswt_mod.to_daily(combined, met["date"],
                                 method=cfg.tw_interp_method)

    df = met.merge(tw_daily[["date", "tw", "tw_obs", "tw_filled"]],
                   on="date", how="left")

    area = ingest_mod.load_water_area(cfg.paths.raw)
    df.attrs["area_km2"] = (float(area["area_km2"].mean()) if area is not None
                            else info["area_km2"])
    df.attrs["lswt_harmonisation"] = harm_info

    Path(cfg.paths.processed).mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache, index=False)
    print(f"Сохранено: {cache}")
    return df


def compute_evaporation(df: pd.DataFrame, cfg) -> pd.DataFrame:
    """
    Расчёт испарения тремя методами.

    Пенман — основной. Массоперенос — независимый контроль. Пристли–Тейлор —
    диагностика адвективного вклада. Дополнительно считается вариант «без
    спутника» (Tw = Ta, G = 0) для оценки того, что даёт дистанционное
    зондирование.
    """
    out = df.copy()

    u2 = ph.wind_at_2m(out["u10"])

    # глубина перемешанного слоя
    if cfg.mixed_depth_source == "constant":
        h_mix = np.full(len(out), cfg.mean_depth_m)
    else:
        h_mix = ph.mixed_layer_depth(out["tw"], cfg.mean_depth_m, u2)
    out["h_mix"] = h_mix

    out["G"] = ph.heat_storage(out["tw"], h_mix, dt_days=1.0,
                               smooth_window=cfg.tw_smooth_days)

    albedo = ph.ALBEDO_WATER
    if "bloom_fraction" in out.columns:
        albedo = ph.albedo_from_bloom(out["bloom_fraction"])
    out["albedo"] = albedo

    out["Rn"] = ph.net_radiation(out["rs_down"], out["rl_down"],
                                 out["tw"], albedo=albedo)

    # --- основной метод ---
    out["E_penman"] = ph.penman_openwater(
        out["ta"], out["tdew"], out["u10"], out["p_kpa"],
        out["rs_down"], out["rl_down"],
        tw_c=out["tw"], g=out["G"], albedo=albedo)

    # --- контроль: массоперенос, откалиброванный по сумме Пенмана ---
    a, b = ph.calibrate_mass_transfer(
        out["E_penman"].to_numpy(), out["tw"].to_numpy(),
        out["tdew"].to_numpy(), out["u10"].to_numpy())
    out["E_masstransfer"] = ph.mass_transfer(out["tw"], out["tdew"],
                                             out["u10"], a=a, b=b)
    out.attrs["mass_transfer_coeffs"] = {"a": a, "b": b}

    # --- диагностика ---
    out["E_priestley"] = ph.priestley_taylor(
        out["ta"], out["p_kpa"], out["rs_down"], out["rl_down"],
        tw_c=out["tw"], g=out["G"], albedo=albedo)

    # --- вариант без спутника: сколько даёт LSWT ---
    out["E_nosat"] = ph.penman_openwater(
        out["ta"], out["tdew"], out["u10"], out["p_kpa"],
        out["rs_down"], out["rl_down"], tw_c=None, g=0.0)

    out["bowen"] = ph.bowen_ratio(out["ta"], out["tw"], out["tdew"], out["p_kpa"])

    # подо льдом испарение с открытой воды не считается
    ice = lswt_mod.ice_flag(out["tw"], out["ta"])
    out["ice"] = ice
    for col in ["E_penman", "E_masstransfer", "E_priestley", "E_nosat"]:
        out.loc[ice, col] = 0.0
        out[col] = out[col].clip(lower=0.0)

    return out


def water_balance(df: pd.DataFrame, area_km2: float,
                  volume_series: pd.DataFrame | None = None) -> pd.DataFrame:
    """
    Месячный водный баланс.

        ΔV = Q_сброс + P·A − E·A − F_фильтрация

    Без ряда объёма считаются только известные члены (осадки и испарение),
    и колонка residual остаётся пустой — её заполняет пользователь, добавив
    ряд объёма из альтиметрии и кривой «площадь–уровень».

    Практический ход: зимой при Tw ≈ 0…4 °C испарение мало и осадки малы,
    поэтому ΔV ≈ Q − F. Это самое чистое окно для разделения членов.
    """
    area_m2 = area_km2 * 1e6

    m = (df.set_index("date")
         .resample("MS")
         .agg(E_mm=("E_penman", "sum"),
              E_mt_mm=("E_masstransfer", "sum"),
              E_nosat_mm=("E_nosat", "sum"),
              P_mm=("prcp", "sum"),
              ta=("ta", "mean"),
              tw=("tw", "mean"),
              ice_days=("ice", "sum"))
         .reset_index())

    m["E_m3"] = m["E_mm"] * 1e-3 * area_m2
    m["P_m3"] = m["P_mm"] * 1e-3 * area_m2
    m["net_atm_m3"] = m["P_m3"] - m["E_m3"]

    if volume_series is not None:
        v = (volume_series.set_index("date").resample("MS")
             .agg(V_m3=("volume_m3", "mean")).reset_index())
        m = m.merge(v, on="date", how="left")
        m["dV_m3"] = m["V_m3"].diff()
        # Q − F = ΔV − (P − E)·A
        m["Q_minus_F_m3"] = m["dV_m3"] - m["net_atm_m3"]
    else:
        m["dV_m3"] = np.nan
        m["Q_minus_F_m3"] = np.nan

    return m


def annual_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Годовые суммы и сравнение методов."""
    a = (df.set_index("date").resample("YS")
         .agg(E_penman=("E_penman", "sum"),
              E_masstransfer=("E_masstransfer", "sum"),
              E_priestley=("E_priestley", "sum"),
              E_nosat=("E_nosat", "sum"),
              P=("prcp", "sum"),
              ta=("ta", "mean"),
              tw=("tw", "mean"),
              u10=("u10", "mean"),
              n_days=("ta", "size"),
              tw_filled=("tw_filled", "sum"))
         .reset_index())

    a = a[a["n_days"] > 350]  # только полные годы
    a["E_minus_P"] = a["E_penman"] - a["P"]
    a["sat_effect_pct"] = 100 * (a["E_penman"] - a["E_nosat"]) / a["E_nosat"]
    a["method_diff_pct"] = 100 * (a["E_penman"] - a["E_masstransfer"]) / a["E_penman"]
    return a


def monthly_climatology(df: pd.DataFrame) -> pd.DataFrame:
    """Средний многолетний годовой ход — главный диагностический продукт."""
    d = df.copy()
    d["month"] = d["date"].dt.month
    d["year"] = d["date"].dt.year

    monthly = (d.groupby(["year", "month"])
               .agg(E_penman=("E_penman", "sum"),
                    E_masstransfer=("E_masstransfer", "sum"),
                    E_nosat=("E_nosat", "sum"),
                    G=("G", "mean"),
                    Rn=("Rn", "mean"),
                    P=("prcp", "sum"))
               .reset_index())

    clim = (monthly.groupby("month")
            .agg(E_mean=("E_penman", "mean"),
                 E_sd=("E_penman", "std"),
                 E_mt_mean=("E_masstransfer", "mean"),
                 E_nosat_mean=("E_nosat", "mean"),
                 G_mean=("G", "mean"),
                 Rn_mean=("Rn", "mean"),
                 P_mean=("P", "mean"))
            .reset_index())
    clim["method_diff_mm"] = clim["E_mean"] - clim["E_mt_mean"]
    clim["sat_effect_mm"] = clim["E_mean"] - clim["E_nosat_mean"]
    return clim
