/**
 * Проверки устойчивости к нештатным ответам Open-Meteo и краевым случаям.
 *
 * Запуск:  cd web && node test-robustness.mjs
 *
 * Появились после ошибки «Cannot read properties of undefined (reading 'map')»
 * у пользователя. Такие сообщения бесполезны: они не говорят ни что пришло от
 * API, ни где сломалось. Здесь проверяется, что любой правдоподобный сбой
 * даёт ВНЯТНУЮ ошибку, а восстановимые отклонения формата не ломают расчёт
 * вовсе.
 */

import { computeEvaporation, aggregateAnnual } from './js/compute.js';
import * as meteo from './js/meteo.js';

const N_DAYS = 365 * 3;
let failures = 0;

function check(ok, name, detail = '') {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}${detail ? '  — ' + detail : ''}`);
  if (!ok) failures++;
}

/** Мок почасового ответа Open-Meteo с управляемыми отклонениями. */
function hourlyMock({ suffix = '', drop = [] } = {}) {
  const time = [], data = {
    temperature_2m: [], dew_point_2m: [], surface_pressure: [],
    wind_speed_10m: [], shortwave_radiation: [], cloud_cover: [],
    precipitation: [],
  };
  const start = Date.UTC(2021, 0, 1);

  for (let i = 0; i < N_DAYS * 24; i++) {
    time.push(new Date(start + i * 3600000).toISOString().slice(0, 13) + ':00');
    const doy = i / 24, w = (2 * Math.PI * doy) / 365.25, hr = i % 24;
    const ta = 10 + 16 * Math.sin(w - 1.9)
             + 6 * Math.sin(((hr - 6) / 24) * 2 * Math.PI);

    data.temperature_2m.push(ta);
    data.dew_point_2m.push(ta - 9);
    data.surface_pressure.push(920);
    // Ветер должен ВАРЬИРОВАТЬ: постоянное значение валидация справедливо
    // считает признаком отсутствующих данных.
    data.wind_speed_10m.push(9 + 5 * Math.sin(i / 37) + 3 * Math.sin(i / 7));  // км/ч
    data.shortwave_radiation.push(
      Math.max(0, 700 * Math.sin((Math.PI * (hr - 6)) / 12))
      * (0.5 + 0.5 * Math.sin(w - 1.9)));
    data.cloud_cover.push(40);
    data.precipitation.push(i % 97 === 0 ? 2 : 0);
  }

  const out = { time };
  for (const [k, v] of Object.entries(data)) {
    if (drop.includes(k)) continue;
    out[k + suffix] = v;
  }
  return out;
}

function mockFetch(hourly, { ok = true, status = 200, reason = null } = {}) {
  globalThis.fetch = async () => ({
    ok, status,
    json: async () => (reason ? { reason } : { hourly }),
  });
}

async function runFull(mockOpts) {
  mockFetch(hourlyMock(mockOpts));
  const met = await meteo.fetchArchive(43.68, 76.58, 2021, 2023,
                                       'era5_land', () => {});
  const rows = computeEvaporation(met, 8, 365);
  return { met, rows, annual: aggregateAnnual(rows) };
}

// ---------- восстановимые отклонения формата ----------
console.log('Отклонения формата ответа:');

for (const [name, opts] of [
  ['обычный ответ', {}],
  // API добавляет суффикс модели к именам полей при указании models=
  ['ключи с суффиксом модели', { suffix: '_era5_land' }],
  ['нет облачности', { drop: ['cloud_cover'] }],
  ['нет давления', { drop: ['surface_pressure'] }],
  ['нет осадков', { drop: ['precipitation'] }],
]) {
  try {
    const { annual } = await runFull(opts);
    const ok = annual.length >= 1
      && annual.every(a => a.E > 400 && a.E < 2500);
    check(ok, name, `лет=${annual.length} E=${annual.map(a => a.E).join(',')}`);
  } catch (e) {
    check(false, name, e.message);
  }
}

// ---------- невосстановимые: нужна ВНЯТНАЯ ошибка ----------
console.log('\nБитые ответы должны давать понятное сообщение:');

for (const [name, hourly] of [
  ['hourly отсутствует', undefined],
  ['пустой объект', {}],
  ['есть time, нет переменных', { time: ['2021-01-01T00:00'] }],
  ['нет температуры', hourlyMock({ drop: ['temperature_2m'] })],
  ['нет точки росы', hourlyMock({ drop: ['dew_point_2m'] })],
]) {
  mockFetch(hourly);
  try {
    await meteo.fetchArchive(43.68, 76.58, 2021, 2021, 'era5_land', () => {});
    check(false, name, 'ошибки не было');
  } catch (e) {
    // сообщение не должно быть внутренней ошибкой доступа к undefined
    const clear = !/reading '|of undefined|is not a function/.test(e.message);
    check(clear, name, `"${e.message.slice(0, 55)}…"`);
  }
}

// ---------- отказ API ----------
console.log('\nОтказ API:');
mockFetch(null, { ok: false, status: 429, reason: 'Too many requests' });
try {
  await meteo.fetchArchive(43.68, 76.58, 2021, 2021, 'era5', () => {});
  check(false, 'статус 429', 'ошибки не было');
} catch (e) {
  check(e.message.includes('429') || e.message.includes('Too many'),
        'статус 429', `"${e.message.slice(0, 45)}…"`);
}

// ---------- краевые случаи расчёта ----------
console.log('\nКраевые случаи расчёта:');

const { rows: fullRows } = await runFull({});

check(computeEvaporation([], 8, 365).length === 0,
      'пустой ряд не падает');

// период короче раскрутки: полных лет нет, и это должно быть видно
const short = computeEvaporation(fullRows.slice(0, 400).map(r => r), 8, 365);
check(aggregateAnnual(short).length === 0,
      'короткий период даёт ноль полных лет',
      'приложение обязано сообщить об этом, а не падать');

// экстремальные глубины не должны ломать модель
for (const d of [0.6, 1, 100, 200]) {
  try {
    const { annual } = await runFull({});
    const r = computeEvaporation(
      (await runFull({})).met, d, 365);
    const a = aggregateAnnual(r);
    const finite = r.every(x => Number.isFinite(x.ePenman)
                             && Number.isFinite(x.tw));
    check(finite && a.length >= 1, `глубина ${d} м`,
          `E=${a.map(x => x.E).join(',')}`);
  } catch (e) {
    check(false, `глубина ${d} м`, e.message);
  }
}


// ---------- NaN не должен доходить до отрисовки ----------
console.log('\nЗащита от NaN:');

/**
 * Реальный случай, из-за которого этот блок появился.
 *
 * Для модели era5_land Open-Meteo возвращает массивы, целиком заполненные
 * null, по переменным, которых в этой модели нет — приземное давление и
 * облачность. Раньше это давало mean([]) = NaN, NaN расползался по всему
 * расчёту, и падение происходило в палитре матрицы год × месяц:
 * Math.min(NaN, 2) возвращает NaN, поэтому stops[NaN] === undefined, и вызов
 * .map на нём давал «Cannot read properties of undefined (reading 'map')».
 *
 * Сообщение указывало на отрисовку, хотя причина была в разборе ответа API.
 */
function hourlyWithNulls(nullVars) {
  const h = hourlyMock({});
  for (const v of nullVars) h[v] = h[v].map(() => null);
  return h;
}

for (const [name, vars] of [
  ['давление из null', ['surface_pressure']],
  ['облачность из null', ['cloud_cover']],
  ['давление и облачность из null', ['surface_pressure', 'cloud_cover']],
]) {
  mockFetch(hourlyWithNulls(vars));
  try {
    const met = await meteo.fetchArchive(43.68, 76.58, 2021, 2023,
                                         'era5_land', () => {});
    const rows = computeEvaporation(met, 8, 365);
    const annual = aggregateAnnual(rows);
    const allFinite = rows.every(r => Number.isFinite(r.ePenman)
                                   && Number.isFinite(r.tw));
    check(allFinite && annual.length >= 1, name,
          `лет=${annual.length} E=${annual.map(a => a.E).join(',')}`);
  } catch (e) {
    check(false, name, e.message);
  }
}

// Ветер и радиация — обязательные: они образуют два слагаемых Пенмана.
// Раньше отсутствие ветра подменялось константой 2 м/с, и расчёт молча
// занижал результат. Теперь это ошибка.
for (const v of ['wind_speed_10m', 'shortwave_radiation']) {
  mockFetch(hourlyWithNulls([v]));
  try {
    await meteo.fetchArchive(43.68, 76.58, 2021, 2022, 'era5', () => {});
    check(false, `${v} из null`, 'посчитал вместо отказа');
  } catch (e) {
    check(!/reading '|of undefined/.test(e.message),
          `${v} из null`, `"${e.message.slice(0, 45)}…"`);
  }
}

// температура из null — считать нечего, нужна внятная ошибка
mockFetch(hourlyWithNulls(['temperature_2m']));
try {
  await meteo.fetchArchive(43.68, 76.58, 2021, 2022, 'era5_land', () => {});
  check(false, 'температура из null', 'ошибки не было');
} catch (e) {
  check(!/reading '|of undefined/.test(e.message),
        'температура из null', `"${e.message.slice(0, 50)}…"`);
}


// ---------- отсутствие ключевых переменных должно ОСТАНАВЛИВАТЬ расчёт ----------
console.log('\nКлючевые переменные:');

/**
 * Случай, обнаруженный на реальном расчёте по Шардаринскому водохранилищу.
 *
 * Open-Meteo для модели era5_land не отдаёт солнечную радиацию, ветер,
 * осадки и давление — только температуру и точку росы. Подстановка значений
 * по умолчанию превратила явный сбой в тихий: расчёт выдал правдоподобную
 * таблицу с испарением 483 мм/год вместо ожидаемых ~1200. Радиационный баланс
 * при этом был отрицательным круглый год, а температура воды не поднималась
 * выше 17 °C.
 *
 * Вывод, закреплённый здесь: для радиации и ветра значения по умолчанию
 * недопустимы. Отсутствие данных обязано быть ошибкой, а не нулём.
 */
function mockTwoModels({ landHasAll = false } = {}) {
  globalThis.fetch = async (url) => {
    const isLand = String(url).includes('era5_land');
    const h = hourlyMock({});
    if (isLand && !landHasAll) {
      // era5_land отдаёт только температуру, точку росы и осадки
      for (const v of ['shortwave_radiation', 'wind_speed_10m',
                       'surface_pressure', 'cloud_cover']) {
        h[v] = h[v].map(() => null);
      }
    }
    return { ok: true, status: 200, json: async () => ({ hourly: h }) };
  };
}

mockTwoModels();
try {
  const met = await meteo.fetchArchive(41.13, 68.16, 2021, 2023, null, () => {});
  const annual = aggregateAnnual(computeEvaporation(met, 10, 365));
  const plausible = annual.length >= 1 && annual.every(a => a.E > 600);
  check(plausible, 'era5_land без радиации — era5 подхватывает',
        `E=${annual.map(a => a.E).join(',')}`);
} catch (e) {
  check(false, 'era5_land без радиации — era5 подхватывает', e.message);
}

// радиации нет ни в одной модели — обязан быть отказ
globalThis.fetch = async () => ({
  ok: true, status: 200,
  json: async () => ({ hourly: hourlyWithNulls(['shortwave_radiation']) }),
});
try {
  await meteo.fetchArchive(41.13, 68.16, 2021, 2023, null, () => {});
  check(false, 'нет радиации нигде', 'посчитал вместо отказа');
} catch (e) {
  check(true, 'нет радиации нигде', `"${e.message.slice(0, 50)}…"`);
}

// ветра нет нигде
globalThis.fetch = async () => ({
  ok: true, status: 200,
  json: async () => ({ hourly: hourlyWithNulls(['wind_speed_10m']) }),
});
try {
  await meteo.fetchArchive(41.13, 68.16, 2021, 2023, null, () => {});
  check(false, 'нет ветра нигде', 'посчитал вместо отказа');
} catch (e) {
  check(true, 'нет ветра нигде', `"${e.message.slice(0, 50)}…"`);
}


// ---------- экспорт диаграмм ----------
console.log('\nЭкспорт PNG:');

/**
 * Диаграммы не скачивались по двум причинам сразу, и обе давали ТИШИНУ,
 * а не ошибку:
 *
 *   1. render() вызывался раньше, чем показывался контейнер результатов.
 *      Chart.js измеряет размеры при создании и в скрытом блоке получает
 *      холст 0×0. На экране графики появлялись (срабатывал resize), но
 *      canvas.width оставался нулевым, и экспорт давал пустой файл.
 *   2. Ссылка для скачивания не вставлялась в документ. Firefox игнорирует
 *      click() по элементу вне DOM, не поднимая исключения.
 *
 * Отсюда требования: нулевой холст обязан давать внятную ошибку, а ссылка —
 * попадать в DOM до клика.
 */
{
  const appended = [], clicked = [];
  const madeCanvases = [];
  const fakeCtx = () => new Proxy({}, {
    get: (t, k) => {
      if (k === 'createLinearGradient') return () => ({ addColorStop() {} });
      if (k === 'measureText') return () => ({ width: 60 });
      return () => {};
    },
    set: () => true,
  });

  const realDoc = globalThis.document;
  globalThis.document = {
    createElement: (t) => {
      if (t === 'canvas') {
        const c = { width: 0, height: 0, getContext: fakeCtx,
                    toDataURL: () => 'data:image/png;base64,AAA' };
        madeCanvases.push(c);
        return c;
      }
      const el = { style: {}, href: '', download: '',
                   click() { clicked.push(el.download); } };
      return el;
    },
    body: { appendChild: (el) => appended.push(el), removeChild() {} },
  };
  const realTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (f) => f();

  const { exportChart, exportHeatmap, metaFor } = await import('./js/export.js');
  const meta = metaFor({
    name: 'Test Reservoir', area: 760, centroid: { lat: 41.13, lng: 68.16 },
    annual: [{ year: 2020, E: 1578 }, { year: 2021, E: 1600 }],
  });

  // нулевой холст — внятная ошибка, не тишина
  try {
    exportChart({ canvas: { width: 0, height: 0 } }, 'x.png', meta);
    check(false, 'нулевой холст', 'экспорт прошёл молча');
  } catch (e) {
    check(/zero size/.test(e.message), 'нулевой холст даёт ошибку',
          `"${e.message.slice(0, 40)}…"`);
  }

  try {
    exportChart(null, 'x.png', meta);
    check(false, 'график не готов', 'экспорт прошёл молча');
  } catch (e) {
    check(/not ready/.test(e.message), 'график не готов даёт ошибку');
  }

  // нормальный график: ссылка в DOM, клик после вставки
  appended.length = 0; clicked.length = 0;
  exportChart({ canvas: { width: 900, height: 380 } }, 'chart.png', meta);
  check(appended.length === 1 && clicked.length === 1,
        'ссылка вставлена в DOM перед click',
        `appended=${appended.length} clicked=${clicked.length}`);
  check(clicked[0] === 'chart.png', 'имя файла передано', clicked[0]);

  // подпись автора попадает в кадр
  const texts = [];
  const savedCreate = globalThis.document.createElement;
  globalThis.document.createElement = (t) => {
    if (t === 'canvas') {
      const c = {
        width: 0, height: 0,
        getContext: () => new Proxy({}, {
          get: (tt, k) => {
            if (k === 'createLinearGradient') return () => ({ addColorStop() {} });
            if (k === 'fillText') return (txt) => texts.push(txt);
            if (k === 'measureText') return () => ({ width: 60 });
            return () => {};
          },
          set: () => true,
        }),
        toDataURL: () => 'data:image/png;base64,AAA',
      };
      return c;
    }
    return { style: {}, href: '', download: '', click() {} };
  };
  exportChart({ canvas: { width: 900, height: 380 } }, 'c.png', meta);
  check(texts.includes('Developed by Aigul Bekbayeva'),
        'подпись автора нанесена на картинку');
  check(texts.some(t => /Test Reservoir/.test(t)), 'заголовок нанесён');
  check(!texts.some(t => typeof t === 'string' && /[а-яА-Я]/.test(t)),
        'в подписях нет кириллицы');

  globalThis.document.createElement = savedCreate;
  globalThis.document = realDoc;
  globalThis.setTimeout = realTimeout;
}

console.log(failures ? `\n${failures} проверок провалено` : '\nвсе проверки пройдены');
process.exit(failures ? 1 : 0);
