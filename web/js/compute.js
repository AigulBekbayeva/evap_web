/**
 * Расчёт испарения и агрегация результатов.
 */

import * as ph from './physics.js';
import { simulateWaterTemperature, iceFlag } from './meteo.js';

/**
 * Полный расчёт по суточному метеоряду.
 *
 * @param {Array} met  ряд из fetchArchive
 * @param {number} depthM средняя глубина водоёма
 * @param {number} spinupDays раскрутка модели Tw, отбрасывается из результата
 */
export function computeEvaporation(met, depthM, spinupDays = 365) {
  const tw = simulateWaterTemperature(met, depthM);
  const ice = iceFlag(met, tw);

  const hMix = met.map((d, i) =>
    ph.mixedLayerDepth(tw[i], depthM, ph.windAt2m(d.u10)));
  const g = ph.heatStorage(tw, hMix);

  const rows = met.map((d, i) => {
    const rn = ph.netRadiation(d.rsDown, d.rlDown, tw[i]);

    let ePen = ph.penmanOpenWater({
      ta: d.ta, tdew: d.tdew, u10: d.u10, pKpa: d.pKpa,
      rsDown: d.rsDown, rlDown: d.rlDown, tw: tw[i], g: g[i],
    });

    // вариант без моделирования воды: показывает вклад теплозапаса
    let eNoTw = ph.penmanOpenWater({
      ta: d.ta, tdew: d.tdew, u10: d.u10, pKpa: d.pKpa,
      rsDown: d.rsDown, rlDown: d.rlDown, tw: null, g: 0.0,
    });

    let ePt = ph.priestleyTaylor({
      ta: d.ta, pKpa: d.pKpa, rsDown: d.rsDown, rlDown: d.rlDown,
      tw: tw[i], g: g[i],
    });

    if (ice[i]) { ePen = 0; eNoTw = 0; ePt = 0; }

    return {
      ...d,
      tw: tw[i], hMix: hMix[i], G: g[i], Rn: rn, ice: ice[i],
      ePenman: Math.max(ePen, 0),
      eNoTw: Math.max(eNoTw, 0),
      ePriestley: Math.max(ePt, 0),
    };
  });

  // массоперенос — независимый контроль, калибруется по сумме Пенмана
  const { a, b } = ph.calibrateMassTransfer(
    rows.map(r => r.ePenman), tw,
    rows.map(r => r.tdew), rows.map(r => r.u10));

  rows.forEach((r, i) => {
    r.eMassTransfer = r.ice ? 0
      : Math.max(ph.massTransfer(tw[i], r.tdew, r.u10, a, b), 0);
  });

  // отбрасываем период раскрутки модели температуры воды
  const result = rows.slice(spinupDays);
  result.massTransferCoeffs = { a, b };
  result.spinupDays = spinupDays;
  return result;
}

/** Суммы по каждому месяцу каждого года. */
export function aggregateMonthly(rows) {
  const map = new Map();

  for (const r of rows) {
    const key = `${r.year}-${String(r.month).padStart(2, '0')}`;
    if (!map.has(key)) {
      map.set(key, {
        year: r.year, month: r.month, E: 0, Emt: 0, EnoTw: 0,
        P: 0, taSum: 0, twSum: 0, gSum: 0, days: 0, iceDays: 0,
      });
    }
    const m = map.get(key);
    m.E += r.ePenman;
    m.Emt += r.eMassTransfer;
    m.EnoTw += r.eNoTw;
    m.P += r.prcp;
    m.taSum += r.ta;
    m.twSum += r.tw;
    m.gSum += r.G;
    m.days++;
    if (r.ice) m.iceDays++;
  }

  return [...map.values()]
    .map(m => ({
      ...m,
      ta: m.taSum / m.days,
      tw: m.twSum / m.days,
      G: m.gSum / m.days,
      deficit: m.E - m.P,
    }))
    .sort((x, y) => x.year - y.year || x.month - y.month);
}

/**
 * Годовые суммы. Неполные годы отбрасываются: их суммы несопоставимы с
 * полными и портят и шкалу графика, и оценку трендов.
 */
