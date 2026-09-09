/**
 * Температура воды и метеорологический вход.
 *
 * ГЛАВНОЕ ОГРАНИЧЕНИЕ БРАУЗЕРНОЙ ВЕРСИИ. Температура поверхности воды со
 * спутника здесь недоступна: Landsat и MODIS требуют Earth Engine, который
 * из статической страницы не вызвать. Поэтому Tw МОДЕЛИРУЕТСЯ — интегрируется
 * тепловой баланс водной толщи с глубиной, которую задаёт пользователь.
 *
 * Это принципиально лучше, чем подставить Tw = Ta: модель воспроизводит
 * тепловую инерцию, летний пик оказывается сдвинут и срезан, а теплозапас G
 * работает физически осмысленно. Но проверить результат нечем, и глубина
 * остаётся свободным параметром. Кто может выгрузить спутниковую Tw —
 * пусть использует Python-версию: там глубина восстанавливается из данных.
 */

import * as ph from './physics.js';

const ARCHIVE_URL = 'https://archive-api.open-meteo.com/v1/archive';

const HOURLY_VARS = [
  'temperature_2m',
  'dew_point_2m',
  'surface_pressure',
  'wind_speed_10m',
  'shortwave_radiation',
  'cloud_cover',
  'precipitation',
];

/**
 * Забирает почасовой ряд ERA5 и агрегирует до суток.
 *
 * Разбивка на чанки — вежливость к бесплатному API и способ показать прогресс:
 * за 15 лет это около 130 000 значений на переменную.
 *
 * @param {function} onProgress вызывается как (доля, текст)
 */
export async function fetchArchive(lat, lon, startYear, endYear,
                                   model = 'era5_land',
                                   onProgress = () => {}) {
  const CHUNK = 5;
  const chunks = [];
  for (let y = startYear; y <= endYear; y += CHUNK) {
    chunks.push([y, Math.min(y + CHUNK - 1, endYear)]);
  }

  const all = [];
  for (let i = 0; i < chunks.length; i++) {
    const [y0, y1] = chunks[i];
    onProgress(i / chunks.length, `Загрузка ${y0}–${y1}…`);

    const params = new URLSearchParams({
      latitude: lat.toFixed(4),
      longitude: lon.toFixed(4),
      start_date: `${y0}-01-01`,
      end_date: `${y1}-12-31`,
      hourly: HOURLY_VARS.join(','),
      models: model,
      timezone: 'UTC',
    });

    let resp;
    try {
      resp = await fetch(`${ARCHIVE_URL}?${params}`);
    } catch (e) {
      throw new Error(
        'Не удалось связаться с Open-Meteo. Проверьте интернет-соединение ' +
        'или блокировщик запросов.');
    }

    if (!resp.ok) {
      if (resp.status === 400 && model === 'era5_land') {
        // ERA5-Land покрывает не всю поверхность (нет над океаном и частью
        // высокогорий) — откатываемся на ERA5 31 км
        onProgress(0, 'ERA5-Land недоступен здесь, перехожу на ERA5…');
        return fetchArchive(lat, lon, startYear, endYear, 'era5', onProgress);
      }
      let reason = `${resp.status}`;
      try {
        const j = await resp.json();
        if (j.reason) reason = j.reason;
      } catch { /* тело не JSON — оставляем код статуса */ }
      throw new Error(`Open-Meteo отклонил запрос: ${reason}`);
    }

    const json = await resp.json();
    const hourly = normaliseHourly(json.hourly, model);

    if (!hourly) {
      throw new Error(
        `Open-Meteo вернул ответ без ожидаемых полей за ${y0}–${y1}. ` +
        'Возможно, для этой точки нет данных выбранной модели.');
    }
    all.push(hourly);
  }

  onProgress(0.95, 'Агрегация до суток…');
  return aggregateDaily(all, model);
}

/**
 * Приводит ответ Open-Meteo к ожидаемым именам полей.
 *
 * При указании параметра models API может возвращать ключи с суффиксом модели
 * — `temperature_2m_era5_land` вместо `temperature_2m`. Поведение зависит от
 * версии и от того, запрошена одна модель или несколько. Без нормализации
 * весь разбор ответа рассыпается на невнятной ошибке доступа к undefined.
 *
 * Возвращает null, если обязательных полей нет вовсе.
 */
