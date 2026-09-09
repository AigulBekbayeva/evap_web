/**
 * Chart export to PNG.
 *
 * Chart.js draws on a canvas with a transparent background, so a plain
 * toDataURL produces an image whose dark labels sit on nothing and become
 * unreadable on a white slide. Here the canvas is rebuilt: background fill,
 * title, a caption carrying the water body and period, and a source line.
 *
 * The year-by-month matrix is an HTML table rather than a canvas, so it is
 * rendered to a canvas separately.
 */

const BG = '#161d24';
const CARD = '#1e2831';
const TEXT = '#e6edf3';
const MUTED = '#8b9aa8';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** Export scale. 2x keeps text legible in slides and in print. */
const SCALE = 2;

const CREDIT = 'Developed by Aigul Bekbayeva';

const FONT = '-apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';

function download(canvas, filename) {
  canvas.toBlob(blob => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }, 'image/png');
}

/**
 * Wraps a rendered canvas in framing: background, title, captions.
 *
 * @param {HTMLCanvasElement} source  the body to draw
 * @param {object} meta  {title, subtitle, footer}
 */
function frame(source, meta) {
  const padX = 28 * SCALE;
  const padTop = 26 * SCALE;
  const titleH = meta.title ? 30 * SCALE : 0;
  const subH = meta.subtitle ? 22 * SCALE : 0;
  const footH = meta.footer ? 44 * SCALE : 0;
  const padBottom = 20 * SCALE;

  const out = document.createElement('canvas');
  out.width = source.width + padX * 2;
  out.height = source.height + padTop + titleH + subH + footH + padBottom;

  const ctx = out.getContext('2d');
  ctx.fillStyle = BG;
  ctx.fillRect(0, 0, out.width, out.height);
  ctx.textAlign = 'left';
  ctx.textBaseline = 'top';

  let y = padTop;

  if (meta.title) {
    ctx.fillStyle = TEXT;
    ctx.font = `600 ${17 * SCALE}px ${FONT}`;
    ctx.fillText(meta.title, padX, y);
    y += titleH;
  }

  if (meta.subtitle) {
    ctx.fillStyle = MUTED;
    ctx.font = `${12.5 * SCALE}px ${FONT}`;
    ctx.fillText(meta.subtitle, padX, y);
    y += subH;
  }

  ctx.drawImage(source, padX, y);
  y += source.height;

  if (meta.footer) {
    ctx.fillStyle = MUTED;
    ctx.font = `${11 * SCALE}px ${FONT}`;
    ctx.fillText(meta.footer, padX, y + 12 * SCALE);
    ctx.fillText(CREDIT, padX, y + 27 * SCALE);
  }

  return out;
}

/** A copy of the Chart.js canvas with an opaque background behind it. */
function opaqueChart(chart) {
  const src = chart.canvas;
  const c = document.createElement('canvas');
  c.width = src.width;
  c.height = src.height;

  const ctx = c.getContext('2d');
  ctx.fillStyle = CARD;
  ctx.fillRect(0, 0, c.width, c.height);
  ctx.drawImage(src, 0, 0);
  return c;
}

/**
 * Export a Chart.js chart.
 *
 * Chart.js already renders at devicePixelRatio, so its canvas is usually
 * larger than its CSS size and needs no extra scaling. Frame text is drawn at
 * SCALE so that it matches that density.
 */
export function exportChart(chart, filename, meta) {
  if (!chart) return;
  download(frame(opaqueChart(chart), meta), filename);
}

/**
 * Render the year-by-month matrix to a canvas.
 *
 * Mirrors the palette used on screen. Cells without data stay background
 * coloured, so the image reads the same way the page does.
 */
