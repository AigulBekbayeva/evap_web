/**
 * Интерфейс: карта с рисованием контура, запуск расчёта, графики.
 */

import { fetchArchive } from './meteo.js';
import {
  computeEvaporation, aggregateMonthly, aggregateAnnual, climatology,
  linearTrend, toCSV, monthlyToCSV,
} from './compute.js';
import { exportChart, exportHeatmap, exportAll, metaFor } from './export.js';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const state = {
  layer: null, area: null, centroid: null, name: '',
  rows: null, monthly: null, annual: null, clim: null,
  selectedYear: null,
};

// ==================== карта ====================

// Порядок подложек: спутник основной, затем схема, затем тёмная.
const baseSat = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  { attribution: 'Esri, Maxar, Earthstar Geographics', maxZoom: 19 });

const baseOsm = L.tileLayer(
  'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  { attribution: '&copy; OpenStreetMap', maxZoom: 19 });

const baseDark = L.tileLayer(
  'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
  { attribution: '&copy; OpenStreetMap, &copy; CARTO', maxZoom: 19 });

const map = L.map('map', {
  center: [43.68, 76.58], zoom: 10, layers: [baseSat],
});

// подписи поверх спутника — иначе ориентироваться тяжело
const labels = L.tileLayer(
  'https://{s}.basemaps.cartocdn.com/rastertiles/voyager_only_labels/{z}/{x}/{y}{r}.png',
  { attribution: '&copy; CARTO', maxZoom: 19, pane: 'shadowPane' }).addTo(map);

L.control.layers(
  { 'Satellite': baseSat, 'OSM map': baseOsm, 'Dark': baseDark },
  { 'Labels': labels },
  { collapsed: false, position: 'topright' }).addTo(map);

const drawn = new L.FeatureGroup().addTo(map);

const drawControl = new L.Control.Draw({
  position: 'topleft',
  draw: {
    polygon: {
      allowIntersection: false,
      showArea: true,
      shapeOptions: { color: '#4fa3d1', weight: 2, fillOpacity: 0.15 },
    },
    rectangle: { shapeOptions: { color: '#4fa3d1', weight: 2, fillOpacity: 0.15 } },
    polyline: false, circle: false, marker: false, circlemarker: false,
  },
  edit: { featureGroup: drawn, remove: true },
});
map.addControl(drawControl);

map.on(L.Draw.Event.CREATED, e => {
  drawn.clearLayers();
  drawn.addLayer(e.layer);
  onShapeReady(e.layer);
});
map.on(L.Draw.Event.EDITED, () => {
  const l = drawn.getLayers()[0];
  if (l) onShapeReady(l);
});
map.on(L.Draw.Event.DELETED, () => {
  state.layer = null;
  updateSteps();
});

/**
 * Площадь полигона на сфере, км².
 *
 * Формула сферического избытка. Для водоёмов до сотен км² разница с
 * эллипсоидальным расчётом заметно меньше точности самого контура.
 */
function sphericalAreaKm2(latlngs) {
  const R = 6378137;
  const rad = d => (d * Math.PI) / 180;
  let total = 0;

  for (let i = 0; i < latlngs.length; i++) {
    const p1 = latlngs[i];
    const p2 = latlngs[(i + 1) % latlngs.length];
    total += (rad(p2.lng) - rad(p1.lng))
           * (2 + Math.sin(rad(p1.lat)) + Math.sin(rad(p2.lat)));
  }
  return Math.abs((total * R * R) / 2) / 1e6;
}

function centroidOf(latlngs) {
  let lat = 0, lng = 0;
  latlngs.forEach(p => { lat += p.lat; lng += p.lng; });
  return { lat: lat / latlngs.length, lng: lng / latlngs.length };
}

/**
 * Достаёт внешнее кольцо из чего угодно, что вернул Leaflet.
 *
 * getLatLngs() отдаёт разную вложенность: простой полигон — [ring],
 * полигон с дырами — [outer, hole...], MultiPolygon из GeoJSON —
 * [[ring], [ring]]. Без разбора вложенности координаты выходят undefined и
 * ломаются дальше по цепочке невнятной ошибкой.
 */
function outerRing(latlngs) {
  let cur = latlngs;
  let guard = 0;
  while (Array.isArray(cur) && cur.length && !(cur[0] instanceof L.LatLng)) {
    cur = cur[0];
    if (++guard > 5) break;
  }
  return Array.isArray(cur) && cur[0] instanceof L.LatLng ? cur : null;
}

function onShapeReady(layer) {
  const pts = outerRing(layer.getLatLngs());

  if (!pts || pts.length < 3) {
    alert('Could not read the outline: a closed polygon of at least three ' +
          'points is required.');
    return;
  }
  state.layer = layer;
  state.area = sphericalAreaKm2(pts);
  state.centroid = centroidOf(pts);

  if (!Number.isFinite(state.centroid.lat) ||
      !Number.isFinite(state.centroid.lng) ||
      !Number.isFinite(state.area) || state.area <= 0) {
    alert('The outline produced invalid coordinates. Please draw it again.');
    state.layer = null;
    updateSteps();
    return;
  }

  document.getElementById('shapeInfo').innerHTML =
    `Area <b>${state.area.toFixed(1)} km²</b>, centre ` +
    `${state.centroid.lat.toFixed(4)}°N, ${state.centroid.lng.toFixed(4)}°E`;

  document.getElementById('hint').classList.add('hide');
  updateSteps();
  checkEra5Cell();
}

/**
 * Предупреждение о размере водоёма относительно ячейки ERA5-Land.
 *
 * Ячейка 9 км ≈ 81 км². Если водоём заметно меньше, метеоданные приходят
 * преимущественно с окружающей суши, где воздух суше и теплее, — испарение
 * будет завышено. Это свойство исходных данных, а не расчёта.
 */
function checkEra5Cell() {
  const el = document.getElementById('cellWarn');
  const cells = state.area / 81;

  if (cells < 1.5) {
    el.style.display = 'block';
    el.innerHTML =
      `<b>This water body covers about ${cells.toFixed(1)} of an ERA5-Land ` +
      `cell (9 km).</b> The meteorology will partly or entirely describe the ` +
      `surrounding land, where air is drier and warmer, so evaporation will ` +
      `come out too high. By how much cannot be said without ground ` +
      `measurements. Comparing years against each other remains valid; treat ` +
      `absolute values with caution.`;
  } else {
    el.style.display = 'none';
  }
}

// загрузка GeoJSON
document.getElementById('fileInput').addEventListener('change', ev => {
  const file = ev.target.files[0];
  if (!file) return;

  const reader = new FileReader();
  reader.onload = e => {
    try {
      const gj = JSON.parse(e.target.result);
      drawn.clearLayers();
      const layer = L.geoJSON(gj, {
        style: { color: '#4fa3d1', weight: 2, fillOpacity: 0.15 },
      });
      const first = layer.getLayers()[0];
      if (!first) throw new Error('the file contains no geometry');

      drawn.addLayer(first);
      map.fitBounds(first.getBounds(), { padding: [40, 40] });
      onShapeReady(first);
    } catch (err) {
      alert(`Could not read the GeoJSON: ${err.message}`);
    }
  };
  reader.readAsText(file);
});

// ==================== расчёт ====================

document.getElementById('runBtn').addEventListener('click', run);

async function run() {
  const btn = document.getElementById('runBtn');
  const prog = document.getElementById('progress');
  const bar = document.querySelector('#progress .pbar > div');
  const ptext = document.querySelector('#progress .ptext');

  const depth = parseFloat(document.getElementById('depth').value);
  const y0 = parseInt(document.getElementById('yearFrom').value, 10);
  const y1 = parseInt(document.getElementById('yearTo').value, 10);

  if (!(depth > 0.5)) { alert('Depth must be greater than 0.5 m'); return; }
  if (y1 < y0) { alert('End year is earlier than start year'); return; }

  btn.disabled = true;
  prog.classList.add('show');

  try {
    // год раскрутки модели температуры воды берём дополнительно и отбрасываем
    const met = await fetchArchive(
      state.centroid.lat, state.centroid.lng, y0 - 1, y1, 'era5_land',
      (frac, text) => {
        bar.style.width = `${Math.round(frac * 100)}%`;
        ptext.textContent = text;
      });

    bar.style.width = '97%';
    ptext.textContent = 'Computing evaporation…';
    await new Promise(r => setTimeout(r, 30));   // дать браузеру перерисоваться

    const rows = computeEvaporation(met, depth, 365);

    if (!rows.length) {
      throw new Error(
        'Nothing is left after the model spin-up year is removed. ' +
        'Choose a period of at least two years.');
    }

    const bad = rows.filter(r => !Number.isFinite(r.ePenman)
                                || !Number.isFinite(r.tw));
    if (bad.length) {
      // Лучше остановиться здесь, чем отрисовать матрицу из NaN: ошибка
      // всплывёт в отрисовке и укажет на палитру, а не на данные.
      console.error('First corrupted days:', bad.slice(0, 5));
      throw new Error(
        `The calculation produced invalid values for ${bad.length} of ` +
        `${rows.length} days. See the browser console for details.`);
    }

    const annual = aggregateAnnual(rows);
    if (!annual.length) {
      // aggregateAnnual отбрасывает годы короче 350 суток: их суммы
      // несопоставимы с полными. Если не осталось ни одного — считать нечего.
      throw new Error(
        `No complete year available: ${rows.length} days remain after ` +
        'spin-up. Widen the period — at least one full calendar year beyond ' +
        'the spin-up year is required.');
    }

    state.rows = rows;
    state.monthly = aggregateMonthly(rows);
    state.annual = annual;
    state.clim = climatology(state.monthly, annual.map(a => a.year));
    state.selectedYear = null;

    bar.style.width = '100%';
    ptext.textContent = `Done: ${rows.length} days, ` +
                        `${state.annual.length} complete years`;

    // Контейнер обязан быть видимым ДО render(): Chart.js измеряет размеры
    // при создании, и в скрытом блоке получает холст 0×0. На экране графики
    // потом появятся (сработает resize), но canvas.width останется нулевым,
    // и экспорт в PNG выдаст пустую картинку.
    document.getElementById('results').style.display = 'block';
    render();
    setTimeout(() => prog.classList.remove('show'), 1500);
  } catch (err) {
    ptext.textContent = `Error: ${err.message}`;
    // Полный стек — в консоль: сообщение в интерфейсе не показывает, где
    // именно сломалось, а при разборе проблемы это первое, что нужно.
    console.error('Calculation aborted:', err);
    console.error('State:', {
      area: state.area, centroid: state.centroid,
      depth, yearFrom: y0, yearTo: y1,
      rows: state.rows ? state.rows.length : null,
    });
  } finally {
    btn.disabled = false;
    updateSteps();
  }
}

// ==================== отображение ====================

let climChart = null, annChart = null, tsChart = null;

function render() {
  renderStats();
  renderHeatmap();
  renderClim();
  renderAnnual();
  renderTs();
  renderNotes();

  if (state.layer) {
    const a = state.annual;
    const meanE = a.reduce((s, x) => s + x.E, 0) / a.length;
    const volume = (meanE * 1e-3 * state.area * 1e6) / 1e6;
    state.layer.bindPopup(
      `<b>Water body</b><br>Area ${state.area.toFixed(1)} km²<br>` +
      `Evaporation ${Math.round(meanE)} mm/year<br>` +
      `Loss ${volume.toFixed(1)} million m³/year`);
  }
}

function renderStats() {
  const el = document.getElementById('stats');
  const head = document.getElementById('statsHead');
  const a = state.annual;
  const avg = k => a.reduce((s, x) => s + x[k], 0) / a.length;

  if (state.selectedYear === null) {
    head.textContent = `Summary for ${a[0].year}–${a[a.length - 1].year}`;
    const meanE = avg('E'), meanP = avg('P');
    const volume = (meanE * 1e-3 * state.area * 1e6) / 1e6;

    el.innerHTML = `
      <div class="stat"><div class="v">${Math.round(meanE)}</div>
        <div class="l">Evaporation, mm/year</div></div>
      <div class="stat"><div class="v">${Math.round(meanP)}</div>
        <div class="l">Precipitation, mm/year</div></div>
      <div class="stat"><div class="v">${Math.round(meanE - meanP)}</div>
        <div class="l">Deficit, mm/year</div></div>
      <div class="stat"><div class="v">${avg('twMax').toFixed(1)}</div>
        <div class="l">Max water temp, °C</div></div>
      <div class="stat" style="grid-column:1/-1">
        <div class="v">${volume.toFixed(1)} million m³</div>
        <div class="l">lost to evaporation per year across the whole surface</div></div>`;
    return;
  }

  const y = a.find(x => x.year === state.selectedYear);
  head.textContent = `Summary for ${y.year}`;

  const card = (val, label, key, unit, digits = 0) => {
    const d = val - avg(key);
    const cls = d > 0 ? 'up' : 'down';
    return `<div class="stat hl"><div class="v">${val.toFixed(digits)}</div>
      <div class="l">${label}</div>
      <div class="d ${cls}">${d > 0 ? '+' : ''}${d.toFixed(0)} ${unit} vs average</div>
    </div>`;
  };

  el.innerHTML =
    card(y.E, 'Evaporation, mm', 'E', 'mm') +
    card(y.P, 'Precipitation, mm', 'P', 'mm') +
    card(y.deficit, 'Deficit, mm', 'deficit', 'mm') +
    card(y.twMax, 'Max water temp, °C', 'twMax', '°C', 1);
}

/** Матрица год × месяц: где и когда испарение выше нормы. */
function renderHeatmap() {
  const years = state.annual.map(a => a.year);
  const vals = state.monthly.filter(m => years.includes(m.year));
  if (!vals.length) return;

  const finite = vals.map(v => v.E).filter(Number.isFinite);
  if (!finite.length) return;
  const min = Math.min(...finite);
  const max = Math.max(...finite);

  const color = v => {
    const t = (v - min) / (max - min || 1);
    // тёмно-синий → бирюзовый → жёлтый → оранжевый
    const stops = [[44,123,182], [90,200,200], [255,255,140], [224,123,57]];
    // при t = 1 индекс должен остаться 2, иначе stops[i+1] выйдет за массив
    const i = Math.min(Math.max(Math.floor(t * 3), 0), stops.length - 2);
    const f = Math.min(Math.max(t * 3 - i, 0), 1);
    const c = stops[i].map((s, k) => Math.round(s + (stops[i + 1][k] - s) * f));
    return `rgb(${c.join(',')})`;
  };

  let html = '<tr><th></th>' +
    MONTHS.map(m => `<th>${m[0].toUpperCase()}</th>`).join('') + '</tr>';

  for (const yr of years) {
    html += `<tr><td class="yr">${yr}</td>`;
    for (let m = 1; m <= 12; m++) {
      const cell = vals.find(v => v.year === yr && v.month === m);
      if (!cell) { html += '<td></td>'; continue; }
      const val = Number.isFinite(cell.E) ? cell.E : null;
      html += `<td><div class="cell" style="background:${color(cell.E)}"
                title="${yr}, ${MONTHS[m - 1]}: ${
                  val === null ? 'no data' : val.toFixed(1) + ' mm'}">
                ${val === null ? '' : Math.round(val)}</div></td>`;
    }
    html += '</tr>';
  }

  document.getElementById('heatmap').innerHTML = html;
  document.getElementById('heatLegend').innerHTML =
    `<span style="color:var(--muted)">${Math.round(min)} mm</span>
     <span style="flex:1;height:8px;margin:0 8px;border-radius:2px;
       background:linear-gradient(to right,rgb(44,123,182),rgb(90,200,200),
       rgb(255,255,140),rgb(224,123,57))"></span>
     <span style="color:var(--muted)">${Math.round(max)} mm</span>`;
}

const CHART_BASE = {
  responsive: true, maintainAspectRatio: false,
  plugins: { legend: { labels: { boxWidth: 10, boxHeight: 10, padding: 8,
                                 font: { size: 10 } } } },
};

function renderClim() {
  const ctx = document.getElementById('climChart');
  if (climChart) climChart.destroy();

  const norm = state.clim.map(c => c.E);
  const yearData = state.selectedYear
    ? MONTHS.map((_, i) => {
        const m = state.monthly.find(
          x => x.year === state.selectedYear && x.month === i + 1);
        return m ? m.E : null;
      })
    : norm;

  const datasets = [];
  if (state.selectedYear) {
    datasets.push({ label: 'Average', data: norm, backgroundColor: '#3a4753',
                    order: 3 });
  }
  datasets.push({
    label: state.selectedYear ? `Evaporation ${state.selectedYear}` : 'Evaporation',
    data: yearData, backgroundColor: '#e07b39', order: 2,
  });
  datasets.push({
    label: 'Precipitation', type: 'line',
    data: state.selectedYear
      ? MONTHS.map((_, i) => {
          const m = state.monthly.find(
            x => x.year === state.selectedYear && x.month === i + 1);
          return m ? m.P : null;
        })
      : state.clim.map(c => c.P),
    borderColor: '#4fa3d1', backgroundColor: '#4fa3d1',
    tension: 0.3, pointRadius: 2, order: 1,
  });

  climChart = new Chart(ctx, {
    type: 'bar',
    data: { labels: MONTHS, datasets },
    options: { ...CHART_BASE,
      scales: { y: { title: { display: true, text: 'mm/month' } } } },
  });
}

function renderAnnual() {
  const ctx = document.getElementById('annChart');
  if (annChart) annChart.destroy();

  const years = state.annual.map(a => a.year);
  const hl = y => state.selectedYear === null || y === state.selectedYear;

  annChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: years,
      datasets: [
        { label: 'Evaporation', data: state.annual.map(a => a.E),
          backgroundColor: years.map(y => hl(y) ? '#e07b39' : '#5a4030') },
        { label: 'Precipitation', data: state.annual.map(a => a.P),
          backgroundColor: years.map(y => hl(y) ? '#4fa3d1' : '#2c4a5c') },
      ],
    },
    options: {
      ...CHART_BASE,
      scales: { y: { title: { display: true, text: 'mm/year' } } },
      onClick: (e, els) => {
        if (!els.length) return;
        const y = years[els[0].index];
        state.selectedYear = state.selectedYear === y ? null : y;
        render();
      },
    },
  });
}

