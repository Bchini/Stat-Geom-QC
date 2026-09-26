# -*- coding: utf-8 -*-
"""Reproducible headless benchmark for STAT GEOM QC.

Run this script with the Python launcher bundled with QGIS, not a system Python.
It writes raw JSON and CSV results and never records a full input path.
"""

import argparse
import configparser
import csv
import json
import math
import os
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from osgeo import gdal  # noqa: E402
from qgis.core import (  # noqa: E402
    Qgis,
    QgsApplication,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProjUtils,
    QgsVectorLayer,
)

from stat_geom_qc import constants as C  # noqa: E402
from stat_geom_qc.analysis_engine import AnalysisOptions, GeomAnalyzer  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser(
        description="Benchmark the STAT GEOM QC analysis engine in headless QGIS.")
    parser.add_argument(
        "--sizes", nargs="+", type=int, default=[1000, 10000],
        help="Synthetic feature counts (default: 1000 10000).")
    parser.add_argument(
        "--scenario", choices=("clean", "controlled-overlap", "both"),
        default="both", help="Synthetic geometry pattern (default: both).")
    parser.add_argument(
        "--profile", choices=tuple(C.PRESET_ORDER), default="standard",
        help="Plugin analysis profile (default: standard).")
    parser.add_argument(
        "--repeats", type=int, default=3,
        help="Timed runs per dataset; use at least 3 for publication.")
    parser.add_argument(
        "--issue-every", type=int, default=500,
        help="Controlled overlap interval (default: every 500 features).")
    parser.add_argument(
        "--input", type=Path,
        help="Optional public vector dataset; disables synthetic scenarios.")
    parser.add_argument(
        "--label", default="Public dataset",
        help="Public label stored for --input; no local path is recorded.")
    parser.add_argument(
        "--output-dir", type=Path, default=SCRIPT_DIR / "results",
        help="Destination directory for JSON and CSV results.")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if any(size < 1 for size in args.sizes):
        parser.error("every --sizes value must be at least 1")
    if args.issue_every < 2:
        parser.error("--issue-every must be at least 2")
    if args.input is not None and not args.input.is_file():
        parser.error("--input does not exist or is not a file")
    return args


def init_qgis():
    prefix = os.environ.get(
        "QGIS_PREFIX_PATH", r"C:\Program Files\QGIS 3.40.5\apps\qgis-ltr")
    QgsApplication.setPrefixPath(prefix, True)
    app = QgsApplication([], False)
    app.initQgis()
    return app


def plugin_version():
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(PROJECT_DIR / "stat_geom_qc" / "metadata.txt", encoding="utf-8")
    return parser.get("general", "version", fallback="unknown")


