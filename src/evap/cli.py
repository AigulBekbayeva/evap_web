"""Командный интерфейс: evap run | forecast | validate | inspect."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from .config import Config


def cmd_inspect(args):
    """Проверка шейпа до всяких выгрузок — первый запуск начинать с неё."""
    from . import waterbody as wb

    gdf = wb.load_lake(path=args.shape, shapes_dir=args.shapes_dir)
    info = wb.describe(gdf)
    print(json.dumps(info, indent=2, ensure_ascii=False))

    try:
        er = wb.erode(gdf, args.erode)
        print(f"после эрозии на {args.erode} м: "
              f"{wb.area_km2(er):.2f} км² "
              f"({100 * wb.area_km2(er) / info['area_km2']:.0f} % исходной)")
    except ValueError as e:
        print(f"! {e}")

    n_era5 = info["area_km2"] / (9 * 9)
    print(f"\nячеек ERA5-Land на акватории: ~{n_era5:.1f}")
    if n_era5 < 0.5:
        print("  ! Озеро меньше ячейки ERA5-Land. Значения будут взяты из"
              "\n    смешанной или сухопутной ячейки — см. раздел о рисках в README.")
    print(f"пикселей Landsat 30 м: ~{info['area_km2'] * 1e6 / 900:,.0f}")
    print(f"пикселей MODIS 1 км:   ~{info['area_km2']:.0f}")


def cmd_status(args):
    """Что уже скачано из GEE, а чего не хватает."""
    from . import ingest

    cfg = Config.from_yaml(args.config) if Path(args.config).exists() else Config()
    print(f"Каталог: {Path(cfg.paths.raw).resolve()}\n")

    st = ingest.status(cfg.paths.raw)
    for stem, info in st.items():
        mark = "  есть  " if info["found"] else "  нет   "
        print(f"{mark} {stem:<22} {info['what']}")

    if not st["landsat_lswt"]["found"]:
        print("\nТемпература воды обязательна. Выгрузите её:")
        print("  1. gee/export_data.js → code.earthengine.google.com")
        print("  2. Run → вкладка Tasks → запустить задачи")
        print(f"  3. Скачать CSV из Drive в {cfg.paths.raw}/")

    src = getattr(cfg, "meteo_source", "openmeteo")
    print(f"\nИсточник метеоданных: {src}")
    if src == "openmeteo":
        print("  Earth Engine для метео не нужен, era5land_daily.csv тоже.")


def cmd_run(args):
    from . import pipeline, plots, validate

    cfg = Config.from_yaml(args.config) if args.config else Config()
    if args.start:
        cfg.start = args.start
    if args.end:
        cfg.end = args.end
    if args.force:
        cfg.force_download = True
    if args.depth:
        cfg.mean_depth_m = args.depth
    if args.meteo:
        cfg.meteo_source = args.meteo

    df = pipeline.build_dataset(cfg)
    res = pipeline.compute_evaporation(df, cfg)

    outdir = Path(cfg.paths.outputs)
    outdir.mkdir(parents=True, exist_ok=True)

    res.to_csv(outdir / "evaporation_daily.csv", index=False)

    annual = pipeline.annual_summary(res)
    annual.to_csv(outdir / "evaporation_annual.csv", index=False)

    clim = pipeline.monthly_climatology(res)
    clim.to_csv(outdir / "evaporation_climatology.csv", index=False)

    area = df.attrs.get("area_km2", args.area or 50.0)
    wb_df = pipeline.water_balance(res, area)
    wb_df.to_csv(outdir / "water_balance_monthly.csv", index=False)

    if not args.no_plots:
        plots.plot_timeseries(res, outdir)
        plots.plot_climatology(clim, outdir)
        plots.plot_annual(annual, outdir)

    rep = validate.report(res)
    (outdir / "validation.txt").write_text(rep, encoding="utf-8")
    print("\n" + rep)

    print(f"\nСреднее годовое испарение: {annual['E_penman'].mean():.0f} мм")
    print(f"Среднегодовые осадки:      {annual['P'].mean():.0f} мм")
    print(f"Дефицит:                   {annual['E_minus_P'].mean():.0f} мм")
    print(f"\nРезультаты в {outdir}")


def cmd_forecast(args):
    from . import forecast, plots, waterbody as wb

    cfg = Config.from_yaml(args.config) if args.config else Config()

    if args.lat and args.lon:
        lat, lon = args.lat, args.lon
    else:
        gdf = wb.load_lake(shapes_dir=cfg.paths.shapes)
        lon, lat = wb.centroid(gdf)

    tw0 = args.tw
    if tw0 is None:
        cache = Path(cfg.paths.processed) / "inputs_daily.parquet"
        if cache.exists():
            hist = pd.read_parquet(cache)
            obs = hist.dropna(subset=["tw_obs"])
            if len(obs):
                tw0 = float(obs.iloc[-1]["tw_obs"])
                print(f"Начальная Tw из последнего снимка "
                      f"({obs.iloc[-1]['date'].date()}): {tw0:.1f} °C")
    if tw0 is None:
        sys.exit("Не задана начальная температура воды. Укажите --tw.")

    fc = forecast.forecast_evaporation(lat, lon, tw0,
                                       cfg.mean_depth_m, days=args.days)

    outdir = Path(cfg.paths.outputs)
    outdir.mkdir(parents=True, exist_ok=True)
    fc.to_csv(outdir / "forecast.csv", index=False)
    if not args.no_plots:
        plots.plot_forecast(fc, outdir)

    print(fc[["date", "ta", "u10", "tw", "E", "E_cum"]].to_string(index=False))
    print(f"\nСуммарно за {args.days} сут: {fc['E'].sum():.1f} мм")


def cmd_map(args):
    """Генерация интерактивной веб-карты."""
    from . import pipeline, webmap, waterbody as wb

    cfg = Config.from_yaml(args.config) if args.config else Config()
    outdir = Path(cfg.paths.outputs)

    daily = outdir / "evaporation_daily.csv"
    if not daily.exists():
        sys.exit(f"Нет {daily}. Сначала выполните `evap run`.")

    df = pd.read_csv(daily, parse_dates=["date"])
    clim = pipeline.monthly_climatology(df)
    annual = pipeline.annual_summary(df)

    gdf = wb.load_lake(shapes_dir=cfg.paths.shapes)

    ee_layers = []
    if not args.no_ee:
        print("Готовлю слои Earth Engine…")
        try:
            pipeline.init_ee(cfg.gee_project)
            ee_layers = webmap.build_ee_layers(gdf, cfg.start, cfg.end,
                                               cfg.erode_metres)
            print(f"  подготовлено слоёв: {len(ee_layers)}")
        except Exception as exc:
            print(f"  ! слои GEE пропущены: {exc}")

    out = webmap.build_map(gdf, df, clim, annual,
                           outdir / "map.html", ee_layers,
                           title=args.title)
    print(f"\nКарта: {out.resolve()}")
    if ee_layers:
        print("Ссылки на тайлы GEE живут около суток — "
              "для постоянной публикации перегенерируйте карту по расписанию.")

    if args.serve:
        import http.server, socketserver, functools, webbrowser
        handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                    directory=str(outdir))
        with socketserver.TCPServer(("", args.port), handler) as httpd:
            url = f"http://localhost:{args.port}/map.html"
            print(f"Сервер: {url}   (Ctrl+C для остановки)")
            webbrowser.open(url)
            httpd.serve_forever()


def cmd_validate(args):
    from . import validate

    cfg = Config.from_yaml(args.config) if args.config else Config()
    path = Path(cfg.paths.outputs) / "evaporation_daily.csv"
    if not path.exists():
        sys.exit(f"Нет {path}. Сначала выполните `evap run`.")

    df = pd.read_csv(path, parse_dates=["date"])
    print(validate.report(df))


def main():
    p = argparse.ArgumentParser(
        prog="evap",
        description="Расчёт испарения с водной поверхности по ERA5-Land "
                    "и спутниковой температуре воды")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("inspect", help="проверить шейп озера")
    pi.add_argument("--shape", default=None)
    pi.add_argument("--shapes-dir", default="data/shapes")
    pi.add_argument("--erode", type=float, default=60.0)
    pi.set_defaults(func=cmd_inspect)

    pr = sub.add_parser("run", help="полный расчёт за период")
    pr.add_argument("--config", default="config/config.yaml")
    pr.add_argument("--start"); pr.add_argument("--end")
    pr.add_argument("--depth", type=float, default=None)
    pr.add_argument("--area", type=float, default=None)
    pr.add_argument("--meteo", choices=["openmeteo", "gee", "csv"],
                    default=None, help="источник метеоданных")
    pr.add_argument("--force", action="store_true", help="игнорировать кэш")
    pr.add_argument("--no-plots", action="store_true")
    pr.set_defaults(func=cmd_run)

    pf = sub.add_parser("forecast", help="прогноз на 10 суток")
    pf.add_argument("--config", default="config/config.yaml")
    pf.add_argument("--days", type=int, default=10)
    pf.add_argument("--tw", type=float, default=None,
                    help="начальная температура воды, °C")
    pf.add_argument("--lat", type=float); pf.add_argument("--lon", type=float)
    pf.add_argument("--no-plots", action="store_true")
    pf.set_defaults(func=cmd_forecast)

    ps = sub.add_parser("status", help="какие данные уже скачаны")
    ps.add_argument("--config", default="config/config.yaml")
    ps.set_defaults(func=cmd_status)

    pm = sub.add_parser("map", help="интерактивная веб-карта")
    pm.add_argument("--config", default="config/config.yaml")
    pm.add_argument("--no-ee", action="store_true",
                    help="без растровых слоёв Earth Engine")
    pm.add_argument("--serve", action="store_true",
                    help="поднять локальный сервер и открыть в браузере")
    pm.add_argument("--port", type=int, default=8000)
    pm.add_argument("--title", default="Испарение с водной поверхности")
    pm.set_defaults(func=cmd_map)

    pv = sub.add_parser("validate", help="отчёт по валидации")
    pv.add_argument("--config", default="config/config.yaml")
    pv.set_defaults(func=cmd_validate)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
