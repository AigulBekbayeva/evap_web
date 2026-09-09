"""
Физика испарения со свободной водной поверхности.

Все функции векторизованы (numpy / pandas Series) и не имеют побочных
эффектов, поэтому модуль тестируется независимо от загрузки данных.

Единицы, принятые во всём модуле:
    температура      °C
    давление         кПа
    радиация         МДж · м⁻² · сут⁻¹
    ветер            м/с
    испарение        мм/сут
    глубина          м
"""

from __future__ import annotations

import numpy as np

# --- физические постоянные -------------------------------------------------

SIGMA = 4.903e-9          # Стефан–Больцман, МДж м⁻² сут⁻¹ K⁻⁴
RHO_CW = 4.18             # объёмная теплоёмкость воды, МДж м⁻³ К⁻¹
EMISS_WATER = 0.97        # излучательная способность воды
ALBEDO_WATER = 0.07       # альбедо чистой воды при среднем зените
ALBEDO_BLOOM = 0.11       # альбедо при плотном поверхностном скоплении
RHO_W = 1000.0            # плотность воды, кг/м³


# --- базовая термодинамика влажного воздуха --------------------------------

def sat_vapour_pressure(t_c):
    """Давление насыщенного водяного пара по Тетенсу, кПа."""
    t = np.asarray(t_c, dtype=float)
    return 0.6108 * np.exp(17.27 * t / (t + 237.3))


def svp_slope(t_c):
    """Наклон кривой насыщения dE/dT, кПа/°C."""
    t = np.asarray(t_c, dtype=float)
    return 4098.0 * sat_vapour_pressure(t) / (t + 237.3) ** 2


def psychrometric_constant(p_kpa):
    """Психрометрическая постоянная, кПа/°C. FAO-56, уравнение 8."""
    return 0.000665 * np.asarray(p_kpa, dtype=float)


def latent_heat(t_c):
    """Удельная теплота парообразования, МДж/кг."""
    return 2.501 - 0.002361 * np.asarray(t_c, dtype=float)


def wind_at_2m(u10):
    """
    Приведение ветра с 10 м к 2 м по логарифмическому профилю (FAO-56).

    u2 = u10 · 4.87 / ln(67.8 · 10 − 5.42) ≈ u10 · 0.748
    """
    return np.asarray(u10, dtype=float) * 4.87 / np.log(67.8 * 10.0 - 5.42)


def relative_humidity(t_c, tdew_c):
    """Относительная влажность, доли единицы."""
    return sat_vapour_pressure(tdew_c) / sat_vapour_pressure(t_c)


# --- радиационный баланс ---------------------------------------------------

def net_radiation(rs_down, rl_down, tw_c, albedo=ALBEDO_WATER,
                  emissivity=EMISS_WATER):
    """
    Радиационный баланс водной поверхности, МДж м⁻² сут⁻¹.

        Rn = (1 − α)·Rs↓ + Rl↓ − ε·σ·(Tw + 273.15)⁴

    Здесь и заключён главный вклад дистанционного зондирования: длинноволновое
    излучение вверх считается по СПУТНИКОВОЙ температуре воды, а не по
    температуре воздуха из реанализа. Летом разница Tw − Ta достигает 3–5 °C,
    что даёт 15–25 Вт/м² в балансе.
    """
    rs_down = np.asarray(rs_down, dtype=float)
    rl_down = np.asarray(rl_down, dtype=float)
    tw_k = np.asarray(tw_c, dtype=float) + 273.15

    rl_up = emissivity * SIGMA * tw_k ** 4
    return (1.0 - albedo) * rs_down + rl_down - rl_up


def albedo_from_bloom(bloom_fraction):
    """
    Альбедо с поправкой на поверхностное скопление цианобактерий.

    bloom_fraction — доля площади с плёнкой (0…1), из Проекта 2 (FAI).
    Линейная интерполяция между чистой водой и плотным скоплением.
    """
    f = np.clip(np.asarray(bloom_fraction, dtype=float), 0.0, 1.0)
    return ALBEDO_WATER + f * (ALBEDO_BLOOM - ALBEDO_WATER)


