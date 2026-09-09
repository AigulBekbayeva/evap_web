"""
Геометрия водоёма.

Пользователь кладёт шейп-файл (или GeoJSON / GPKG) озера в data/shapes/.
Модуль читает его, приводит к EPSG:4326, считает площадь в равновеликой
проекции и отдаёт геометрию в форме, пригодной для Earth Engine.

Требуется geopandas; импорт ленивый, чтобы модуль physics оставался
тестируемым в минимальном окружении.
"""

from __future__ import annotations

import json
from pathlib import Path

# Равновеликая азимутальная проекция, центрированная на Сорбулаке.
# Для расчёта площади и буферов в метрах. Искажения на масштабе десятков км
# пренебрежимы.
EQUAL_AREA_CRS = (
    "+proj=laea +lat_0=43.86 +lon_0=76.75 +x_0=0 +y_0=0 "
    "+datum=WGS84 +units=m +no_defs"
)


def _require_geopandas():
    try:
        import geopandas as gpd  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Нужен geopandas. Установите: pip install geopandas pyogrio"
        ) from exc
    import geopandas as gpd
    return gpd


def find_shape(shapes_dir: str | Path = "data/shapes") -> Path:
    """
    Находит единственный векторный файл в каталоге.

    Поддерживаются .shp, .geojson, .json, .gpkg. Если файлов несколько или
    ни одного — понятная ошибка вместо молчаливого выбора наугад.
    """
    shapes_dir = Path(shapes_dir)
    if not shapes_dir.exists():
        raise FileNotFoundError(f"Каталог {shapes_dir} не найден")

    patterns = ("*.shp", "*.geojson", "*.json", "*.gpkg")
    found = [p for pat in patterns for p in sorted(shapes_dir.glob(pat))]

    if not found:
        raise FileNotFoundError(
            f"В {shapes_dir} нет векторных файлов. "
            "Положите туда шейп озера (.shp вместе с .dbf, .shx, .prj)."
        )
    if len(found) > 1:
        names = ", ".join(p.name for p in found)
        raise ValueError(
            f"В {shapes_dir} найдено несколько файлов ({names}). "
            "Оставьте один или укажите путь явно."
        )
    return found[0]


def load_lake(path: str | Path | None = None,
              shapes_dir: str | Path = "data/shapes",
              dissolve: bool = True):
    """
    Читает контур озера и возвращает GeoDataFrame в EPSG:4326.

    dissolve=True объединяет все полигоны в один — обычно нужно, поскольку
    Сорбулак представляет собой систему прудов, и для метеорасчёта берётся
    суммарная акватория.
    """
    gpd = _require_geopandas()

    path = Path(path) if path else find_shape(shapes_dir)
    gdf = gpd.read_file(path)

    if gdf.empty:
        raise ValueError(f"{path} не содержит геометрий")

    if gdf.crs is None:
        raise ValueError(
            f"У {path} не задана система координат. "
            "Проверьте наличие .prj или задайте CRS вручную."
        )

    gdf = gdf.to_crs("EPSG:4326")

    if dissolve:
        gdf = gdf.dissolve().reset_index(drop=True)

    return gdf


def area_km2(gdf) -> float:
    """Площадь в км², посчитанная в равновеликой проекции."""
    return float(gdf.to_crs(EQUAL_AREA_CRS).area.sum() / 1e6)


def bbox(gdf, pad_deg: float = 0.0) -> list[float]:
    """Ограничивающий прямоугольник [minx, miny, maxx, maxy] с опциональным полем."""
    minx, miny, maxx, maxy = gdf.total_bounds
    return [minx - pad_deg, miny - pad_deg, maxx + pad_deg, maxy + pad_deg]


def centroid(gdf) -> tuple[float, float]:
    """Центроид (lon, lat), посчитанный в метрической проекции."""
    c = gdf.to_crs(EQUAL_AREA_CRS).geometry.centroid.to_crs("EPSG:4326").iloc[0]
    return float(c.x), float(c.y)


def erode(gdf, metres: float = 60.0):
    """
    Внутренний буфер — отступ от береговой линии.

    Для термометрии и оптики прибрежные пиксели смешаны с растительностью и
    грунтом; 60 м отсекают два пикселя Landsat и три пикселя Sentinel-2 20 м.
    """
    g = gdf.to_crs(EQUAL_AREA_CRS)
    g["geometry"] = g.geometry.buffer(-abs(metres))
    g = g[~g.geometry.is_empty]
    if g.empty:
        raise ValueError(
            f"После эрозии на {metres} м геометрия пуста — "
            "водоём слишком узкий, уменьшите отступ."
        )
    return g.to_crs("EPSG:4326")


def buffer_rings(gdf, distances_m=(1000, 3000, 5000, 10000)):
    """
    Кольцевые буферы вокруг озера — для анализа засоления и подтопления
    прилегающих земель (см. Проект 2).
    """
    gpd = _require_geopandas()
    g = gdf.to_crs(EQUAL_AREA_CRS)
    base = g.geometry.union_all() if hasattr(g.geometry, "union_all") \
        else g.geometry.unary_union

    rows, prev = [], base
    for d in distances_m:
        ring = base.buffer(d).difference(prev)
        rows.append({"dist_m": d, "geometry": ring})
        prev = base.buffer(d)

    return gpd.GeoDataFrame(rows, crs=EQUAL_AREA_CRS).to_crs("EPSG:4326")


def to_ee_geometry(gdf, simplify_m: float | None = 100.0):
    """
    Конвертация в ee.Geometry.

    Упрощение обязательно: детальный контур с тысячами вершин упирается в
    лимиты запроса Earth Engine. 100 м не влияют на результат осреднения по
    ячейке ERA5 9 км, но заметно ускоряют работу.
    """
    import ee

    g = gdf
    if simplify_m:
        g = gdf.to_crs(EQUAL_AREA_CRS)
        g = g.copy()
        g["geometry"] = g.geometry.simplify(simplify_m)
        g = g.to_crs("EPSG:4326")

    geom = json.loads(g.to_json())["features"][0]["geometry"]
    return ee.Geometry(geom)


def describe(gdf) -> dict:
    """Сводка по геометрии — печатается при запуске пайплайна."""
    lon, lat = centroid(gdf)
    b = bbox(gdf)
    return {
        "area_km2": round(area_km2(gdf), 2),
        "centroid_lon": round(lon, 5),
        "centroid_lat": round(lat, 5),
        "bbox": [round(v, 5) for v in b],
        "n_parts": int(len(gdf.explode(index_parts=False))),
    }