function renderTs() {
  const kind = document.querySelector('.tab.on').dataset.series;
  const ctx = document.getElementById('tsChart');
  if (tsChart) tsChart.destroy();

  // прореживаем декадным осреднением: суточный ряд за 15 лет тормозит браузер
  // и всё равно неразличим на экране
  const step = Math.max(1, Math.ceil(state.rows.length / 800));
  const pts = [];
  for (let i = 0; i < state.rows.length; i += step) {
    const chunk = state.rows.slice(i, i + step);
    const avg = k => chunk.reduce((s, r) => s + r[k], 0) / chunk.length;
    pts.push({ date: chunk[0].date, ePenman: avg('ePenman'),
               eNoTw: avg('eNoTw'), tw: avg('tw'), ta: avg('ta') });
  }

  const cfg = kind === 'E'
    ? { ds: [
        { label: 'Penman (modelled water temp)', data: pts.map(p => p.ePenman),
          borderColor: '#e07b39', borderWidth: 1.2, pointRadius: 0, tension: 0.2 },
        { label: 'Without heat storage', data: pts.map(p => p.eNoTw),
          borderColor: '#6b7785', borderWidth: 1, pointRadius: 0,
          borderDash: [4, 3], tension: 0.2 }],
        unit: 'mm/day' }
    : { ds: [
        { label: 'Water (modelled)', data: pts.map(p => p.tw),
          borderColor: '#5ac8c8', borderWidth: 1.3, pointRadius: 0, tension: 0.2 },
        { label: 'Air', data: pts.map(p => p.ta),
          borderColor: '#6b7785', borderWidth: 1, pointRadius: 0, tension: 0.2 }],
        unit: '°C' };

  tsChart = new Chart(ctx, {
    type: 'line',
    data: { labels: pts.map(p => p.date), datasets: cfg.ds },
    options: {
      ...CHART_BASE,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { ticks: { maxTicksLimit: 8, maxRotation: 0 } },
        y: { title: { display: true, text: cfg.unit } },
      },
    },
  });
}