export function exportHeatmap(monthly, years, filename, meta) {
  const cellW = 46 * SCALE;
  const cellH = 26 * SCALE;
  const labelW = 54 * SCALE;
  const headH = 24 * SCALE;
  const legendH = 46 * SCALE;

  const vals = monthly
    .filter(m => years.includes(m.year) && Number.isFinite(m.E))
    .map(m => m.E);
  if (!vals.length) return;

  const body = document.createElement('canvas');
  body.width = labelW + cellW * 12;
  body.height = headH + cellH * years.length + legendH;

  const ctx = body.getContext('2d');
  ctx.fillStyle = CARD;
  ctx.fillRect(0, 0, body.width, body.height);
  ctx.textBaseline = 'middle';

  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const stops = [[44, 123, 182], [90, 200, 200], [255, 255, 140], [224, 123, 57]];

  const color = v => {
    if (!Number.isFinite(v)) return CARD;
    const span = max - min;
    const t = span > 0 ? Math.min(Math.max((v - min) / span, 0), 1) : 0.5;
    const i = Math.min(Math.floor(t * 3), stops.length - 2);
    const f = t * 3 - i;
    const c = stops[i].map((s, k) => Math.round(s + (stops[i + 1][k] - s) * f));
    return `rgb(${c.join(',')})`;
  };

  ctx.fillStyle = MUTED;
  ctx.font = `${11 * SCALE}px ${FONT}`;
  ctx.textAlign = 'center';
  MONTHS.forEach((m, i) => {
    ctx.fillText(m, labelW + cellW * i + cellW / 2, headH / 2);
  });

  years.forEach((yr, r) => {
    const y = headH + cellH * r;

    ctx.fillStyle = MUTED;
    ctx.textAlign = 'right';
    ctx.font = `${11 * SCALE}px ${FONT}`;
    ctx.fillText(String(yr), labelW - 8 * SCALE, y + cellH / 2);

    for (let m = 1; m <= 12; m++) {
      const cell = monthly.find(x => x.year === yr && x.month === m);
      const x = labelW + cellW * (m - 1);

      ctx.fillStyle = cell ? color(cell.E) : CARD;
      ctx.fillRect(x + 1.5 * SCALE, y + 1.5 * SCALE,
                   cellW - 3 * SCALE, cellH - 3 * SCALE);

      if (cell && Number.isFinite(cell.E)) {
        ctx.fillStyle = '#0b0f14';
        ctx.font = `600 ${10.5 * SCALE}px ${FONT}`;
        ctx.textAlign = 'center';
        ctx.fillText(String(Math.round(cell.E)), x + cellW / 2, y + cellH / 2);
      }
    }
  });

  // colour scale
  const lgY = headH + cellH * years.length + 16 * SCALE;
  const lgX = labelW;
  const lgW = cellW * 12 - 100 * SCALE;

  const grad = ctx.createLinearGradient(lgX + 46 * SCALE, 0,
                                        lgX + 46 * SCALE + lgW, 0);
  stops.forEach((c, i) => {
    grad.addColorStop(i / (stops.length - 1), `rgb(${c.join(',')})`);
  });
  ctx.fillStyle = grad;
  ctx.fillRect(lgX + 46 * SCALE, lgY, lgW, 9 * SCALE);

  ctx.fillStyle = MUTED;
  ctx.font = `${10.5 * SCALE}px ${FONT}`;
  ctx.textAlign = 'right';
  ctx.fillText(`${Math.round(min)}`, lgX + 42 * SCALE, lgY + 5 * SCALE);
  ctx.textAlign = 'left';
  ctx.fillText(`${Math.round(max)} mm`, lgX + 52 * SCALE + lgW, lgY + 5 * SCALE);

  download(frame(body, meta), filename);
}

/**
 * One image holding every chart.
 *
 * For a talk or a post a single picture beats four files. Charts are laid out
 * in two columns.
 */
export function exportAll(charts, heatmapCanvas, filename, meta) {
  const gap = 18 * SCALE;
  const labelH = 20 * SCALE;
  const items = charts.filter(Boolean);
  if (!items.length) return;

  const canvases = items.map(c => opaqueChart(c.chart));
  const colW = Math.max(...canvases.map(c => c.width));
  const cols = canvases.length > 1 ? 2 : 1;
  const rows = Math.ceil(canvases.length / cols);
  const rowH = Math.max(...canvases.map(c => c.height)) + labelH;

  const gridW = colW * cols + gap * (cols - 1);
  const hmH = heatmapCanvas ? heatmapCanvas.height + gap : 0;

  const body = document.createElement('canvas');
  body.width = Math.max(gridW, heatmapCanvas ? heatmapCanvas.width : 0);
  body.height = hmH + rowH * rows + gap * (rows - 1);

  const ctx = body.getContext('2d');
  ctx.fillStyle = BG;
  ctx.fillRect(0, 0, body.width, body.height);

  if (heatmapCanvas) ctx.drawImage(heatmapCanvas, 0, 0);

  ctx.textAlign = 'left';
  ctx.textBaseline = 'top';

  canvases.forEach((c, i) => {
    const col = i % cols;
    const row = Math.floor(i / cols);
    const x = col * (colW + gap);
    const y = hmH + row * (rowH + gap);

    ctx.fillStyle = MUTED;
    ctx.font = `600 ${12 * SCALE}px ${FONT}`;
    ctx.fillText(items[i].label, x + 4 * SCALE, y);
    ctx.drawImage(c, x, y + labelH);
  });

  download(frame(body, meta), filename);
}

/** A single caption shared by every export. */
export function metaFor(state, extra = '') {
  const a = state.annual || [];
  const period = a.length ? `${a[0].year}–${a[a.length - 1].year}` : '';
  const meanE = a.length
    ? Math.round(a.reduce((s, x) => s + x.E, 0) / a.length) : null;

  return {
    title: state.name || 'Open-water evaporation',
    subtitle: [
      state.area ? `${state.area.toFixed(0)} km²` : null,
      state.centroid
        ? `${state.centroid.lat.toFixed(3)}°N, ${state.centroid.lng.toFixed(3)}°E`
        : null,
      period,
      meanE ? `${meanE} mm/year on average` : null,
      extra || null,
    ].filter(Boolean).join(' · '),
    footer: 'Penman method · ERA5 reanalysis (ECMWF) via Open-Meteo · '
          + 'water temperature from a heat-balance model',
  };
}