# --- теплозапас водоёма ----------------------------------------------------

def heat_storage(tw_c, mixed_depth, dt_days=1.0, smooth_window=None):
    """
    Поток тепла в водную толщу, МДж м⁻² сут⁻¹.

        G = ρw · cw · h · dTw/dt

    Положительное G означает, что озеро НАКАПЛИВАЕТ тепло, и на испарение
    остаётся Rn − G. Игнорирование G даёт ошибку до 40 % в отдельные месяцы:
    весной испарение завышается, осенью занижается. На годовой сумме эффект
    почти замыкается, но помесячная динамика без него неверна принципиально.

    tw_c        : ряд температуры воды (°C), уже интерполированный на суточный шаг
    mixed_depth : глубина перемешанного слоя (м), скаляр или ряд
    smooth_window : если задано, ряд Tw сглаживается скользящим средним перед
                    дифференцированием — производная по шумному спутниковому
                    ряду иначе даёт нефизичные скачки
    """
    tw = np.asarray(tw_c, dtype=float)

    if smooth_window is not None and smooth_window > 1:
        import pandas as pd
        tw = (pd.Series(tw)
              .rolling(smooth_window, center=True, min_periods=1)
              .mean()
              .to_numpy())

    dtw_dt = np.gradient(tw) / float(dt_days)
    return RHO_CW * np.asarray(mixed_depth, dtype=float) * dtw_dt


def mixed_layer_depth(tw_c, mean_depth, wind_u2, strat_threshold=12.0):
    """
    Грубая параметризация глубины перемешанного слоя, м.

    Заглушка на случай, если 1D-модель (Simstrat/GLM/FLake) ещё не подключена.
    При Tw ниже порога считаем толщу перемешанной целиком; выше — оцениваем
    термоклин по балансу ветровой энергии и плавучести.

    ВАЖНО: это упрощение вносит основную неопределённость в G. Для итогового
    расчёта заменить выходом 1D-модели (см. evap.lakemodel).
    """
    tw = np.asarray(tw_c, dtype=float)
    u2 = np.asarray(wind_u2, dtype=float)
    h_full = float(mean_depth)

    # доля толщи, вовлечённая в перемешивание: растёт с ветром, падает с
    # прогревом поверхности
    warm = np.clip((tw - strat_threshold) / 15.0, 0.0, 1.0)
    wind_mix = np.clip(u2 / 6.0, 0.0, 1.0)

    frac = 1.0 - warm * (1.0 - wind_mix) * 0.75
    return np.clip(frac * h_full, 1.0, h_full)


# --- методы расчёта испарения ---------------------------------------------

def penman_openwater(ta_c, tdew_c, u10, p_kpa, rs_down, rl_down,
                     tw_c=None, g=0.0, albedo=ALBEDO_WATER,
                     wind_a=6.43, wind_b=0.536):
    """
    Испарение со свободной водной поверхности по Пенману, мм/сут.

                Δ (Rn − G) + γ · f(u) · (es − ea)
        E  =  ─────────────────────────────────────
                        λ (Δ + γ)

        f(u) = wind_a · (1 + wind_b · u2)        [Penman 1948/1956]

    Первое слагаемое — радиационная составляющая, второе — адвективная.
    В семиаридном климате адвективная часть велика, поэтому продукты,
    построенные на Пристли–Тейлоре (PML_V2 и подобные), здесь систематически
    занижают испарение.

    tw_c : температура воды. Если None, подставляется ta_c — режим «без
           спутника», годится для быстрой оценки годовой суммы, но не для
           сезонного хода.
    """
    ta = np.asarray(ta_c, dtype=float)
    tw = ta if tw_c is None else np.asarray(tw_c, dtype=float)

    es = sat_vapour_pressure(ta)
    ea = sat_vapour_pressure(tdew_c)
    delta = svp_slope(ta)
    gamma = psychrometric_constant(p_kpa)
    lam = latent_heat(ta)
    u2 = wind_at_2m(u10)

    rn = net_radiation(rs_down, rl_down, tw, albedo=albedo)
    fu = wind_a * (1.0 + wind_b * u2)

    numerator = delta * (rn - np.asarray(g, dtype=float)) + gamma * fu * (es - ea)
    return numerator / (lam * (delta + gamma))


