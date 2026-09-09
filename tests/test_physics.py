"""
Тесты физического ядра.

Проверяются известные значения из FAO-56, аналитические предельные случаи и
физическая согласованность. Наземных данных нет, поэтому эти тесты —
единственная строгая проверка в проекте.
"""

import numpy as np
import pytest

from evap import physics as ph


class TestThermodynamics:

    def test_svp_reference_values(self):
        """Опорные значения из таблицы FAO-56."""
        assert ph.sat_vapour_pressure(0.0) == pytest.approx(0.6108, abs=1e-4)
        assert ph.sat_vapour_pressure(20.0) == pytest.approx(2.338, abs=0.005)
        assert ph.sat_vapour_pressure(30.0) == pytest.approx(4.243, abs=0.01)

    def test_svp_monotonic(self):
        t = np.arange(-20, 45, 1.0)
        assert np.all(np.diff(ph.sat_vapour_pressure(t)) > 0)

    def test_svp_slope_matches_numeric(self):
        """
        Аналитический наклон совпадает с численной производной.

        Допуск 1e-4, а не машинная точность: в FAO-56 числитель округлён до
        4098, тогда как точное значение 17.27 × 237.3 = 4098.2. Расхождение
        около 4e-5 относительных — заведомо ниже любой ошибки входных данных.
        """
        for t in (0.0, 10.0, 25.0, 35.0):
            h = 1e-4
            num = (ph.sat_vapour_pressure(t + h)
                   - ph.sat_vapour_pressure(t - h)) / (2 * h)
            assert ph.svp_slope(t) == pytest.approx(num, rel=1e-4)

    def test_psychrometric_constant(self):
        """При 101.3 кПа γ ≈ 0.0674 кПа/°C (FAO-56)."""
        assert ph.psychrometric_constant(101.3) == pytest.approx(0.0674, abs=1e-4)

    def test_wind_conversion(self):
        """Приведение 10 м → 2 м даёт коэффициент ≈ 0.748."""
        assert ph.wind_at_2m(1.0) == pytest.approx(0.748, abs=0.001)
        assert ph.wind_at_2m(0.0) == 0.0

    def test_relative_humidity_saturated(self):
        """При Tdew = Ta влажность равна единице."""
        assert ph.relative_humidity(20.0, 20.0) == pytest.approx(1.0)
        assert ph.relative_humidity(20.0, 10.0) < 1.0


class TestRadiation:

    def test_net_radiation_sign(self):
        """Ясный летний день: баланс положительный."""
        rn = ph.net_radiation(rs_down=25.0, rl_down=30.0, tw_c=22.0)
        assert rn > 0

    def test_net_radiation_night_negative(self):
        """Без солнца баланс отрицательный — озеро выхолаживается."""
        rn = ph.net_radiation(rs_down=0.0, rl_down=25.0, tw_c=20.0)
        assert rn < 0

    def test_warmer_water_lowers_rn(self):
        """Более тёплая вода излучает сильнее, Rn падает.
        Это и есть механизм, ради которого нужна спутниковая Tw."""
        cold = ph.net_radiation(20.0, 28.0, tw_c=10.0)
        warm = ph.net_radiation(20.0, 28.0, tw_c=25.0)
        assert warm < cold

    def test_albedo_effect(self):
        """Высокое альбедо (цветение) уменьшает поглощённую радиацию."""
        clean = ph.net_radiation(25.0, 30.0, 20.0, albedo=ph.ALBEDO_WATER)
        bloom = ph.net_radiation(25.0, 30.0, 20.0, albedo=ph.ALBEDO_BLOOM)
        assert bloom < clean

    def test_albedo_from_bloom_bounds(self):
        assert ph.albedo_from_bloom(0.0) == pytest.approx(ph.ALBEDO_WATER)
        assert ph.albedo_from_bloom(1.0) == pytest.approx(ph.ALBEDO_BLOOM)
        assert ph.albedo_from_bloom(2.0) == pytest.approx(ph.ALBEDO_BLOOM)


class TestHeatStorage:

    def test_zero_when_temperature_constant(self):
        tw = np.full(50, 15.0)
        assert np.allclose(ph.heat_storage(tw, mixed_depth=10.0), 0.0)

    def test_positive_when_warming(self):
        tw = np.linspace(5.0, 25.0, 100)
        g = ph.heat_storage(tw, mixed_depth=10.0)
        assert np.all(g > 0)

    def test_magnitude(self):
        """Прогрев на 0.1 °C/сут при 10 м даёт ~4.2 МДж/м²/сут."""
        tw = np.arange(100) * 0.1 + 10.0
        g = ph.heat_storage(tw, mixed_depth=10.0)
        assert np.median(g) == pytest.approx(4.18, rel=0.02)

    def test_annual_cycle_closes(self):
        """За полный годовой цикл сумма G близка к нулю."""
        doy = np.arange(365)
        tw = 12.0 + 10.0 * np.sin(2 * np.pi * (doy - 100) / 365)
        g = ph.heat_storage(tw, mixed_depth=10.0)
        assert abs(g.sum()) < 0.02 * np.abs(g).sum()

    def test_scales_with_depth(self):
        tw = np.linspace(5.0, 20.0, 60)
        g5 = ph.heat_storage(tw, mixed_depth=5.0)
        g20 = ph.heat_storage(tw, mixed_depth=20.0)
        assert np.allclose(g20, 4.0 * g5)


