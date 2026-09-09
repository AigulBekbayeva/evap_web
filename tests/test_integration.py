"""
Интеграционные тесты на синтетическом годовом ряде.

Проверяют, что связка «метео → температура воды → испарение → баланс»
даёт физически осмысленный результат. Реальных данных не требуют, поэтому
работают в CI без доступа к Earth Engine.
"""

import numpy as np
import pandas as pd
import pytest

from evap import physics as ph
from evap import pipeline, validate
from evap.config import Config


@pytest.fixture
def synthetic_years():
    """Три года правдоподобной погоды для семиаридной зоны 43.9° с.ш."""
    rng = np.random.default_rng(7)
    dates = pd.date_range("2020-01-01", "2022-12-31", freq="D")
    n = len(dates)
    doy = dates.dayofyear.to_numpy(dtype=float)
    w = 2 * np.pi * doy / 365.25

    ta = 10.0 + 15.0 * np.sin(w - 1.9) + rng.normal(0, 3.0, n)
    tdew = ta - (6.0 + 6.0 * np.clip(np.sin(w - 1.9), 0, 1)) + rng.normal(0, 1.5, n)
    rs = np.clip(14.0 + 11.0 * np.sin(w - 1.9), 1.5, None) * rng.uniform(0.55, 1.0, n)
    rl = 22.0 + 8.0 * np.sin(w - 1.9) + rng.normal(0, 1.0, n)
    u10 = np.abs(rng.gamma(4.0, 0.9, n))
    prcp = rng.gamma(0.4, 2.0, n) * (rng.random(n) < 0.22)

    # вода отстаёт от воздуха примерно на месяц и имеет меньшую амплитуду
    tw = np.clip(11.5 + 13.0 * np.sin(w - 2.25), 0.0, None)

    return pd.DataFrame({
        "date": dates, "ta": ta, "tdew": tdew, "p_kpa": np.full(n, 92.0),
        "u10": u10, "rs_down": rs, "rl_down": rl, "prcp": prcp,
        "tw": tw, "tw_obs": np.where(np.arange(n) % 8 == 0, tw, np.nan),
        "tw_filled": np.arange(n) % 8 != 0,
    })


class TestComputeEvaporation:

    def test_runs_and_produces_columns(self, synthetic_years):
        cfg = Config(mean_depth_m=10.0)
        res = pipeline.compute_evaporation(synthetic_years, cfg)

        for col in ["E_penman", "E_masstransfer", "E_priestley",
                    "E_nosat", "G", "Rn", "h_mix", "ice"]:
            assert col in res.columns
        assert res["E_penman"].notna().all()

    def test_annual_total_plausible(self, synthetic_years):
        """Годовая сумма для семиаридной зоны: 700–1900 мм."""
        cfg = Config(mean_depth_m=10.0)
        res = pipeline.compute_evaporation(synthetic_years, cfg)
        ann = pipeline.annual_summary(res)
        assert ann["E_penman"].between(700, 1900).all()

    def test_no_negative_evaporation(self, synthetic_years):
        cfg = Config(mean_depth_m=10.0)
        res = pipeline.compute_evaporation(synthetic_years, cfg)
        assert (res["E_penman"] >= 0).all()

    def test_summer_exceeds_winter(self, synthetic_years):
        cfg = Config(mean_depth_m=10.0)
        res = pipeline.compute_evaporation(synthetic_years, cfg)
        m = res.groupby(res["date"].dt.month)["E_penman"].mean()
        assert m.loc[[6, 7, 8]].mean() > 4 * m.loc[[12, 1, 2]].mean()

    def test_heat_storage_closes_annually(self, synthetic_years):
        """Ключевая проверка: за год озеро не должно накапливать тепло."""
        cfg = Config(mean_depth_m=10.0)
        res = pipeline.compute_evaporation(synthetic_years, cfg)
        hs = validate.heat_storage_closure(res)
        assert hs["ok"].all(), hs[["date", "G_over_Rn_pct"]].to_dict("records")

    def test_deeper_lake_shifts_peak_later(self, synthetic_years):
        """Больше глубина — больше инерция — позже пик испарения.
        Это главный содержательный эффект теплозапаса."""
        shallow = pipeline.compute_evaporation(
            synthetic_years, Config(mean_depth_m=2.0,
                                    mixed_depth_source="constant"))
        deep = pipeline.compute_evaporation(
            synthetic_years, Config(mean_depth_m=25.0,
                                    mixed_depth_source="constant"))

        ms = shallow.groupby(shallow["date"].dt.month)["E_penman"].mean()
        md = deep.groupby(deep["date"].dt.month)["E_penman"].mean()
        # у глубокого водоёма летний пик срезан, осень усилена
        assert md.loc[9:11].sum() / md.sum() > ms.loc[9:11].sum() / ms.sum()

    def test_satellite_changes_seasonal_shape(self, synthetic_years):
        """Спутниковая Tw меняет сезонный ход сильнее, чем годовую сумму."""
        cfg = Config(mean_depth_m=12.0)
        res = pipeline.compute_evaporation(synthetic_years, cfg)
        sc = validate.satellite_contribution(res)
        assert abs(sc["annual_diff_pct"]) < 40
        assert sc["max_monthly_diff_mm"] > 3   # мм/мес, не мм/сут


class TestWaterBalance:

    def test_monthly_balance_shape(self, synthetic_years):
        cfg = Config(mean_depth_m=10.0)
        res = pipeline.compute_evaporation(synthetic_years, cfg)
        wb = pipeline.water_balance(res, area_km2=50.0)

        assert len(wb) == 36
        assert (wb["E_m3"] >= 0).all()
        # в семиаридном климате испарение превышает осадки за год
        assert wb["net_atm_m3"].sum() < 0

    def test_volume_conversion_consistent(self, synthetic_years):
        cfg = Config(mean_depth_m=10.0)
        res = pipeline.compute_evaporation(synthetic_years, cfg)
        wb = pipeline.water_balance(res, area_km2=50.0)
        expected = wb["E_mm"] * 1e-3 * 50e6
        assert np.allclose(wb["E_m3"], expected)


class TestValidationReport:

    def test_physical_checks_pass(self, synthetic_years):
        cfg = Config(mean_depth_m=10.0)
        res = pipeline.compute_evaporation(synthetic_years, cfg)
        checks = validate.physical_checks(res)
        failed = [c for c in checks if not c["ok"]]
        assert not failed, failed

    def test_report_renders(self, synthetic_years):
        cfg = Config(mean_depth_m=10.0)
        res = pipeline.compute_evaporation(synthetic_years, cfg)
        text = validate.report(res)
        assert "ВАЛИДАЦИЯ" in text
        assert len(text.splitlines()) > 20