def priestley_taylor(ta_c, p_kpa, rs_down, rl_down, tw_c=None,
                     g=0.0, alpha=1.26, albedo=ALBEDO_WATER):
    """
    Испарение по Пристли–Тейлору, мм/сут. Только радиационная часть.

    Включено как диагностика: разность Пенман − Пристли-Тейлор есть мера
    адвективного вклада. Для Сорбулака она должна быть заметной, и если
    методы близки — стоит проверить дефицит влажности во входных данных.
    """
    ta = np.asarray(ta_c, dtype=float)
    tw = ta if tw_c is None else np.asarray(tw_c, dtype=float)

    delta = svp_slope(ta)
    gamma = psychrometric_constant(p_kpa)
    lam = latent_heat(ta)
    rn = net_radiation(rs_down, rl_down, tw, albedo=albedo)

    return alpha * delta / (delta + gamma) * (rn - np.asarray(g, dtype=float)) / lam


def mass_transfer(tw_c, tdew_c, u10, a=0.0, b=1.40):
    """
    Испарение методом массопереноса (далтоновский тип), мм/сут.

        E = (a + b · u2) · (es(Tw) − ea)

    Контрольный метод. Его ценность в НЕЗАВИСИМОСТИ ошибок: Пенман
    чувствителен к радиационному балансу и теплозапасу, массоперенос — к
    температуре воды и ветру. Систематическое расхождение по сезонам прямо
    указывает, где проблема.

    Коэффициенты a, b калибруются так, чтобы совпали годовые суммы
    (см. calibrate_mass_transfer).
    """
    u2 = wind_at_2m(u10)
    return (a + b * u2) * (sat_vapour_pressure(tw_c)
                           - sat_vapour_pressure(tdew_c))


def calibrate_mass_transfer(e_penman, tw_c, tdew_c, u10, fix_a=0.0):
    """
    Подбор коэффициента b массопереноса по годовой сумме Пенмана.

    Возвращает (a, b). Калибруется одно число, поэтому совпадение годовых сумм
    гарантировано по построению — информативно именно РАСХОЖДЕНИЕ ПО МЕСЯЦАМ
    после калибровки.
    """
    u2 = wind_at_2m(u10)
    vpd = sat_vapour_pressure(tw_c) - sat_vapour_pressure(tdew_c)

    driver = (fix_a + u2) * vpd
    valid = np.isfinite(driver) & np.isfinite(e_penman)
    if valid.sum() == 0:
        raise ValueError("нет валидных значений для калибровки")

    b = float(np.nansum(np.asarray(e_penman)[valid]) / np.nansum(driver[valid]))
    return fix_a, b


# --- производные величины --------------------------------------------------

def bowen_ratio(ta_c, tw_c, tdew_c, p_kpa):
    """Отношение Боуэна H/LE. Диагностика режима теплообмена."""
    gamma = psychrometric_constant(p_kpa)
    dt = np.asarray(tw_c, dtype=float) - np.asarray(ta_c, dtype=float)
    de = sat_vapour_pressure(tw_c) - sat_vapour_pressure(tdew_c)
    with np.errstate(divide="ignore", invalid="ignore"):
        return gamma * dt / de


def mm_to_volume(e_mm, area_m2):
    """Перевод слоя испарения (мм) в объём (м³)."""
    return np.asarray(e_mm, dtype=float) * 1e-3 * np.asarray(area_m2, dtype=float)