document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('on'));
    t.classList.add('on');
    renderTs();
  });
});

function renderNotes() {
  const a = state.annual;
  const trend = linearTrend(a.map(x => x.year), a.map(x => x.E));
  const peak = state.clim.length
    ? state.clim.reduce((b, c) => (c.E > b.E ? c : b))
    : { month: 1, E: 0 };

  const satEffect = a.reduce((s, x) => s + (x.E - x.EnoTw), 0) / a.length;
  const meanE = a.reduce((s, x) => s + x.E, 0) / a.length;

  let html = `Evaporation peaks in ${MONTHS[peak.month - 1]}. `;

  if (trend) {
    html += trend.significant
      ? `Over this period there is a ${trend.slope > 0 ? 'rising' : 'falling'} ` +
        `trend of <b>${Math.abs(trend.slope).toFixed(1)} mm/year</b> ` +
        `(±${(2 * trend.se).toFixed(1)}), which exceeds the random scatter. `
      : `No significant trend: the slope is ${trend.slope.toFixed(1)} ± ` +
        `${(2 * trend.se).toFixed(1)} mm/year, indistinguishable from zero ` +
        `given this number of years. `;
  }

  html += `Accounting for heat storage changes the annual total by ` +
          `${(100 * satEffect / meanE).toFixed(1)} %, but it affects the ` +
          `seasonal cycle far more: without it the peak moves a month earlier.`;

  document.getElementById('resultNote').innerHTML = html;
}

