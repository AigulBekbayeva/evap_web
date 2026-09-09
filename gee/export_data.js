/**
 * ЭКСПОРТ ДАННЫХ ДЛЯ РАСЧЁТА ИСПАРЕНИЯ
 * Вставить в https://code.earthengine.google.com
 *
 * ПОЧЕМУ ЭТОТ СКРИПТ, А НЕ PYTHON API
 *
 * Python-клиент вызывает getInfo(), который считает всё синхронно и упирается
 * в пользовательский лимит памяти ("User memory limit exceeded") уже на
 * нескольких годах. Export.table.toDrive ставит задачу в очередь на серверах
 * Google: лимиты там на порядки выше, а результат складывается в Drive.
 *
 * ЧТО ДЕЛАТЬ
 *   1. Загрузить шейп озера: Assets → New → Shape files → импортировать
 *      в скрипт под именем lakeAsset (или оставить рамку ниже).
 *   2. Проверить START / END.
 *   3. Run → вкладка Tasks справа → запустить все задачи (кнопка RUN у каждой).
 *   4. Дождаться (ERA5 около 10 мин, Landsat 15–30, MODIS до часа).
 *   5. Скачать CSV из Google Drive в data/raw/ проекта.
 *   6. evap ingest && evap run
 *
 * ВАЖНО ПРО ERA5-Land: если озеро замаскировано (типично для водоёмов
 * крупнее нескольких км²), метеоряд выйдет пустым. Тогда либо берите
 * метеоданные через Open-Meteo (evap fetch --source openmeteo — проще и
 * без этого скрипта), либо включите ниже USE_LAND_BUFFER.
 */

// ============================ НАСТРОЙКИ ============================

// Вариант А: свой ассет (раскомментировать и подставить путь)
// var lake = ee.FeatureCollection('projects/ee-yourname/assets/sorbulak');

// Вариант Б: прямоугольник — ЗАМЕНИТЬ на свои границы
var lake = ee.FeatureCollection([
  ee.Feature(ee.Geometry.Rectangle([76.52482, 43.62944, 76.64977, 43.74182]))
]);

var START = '2000-01-01';
var END   = '2026-01-01';

var FOLDER = 'sorbulak_evap';   // папка в Google Drive
var ERODE_M = 60;               // отступ от берега для термометрии
var USE_LAND_BUFFER = false;    // true — брать ERA5 с кольца вокруг озера,
                                // если ячейка над водой замаскирована

var EXPORT_ERA5    = true;      // false, если метео берёте через Open-Meteo
var EXPORT_LANDSAT = true;
var EXPORT_MODIS   = true;

// ===================================================================

var geom = lake.geometry();
var geomEroded = geom.buffer(-ERODE_M);
var era5Geom = USE_LAND_BUFFER
  ? geom.buffer(6000).difference(geom, 100)   // кольцо на суше
  : geom;

Map.centerObject(geom, 11);
Map.addLayer(geom, {color: '4fa3d1'}, 'Контур');
Map.addLayer(geomEroded, {color: 'e07b39'}, 'После эрозии', false);

print('Площадь, км²:', geom.area(100).divide(1e6));
print('Период:', START, '—', END);


// ------------------------- 1. ERA5-Land ----------------------------
// Используется ГОТОВАЯ суточная коллекция DAILY_AGGR: самостоятельная
// агрегация почасовой в 24 раза тяжелее и не нужна.