function normaliseHourly(hourly, model) {
  if (!hourly || !Array.isArray(hourly.time)) return null;

  const out = { time: hourly.time };
  const suffix = `_${model}`;

  for (const v of HOURLY_VARS) {
    if (Array.isArray(hourly[v])) {
      out[v] = hourly[v];
    } else if (Array.isArray(hourly[v + suffix])) {
      out[v] = hourly[v + suffix];
    } else {
      // ищем любой ключ, начинающийся с имени переменной
      const key = Object.keys(hourly).find(
        k => k.startsWith(v) && Array.isArray(hourly[k]));
      out[v] = key ? hourly[key] : null;
    }
  }

  // без температуры и точки росы считать нечего; облачность и осадки
  // можно пережить
  if (!out.temperature_2m || !out.dew_point_2m) return null;
  return out;
}

/**
 * Почасовые значения → суточные, сразу в единицах physics.js.
 *
 * Радиация приходит в Вт/м² как среднее за час: сумма за сутки умножается на
 * 3600 с и переводится в МДж. Ветер Open-Meteo отдаёт в км/ч.
 */
function aggregateDaily(chunks, model) {
  const byDay = new Map();

  for (const h of chunks) {
    for (let i = 0; i < h.time.length; i++) {
      const day = h.time[i].slice(0, 10);
      if (!byDay.has(day)) {
        byDay.set(day, { ta: [], tdew: [], p: [], u: [], rs: [], cc: [], pr: [] });
      }
      const d = byDay.get(day);
      // Часть переменных может отсутствовать целиком — берём безопасно,
      // иначе одна недостающая колонка рушит весь расчёт.
      const at = (name, idx) => {
        const arr = h[name];
        return Array.isArray(arr) ? arr[idx] : null;
      };
      const push = (arr, v) => {
        if (v !== null && v !== undefined && Number.isFinite(v)) arr.push(v);
      };
      push(d.ta, at('temperature_2m', i));
      push(d.tdew, at('dew_point_2m', i));
      push(d.p, at('surface_pressure', i));
      push(d.u, at('wind_speed_10m', i));
      push(d.rs, at('shortwave_radiation', i));
      push(d.cc, at('cloud_cover', i));
      push(d.pr, at('precipitation', i));
    }
  }

  const mean = a => a.reduce((x, y) => x + y, 0) / a.length;
  const sum = a => a.reduce((x, y) => x + y, 0);

  const out = [];
  const skipped = [];
  for (const [date, d] of [...byDay.entries()].sort()) {
    // неполные сутки отбрасываем; без давления и ветра расчёт невозможен
    if (d.ta.length < 20 || !d.tdew.length) continue;

    const ta = mean(d.ta);
    const tdew = mean(d.tdew);
    const cloud = d.cc.length ? mean(d.cc) : 50;
    const pKpa = d.p.length ? mean(d.p) / 10.0 : 101.3;   // гПа → кПа
    const u10 = d.u.length ? mean(d.u) / 3.6 : 2.0;       // км/ч → м/с

    const rec = {
      date,
      year: +date.slice(0, 4),
      month: +date.slice(5, 7),
      ta,
      taMin: Math.min(...d.ta),
      taMax: Math.max(...d.ta),
      tdew,
      pKpa,
      u10,
      u10Max: d.u.length ? Math.max(...d.u) / 3.6 : u10,
      rsDown: (sum(d.rs) * 3600) / 1e6,   // Вт/м²·ч → МДж/м²
      rlDown: ph.estimateRlDown(ta, tdew, cloud),
      cloud,
      prcp: sum(d.pr),
    };

    // Последний рубеж: ни одно поле не должно быть NaN или Infinity.
    // Один испорченный день заражает месячную сумму, та — палитру матрицы,
    // и падение случается далеко от причины, с бесполезным сообщением.
    const bad = ['ta', 'tdew', 'pKpa', 'u10', 'rsDown', 'rlDown', 'prcp']
      .filter(k => !Number.isFinite(rec[k]));
    if (bad.length) { skipped.push({ date, bad }); continue; }

    out.push(rec);
  }

  if (skipped.length) {
    console.warn(`Отброшено ${skipped.length} суток с некорректными ` +
                 'значениями. Первые:', skipped.slice(0, 5));
  }
  if (skipped.length > byDay.size * 0.5) {
    throw new Error(
      `Более половины суток (${skipped.length} из ${byDay.size}) содержат ` +
      `некорректные значения (${[...new Set(skipped.flatMap(x => x.bad))]
        .join(', ')}). Для этой точки данных выбранной модели, ` +
      'по-видимому, нет.');
  }

  if (!out.length) {
    throw new Error(
      'Open-Meteo не вернул пригодных данных за указанный период. ' +
      'Проверьте координаты и годы.');
  }
  out.model = model;
  return out;
}

