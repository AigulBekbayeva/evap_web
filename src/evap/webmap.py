"""
Интерактивная веб-карта результатов.

Генерирует один самодостаточный HTML-файл: Leaflet, контур водоёма, слои
Earth Engine (температура воды, частота обводнения), панель с графиками и
сводкой. Внешних зависимостей у результата нет — только CDN для Leaflet и
Chart.js, всё остальное встроено в файл.

Тайлы Earth Engine отдаются через getMapId(): GEE публикует временный
тайловый сервис, ссылка на который зашивается в HTML. Ссылки живут около
суток, поэтому для постоянной публикации карту надо либо перегенерировать
по расписанию, либо экспортировать растры в GeoTIFF и раздавать самостоятельно.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

# --- палитры ---------------------------------------------------------------

PALETTE_TEMP = ["#2c7bb6", "#00a6ca", "#00ccbc", "#90eb9d", "#ffff8c",
                "#f9d057", "#f29e2e", "#e76818", "#d7191c"]
PALETTE_WATER = ["#f7fbff", "#c6dbef", "#6baed6", "#2171b5", "#08306b"]


def _ee_tile_url(image, vis_params: dict) -> str | None:
    """Публикует изображение как тайловый слой и возвращает шаблон URL."""
    try:
        mapid = image.getMapId(vis_params)
        return mapid["tile_fetcher"].url_format
    except Exception as exc:  # pragma: no cover
        print(f"  ! не удалось получить тайлы GEE: {exc}")
        return None


def build_ee_layers(gdf, start: str, end: str,
                    erode_metres: float = 60.0) -> list[dict]:
    """
    Готовит растровые слои Earth Engine для карты.

    Возвращает список словарей {name, url, legend}. Если Earth Engine
    недоступен, возвращает пустой список — карта всё равно построится,
    только без растров.
    """
    try:
        import ee
        from . import waterbody as wb
    except ImportError:
        return []

    try:
        ee.Initialize()
    except Exception:
        print("  ! Earth Engine не инициализирован, растровые слои пропущены")
        return []

    layers = []
    geom = wb.to_ee_geometry(gdf)
    try:
        geom_er = wb.to_ee_geometry(wb.erode(gdf, erode_metres))
    except ValueError:
        geom_er = geom

    # --- температура поверхности воды, летняя медиана ---
    def prep_st(img):
        qa = img.select("QA_PIXEL")
        clear = (qa.bitwiseAnd(1 << 1).eq(0)
                 .And(qa.bitwiseAnd(1 << 2).eq(0))
                 .And(qa.bitwiseAnd(1 << 3).eq(0))
                 .And(qa.bitwiseAnd(1 << 4).eq(0)))
        st = (img.select("ST_B10").multiply(0.00341802).add(149.0)
              .subtract(273.15).rename("tw"))
        return st.updateMask(clear)

    ls = (ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
          .merge(ee.ImageCollection("LANDSAT/LC09/C02/T1_L2"))
          .filterBounds(geom).filterDate(start, end)
          .filter(ee.Filter.lt("CLOUD_COVER", 60))
          .filter(ee.Filter.calendarRange(6, 8, "month"))
          .map(prep_st))

    tw_median = ls.median().clip(geom_er)
    url = _ee_tile_url(tw_median, {"min": 15, "max": 30,
                                   "palette": PALETTE_TEMP})
    if url:
        layers.append({
            "name": "Температура воды, лето (Landsat)",
            "url": url,
            "legend": {"title": "Tw, °C", "min": 15, "max": 30,
                       "palette": PALETTE_TEMP},
            "default": True,
        })

    # --- частота обводнения: как часто пиксель был водой ---
    def water_mask(img):
        scaled = img.select(["SR_B3", "SR_B6"]).multiply(2.75e-05).add(-0.2)
        mndwi = scaled.normalizedDifference(["SR_B3", "SR_B6"])
        return mndwi.gt(0.0).rename("water")

    coll = (ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
            .merge(ee.ImageCollection("LANDSAT/LC09/C02/T1_L2"))
            .filterBounds(geom).filterDate(start, end)
            .filter(ee.Filter.lt("CLOUD_COVER", 40)))

    freq = coll.map(water_mask).mean().clip(geom.buffer(2000))
    url = _ee_tile_url(freq, {"min": 0, "max": 1, "palette": PALETTE_WATER})
    if url:
        layers.append({
            "name": "Частота обводнения",
            "url": url,
            "legend": {"title": "доля снимков", "min": 0, "max": 1,
                       "palette": PALETTE_WATER},
            "default": False,
        })

    return layers


def prepare_series(df: pd.DataFrame, max_points: int = 900) -> dict:
    """
    Готовит ряды для графиков, прореживая до разумного объёма.

    Суточный ряд за 25 лет — 9000 точек, что тормозит браузер и всё равно
    неразличимо на экране. Прореживание идёт декадным осреднением, а не
    выборкой каждой n-й точки: так сохраняется сезонный ход без алиасинга.
    """
    d = df.dropna(subset=["E_penman"]).copy()

    if len(d) > max_points:
        freq = "10D" if len(d) / 36.5 < max_points else "MS"
        d = (d.set_index("date").resample(freq)
             .agg(E_penman=("E_penman", "mean"),
                  E_masstransfer=("E_masstransfer", "mean"),
                  E_nosat=("E_nosat", "mean"),
                  tw=("tw", "mean"), ta=("ta", "mean"),
                  prcp=("prcp", "sum"))
             .reset_index().dropna(subset=["E_penman"]))

    obs = df.dropna(subset=["tw_obs"]) if "tw_obs" in df.columns else pd.DataFrame()

    return {
        "dates": d["date"].dt.strftime("%Y-%m-%d").tolist(),
        "E": d["E_penman"].round(3).tolist(),
        "E_mt": d.get("E_masstransfer", pd.Series(dtype=float)).round(3).tolist(),
        "E_nosat": d.get("E_nosat", pd.Series(dtype=float)).round(3).tolist(),
        "tw": d["tw"].round(2).tolist(),
        "ta": d["ta"].round(2).tolist(),
        "obs_dates": (obs["date"].dt.strftime("%Y-%m-%d").tolist()
                      if len(obs) else []),
        "obs_tw": obs["tw_obs"].round(2).tolist() if len(obs) else [],
    }


def prepare_climatology(clim: pd.DataFrame) -> dict:
    """Средний годовой ход для столбчатого графика."""
    return {
        "months": ["янв", "фев", "мар", "апр", "май", "июн",
                   "июл", "авг", "сен", "окт", "ноя", "дек"],
        "E": clim["E_mean"].round(1).tolist(),
        "E_mt": clim["E_mt_mean"].round(1).tolist(),
        "P": clim["P_mean"].round(1).tolist(),
        "G": (clim["G_mean"] * 30).round(1).tolist(),
    }


def prepare_by_year(df: pd.DataFrame) -> dict:
    """
    Данные по каждому году отдельно: годовой ход и сводка.

    Питает слайдер на карте. Ключ — год, значение — 12 месячных сумм плюс
    итоги. Неполные годы (меньше 350 суток) отбрасываются: их суммы
    несопоставимы с полными и портят шкалу.
    """
    d = df.dropna(subset=["E_penman"]).copy()
    d["year"] = d["date"].dt.year
    d["month"] = d["date"].dt.month

    out = {}
    for year, g in d.groupby("year"):
        if len(g) < 350:
            continue
        m = g.groupby("month").agg(
            E=("E_penman", "sum"), P=("prcp", "sum"),
            tw=("tw", "mean"), ta=("ta", "mean"))
        m = m.reindex(range(1, 13))

        # Дефицит считается из УЖЕ ОКРУГЛЁННЫХ сумм. Иначе int(E − P) может
        # отличаться на единицу от int(E) − int(P), и на карте числа не
        # сходятся друг с другом — мелочь, которая подрывает доверие ко всему.
        e_total = int(round(g["E_penman"].sum()))
        p_total = int(round(g["prcp"].sum()))

        out[str(year)] = {
            "E": m["E"].round(1).where(m["E"].notna(), None).tolist(),
            "P": m["P"].round(1).where(m["P"].notna(), None).tolist(),
            "tw": m["tw"].round(1).where(m["tw"].notna(), None).tolist(),
            "ta": m["ta"].round(1).where(m["ta"].notna(), None).tolist(),
            "E_total": e_total,
            "P_total": p_total,
            "deficit": e_total - p_total,
            "tw_max": round(float(g["tw"].max()), 1),
            "tw_mean": round(float(g["tw"].mean()), 1),
            "peak_month": int(m["E"].idxmax()) if m["E"].notna().any() else None,
        }
    return out


def prepare_annual(annual: pd.DataFrame) -> dict:
    return {
        "years": annual["date"].dt.year.tolist(),
        "E": annual["E_penman"].round(0).tolist(),
        "P": annual["P"].round(0).tolist(),
        "deficit": annual["E_minus_P"].round(0).tolist(),
    }


def _geojson_of(source) -> dict:
    """Принимает GeoDataFrame, dict-GeoJSON или объект с методом to_json()."""
    if isinstance(source, dict):
        return source
    if hasattr(source, "to_json"):
        return json.loads(source.to_json())
    raise TypeError(f"не понимаю тип геометрии: {type(source)}")


def _bounds_of(geojson: dict) -> tuple[float, float, float, float]:
    """Ограничивающий прямоугольник по сырому GeoJSON, без geopandas."""
    xs, ys = [], []

    def walk(coords):
        if (isinstance(coords, (list, tuple)) and len(coords) >= 2
                and isinstance(coords[0], (int, float))):
            xs.append(float(coords[0]))
            ys.append(float(coords[1]))
        else:
            for c in coords:
                walk(c)

    for feat in geojson.get("features", [geojson]):
        geom = feat.get("geometry", feat)
        if geom and geom.get("coordinates"):
            walk(geom["coordinates"])

    if not xs:
        raise ValueError("в GeoJSON нет координат")
    return min(xs), min(ys), max(xs), max(ys)


def _fallback_centroid(geojson: dict) -> tuple[float, float]:
    minx, miny, maxx, maxy = _bounds_of(geojson)
    return (minx + maxx) / 2.0, (miny + maxy) / 2.0


def build_map(source, df: pd.DataFrame, clim: pd.DataFrame,
              annual: pd.DataFrame, out_path: str | Path,
              ee_layers: list[dict] | None = None,
              title: str = "Испарение с водной поверхности",
              centroid: tuple[float, float] | None = None,
              area_km2: float | None = None,
              ee_layers_by_year: dict | None = None) -> Path:
    """
    Собирает HTML-карту и сохраняет её.

    source может быть GeoDataFrame, готовым GeoJSON-словарём или любым
    объектом с методом to_json(). Модуль намеренно не требует geopandas:
    карта должна строиться и там, где стоит только минимальный набор
    зависимостей.

    centroid и area_km2 вычисляются через waterbody, если он доступен;
    иначе центр берётся по ограничивающему прямоугольнику, а площадь
    остаётся неизвестной и просто не показывается в сводке.
    """
    geojson = _geojson_of(source)

    if centroid is None or area_km2 is None:
        try:
            from . import waterbody as wb
            if centroid is None:
                centroid = wb.centroid(source)
            if area_km2 is None:
                area_km2 = wb.area_km2(source)
        except Exception:
            if centroid is None:
                centroid = _fallback_centroid(geojson)

    lon, lat = centroid

    stats = {
        "area_km2": round(area_km2, 1) if area_km2 else None,
        "lat": round(lat, 4),
        "lon": round(lon, 4),
        "period": f"{df['date'].min():%Y}–{df['date'].max():%Y}",
        "E_annual": int(annual["E_penman"].mean()) if len(annual) else None,
        "P_annual": int(annual["P"].mean()) if len(annual) else None,
        "deficit": int(annual["E_minus_P"].mean()) if len(annual) else None,
        "tw_max": round(float(df["tw"].max()), 1),
        "n_obs": int(df["tw_obs"].notna().sum()) if "tw_obs" in df else 0,
    }
    stats["E_volume_mcm"] = (
        round(annual["E_penman"].mean() * 1e-3 * area_km2 * 1e6 / 1e6, 1)
        if area_km2 and len(annual) else None)

    payload = {
        "geojson": geojson,
        "center": [lat, lon],
        "stats": stats,
        "series": prepare_series(df),
        "clim": prepare_climatology(clim),
        "annual": prepare_annual(annual),
        "byYear": prepare_by_year(df),
        "eeLayers": ee_layers or [],
        "eeLayersByYear": ee_layers_by_year or {},
        "title": title,
    }

    html = _HTML_TEMPLATE.replace(
        "/*__DATA__*/", json.dumps(payload, ensure_ascii=False, allow_nan=False))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def extract_payload(html: str) -> dict:
    """
    Достаёт встроенные данные из готового HTML.

    Нужна для тестов и отладки. Вынесена в публичный API намеренно: иначе
    проверки цепляются за конкретную вёрстку шаблона и ломаются при каждой
    правке разметки. Границы JSON ищутся по балансу скобок, а не по
    текстовому маркеру.
    """
    marker = "const DATA = "
    start = html.index(marker) + len(marker)

    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(html[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return json.loads(html[start:i + 1])

    raise ValueError("не удалось найти границы JSON в HTML")


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Испарение с водной поверхности</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root{--bg:#0f1419;--panel:#161d24;--card:#1e2831;--line:#2c3a47;
        --text:#e6edf3;--muted:#8b9aa8;--accent:#4fa3d1;--warm:#e07b39;
        --cool:#5ac8c8;--dim:#6b7785}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",
       Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--text)}
  #app{display:flex;height:100vh;overflow:hidden}
  #left{flex:1 1 auto;min-width:0;display:flex;flex-direction:column}
  #map{flex:1 1 auto;background:#0b0f14}
  #side{width:420px;flex:0 0 420px;background:var(--panel);
        border-left:1px solid var(--line);overflow-y:auto;padding:18px}

  /* ---- слайдер лет ---- */
  #timebar{background:var(--panel);border-top:1px solid var(--line);
           padding:12px 18px 14px}
  .tbhead{display:flex;align-items:baseline;gap:12px;margin-bottom:8px}
  #yearNow{font-size:26px;font-weight:700;letter-spacing:-.01em;
           font-variant-numeric:tabular-nums}
  #yearInfo{color:var(--muted);font-size:12px}
  .ctrls{margin-left:auto;display:flex;gap:6px;align-items:center}
  button{background:var(--card);border:1px solid var(--line);color:var(--text);
         border-radius:5px;padding:5px 11px;cursor:pointer;font-size:13px}
  button:hover{border-color:var(--accent)}
  button.on{background:var(--accent);border-color:var(--accent);color:#08131c;
            font-weight:600}
  input[type=range]{width:100%;accent-color:var(--accent);cursor:pointer}
  .ticks{display:flex;justify-content:space-between;color:var(--muted);
         font-size:10px;margin-top:2px;font-variant-numeric:tabular-nums}

  h1{font-size:16px;margin:0 0 3px;font-weight:600}
  .sub{color:var(--muted);font-size:12px;margin-bottom:16px}
  h2{font-size:11px;text-transform:uppercase;letter-spacing:.07em;
     color:var(--muted);margin:20px 0 8px;font-weight:600}

  .stats{display:grid;grid-template-columns:1fr 1fr;gap:8px}
  .stat{background:var(--card);border-radius:7px;padding:11px 13px;
        border:1px solid transparent;transition:border-color .15s}
  .stat.hl{border-color:var(--accent)}
  .stat .v{font-size:20px;font-weight:600;line-height:1.15;
           font-variant-numeric:tabular-nums}
  .stat .l{font-size:11px;color:var(--muted);margin-top:3px}
  .stat .d{font-size:11px;margin-top:2px;font-variant-numeric:tabular-nums}
  .up{color:var(--warm)} .down{color:var(--cool)}

  .box{background:var(--card);border-radius:7px;padding:11px;margin-bottom:10px}
  .box canvas{max-height:180px}
  .tabs{display:flex;gap:4px;margin-bottom:8px}
  .tab{flex:1;padding:6px 4px;text-align:center;font-size:12px;
       background:var(--card);border:1px solid var(--line);border-radius:5px;
       cursor:pointer;color:var(--muted)}
  .tab.on{background:var(--accent);border-color:var(--accent);color:#08131c;
          font-weight:600}
  .note{font-size:11px;color:var(--muted);line-height:1.45;
        border-left:2px solid var(--line);padding-left:9px;margin-top:10px}

  .legend{background:rgba(22,29,36,.95);padding:9px 11px;border-radius:6px;
          font-size:11px;line-height:1.4;border:1px solid var(--line)}
  .legend .bar{height:9px;border-radius:2px;margin:5px 0 3px;width:150px}
  .legend .ends{display:flex;justify-content:space-between;color:var(--muted)}
  .leaflet-control-layers{background:rgba(22,29,36,.95)!important;
      color:var(--text)!important;border:1px solid var(--line)!important}
  .leaflet-control-layers label{color:var(--text)!important}
  .leaflet-popup-content-wrapper,.leaflet-popup-tip{background:var(--card);
      color:var(--text)}
  .leaflet-container{background:#0b0f14}

  @media (max-width:900px){
    #app{flex-direction:column;height:auto}
    #left{height:60vh;flex:none}
    #side{width:100%;flex:none;border-left:none;border-top:1px solid var(--line)}
  }
</style>
</head>
<body>
<div id="app">
  <div id="left">
    <div id="map"></div>
    <div id="timebar">
      <div class="tbhead">
        <span id="yearNow">—</span>
        <span id="yearInfo"></span>
        <span class="ctrls">
          <button id="playBtn">▶ Проигрывать</button>
          <button id="allBtn" class="on">Все годы</button>
        </span>
      </div>
      <input type="range" id="yearSlider" min="0" max="0" value="0" step="1">
      <div class="ticks" id="ticks"></div>
    </div>
  </div>

  <div id="side">
    <h1 id="title"></h1>
    <div class="sub" id="subtitle"></div>

    <h2 id="statsHead">Сводка</h2>
    <div class="stats" id="stats"></div>

    <h2>Годовой ход</h2>
    <div class="box"><canvas id="climChart"></canvas></div>
    <div class="note" id="climNote"></div>

    <h2>Межгодовая динамика</h2>
    <div class="box"><canvas id="annChart"></canvas></div>

    <h2>Временной ряд</h2>
    <div class="tabs">
      <div class="tab on" data-series="E">Испарение</div>
      <div class="tab" data-series="T">Температура</div>
    </div>
    <div class="box"><canvas id="tsChart"></canvas></div>

    <div class="note">
      Слой температуры воды — медиана летних снимков Landsat 8/9 после отсева
      облаков и отступа от берега. Прибрежные пиксели смешаны с сушей и
      завышают температуру, поэтому исключены.
    </div>
  </div>
</div>

<script>
const DATA = /*__DATA__*/;
const MONTHS = ['янв','фев','мар','апр','май','июн',
                'июл','авг','сен','окт','ноя','дек'];
const YEARS = Object.keys(DATA.byYear).sort();
let curYear = null;          // null = режим «все годы»
let playing = null;

// ================= карта =================
const map = L.map('map', {center: DATA.center, zoom: 11});
const baseDark = L.tileLayer(
  'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
  {attribution:'&copy; OpenStreetMap, &copy; CARTO', maxZoom:19}).addTo(map);
const baseSat = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  {attribution:'Esri, Maxar', maxZoom:19});

const overlays = {};
let activeLegend = null;

DATA.eeLayers.forEach(l => {
  const layer = L.tileLayer(l.url, {opacity:.85, attribution:'Google Earth Engine'});
  overlays[l.name] = layer;
  if (l.default){ layer.addTo(map); activeLegend = l.legend; }
  layer.on('add',    () => { if(l.legend){activeLegend=l.legend; drawLegend();} });
  layer.on('remove', () => { activeLegend=null; drawLegend(); });
});

// годовые растровые слои: показывается только слой выбранного года
const yearLayers = {};
Object.entries(DATA.eeLayersByYear || {}).forEach(([y, l]) => {
  yearLayers[y] = L.tileLayer(l.url, {opacity:.85,
                    attribution:'Google Earth Engine'});
  if (l.legend && !activeLegend) activeLegend = l.legend;
});

const lake = L.geoJSON(DATA.geojson, {
  style:{color:'#4fa3d1', weight:2, fillColor:'#4fa3d1', fillOpacity:.06}
}).addTo(map);
overlays['Контур водоёма'] = lake;

L.control.layers({'Тёмная схема':baseDark,'Снимок':baseSat}, overlays,
                 {collapsed:false}).addTo(map);
map.fitBounds(lake.getBounds(), {padding:[35,35]});

const legendCtl = L.control({position:'bottomright'});
legendCtl.onAdd = function(){ this._div = L.DomUtil.create('div','legend');
                              return this._div; };
legendCtl.addTo(map);

function drawLegend(){
  const el = legendCtl._div;
  if(!activeLegend){ el.innerHTML=''; el.style.display='none'; return; }
  el.style.display='block';
  el.innerHTML = `<div><b>${activeLegend.title}</b></div>
    <div class="bar" style="background:linear-gradient(to right,
      ${activeLegend.palette.join(',')})"></div>
    <div class="ends"><span>${activeLegend.min}</span>
    <span>${activeLegend.max}</span></div>`;
}
drawLegend();

function updateMapPopup(){
  const s = DATA.stats;
  const y = curYear ? DATA.byYear[curYear] : null;
  lake.bindPopup(`<b>Водоём</b>` +
    (s.area_km2 ? `<br>Площадь ${s.area_km2} км²` : '') +
    `<br>${s.lat}° с.ш., ${s.lon}° в.д.` +
    (y ? `<br><br><b>${curYear}</b><br>Испарение ${y.E_total} мм<br>` +
         `Осадки ${y.P_total} мм<br>Дефицит ${y.deficit} мм`
       : (s.E_annual ? `<br>Испарение ${s.E_annual} мм/год (средн.)` : '')));
}

// ================= панель =================
document.getElementById('title').textContent = DATA.title;
const S = DATA.stats;
document.getElementById('subtitle').textContent =
  (S.area_km2 ? `${S.area_km2} км² · ` : '') +
  `${S.period} · ${S.n_obs} снимков с температурой воды`;

function renderStats(){
  const head = document.getElementById('statsHead');
  const el = document.getElementById('stats');

  if(!curYear){
    head.textContent = 'Сводка за весь период';
    const defs = [['E_annual','Испарение, мм/год'],['P_annual','Осадки, мм/год'],
                  ['deficit','Дефицит, мм/год'],['tw_max','Макс. Tw, °C']];
    el.innerHTML = defs.filter(([k])=>S[k]!=null)
      .map(([k,l])=>`<div class="stat"><div class="v">${S[k]}</div>
                     <div class="l">${l}</div></div>`).join('') +
      (S.E_volume_mcm ? `<div class="stat" style="grid-column:1/-1">
         <div class="v">${S.E_volume_mcm} млн м³</div>
         <div class="l">теряется на испарение за год</div></div>`:'');
    return;
  }

  head.textContent = `Сводка за ${curYear} год`;
  const y = DATA.byYear[curYear];
  const mean = k => {
    const vs = YEARS.map(yy=>DATA.byYear[yy][k]);
    return vs.reduce((a,b)=>a+b,0)/vs.length;
  };
  const card = (val, label, key, unit) => {
    const d = val - mean(key);
    const cls = d>0 ? 'up' : 'down';
    const sign = d>0 ? '+' : '';
    return `<div class="stat hl"><div class="v">${val}</div>
            <div class="l">${label}</div>
            <div class="d ${cls}">${sign}${d.toFixed(0)} ${unit} к норме</div></div>`;
  };
  el.innerHTML =
    card(y.E_total,'Испарение, мм','E_total','мм') +
    card(y.P_total,'Осадки, мм','P_total','мм') +
    card(y.deficit,'Дефицит, мм','deficit','мм') +
    card(y.tw_max,'Макс. Tw, °C','tw_max','°C');
}

// ================= графики =================
Chart.defaults.color = '#8b9aa8';
Chart.defaults.borderColor = '#2c3a47';
Chart.defaults.font.size = 10;
const LEG = {display:true, labels:{boxWidth:10,boxHeight:10,padding:8,
                                   font:{size:10}}};

// средний многолетний ход — серая подложка для сравнения
const climE = DATA.clim.E, climP = DATA.clim.P;

const climChart = new Chart(document.getElementById('climChart'), {
  type:'bar',
  data:{labels:MONTHS, datasets:[
    {label:'Норма', data:climE, backgroundColor:'#3a4753', order:3},
    {label:'Испарение', data:climE, backgroundColor:'#e07b39', order:2},
    {label:'Осадки', data:climP, type:'line', borderColor:'#4fa3d1',
     backgroundColor:'#4fa3d1', tension:.3, pointRadius:2, order:1}
  ]},
  options:{responsive:true, maintainAspectRatio:false, plugins:{legend:LEG},
    scales:{y:{title:{display:true,text:'мм/мес'}}}}
});

function updateClim(){
  const d = climChart.data.datasets;
  if(!curYear){
    d[0].hidden = true;
    d[1].data = climE; d[1].label = 'Испарение (средн.)';
    d[2].data = climP;
    document.getElementById('climNote').textContent =
      `Средний многолетний ход. Максимум в ${MONTHS[climE.indexOf(Math.max(...climE))]}. ` +
      `Выберите год на шкале внизу, чтобы сравнить его с нормой.`;
  } else {
    const y = DATA.byYear[curYear];
    d[0].hidden = false;
    d[1].data = y.E; d[1].label = `Испарение ${curYear}`;
    d[2].data = y.P;
    const dev = y.E_total - climE.reduce((a,b)=>a+b,0);
    document.getElementById('climNote').textContent =
      `${curYear}: пик в ${MONTHS[(y.peak_month||1)-1]}, ` +
      `сумма ${dev>0?'выше':'ниже'} нормы на ${Math.abs(dev).toFixed(0)} мм. ` +
      `Серые столбцы — многолетняя норма.`;
  }
  climChart.update();
}

const annChart = new Chart(document.getElementById('annChart'), {
  type:'bar',
  data:{labels:DATA.annual.years, datasets:[
    {label:'Испарение', data:DATA.annual.E,
     backgroundColor:DATA.annual.years.map(()=>'#e07b39')},
    {label:'Осадки', data:DATA.annual.P,
     backgroundColor:DATA.annual.years.map(()=>'#4fa3d1')}
  ]},
  options:{responsive:true, maintainAspectRatio:false, plugins:{legend:LEG},
    scales:{y:{title:{display:true,text:'мм/год'}}},
    onClick:(e,els)=>{ if(els.length){
      const y = String(DATA.annual.years[els[0].index]);
      if(DATA.byYear[y]) setYear(YEARS.indexOf(y));
    }}}
});

function updateAnn(){
  // выбранный год подсвечивается, остальные приглушаются
  annChart.data.datasets[0].backgroundColor = DATA.annual.years.map(
    y => (!curYear || String(y)===curYear) ? '#e07b39' : '#5a4030');
  annChart.data.datasets[1].backgroundColor = DATA.annual.years.map(
    y => (!curYear || String(y)===curYear) ? '#4fa3d1' : '#2c4a5c');
  annChart.update();
}

let tsChart = null;
function buildTs(kind){
  if(tsChart) tsChart.destroy();
  const cfg = kind==='E'
    ? {ds:[{label:'Пенман (спутн. Tw)', data:DATA.series.E, borderColor:'#e07b39',
            borderWidth:1.2, pointRadius:0, tension:.2},
           {label:'Без спутника', data:DATA.series.E_nosat, borderColor:'#6b7785',
            borderWidth:1, pointRadius:0, borderDash:[4,3], tension:.2}],
       unit:'мм/сут'}
    : {ds:[{label:'Вода (модель)', data:DATA.series.tw, borderColor:'#5ac8c8',
            borderWidth:1.3, pointRadius:0, tension:.2},
           {label:'Воздух', data:DATA.series.ta, borderColor:'#6b7785',
            borderWidth:1, pointRadius:0, tension:.2}],
       unit:'°C'};
  tsChart = new Chart(document.getElementById('tsChart'), {
    type:'line', data:{labels:DATA.series.dates, datasets:cfg.ds},
    options:{responsive:true, maintainAspectRatio:false,
      interaction:{mode:'index', intersect:false}, plugins:{legend:LEG},
      scales:{x:{ticks:{maxTicksLimit:8,maxRotation:0}},
              y:{title:{display:true,text:cfg.unit}}}}
  });
}
buildTs('E');
document.querySelectorAll('.tab').forEach(t=>{
  t.onclick = () => {
    document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
    t.classList.add('on'); buildTs(t.dataset.series);
  };
});

// ================= слайдер лет =================
const slider = document.getElementById('yearSlider');
const playBtn = document.getElementById('playBtn');
const allBtn = document.getElementById('allBtn');

if(YEARS.length){
  slider.max = YEARS.length - 1;
  document.getElementById('ticks').innerHTML =
    `<span>${YEARS[0]}</span><span>${YEARS[YEARS.length-1]}</span>`;
} else {
  document.getElementById('timebar').style.display = 'none';
}

function setYear(idx){
  curYear = YEARS[idx];
  slider.value = idx;
  allBtn.classList.remove('on');

  document.getElementById('yearNow').textContent = curYear;
  const y = DATA.byYear[curYear];
  document.getElementById('yearInfo').textContent =
    `испарение ${y.E_total} мм · осадки ${y.P_total} мм · Tw до ${y.tw_max} °C`;

  Object.entries(yearLayers).forEach(([yy, layer]) => {
    if(yy === curYear){ if(!map.hasLayer(layer)) layer.addTo(map); }
    else if(map.hasLayer(layer)) map.removeLayer(layer);
  });
  lake.bringToFront();

  renderStats(); updateClim(); updateAnn(); updateMapPopup();
}

function setAll(){
  curYear = null;
  stopPlay();
  allBtn.classList.add('on');
  document.getElementById('yearNow').textContent = 'Все годы';
  document.getElementById('yearInfo').textContent =
    `${S.period} · среднее за период`;
  Object.values(yearLayers).forEach(l => { if(map.hasLayer(l)) map.removeLayer(l); });
  renderStats(); updateClim(); updateAnn(); updateMapPopup();
}

slider.oninput = e => { stopPlay(); setYear(+e.target.value); };
allBtn.onclick = setAll;

function stopPlay(){
  if(playing){ clearInterval(playing); playing = null;
               playBtn.textContent = '▶ Проигрывать';
               playBtn.classList.remove('on'); }
}
playBtn.onclick = () => {
  if(playing){ stopPlay(); return; }
  playBtn.textContent = '❚❚ Пауза';
  playBtn.classList.add('on');
  let i = curYear ? YEARS.indexOf(curYear) : -1;
  playing = setInterval(() => {
    i = (i + 1) % YEARS.length;
    setYear(i);
  }, 900);
};

setAll();
</script>
</body>
</html>
"""