if (EXPORT_ERA5) {
  var era5Bands = [
    'temperature_2m', 'temperature_2m_min', 'temperature_2m_max',
    'dewpoint_temperature_2m', 'surface_pressure',
    'u_component_of_wind_10m', 'v_component_of_wind_10m',
    'surface_solar_radiation_downwards_sum',
    'surface_thermal_radiation_downwards_sum',
    'total_precipitation_sum'
  ];

  var era5 = ee.ImageCollection('ECMWF/ERA5_LAND/DAILY_AGGR')
      .filterDate(START, END)
      .select(era5Bands);

  print('ERA5-Land, снимков:', era5.size());

  var era5Table = era5.map(function(img) {
    var v = img.reduceRegion({
      reducer: ee.Reducer.mean(),
      geometry: era5Geom,
      scale: 9000,
      maxPixels: 1e9,
      bestEffort: true
    });
    return ee.Feature(null, v)
        .set('date', img.date().format('YYYY-MM-dd'));
  });

  Export.table.toDrive({
    collection: era5Table,
    description: 'era5land_daily',
    folder: FOLDER,
    fileNamePrefix: 'era5land_daily',
    fileFormat: 'CSV',
    selectors: ['date'].concat(era5Bands)
  });

  // быстрая проверка, не пуста ли ячейка над водой
  var testDay = ee.Image(era5.filterDate('2020-07-15', '2020-07-16').first());
  print('Проверка ERA5 (15.07.2020, K):',
        testDay.select('temperature_2m').reduceRegion({
          reducer: ee.Reducer.mean(), geometry: era5Geom,
          scale: 9000, bestEffort: true}));
  print('  Если null — ячейка замаскирована над водой. Включите ' +
        'USE_LAND_BUFFER или берите метео через Open-Meteo.');
}


// --------------------- 2. Landsat: температура воды ------------------

if (EXPORT_LANDSAT) {
  var landsatST = function(img) {
    var qa = img.select('QA_PIXEL');
    var clear = qa.bitwiseAnd(1 << 1).eq(0)     // dilated cloud
        .and(qa.bitwiseAnd(1 << 2).eq(0))       // cirrus
        .and(qa.bitwiseAnd(1 << 3).eq(0))       // cloud
        .and(qa.bitwiseAnd(1 << 4).eq(0));      // cloud shadow

    var st = img.select('ST_B10').multiply(0.00341802).add(149.0)
                .subtract(273.15).rename('tw');
    var stq = img.select('ST_QA').multiply(0.01);   // неопределённость, K

    return st.updateMask(clear).updateMask(stq.lt(3.0))
             .copyProperties(img, ['system:time_start', 'SPACECRAFT_ID',
                                   'CLOUD_COVER']);
  };

  var landsat = ee.ImageCollection('LANDSAT/LC08/C02/T1_L2')
      .merge(ee.ImageCollection('LANDSAT/LC09/C02/T1_L2'))
      .filterBounds(geomEroded)
      .filterDate(START, END)
      .filter(ee.Filter.lt('CLOUD_COVER', 80))
      .map(landsatST);

  print('Landsat, снимков:', landsat.size());

  var lsTable = landsat.map(function(img) {
    var stats = img.reduceRegion({
      reducer: ee.Reducer.mean()
          .combine(ee.Reducer.count(), '', true)
          .combine(ee.Reducer.stdDev(), '', true),
      geometry: geomEroded,
      scale: 30,
      maxPixels: 1e9,
      bestEffort: true
    });
    return ee.Feature(null, {
      date:   ee.Date(img.get('system:time_start')).format('YYYY-MM-dd'),
      tw:     stats.get('tw_mean'),
      tw_sd:  stats.get('tw_stdDev'),
      n_pix:  stats.get('tw_count'),
      source: img.get('SPACECRAFT_ID'),
      cloud:  img.get('CLOUD_COVER')
    });
  }).filter(ee.Filter.notNull(['tw']));

  Export.table.toDrive({
    collection: lsTable,
    description: 'landsat_lswt',
    folder: FOLDER,
    fileNamePrefix: 'landsat_lswt',
    fileFormat: 'CSV',
    selectors: ['date', 'tw', 'tw_sd', 'n_pix', 'source', 'cloud']
  });

  // Летняя медиана — визуальная проверка, что маска работает
  Map.addLayer(
    landsat.filter(ee.Filter.calendarRange(6, 8, 'month')).median().clip(geomEroded),
    {min: 15, max: 30,
     palette: ['2c7bb6','00ccbc','90eb9d','ffff8c','f29e2e','d7191c']},
    'Tw, лето, медиана');

  print(ui.Chart.image.series({
    imageCollection: landsat.select('tw'),
    region: geomEroded,
    reducer: ee.Reducer.mean(),
    scale: 30
  }).setOptions({
    title: 'Температура поверхности воды, Landsat 8/9',
    vAxis: {title: '°C'}, pointSize: 3, lineWidth: 0
  }));
}