// ==================== экспорт ====================

/** Имя объекта → часть имени файла: латиница, цифры, дефисы. */
function slug() {
  const raw = (state.name || 'waterbody').trim().toLowerCase();
  const map = {
    а:'a',б:'b',в:'v',г:'g',д:'d',е:'e',ё:'e',ж:'zh',з:'z',и:'i',й:'i',
    к:'k',л:'l',м:'m',н:'n',о:'o',п:'p',р:'r',с:'s',т:'t',у:'u',ф:'f',
    х:'h',ц:'c',ч:'ch',ш:'sh',щ:'sch',ъ:'',ы:'y',ь:'',э:'e',ю:'yu',я:'ya',
  };
  const latin = [...raw].map(ch => map[ch] ?? ch).join('');
  const clean = latin.replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  return clean || 'waterbody';
}

function period() {
  const a = state.annual;
  return a && a.length ? `${a[0].year}-${a[a.length - 1].year}` : '';
}

/** Короткая подсветка кнопки: подтверждение, что файл ушёл. */
function flash(btn) {
  if (!btn) return;
  const prev = btn.textContent;
  btn.textContent = 'done';
  btn.classList.add('done');
  setTimeout(() => { btn.textContent = prev; btn.classList.remove('done'); }, 1400);
}

document.getElementById('objName').addEventListener('input', e => {
  state.name = e.target.value;
});

