#!/usr/bin/env python3
"""
Сверка JavaScript-порта физики с эталонной Python-версией.

ЗАЧЕМ. web/js/physics.js — ручной перевод src/evap/physics.py. Перепутанная
константа, потерянный множитель или другой порядок операций дают результат,
который выглядит правдоподобно и потому не обнаруживается глазами. Здесь обе
реализации прогоняются на одинаковых входах и сравниваются численно.

Запуск:
    python scripts/check_js_port.py

Требуется node. Ненулевой код возврата означает расхождение — правьте JS.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evap import physics as ph  # noqa: E402

TOL_REL = 1e-9      # обе реализации в double, расхождение допустимо только
                    # на уровне порядка операций

# Наборы входов: обычные условия, крайние, зимние, штиль, насыщенный воздух
CASES = [
    dict(ta=25.0, tdew=10.0, u10=3.0, p=92.0, rs=25.0, rl=32.0, tw=20.0, g=2.0),
    dict(ta=35.0, tdew=2.0, u10=9.0, p=88.0, rs=31.0, rl=36.0, tw=27.0, g=5.0),
    dict(ta=-8.0, tdew=-14.0, u10=4.0, p=95.0, rs=3.5, rl=17.0, tw=1.0, g=-2.0),
    dict(ta=18.0, tdew=18.0, u10=0.2, p=101.3, rs=14.0, rl=28.0, tw=17.0, g=0.0),
    dict(ta=5.0, tdew=-3.0, u10=12.0, p=90.0, rs=8.0, rl=22.0, tw=6.0, g=-1.5),
    dict(ta=30.0, tdew=25.0, u10=1.5, p=99.0, rs=22.0, rl=34.0, tw=29.0, g=8.0),
]

JS_DRIVER = """
import * as ph from '../web/js/physics.js';

const cases = JSON.parse(process.argv[2]);
const out = cases.map(c => ({
  svp:        ph.satVapourPressure(c.ta),
  slope:      ph.svpSlope(c.ta),
  gamma:      ph.psychrometricConstant(c.p),
  lambda:     ph.latentHeat(c.ta),
  u2:         ph.windAt2m(c.u10),
  rn:         ph.netRadiation(c.rs, c.rl, c.tw),
  rlDown:     ph.estimateRlDown(c.ta, c.tdew, 40),
  penman:     ph.penmanOpenWater({ta: c.ta, tdew: c.tdew, u10: c.u10,
                pKpa: c.p, rsDown: c.rs, rlDown: c.rl, tw: c.tw, g: c.g}),
  penmanNoTw: ph.penmanOpenWater({ta: c.ta, tdew: c.tdew, u10: c.u10,
                pKpa: c.p, rsDown: c.rs, rlDown: c.rl, tw: null, g: 0}),
  massTr:     ph.massTransfer(c.tw, c.tdew, c.u10, 0, 1.4),
  priestley:  ph.priestleyTaylor({ta: c.ta, pKpa: c.p, rsDown: c.rs,
                rlDown: c.rl, tw: c.tw, g: c.g}),
  mixedDepth: ph.mixedLayerDepth(c.tw, 12.0, ph.windAt2m(c.u10)),
}));
console.log(JSON.stringify(out));
"""

HEAT_STORAGE_DRIVER = """
import * as ph from '../web/js/physics.js';
const tw = JSON.parse(process.argv[2]);
console.log(JSON.stringify(ph.heatStorage(tw, 10.0, 15)));
"""


def python_reference(c: dict) -> dict:
    """Эталонные значения из Python-модуля."""
    from evap.openmeteo import estimate_rl_down

    return {
        "svp": float(ph.sat_vapour_pressure(c["ta"])),
        "slope": float(ph.svp_slope(c["ta"])),
        "gamma": float(ph.psychrometric_constant(c["p"])),
        "lambda": float(ph.latent_heat(c["ta"])),
        "u2": float(ph.wind_at_2m(c["u10"])),
        "rn": float(ph.net_radiation(c["rs"], c["rl"], c["tw"])),
        "rlDown": float(estimate_rl_down(c["ta"], c["tdew"], 40)),
        "penman": float(ph.penman_openwater(
            c["ta"], c["tdew"], c["u10"], c["p"], c["rs"], c["rl"],
            tw_c=c["tw"], g=c["g"])),
        "penmanNoTw": float(ph.penman_openwater(
            c["ta"], c["tdew"], c["u10"], c["p"], c["rs"], c["rl"],
            tw_c=None, g=0.0)),
        "massTr": float(ph.mass_transfer(c["tw"], c["tdew"], c["u10"], 0, 1.4)),
        "priestley": float(ph.priestley_taylor(
            c["ta"], c["p"], c["rs"], c["rl"], tw_c=c["tw"], g=c["g"])),
        "mixedDepth": float(ph.mixed_layer_depth(
            np.array([c["tw"]]), 12.0, np.array([ph.wind_at_2m(c["u10"])]))[0]),
    }


def run_node(driver: str, payload) -> list:
    tmp = ROOT / "scripts" / "_tmp_driver.mjs"
    tmp.write_text(driver, encoding="utf-8")
    try:
        r = subprocess.run(
            ["node", str(tmp), json.dumps(payload)],
            capture_output=True, text=True, cwd=ROOT / "scripts")
        if r.returncode != 0:
            print("Ошибка node:\n" + r.stderr)
            sys.exit(2)
        return json.loads(r.stdout)
    finally:
        tmp.unlink(missing_ok=True)


def main():
    print("Сверка web/js/physics.js с src/evap/physics.py\n")

    js_results = run_node(JS_DRIVER, CASES)
    failures = []

    for i, (case, js) in enumerate(zip(CASES, js_results)):
        py = python_reference(case)
        for key in py:
            a, b = py[key], js[key]
            denom = max(abs(a), abs(b), 1e-12)
            rel = abs(a - b) / denom
            if rel > TOL_REL:
                failures.append((i, key, a, b, rel))

    # отдельно: теплозапас на годовом ходе — там сглаживание и градиент,
    # где расхождения между реализациями наиболее вероятны
    doy = np.arange(400)
    tw_series = (12.0 + 10.0 * np.sin(2 * np.pi * (doy - 100) / 365)).tolist()

    js_g = run_node(HEAT_STORAGE_DRIVER, tw_series)
    py_g = ph.heat_storage(np.array(tw_series), 10.0, smooth_window=15)

    max_diff = float(np.max(np.abs(np.array(js_g) - py_g)))
    scale = float(np.max(np.abs(py_g)))
    if max_diff / scale > 1e-9:
        failures.append(("heatStorage", "ряд", scale, max_diff,
                         max_diff / scale))

    n_checks = len(CASES) * len(python_reference(CASES[0])) + 1

    if failures:
        print(f"РАСХОЖДЕНИЯ ({len(failures)} из {n_checks}):\n")
        for case_i, key, a, b, rel in failures:
            print(f"  случай {case_i}, {key}:")
            print(f"    python = {a!r}")
            print(f"    js     = {b!r}")
            print(f"    отн. расхождение = {rel:.3e}\n")
        sys.exit(1)

    print(f"Совпало: {n_checks} проверок на {len(CASES)} наборах входов.")
    print(f"Допуск {TOL_REL:.0e}, теплозапас сверен на годовом ходе (400 сут).")


if __name__ == "__main__":
    main()
