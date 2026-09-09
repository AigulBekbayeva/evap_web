/**
 * Проверки браузерного расчёта на синтетических данных.
 *
 * Запуск:  cd web && node test.mjs
 * Ненулевой код возврата — расхождение.
 *
 * Пороги здесь намеренно жёсткие. Ранняя версия этих тестов проверяла только
 * границы диапазонов и пропустила две грубые ошибки:
 *
 *   1. Формула Брунта была применена с коэффициентом чистого длинноволнового
 *      баланса вместо излучательной способности неба. Rl↓ занижалось на треть.
 *   2. Внутри модели теплового баланса стоял Пенман вместо массопереноса.
 *      Пенман берёт дефицит по es(Ta) и почти не реагирует на температуру
 *      воды, из-за чего возникала положительная обратная связь.
 *
 * Обе ошибки давали правдоподобные на вид числа, но температура воды
 * схлопывалась к нулю круглый год. Поэтому проверяются АМПЛИТУДА годового
 * хода Tw и знак влияния глубины, а не только то, что значения конечны.
 */

import { computeEvaporation, aggregateMonthly, aggregateAnnual, climatology,
         linearTrend, toCSV } from './js/compute.js';
import * as ph from './js/physics.js';

// синтетическая погода семиаридной зоны, 6 лет (первый — раскрутка модели Tw)
const met = [];
const start = new Date(Date.UTC(2018, 0, 1));
for (let i = 0; i < 365 * 6 + 1; i++) {
  const d = new Date(start.getTime() + i * 86400000);
  const doy = (d - new Date(Date.UTC(d.getUTCFullYear(), 0, 0))) / 86400000;
  const w = 2 * Math.PI * doy / 365.25;
  const ta = 10.5 + 16.0 * Math.sin(w - 1.9) + (Math.random() - .5) * 5;
  const tdew = ta - (7 + 7 * Math.max(0, Math.sin(w - 1.9)));
  const rs = Math.max(1.5, 14.5 + 11.5 * Math.sin(w - 1.9)) * (0.6 + Math.random() * 0.4);
  const cloud = 30 + Math.random() * 40;
  met.push({
    date: d.toISOString().slice(0, 10),
    year: d.getUTCFullYear(), month: d.getUTCMonth() + 1,
    ta, taMin: ta - 6, taMax: ta + 6, tdew, pKpa: 92.0,
    u10: 2 + Math.random() * 4, u10Max: 8,
    rsDown: rs, rlDown: ph.estimateRlDown(ta, tdew, cloud), cloud,
    prcp: Math.random() < 0.25 ? Math.random() * 8 : 0,
  });
}

const rows = computeEvaporation(met, 8.0, 365);
const monthly = aggregateMonthly(rows);
const annual = aggregateAnnual(rows);
const clim = climatology(monthly, annual.map(a => a.year));

console.log(`суток после раскрутки: ${rows.length}`);
console.log(`полных лет: ${annual.length}`);
console.log(`годовое испарение: ${annual.map(a => a.E).join(', ')} мм`);
console.log(`осадки: ${annual.map(a => a.P).join(', ')} мм`);

const twMin = Math.min(...rows.map(r => r.tw)), twMax = Math.max(...rows.map(r => r.tw));
console.log(`Tw диапазон: ${twMin.toFixed(1)} … ${twMax.toFixed(1)} °C`);

const peak = clim.reduce((b, c) => c.E > b.E ? c : b);
console.log(`месяц максимума: ${peak.month}`);

// проверки
const errs = [];
if (annual.some(a => a.E < 500 || a.E > 2200)) errs.push('годовая сумма вне разумного диапазона');
if (![6,7,8].includes(peak.month)) errs.push(`пик в месяце ${peak.month}, ожидался 6-8`);
// КРИТИЧНО: модель Tw не должна схлопываться. Ранее ошибка в формуле Брунта
// занижала Rl↓ на треть, баланс уходил в минус и Tw падала к 0 круглый год —
// а тест это пропускал, потому что проверял только границы диапазона.
if (twMax > 40 || twMin < -1) errs.push(`Tw вне диапазона: ${twMin}..${twMax}`);
if (twMax - twMin < 8) errs.push(`амплитуда Tw всего ${(twMax-twMin).toFixed(1)} °C — модель схлопнулась`);
if (twMax < 12) errs.push(`летний максимум Tw ${twMax.toFixed(1)} °C слишком низкий`);
const rlMean = rows.reduce((s,r)=>s+r.rlDown,0)/rows.length;
if (rlMean < 18 || rlMean > 40) errs.push(`среднее Rl↓ ${rlMean.toFixed(1)} МДж вне разумного`);
const rnMean = rows.reduce((s,r)=>s+r.Rn,0)/rows.length;
if (rnMean < 1) errs.push(`средний Rn ${rnMean.toFixed(1)} — баланс не может быть отрицательным в среднем за год`);
console.log(`Rl↓ средн: ${rlMean.toFixed(1)}, Rn средн: ${rnMean.toFixed(1)} МДж/м²/сут`);
if (rows.some(r => !Number.isFinite(r.ePenman))) errs.push('NaN в испарении');
if (rows.some(r => r.ePenman < 0)) errs.push('отрицательное испарение');
if (annual.some(a => a.deficit !== a.E - a.P)) errs.push('дефицит не сходится с E-P');

// теплозапас должен замыкаться за год
const gSum = rows.reduce((s, r) => s + r.G, 0);
const gAbs = rows.reduce((s, r) => s + Math.abs(r.G), 0);
console.log(`замыкание теплозапаса: ${(100*gSum/gAbs).toFixed(2)} % от оборота`);
if (Math.abs(100*gSum/gAbs) > 6) errs.push('теплозапас не замыкается');

// глубина должна сдвигать пик
const shallow = climatology(aggregateMonthly(computeEvaporation(met, 2, 365)),
                            aggregateAnnual(computeEvaporation(met, 2, 365)).map(a=>a.year));
const deep = climatology(aggregateMonthly(computeEvaporation(met, 30, 365)),
                         aggregateAnnual(computeEvaporation(met, 30, 365)).map(a=>a.year));
const autumnFrac = c => (c[8].E + c[9].E + c[10].E) / c.reduce((s,x)=>s+x.E,0);
console.log(`доля осени: мелкий ${(autumnFrac(shallow)*100).toFixed(1)} %, глубокий ${(autumnFrac(deep)*100).toFixed(1)} %`);
if (autumnFrac(deep) <= autumnFrac(shallow) + 0.005)
  errs.push(`глубина почти не сдвигает пик: ${(autumnFrac(shallow)*100).toFixed(1)} vs ${(autumnFrac(deep)*100).toFixed(1)} %`);

const csv = toCSV(rows);
if (csv.split('\n').length !== rows.length + 1) errs.push('CSV неверной длины');

const trend = linearTrend(annual.map(a=>a.year), annual.map(a=>a.E));
console.log(`тренд: ${trend.slope.toFixed(1)} ± ${(2*trend.se).toFixed(1)} мм/год, значим: ${trend.significant}`);

if (errs.length) { console.log('\nОШИБКИ:'); errs.forEach(e => console.log('  ! ' + e)); process.exit(1); }
console.log('\nвсе проверки пройдены');
