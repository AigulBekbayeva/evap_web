/**
 * Физика испарения со свободной водной поверхности.
 *
 * Порт src/evap/physics.py на JavaScript для браузерного расчёта.
 * Формулы и константы должны совпадать с Python-версией до последнего знака —
 * расхождение проверяется скриптом scripts/check_js_port.py.
 *
 * Единицы во всём модуле:
 *   температура   °C
 *   давление      кПа
 *   радиация      МДж · м⁻² · сут⁻¹
 *   ветер         м/с
 *   испарение     мм/сут
 *   глубина       м
 */

export const SIGMA = 4.903e-9;      // Стефан–Больцман, МДж м⁻² сут⁻¹ K⁻⁴
export const RHO_CW = 4.18;         // объёмная теплоёмкость воды, МДж м⁻³ К⁻¹
export const EMISS_WATER = 0.97;
export const ALBEDO_WATER = 0.07;

// ---------- термодинамика влажного воздуха ----------

/** Давление насыщенного водяного пара по Тетенсу, кПа. */
export function satVapourPressure(tC) {
  return 0.6108 * Math.exp((17.27 * tC) / (tC + 237.3));
}

/** Наклон кривой насыщения dE/dT, кПа/°C. */
export function svpSlope(tC) {
  return (4098.0 * satVapourPressure(tC)) / Math.pow(tC + 237.3, 2);
}

/** Психрометрическая постоянная, кПа/°C (FAO-56, ур. 8). */
export function psychrometricConstant(pKpa) {
  return 0.000665 * pKpa;
}

/** Удельная теплота парообразования, МДж/кг. */
export function latentHeat(tC) {
  return 2.501 - 0.002361 * tC;
}

/** Приведение ветра с 10 м к 2 м по логарифмическому профилю (FAO-56). */
export function windAt2m(u10) {
  return (u10 * 4.87) / Math.log(67.8 * 10 - 5.42);
}

// ---------- радиационный баланс ----------

/**
 * Радиационный баланс водной поверхности, МДж м⁻² сут⁻¹.
 *
 *   Rn = (1 − α)·Rs↓ + Rl↓ − ε·σ·(Tw + 273.15)⁴
 *
 * Длинноволновое излучение вверх считается по температуре ВОДЫ, а не воздуха.
 * Летом разница Tw − Ta достигает нескольких градусов, что заметно в балансе.
 */
export function netRadiation(rsDown, rlDown, twC, albedo = ALBEDO_WATER) {
  const twK = twC + 273.15;
  const rlUp = EMISS_WATER * SIGMA * Math.pow(twK, 4);
  return (1.0 - albedo) * rsDown + rlDown - rlUp;
}

/**
 * Нисходящее длинноволновое излучение по Брунту, МДж м⁻² сут⁻¹.
 *
 *   Rl↓ = ε_sky · σ · Ta⁴
 *   ε_clear = 0.605 + 0.048 · √(ea в гПа)          [Brunt 1932]
 *   ε_sky   = ε_clear · (1 + 0.22 · c²)            [Bolz 1949]
 *
 * ВНИМАНИЕ на константы. В литературе рядом ходит выражение
 * (0.34 − 0.14·√ea), но это коэффициент для ЧИСТОГО длинноволнового баланса
 * (FAO-56, ур. 39), а не излучательная способность неба. Подставить его сюда
 * — распространённая ошибка: Rl↓ занижается примерно на треть, радиационный
 * баланс уходит в минус, и в модели теплового баланса температура воды
 * схлопывается к нулю.
 *
 * Ориентир для проверки: при Ta = 25 °C, Tdew = 10 °C иполовинной облачности
 * Rl↓ должно быть около 30–33 МДж м⁻² сут⁻¹.
 */
export function estimateRlDown(taC, tdewC, cloudPct) {
  const eaHpa = satVapourPressure(tdewC) * 10.0;   // кПа → гПа
  const cloud = Math.min(Math.max(cloudPct / 100.0, 0), 1);

  const epsClear = 0.605 + 0.048 * Math.sqrt(Math.max(eaHpa, 0));
  const epsSky = Math.min(epsClear * (1.0 + 0.22 * cloud * cloud), 1.0);

  return epsSky * SIGMA * Math.pow(taC + 273.15, 4);
}

// ---------- теплозапас ----------

/**
 * Поток тепла в водную толщу, МДж м⁻² сут⁻¹.
 *
 *   G = ρw · cw · h · dTw/dt
 *
 * Положительное G — озеро накапливает тепло, и на испарение остаётся Rn − G.
 * Игнорирование G даёт ошибку до 40 % в отдельные месяцы: весной завышение,
 * осенью занижение. На годовой сумме почти замыкается, но помесячная динамика
 * без него неверна принципиально.
 *
 * @param {number[]} twSeries суточный ряд температуры воды
 * @param {number|number[]} mixedDepth глубина перемешанного слоя, м
 */