export function aggregateAnnual(rows) {
  const map = new Map();

  for (const r of rows) {
    if (!map.has(r.year)) {
      map.set(r.year, { year: r.year, E: 0, Emt: 0, EnoTw: 0, P: 0,
                        taSum: 0, twSum: 0, twMax: -99, days: 0, iceDays: 0 });
    }
    const a = map.get(r.year);
    a.E += r.ePenman;
    a.Emt += r.eMassTransfer;
    a.EnoTw += r.eNoTw;
    a.P += r.prcp;
    a.taSum += r.ta;
    a.twSum += r.tw;
    a.twMax = Math.max(a.twMax, r.tw);
    a.days++;
    if (r.ice) a.iceDays++;
  }

  return [...map.values()]
    .filter(a => a.days > 350)
    .map(a => {
      const E = Math.round(a.E);
      const P = Math.round(a.P);
      return {
        ...a,
        E, P,
        deficit: E - P,          // из округлённых — иначе числа не сходятся
        ta: a.taSum / a.days,
        tw: a.twSum / a.days,
        twMax: Math.round(a.twMax * 10) / 10,
      };
    })
    .sort((x, y) => x.year - y.year);
}

/** Средний многолетний годовой ход по 12 месяцам. */
export function climatology(monthly, fullYears) {
  const keep = new Set(fullYears);
  const out = [];

  for (let m = 1; m <= 12; m++) {
    const vals = monthly.filter(x => x.month === m && keep.has(x.year));
    if (!vals.length) { out.push({ month: m, E: 0, Emt: 0, P: 0, tw: 0, G: 0 }); continue; }
    const avg = k => vals.reduce((s, v) => s + v[k], 0) / vals.length;
    out.push({
      month: m, E: avg('E'), Emt: avg('Emt'), EnoTw: avg('EnoTw'),
      P: avg('P'), tw: avg('tw'), G: avg('G'),
      sd: Math.sqrt(vals.reduce((s, v) => s + Math.pow(v.E - avg('E'), 2), 0)
                    / vals.length),
    });
  }
  return out;
}

/** Линейный тренд методом наименьших квадратов. Возвращает наклон в год. */
export function linearTrend(years, values) {
  const n = years.length;
  if (n < 4) return null;

  const mx = years.reduce((a, b) => a + b, 0) / n;
  const my = values.reduce((a, b) => a + b, 0) / n;

  let num = 0, den = 0;
  for (let i = 0; i < n; i++) {
    num += (years[i] - mx) * (values[i] - my);
    den += Math.pow(years[i] - mx, 2);
  }
  if (den === 0) return null;

  const slope = num / den;

  // остаточная дисперсия → стандартная ошибка наклона
  let ss = 0;
  for (let i = 0; i < n; i++) {
    const pred = my + slope * (years[i] - mx);
    ss += Math.pow(values[i] - pred, 2);
  }
  const se = Math.sqrt(ss / (n - 2) / den);

  return { slope, se, significant: Math.abs(slope) > 2 * se };
}

/** Экспорт суточного ряда в CSV. */
export function toCSV(rows) {
  const cols = ['date', 'ta', 'tdew', 'u10', 'pKpa', 'rsDown', 'rlDown',
                'prcp', 'tw', 'hMix', 'G', 'Rn', 'ice',
                'ePenman', 'eMassTransfer', 'ePriestley', 'eNoTw'];
  const head = cols.join(',');
  const body = rows.map(r => cols.map(c => {
    const v = r[c];
    if (typeof v === 'number') return Math.round(v * 1e4) / 1e4;
    if (typeof v === 'boolean') return v ? 1 : 0;
    return v;
  }).join(',')).join('\n');
  return `${head}\n${body}`;
}

/** Экспорт месячных сумм в CSV. */
export function monthlyToCSV(monthly) {
  const cols = ['year', 'month', 'E', 'Emt', 'P', 'deficit', 'ta', 'tw',
                'days', 'iceDays'];
  const head = cols.join(',');
  const body = monthly.map(m => cols.map(c => {
    const v = m[c];
    return typeof v === 'number' ? Math.round(v * 100) / 100 : v;
  }).join(',')).join('\n');
  return `${head}\n${body}`;
}