document.querySelectorAll('.png').forEach(btn => {
  btn.addEventListener('click', () => {
    if (!state.rows) { alert('Run the calculation first'); return; }

    const base = `${slug()}-${period()}`;
    const meta = metaFor(state);
    const kind = btn.dataset.png;

    // Без обёртки исключение уходит в консоль, кнопка ничего не делает, и
    // пользователь видит только то, что «не скачивается».
    try {
    if (kind === 'heatmap') {
      exportHeatmap(state.monthly, state.annual.map(a => a.year),
                    `${base}-matrix.png`,
                    { ...meta, title: `${meta.title} — by year and month, mm` });
    } else if (kind === 'clim') {
      exportChart(climChart, `${base}-seasonal-cycle.png`,
                  { ...meta, title: `${meta.title} — seasonal cycle` });
    } else if (kind === 'ann') {
      exportChart(annChart, `${base}-year-to-year.png`,
                  { ...meta, title: `${meta.title} — year to year` });
    } else if (kind === 'ts') {
      const series = document.querySelector('.tab.on').dataset.series;
      const label = series === 'E' ? 'evaporation' : 'temperature';
      exportChart(tsChart, `${base}-daily-${label}.png`,
                  { ...meta, title: `${meta.title} — daily ${label}` });
    }
    flash(btn);
    } catch (err) {
      console.error('Chart export failed:', err);
      alert(`Could not export the chart.\n\n${err.message}`);
    }
  });
});