export function heatStorage(twSeries, mixedDepth, smoothWindow = 15) {
  const n = twSeries.length;
  let tw = twSeries;

  // Производная по шумному ряду даёт нефизичные скачки — сглаживаем
  if (smoothWindow > 1) {
    tw = new Array(n);
    const half = Math.floor(smoothWindow / 2);
    for (let i = 0; i < n; i++) {
      let sum = 0, cnt = 0;
      for (let j = Math.max(0, i - half); j <= Math.min(n - 1, i + half); j++) {
        sum += twSeries[j];
        cnt++;
      }
      tw[i] = sum / cnt;
    }
  }

  // центральная разность, односторонняя на краях — аналог np.gradient
  const g = new Array(n);
  for (let i = 0; i < n; i++) {
    let d;
    if (i === 0) d = tw[1] - tw[0];
    else if (i === n - 1) d = tw[n - 1] - tw[n - 2];
    else d = (tw[i + 1] - tw[i - 1]) / 2.0;

    const h = Array.isArray(mixedDepth) ? mixedDepth[i] : mixedDepth;
    g[i] = RHO_CW * h * d;
  }
  return g;
}

/**
 * Грубая параметризация глубины перемешанного слоя, м.
 *
 * При Tw ниже порога толща перемешана целиком; выше — термоклин оценивается
 * по балансу ветровой энергии и плавучести. Это упрощение, а не физика:
 * основная неопределённость теплозапаса сидит именно здесь.
 */
export function mixedLayerDepth(twC, meanDepth, windU2, stratThreshold = 12.0) {
  const warm = Math.min(Math.max((twC - stratThreshold) / 15.0, 0), 1);
  const windMix = Math.min(Math.max(windU2 / 6.0, 0), 1);
  const frac = 1.0 - warm * (1.0 - windMix) * 0.75;
  return Math.min(Math.max(frac * meanDepth, 1.0), meanDepth);
}

// ---------- методы расчёта испарения ----------

/**
 * Испарение со свободной водной поверхности по Пенману, мм/сут.
 *
 *         Δ (Rn − G) + γ · f(u) · (es − ea)
 *   E = ─────────────────────────────────────
 *                 λ (Δ + γ)
 *
 *   f(u) = 6.43 · (1 + 0.536 · u2)      [Penman 1948/1956]
 *
 * Первое слагаемое числителя — радиационная составляющая, второе —
 * адвективная. В сухом климате адвективная часть велика, поэтому подходы на
 * основе Пристли–Тейлора здесь систематически занижают результат.
 */
export function penmanOpenWater({ ta, tdew, u10, pKpa, rsDown, rlDown,
                                  tw = null, g = 0.0,
                                  albedo = ALBEDO_WATER }) {
  const twEff = tw === null ? ta : tw;

  const es = satVapourPressure(ta);
  const ea = satVapourPressure(tdew);
  const delta = svpSlope(ta);
  const gamma = psychrometricConstant(pKpa);
  const lam = latentHeat(ta);
  const u2 = windAt2m(u10);

  const rn = netRadiation(rsDown, rlDown, twEff, albedo);
  const fu = 6.43 * (1.0 + 0.536 * u2);

  return (delta * (rn - g) + gamma * fu * (es - ea)) / (lam * (delta + gamma));
}

/**
 * Испарение методом массопереноса, мм/сут.
 *
 *   E = (a + b · u2) · (es(Tw) − ea)
 *
 * Независимый контроль: Пенман чувствителен к радиационному балансу и
 * теплозапасу, массоперенос — к температуре воды и ветру. Коэффициент b
 * калибруется по годовой сумме Пенмана, поэтому годовые итоги совпадают по
 * построению; информативно расхождение ПО МЕСЯЦАМ.
 */
export function massTransfer(twC, tdewC, u10, a = 0.0, b = 1.4) {
  const u2 = windAt2m(u10);
  return (a + b * u2) * (satVapourPressure(twC) - satVapourPressure(tdewC));
}

/** Подбор коэффициента b по годовой сумме Пенмана. */
export function calibrateMassTransfer(ePenman, tw, tdew, u10) {
  let num = 0, den = 0;
  for (let i = 0; i < ePenman.length; i++) {
    const driver = windAt2m(u10[i])
      * (satVapourPressure(tw[i]) - satVapourPressure(tdew[i]));
    if (Number.isFinite(driver) && Number.isFinite(ePenman[i])) {
      num += ePenman[i];
      den += driver;
    }
  }
  return { a: 0.0, b: den !== 0 ? num / den : 1.4 };
}

/** Испарение по Пристли–Тейлору, мм/сут. Только радиационная часть. */
export function priestleyTaylor({ ta, pKpa, rsDown, rlDown, tw = null,
                                  g = 0.0, alpha = 1.26,
                                  albedo = ALBEDO_WATER }) {
  const twEff = tw === null ? ta : tw;
  const delta = svpSlope(ta);
  const gamma = psychrometricConstant(pKpa);
  const lam = latentHeat(ta);
  const rn = netRadiation(rsDown, rlDown, twEff, albedo);
  return (alpha * delta / (delta + gamma)) * (rn - g) / lam;
}