// ------------------- 3. MODIS: температура воды ----------------------
// 1 км, но 4 срока в сутки. Ночные значения предпочтительнее: нет прогрева
// тонкой поверхностной плёнки, и Tw ближе к температуре перемешанного слоя.

if (EXPORT_MODIS) {
  var modisLST = function(bandName, qcName, label) {
    return function(img) {
      var good = img.select(qcName).bitwiseAnd(3).eq(0);   // биты 0-1 == 00
      var lst = img.select(bandName).multiply(0.02).subtract(273.15)
                   .updateMask(good).rename('tw');
      var stats = lst.reduceRegion({
        reducer: ee.Reducer.mean().combine(ee.Reducer.count(), '', true),
        geometry: geomEroded, scale: 1000, maxPixels: 1e9, bestEffort: true
      });
      return ee.Feature(null, {
        date:   ee.Date(img.get('system:time_start')).format('YYYY-MM-dd'),
        tw:     stats.get('tw_mean'),
        n_pix:  stats.get('tw_count'),
        source: label
      });
    };
  };

  var modis = ee.ImageCollection('MODIS/061/MOD11A1')
      .merge(ee.ImageCollection('MODIS/061/MYD11A1'))
      .filterBounds(geomEroded)
      .filterDate(START, END);

  print('MODIS, снимков:', modis.size());

  var modisNight = modis.map(modisLST('LST_Night_1km', 'QC_Night', 'MODIS_night'))
                        .filter(ee.Filter.notNull(['tw']));

  Export.table.toDrive({
    collection: modisNight,
    description: 'modis_lswt_night',
    folder: FOLDER,
    fileNamePrefix: 'modis_lswt_night',
    fileFormat: 'CSV',
    selectors: ['date', 'tw', 'n_pix', 'source']
  });
}


// ------------------- 4. Площадь зеркала по годам ---------------------
// Нужна для перехода мм → м³ и для водного баланса.

var waterArea = function(year) {
  var s = ee.Date.fromYMD(year, 1, 1);
  var e = s.advance(1, 'year');

  var coll = ee.ImageCollection('LANDSAT/LC08/C02/T1_L2')
      .merge(ee.ImageCollection('LANDSAT/LC09/C02/T1_L2'))
      .filterBounds(geom).filterDate(s, e)
      .filter(ee.Filter.lt('CLOUD_COVER', 40));

  var mndwi = coll.map(function(img) {
    var sc = img.select(['SR_B3', 'SR_B6']).multiply(2.75e-05).add(-0.2);
    return sc.normalizedDifference(['SR_B3', 'SR_B6']).rename('mndwi');
  }).median();

  var water = mndwi.gt(0.0);
  var area = water.multiply(ee.Image.pixelArea())
      .reduceRegion({reducer: ee.Reducer.sum(), geometry: geom.buffer(3000),
                     scale: 30, maxPixels: 1e10, bestEffort: true});

  return ee.Feature(null, {
    year: year,
    area_km2: ee.Number(area.get('mndwi')).divide(1e6),
    n_images: coll.size()
  });
};

var years = ee.List.sequence(2013, 2025);
var areaTable = ee.FeatureCollection(years.map(waterArea));

Export.table.toDrive({
  collection: areaTable,
  description: 'water_area_annual',
  folder: FOLDER,
  fileNamePrefix: 'water_area_annual',
  fileFormat: 'CSV',
  selectors: ['year', 'area_km2', 'n_images']
});

print('');
print('>>> Откройте вкладку Tasks справа и запустите задачи.');
print('>>> Готовые CSV скачайте из Google Drive в data/raw/ проекта.');
print('>>> Затем: evap ingest && evap run');
