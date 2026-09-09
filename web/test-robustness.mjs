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
    data.wind_speed_10m.push(11);            // км/ч
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
  ['нет ветра', { drop: ['wind_speed_10m'] }],
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
  ['ветер из null', ['wind_speed_10m']],
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

// температура из null — считать нечего, нужна внятная ошибка
mockFetch(hourlyWithNulls(['temperature_2m']));
try {
  await meteo.fetchArchive(43.68, 76.58, 2021, 2022, 'era5_land', () => {});
  check(false, 'температура из null', 'ошибки не было');
} catch (e) {
  check(!/reading '|of undefined/.test(e.message),
        'температура из null', `"${e.message.slice(0, 50)}…"`);
}

console.log(failures ? `\n${failures} проверок провалено` : '\nвсе проверки пройдены');
process.exit(failures ? 1 : 0);