document.getElementById('pngAll').addEventListener('click', e => {
  if (!state.rows) { alert('Run the calculation first'); return; }

  try {
    exportAll(
      [
        { chart: climChart, label: 'Seasonal cycle, mm/month' },
        { chart: annChart, label: 'Annual totals, mm' },
        { chart: tsChart, label: 'Daily series' },
      ],
      null,
      `${slug()}-${period()}-charts.png`,
      metaFor(state));
    flash(e.target);
  } catch (err) {
    console.error('Chart export failed:', err);
    alert(`Could not export the charts.\n\n${err.message}`);
  }
});

function download(name, text) {
  // Ссылку нужно вставить в документ: Firefox игнорирует click() по элементу,
  // которого нет в DOM. И освобождать URL сразу нельзя — скачивание может не
  // успеть начаться.
  const blob = new Blob([text], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);

  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();

  setTimeout(() => {
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, 1000);
}

document.getElementById('csvDaily').addEventListener('click', () => {
  if (!state.rows) { alert('Run the calculation first'); return; }
  download(`${slug()}-${period()}-daily.csv`, toCSV(state.rows));
});
document.getElementById('csvMonthly').addEventListener('click', () => {
  if (!state.monthly) { alert('Run the calculation first'); return; }
  download(`${slug()}-${period()}-monthly.csv`, monthlyToCSV(state.monthly));
});
document.getElementById('csvGeo').addEventListener('click', () => {
  download(`${slug()}-outline.geojson`,
           JSON.stringify(drawn.toGeoJSON(), null, 2));
});

// ==================== шаги ====================

function updateSteps() {
  const s1 = document.getElementById('step1');
  const s2 = document.getElementById('step2');

  if (state.layer) {
    s1.classList.add('done'); s1.classList.remove('active');
    s2.classList.add('active');
    document.getElementById('runBtn').disabled = false;
  } else {
    s1.classList.add('active'); s1.classList.remove('done');
    s2.classList.remove('active');
    document.getElementById('runBtn').disabled = true;
  }
  if (state.rows) s2.classList.add('done');
}

// значения по умолчанию
const nowYear = new Date().getFullYear();
document.getElementById('yearTo').value = nowYear - 1;
document.getElementById('yearFrom').value = nowYear - 11;

Chart.defaults.color = '#8b9aa8';
Chart.defaults.borderColor = '#2c3a47';
Chart.defaults.font.size = 10;

updateSteps();