class TestMixedLayer:

    def test_within_bounds(self):
        tw = np.linspace(0.0, 30.0, 50)
        u2 = np.full(50, 3.0)
        h = ph.mixed_layer_depth(tw, mean_depth=12.0, wind_u2=u2)
        assert np.all(h >= 1.0) and np.all(h <= 12.0)

    def test_cold_water_fully_mixed(self):
        h = ph.mixed_layer_depth(np.array([2.0]), 12.0, np.array([2.0]))
        assert h[0] == pytest.approx(12.0)

    def test_wind_deepens_mixing(self):
        calm = ph.mixed_layer_depth(np.array([25.0]), 12.0, np.array([0.5]))
        windy = ph.mixed_layer_depth(np.array([25.0]), 12.0, np.array([8.0]))
        assert windy[0] > calm[0]


class TestPenman:

    def _summer_day(self, **kw):
        args = dict(ta_c=25.0, tdew_c=10.0, u10=3.0, p_kpa=92.0,
                    rs_down=25.0, rl_down=32.0, tw_c=20.0, g=2.0)
        args.update(kw)
        return ph.penman_openwater(**args)

    def test_summer_magnitude(self):
        """Летний день в семиаридном климате: 4–12 мм/сут."""
        e = self._summer_day()
        assert 4.0 < e < 12.0

    def test_wind_increases_evaporation(self):
        assert self._summer_day(u10=8.0) > self._summer_day(u10=1.0)

    def test_dry_air_increases_evaporation(self):
        """Сухой воздух — больше испарения при том же тепле."""
        assert self._summer_day(tdew_c=0.0) > self._summer_day(tdew_c=22.0)

    def test_heat_storage_reduces_evaporation(self):
        """Тепло, уходящее в толщу, не тратится на испарение."""
        assert self._summer_day(g=8.0) < self._summer_day(g=0.0)

    def test_saturated_air_still_evaporates_with_wind(self):
        """При Ta = Tdew адвективный член нулевой, но радиационный остаётся."""
        e = self._summer_day(tdew_c=25.0)
        assert e > 0

    def test_winter_small(self):
        e = ph.penman_openwater(ta_c=-5.0, tdew_c=-8.0, u10=3.0, p_kpa=93.0,
                                rs_down=4.0, rl_down=18.0, tw_c=2.0, g=-1.0)
        assert -1.0 < e < 2.0

    def test_no_satellite_mode(self):
        """Режим tw_c=None не падает и даёт близкий порядок величины."""
        with_sat = self._summer_day(tw_c=20.0)
        without = ph.penman_openwater(25.0, 10.0, 3.0, 92.0, 25.0, 32.0,
                                      tw_c=None, g=0.0)
        assert without > 0
        assert 0.3 < without / with_sat < 3.0

    def test_vectorised(self):
        n = 365
        e = ph.penman_openwater(
            np.full(n, 20.0), np.full(n, 5.0), np.full(n, 3.0),
            np.full(n, 92.0), np.full(n, 20.0), np.full(n, 30.0),
            tw_c=np.full(n, 18.0), g=np.zeros(n))
        assert e.shape == (n,)
        assert np.all(np.isfinite(e))


class TestMassTransfer:

    def test_zero_at_no_wind_and_no_deficit(self):
        e = ph.mass_transfer(tw_c=20.0, tdew_c=20.0, u10=0.0, a=0.0, b=1.4)
        assert e == pytest.approx(0.0)

    def test_increases_with_wind_and_deficit(self):
        base = ph.mass_transfer(20.0, 10.0, 2.0)
        assert ph.mass_transfer(20.0, 10.0, 6.0) > base
        assert ph.mass_transfer(20.0, 0.0, 2.0) > base

    def test_negative_when_water_colder_than_dewpoint(self):
        """Конденсация на холодную воду — физически корректный отрицательный поток."""
        assert ph.mass_transfer(2.0, 12.0, 4.0) < 0

    def test_calibration_matches_total(self):
        """После калибровки суммы двух методов совпадают по построению."""
        rng = np.random.default_rng(42)
        n = 730
        tw = 15.0 + 10 * np.sin(np.arange(n) * 2 * np.pi / 365)
        tdew = tw - 8.0 + rng.normal(0, 1, n)
        u10 = np.abs(rng.normal(3.5, 1.2, n))
        e_pen = np.abs(rng.normal(4.0, 1.5, n))

        a, b = ph.calibrate_mass_transfer(e_pen, tw, tdew, u10)
        e_mt = ph.mass_transfer(tw, tdew, u10, a=a, b=b)
        assert e_mt.sum() == pytest.approx(e_pen.sum(), rel=1e-6)


class TestPriestleyTaylor:

    def test_below_penman_in_dry_advective_conditions(self):
        """В сухом климате Пристли–Тейлор занижает: нет адвективного члена.
        Это и есть причина, почему PML_V2 и подобные продукты
        систематически занижают испарение над водоёмами семиаридной зоны."""
        common = dict(rs_down=25.0, rl_down=32.0, tw_c=20.0, g=2.0)
        pt = ph.priestley_taylor(ta_c=25.0, p_kpa=92.0, **common)
        pen = ph.penman_openwater(ta_c=25.0, tdew_c=2.0, u10=5.0,
                                  p_kpa=92.0, **common)
        assert pt < pen


class TestDerived:

    def test_bowen_ratio_sign(self):
        """Вода теплее воздуха — поток тепла вверх, отношение положительное."""
        assert ph.bowen_ratio(15.0, 20.0, 5.0, 92.0) > 0
        assert ph.bowen_ratio(25.0, 20.0, 5.0, 92.0) < 0

    def test_volume_conversion(self):
        """1000 мм с 50 км² = 50 млн м³."""
        v = ph.mm_to_volume(1000.0, 50e6)
        assert v == pytest.approx(50e6)
