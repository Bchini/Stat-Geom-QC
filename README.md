# STAT GEOM QC

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-ffdd00?style=flat&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/bchini)

STAT GEOM QC is a QGIS plugin for inspecting, locating and documenting vector
geometry problems in one workflow. It combines a weighted correctness score,
an issue layer, safe selective repair and exportable HTML, CSV and JSON reports.

- Native PyQGIS implementation
- No external Python dependencies
- QGIS 3.16 to 3.99
- Original layers are never modified by repair operations
- GUI, Processing modeler, batch and `qgis_process` workflows

[Install from the official QGIS repository](https://plugins.qgis.org/plugins/stat_geom_qc/)

## Why it exists

A raw error count is not enough. A useful quality-control workflow must answer
four questions:

1. How reliable is this layer?
2. Which checks actually ran?
3. Where is every issue?
4. Can a correction be reviewed without changing the source?

STAT GEOM QC answers these questions with a score accompanied by check
coverage, exact issue locations, explicit inconclusive states, and repair output
written to a new file.

## Checks

| Area | Checks |
| --- | --- |
| Geometry integrity | Null or empty geometries, invalid geometries, self-intersections |
| Polygon relationships | Overlaps, contained polygons, missing-vertex boundary cases |
| Duplication | Duplicate geometries, exactly duplicated vertices |
| Shape quality | Small polygons, small holes, multipart features, sharp angles, close vertices |
| Building data | Optional height consistency for nested polygons using `AGL` or `HEIGHT` |

Optional checks are recorded as not requested when disabled. Checks which run
but cannot reach a conclusion are reported separately instead of being treated
as clean results.

## Quick start in QGIS

1. Open **Plugins > Manage and Install Plugins**.
2. Search for **STAT GEOM QC** and install it.
3. Open the plugin from the **Vector** menu or toolbar.
4. Select a vector layer and an analysis profile.
5. Run the analysis, inspect the score and create an error layer or report.

## Automation with QGIS Processing

The source version exposes three Processing algorithms:

| Algorithm | Purpose |
| --- | --- |
| `statgeomqc:analyze` | Return score, coverage and issue counters |
| `statgeomqc:errorlayer` | Create a point layer locating detected issues |
| `statgeomqc:report` | Write HTML, CSV and/or JSON reports |

Example:

```bash
qgis_process plugins enable stat_geom_qc
qgis_process run statgeomqc:analyze -- INPUT=buildings.gpkg
qgis_process run statgeomqc:report -- INPUT=buildings.gpkg OUTPUT_HTML=qc-report.html
```

Each algorithm is autonomous and performs its own analysis. Running all three
therefore performs three analyses. This avoids passing stale feature identifiers
between independent model steps.

## Interpreting the score

The score is a triage and monitoring indicator, not a formal certification.
Compare scores only when the same profile, thresholds and eligible population
were used. Always read the coverage and warning fields alongside the score.

## Documentation

- [90-second English demo script](docs/DEMO_EN.md)
- [Professional use cases](docs/USE_CASES_EN.md)
- [International release checklist](docs/LAUNCH_CHECKLIST.md)
- [English release notes for 2.9.36](docs/RELEASE_NOTES_2.9.36_EN.md)
- [Reproducible benchmarks](benchmarks/README.md)
- [Latest measured benchmark results](benchmarks/results/RESULTS_2026-09-02.md)
- [Synthetic public demo layer](examples/README.md)
- [Detailed French user guide](stat_geom_qc/README.md)

## Development

The plugin package is in [`stat_geom_qc/`](stat_geom_qc/). Headless tests can be
run with the Python environment bundled with QGIS:

```powershell
& "C:\Program Files\QGIS 3.40.5\bin\python-qgis-ltr.bat" `
  "stat_geom_qc\tests\test_analysis_engine.py"
```

## License and contact

GNU General Public License v2.0 or later. Maintained by Adel Bchini:
<adel.bchini@gmail.com>.