/**
 * Моделирование температуры воды: интегрирование теплового баланса толщи.
 *
 *   ρw·cw·h · dTw/dt = Rn − LE − H
 *
 * Явная схема Эйлера с суточным шагом. Для инерционного водоёма устойчива при
 * h > 1 м. Явный поток тепла H оценивается через отношение Боуэна.
 *
 * Первый год отбрасывается как раскрутка: начальное условие произвольно, и
 * модели нужно время, чтобы забыть его.
 *
 * @param {number} depthM средняя глубина — СВОБОДНЫЙ ПАРАМЕТР
 * @returns {number[]} ряд Tw той же длины, что и met
 */
export function simulateWaterTemperature(met, depthM, windCoef = 1.4) {
  const n = met.length;
  const tw = new Array(n);

  // начальное условие — средняя температура воздуха за первый год
  const firstYear = met.slice(0, Math.min(365, n));
  let cur = firstYear.reduce((s, d) => s + d.ta, 0) / firstYear.length;
  cur = Math.max(cur, 4.0);

  const heatCapacity = ph.RHO_CW * depthM;   // МДж м⁻² К⁻¹

  for (let i = 0; i < n; i++) {
    const d = met[i];
    const rn = ph.netRadiation(d.rsDown, d.rlDown, cur);

    // ВАЖНО: здесь массоперенос, а НЕ Пенман.
    //
    // Пенман считает дефицит влажности через es(Ta) — температуру воздуха, —
    // поэтому его испарение почти не зависит от температуры воды. Подставить
    // его в модель теплового баланса нельзя: возникает положительная обратная
    // связь. Холодная вода слабее излучает вверх → Rn растёт → испарение по
    // Пенману растёт → вода остывает ещё сильнее, и Tw схлопывается к нулю
    // круглый год.
    //
    // Массоперенос берёт es(Tw) — давление насыщения при температуре самой
    // ПОВЕРХНОСТИ. Холодная вода испаряет мало, баланс замыкается, и модель
    // приходит к физически осмысленной равновесной температуре.
    const eMm = Math.max(
      ph.massTransfer(cur, d.tdew, d.u10, 0.0, windCoef), 0);
    const le = eMm * ph.latentHeat(d.ta);       // МДж м⁻² сут⁻¹

    // явный поток тепла через отношение Боуэна
    const gamma = ph.psychrometricConstant(d.pKpa);
    const de = ph.satVapourPressure(cur) - ph.satVapourPressure(d.tdew);
    let beta = Math.abs(de) > 0.05 ? (gamma * (cur - d.ta)) / de : 0.0;
    beta = Math.min(Math.max(beta, -2.0), 3.0);

    cur = cur + (rn - le - beta * le) / heatCapacity;
    cur = Math.max(cur, 0.0);                  // лёд отдельно не моделируем
    tw[i] = cur;
  }

  return tw;
}

/**
 * Признак ледостава: холодная вода при устойчиво отрицательном воздухе.
 * Подо льдом испарение с открытой поверхности не считается.
 */
export function iceFlag(met, tw, threshold = 1.0) {
  const n = met.length;
  const flag = new Array(n).fill(false);

  for (let i = 0; i < n; i++) {
    let s = 0, c = 0;
    for (let j = Math.max(0, i - 4); j <= i; j++) { s += met[j].ta; c++; }
    flag[i] = tw[i] < threshold && s / c < 0;
  }
  return flag;
}
