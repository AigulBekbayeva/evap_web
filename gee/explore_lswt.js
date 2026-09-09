// Интерактивный просмотр температуры воды и покрытия снимками.
// Вставить в https://code.earthengine.google.com
// Заменить geometry на свой контур (Assets → загрузить шейп → импортировать).

var lake = ee.Geometry.Rectangle([76.60, 43.75, 76.90, 43.98]);  // ЗАМЕНИТЬ
var start = '2015-01-01';
var end   = '2026-01-01';

// Отступ от берега: прибрежные пиксели смешаны с сушей
var eroded = lake.buffer(-60);

function landsatST(img) {
  var qa = img.select('QA_PIXEL');
  var clear = qa.bitwiseAnd(1 << 1).eq(0)
      .and(qa.bitwiseAnd(1 << 2).eq(0))
      .and(qa.bitwiseAnd(1 << 3).eq(0))
      .and(qa.bitwiseAnd(1 << 4).eq(0));

  var st = img.select('ST_B10').multiply(0.00341802).add(149.0)
              .subtract(273.15).rename('tw');
  var stq = img.select('ST_QA').multiply(0.01);

  return st.updateMask(clear).updateMask(stq.lt(3))
           .copyProperties(img, ['system:time_start']);
}

var coll = ee.ImageCollection('LANDSAT/LC08/C02/T1_L2')
    .merge(ee.ImageCollection('LANDSAT/LC09/C02/T1_L2'))
    .filterBounds(eroded).filterDate(start, end)
    .filter(ee.Filter.lt('CLOUD_COVER', 80))
    .map(landsatST);

print('Снимков всего:', coll.size());

// Карта: медианная температура за лето
var summer = coll.filter(ee.Filter.calendarRange(6, 8, 'month')).median();
Map.centerObject(lake, 11);
Map.addLayer(summer.clip(eroded), {
  min: 15, max: 30,
  palette: ['0000ff', '00ffff', 'ffff00', 'ff8800', 'ff0000']
}, 'Tw, лето, медиана');

// График временного ряда
var chart = ui.Chart.image.series({
  imageCollection: coll.select('tw'),
  region: eroded,
  reducer: ee.Reducer.mean(),
  scale: 30
}).setOptions({
  title: 'Температура поверхности воды, Landsat 8/9',
  vAxis: {title: '°C'},
  pointSize: 3, lineWidth: 0
});
print(chart);

// Распределение снимков по месяцам — где дыры в покрытии
var byMonth = ee.List.sequence(1, 12).map(function(m) {
  var n = coll.filter(ee.Filter.calendarRange(m, m, 'month')).size();
  return ee.Feature(null, {month: m, count: n});
});
print(ui.Chart.feature.byFeature(ee.FeatureCollection(byMonth), 'month', 'count')
        .setChartType('ColumnChart')
        .setOptions({title: 'Снимков по месяцам', vAxis: {title: 'шт'}}));