def system_memory_gb():
    """Best-effort physical memory without adding a dependency."""
    if os.name == "nt":
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_phys", ctypes.c_ulonglong),
                    ("avail_phys", ctypes.c_ulonglong),
                    ("total_page", ctypes.c_ulonglong),
                    ("avail_page", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("avail_virtual", ctypes.c_ulonglong),
                    ("avail_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return round(status.total_phys / (1024 ** 3), 1)
        except Exception:
            return None
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        pages = os.sysconf("SC_PHYS_PAGES")
        return round(page_size * pages / (1024 ** 3), 1)
    except (AttributeError, OSError, ValueError):
        return None


def environment_info():
    processor = platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "")
    return {
        "plugin_version": plugin_version(),
        "qgis_version": Qgis.QGIS_VERSION,
        "python_version": platform.python_version(),
        "geos_version": Qgis.geosVersion(),
        "gdal_version": gdal.VersionInfo("RELEASE_NAME"),
        "proj_version": "%s.%s" % (
            QgsProjUtils.projVersionMajor(), QgsProjUtils.projVersionMinor()),
        "os": platform.platform(),
        "processor": processor,
        "logical_cores": os.cpu_count(),
        "system_memory_gb": system_memory_gb(),
    }


def options_for_profile(profile):
    preset = C.PRESETS[profile]
    return AnalysisOptions(
        topology_checks=preset["topology"],
        check_overlaps=preset["overlaps"],
        check_duplicates=preset["duplicates"],
        check_small_polygons=preset["small"],
        check_duplicate_vertex=preset["duplicate_vertex"],
        check_holes=preset["holes"],
        check_sharp_angles=preset["sharp_angles"],
        check_close_vertices=preset["close_vertices"],
        check_nested_height=preset["nested_height"],
        ignore_missing_vertex_overlaps=preset["ignore_missing_vertex"],
        allow_contained_polygons=preset["allow_contained"],
        profile_name=profile,
        preview_limit_rows=0,
    )


def square_geometry(x, y, size=10.0):
    ring = [
        QgsPointXY(x, y),
        QgsPointXY(x + size, y),
        QgsPointXY(x + size, y + size),
        QgsPointXY(x, y + size),
        QgsPointXY(x, y),
    ]
    return QgsGeometry.fromPolygonXY([ring])


def synthetic_layer(feature_count, scenario, issue_every):
    layer = QgsVectorLayer("Polygon?crs=EPSG:3857", "synthetic-%s-%d" % (
        scenario, feature_count), "memory")
    if not layer.isValid():
        raise RuntimeError("QGIS could not create the synthetic memory layer")

    provider = layer.dataProvider()
    columns = int(math.ceil(math.sqrt(feature_count)))
    batch = []
    injected_overlaps = 0
    next_overlap_at = issue_every
    for index in range(feature_count):
        row, column = divmod(index, columns)
        x = column * 12.0
        y = row * 12.0
        if (scenario == "controlled-overlap" and index >= next_overlap_at
                and column > 0):
            # The previous square ends at x - 2. Moving this one left by 3 m
            # creates an exact 1 m wide, 10 m high overlap.
            x -= 3.0
            injected_overlaps += 1
            next_overlap_at += issue_every
        feature = QgsFeature(layer.fields())
        feature.setGeometry(square_geometry(x, y))
        batch.append(feature)
        if len(batch) == 5000:
            ok, _added = provider.addFeatures(batch)
            if not ok:
                raise RuntimeError("QGIS rejected a synthetic feature batch")
            batch = []
    if batch:
        ok, _added = provider.addFeatures(batch)
        if not ok:
            raise RuntimeError("QGIS rejected the final synthetic feature batch")
    layer.updateExtents()
    if layer.featureCount() != feature_count:
        raise RuntimeError("synthetic layer feature count mismatch")
    return layer, injected_overlaps


def load_input(path, label):
    layer = QgsVectorLayer(str(path), label, "ogr")
    if not layer.isValid():
        raise RuntimeError("QGIS could not load the input dataset")
    if layer.featureCount() < 1:
        raise RuntimeError("input dataset contains no features")
    return layer


def warm_up(options):
    layer, _expected = synthetic_layer(100, "clean", 50)
    GeomAnalyzer(options).analyze(layer)


def run_one(layer, label, scenario, profile, repeats, issue_every,
            expected_overlap_pairs=None):
    options = options_for_profile(profile)
    durations = []
    result = None
    for repetition in range(1, repeats + 1):
        started = time.perf_counter()
        result = GeomAnalyzer(options).analyze(layer)
        elapsed = time.perf_counter() - started
        durations.append(round(elapsed, 6))
        print("%-32s run %d/%d: %.3f s" % (
            label, repetition, repeats, elapsed), flush=True)

    median = statistics.median(durations)
    q = result.quality_report
    if (expected_overlap_pairs is not None
            and q.overlap_pairs != expected_overlap_pairs):
        raise RuntimeError(
            "%s: injected %d overlap pair(s), detected %d"
            % (label, expected_overlap_pairs, q.overlap_pairs))
    return {
        "dataset": label,
        "scenario": scenario,
        "features": int(result.total_features),
        "profile": profile,
        "issue_every": issue_every if scenario == "controlled-overlap" else None,
        "expected_overlap_pairs": expected_overlap_pairs,
        "repetitions_seconds": durations,
        "median_seconds": round(median, 6),
        "min_seconds": round(min(durations), 6),
        "max_seconds": round(max(durations), 6),
        "features_per_second": round(result.total_features / median, 1),
        "score": float(result.correctness.score),
        "coverage_pct": float(result.correctness.coverage_pct()),
        "total_issues": int(result.total_issues()),
        "invalid": int(q.invalid_count),
        "null_empty": int(q.null_empty_count),
        "overlap_features": int(q.overlap_count),
        "overlap_pairs": int(q.overlap_pairs),
        "duplicates": int(q.duplicate_count),
        "warnings": list(result.warnings),
        "errors": list(result.errors),
    }


def write_results(payload, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = output_dir / ("benchmark-%s.json" % stamp)
    csv_path = output_dir / ("benchmark-%s.csv" % stamp)

    with json_path.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")

    fields = [
        "plugin_version", "qgis_version", "scenario", "dataset", "features",
        "profile", "median_seconds", "min_seconds", "max_seconds",
        "features_per_second", "score", "coverage_pct", "total_issues",
        "invalid", "null_empty", "overlap_features", "overlap_pairs",
        "expected_overlap_pairs", "duplicates", "repetitions_seconds",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for result in payload["results"]:
            row = {key: result.get(key, "") for key in fields}
            row["plugin_version"] = payload["environment"]["plugin_version"]
            row["qgis_version"] = payload["environment"]["qgis_version"]
            row["repetitions_seconds"] = json.dumps(
                result["repetitions_seconds"], separators=(",", ":"))
            writer.writerow(row)
    return json_path, csv_path


def main():
    args = parse_args()
    app = init_qgis()
    try:
        options = options_for_profile(args.profile)
        print("Warming up QGIS and GEOS...", flush=True)
        warm_up(options)

        results = []
        if args.input is not None:
            layer = load_input(args.input, args.label)
            results.append(run_one(
                layer, args.label, "public-input", args.profile,
                args.repeats, None, None))
            input_file = args.input.name
        else:
            scenarios = ("clean", "controlled-overlap") \
                if args.scenario == "both" else (args.scenario,)
            for size in args.sizes:
                for scenario in scenarios:
                    label = "synthetic-%s-%d" % (scenario, size)
                    print("Building %s outside timed section..." % label, flush=True)
                    layer, expected = synthetic_layer(
                        size, scenario, args.issue_every)
                    results.append(run_one(
                        layer, label, scenario, args.profile,
                        args.repeats, args.issue_every, expected))
                    del layer
            input_file = None

        payload = {
            "schema_version": 1,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "method": "warm-up plus repeated in-process GeomAnalyzer runs",
            "timed_scope": "analysis only; excludes load, generation and exports",
            "environment": environment_info(),
            "configuration": {
                "profile": args.profile,
                "repeats": args.repeats,
                "scenario": "public-input" if args.input else args.scenario,
                "sizes": None if args.input else args.sizes,
                "issue_every": None if args.input else args.issue_every,
                "input_file": input_file,
            },
            "results": results,
        }
        json_path, csv_path = write_results(payload, args.output_dir)
        print("JSON: %s" % json_path, flush=True)
        print("CSV:  %s" % csv_path, flush=True)
        return 0
    finally:
        app.exitQgis()


if __name__ == "__main__":
    raise SystemExit(main())
