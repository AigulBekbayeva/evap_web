"""Тесты генератора веб-карты. Работают без geopandas и Earth Engine."""

import json
import numpy as np
import pandas as pd
import pytest

from evap import pipeline, webmap
from evap.config import Config


@pytest.fixture
def computed():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from make_demo_map import synthetic_weather, synthetic_outline

    df = synthetic_weather("2020-01-01", "2022-12-31")
    res = pipeline.compute_evaporation(df, Config(mean_depth_m=11.0))
    return {
        "res": res,
        "clim": pipeline.monthly_climatology(res),
        "annual": pipeline.annual_summary(res),
        "gdf": synthetic_outline(),
    }


class TestSeriesPrep:

    def test_decimates_long_series(self, computed):
        s = webmap.prepare_series(computed["res"], max_points=100)
        assert len(s["dates"]) <= 400          # месячное осреднение
        assert len(s["dates"]) == len(s["E"]) == len(s["tw"])

    def test_keeps_short_series_intact(self, computed):
        short = computed["res"].head(200)
        s = webmap.prepare_series(short, max_points=900)
        assert len(s["dates"]) == 200

    def test_no_nan_leaks_into_json(self, computed):
        """NaN не сериализуется в валидный JSON и ломает карту молча."""
        s = webmap.prepare_series(computed["res"])
        for key in ["E", "tw", "ta", "E_nosat"]:
            assert not any(v != v for v in s[key]), f"NaN в {key}"

    def test_climatology_twelve_months(self, computed):
        c = webmap.prepare_climatology(computed["clim"])
        assert len(c["months"]) == 12 == len(c["E"]) == len(c["P"])


class TestBuildMap:

    def test_writes_valid_html(self, computed, tmp_path):
        out = webmap.build_map(computed["gdf"], computed["res"],
                               computed["clim"], computed["annual"],
                               tmp_path / "map.html")
        html = out.read_text(encoding="utf-8")

        assert out.exists() and len(html) > 10_000
        assert "/*__DATA__*/" not in html, "плейсхолдер не заменён"
        assert html.strip().startswith("<!DOCTYPE html>")
        assert html.count("<script") >= 3
        assert "leaflet" in html.lower() and "chart.js" in html.lower()

    def test_payload_is_parseable_json(self, computed, tmp_path):
        """Данные внутри HTML должны быть корректным JSON."""
        out = webmap.build_map(computed["gdf"], computed["res"],
                               computed["clim"], computed["annual"],
                               tmp_path / "map.html")
        html = out.read_text(encoding="utf-8")

        payload = webmap.extract_payload(html)

        # площадь может быть None, если geopandas недоступен и она не задана
        # явно — это штатное поведение, карта строится и без неё
        assert payload["stats"]["area_km2"] is None or payload["stats"]["area_km2"] > 0
        assert payload["stats"]["lat"] is not None
        assert len(payload["series"]["dates"]) > 0
        assert payload["geojson"]["type"] == "FeatureCollection"
        assert isinstance(payload["eeLayers"], list)

    def test_creates_parent_directory(self, computed, tmp_path):
        target = tmp_path / "deep" / "nested" / "map.html"
        out = webmap.build_map(computed["gdf"], computed["res"],
                               computed["clim"], computed["annual"], target)
        assert out.exists()

    def test_explicit_area_and_centroid(self, computed, tmp_path):
        """Площадь и центр можно передать явно — путь без geopandas."""
        out = webmap.build_map(computed["gdf"], computed["res"],
                               computed["clim"], computed["annual"],
                               tmp_path / "map.html",
                               centroid=(76.745, 43.862), area_km2=52.4)
        html = out.read_text(encoding="utf-8")
        payload = webmap.extract_payload(html)

        assert payload["stats"]["area_km2"] == 52.4
        assert payload["center"] == [43.862, 76.745]
        # объём потерь считается только когда известна площадь
        assert payload["stats"]["E_volume_mcm"] > 0

    def test_centroid_falls_back_to_bbox(self, computed, tmp_path):
        """Без geopandas центр берётся по ограничивающему прямоугольнику."""
        out = webmap.build_map(computed["gdf"], computed["res"],
                               computed["clim"], computed["annual"],
                               tmp_path / "map.html")
        html = out.read_text(encoding="utf-8")
        payload = webmap.extract_payload(html)

        lat, lon = payload["center"]
        assert 43.5 < lat < 44.2 and 76.4 < lon < 77.1

    def test_works_without_ee_layers(self, computed, tmp_path):
        """Карта строится, даже когда Earth Engine недоступен."""
        out = webmap.build_map(computed["gdf"], computed["res"],
                               computed["clim"], computed["annual"],
                               tmp_path / "map.html", ee_layers=None)
        assert "eeLayers" in out.read_text(encoding="utf-8")


class TestYearSlider:

    def test_by_year_present(self, computed, tmp_path):
        """Слайдер лет питается блоком byYear."""
        out = webmap.build_map(computed["gdf"], computed["res"],
                               computed["clim"], computed["annual"],
                               tmp_path / "map.html")
        payload = webmap.extract_payload(out.read_text(encoding="utf-8"))

        assert "byYear" in payload and payload["byYear"]
        for year, y in payload["byYear"].items():
            assert len(y["E"]) == 12, f"{year}: не 12 месяцев"
            assert y["E_total"] > 0
            assert y["deficit"] == y["E_total"] - y["P_total"]

    def test_partial_years_dropped(self, computed):
        """Неполные годы отбрасываются — их суммы несопоставимы с полными."""
        res = computed["res"]
        trimmed = res[res["date"] < res["date"].max() - pd.Timedelta(days=200)]
        by = webmap.prepare_by_year(trimmed)
        last = str(trimmed["date"].max().year)
        assert last not in by

    def test_no_nan_in_year_blocks(self, computed):
        by = webmap.prepare_by_year(computed["res"])
        for y in by.values():
            for key in ["E", "P", "tw"]:
                assert not any(v is not None and v != v for v in y[key])


class TestDemoScript:

    def test_precipitation_realistic(self):
        """Синтетика должна давать правдоподобные для региона осадки."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from make_demo_map import synthetic_weather

        df = synthetic_weather()
        annual_p = df.set_index("date").resample("YS")["prcp"].sum()
        assert 200 < annual_p.mean() < 550, f"осадки {annual_p.mean():.0f} мм"
