# Reproducible benchmark protocol

This benchmark measures the analysis engine with QGIS itself. It does not time
plugin startup, layer loading, synthetic-data generation, report rendering or
file writing.

The benchmark has no Python dependency outside the QGIS installation.

## Why synthetic data is the default

Public performance claims must be reproducible and safe to share. The default
generator creates projected polygon layers in EPSG:3857 and never reads company
or client data.

Two scenarios are available:

| Scenario | Geometry pattern | Purpose |
| --- | --- | --- |
| `clean` | 10 m squares separated by 2 m | Typical sparse spatial-index workload |
| `controlled-overlap` | Same grid, with a 1 m overlap at a fixed interval | Repeatable candidate and overlap workload |

The generated data is a performance fixture, not a model of a real city.

## Run on Windows with QGIS 3.40 LTR

From the `qgis_plugin` directory:

```powershell
& "C:\Program Files\QGIS 3.40.5\bin\python-qgis-ltr.bat" `
  "benchmarks\benchmark_stat_geom_qc.py" `
  --sizes 1000 10000 50000 `
  --scenario both `
  --profile standard `
  --repeats 3
```

Quick smoke benchmark:

```powershell
& "C:\Program Files\QGIS 3.40.5\bin\python-qgis-ltr.bat" `
  "benchmarks\benchmark_stat_geom_qc.py" --sizes 1000 --repeats 1
```

Outputs are written to `benchmarks/results/` as matching JSON and CSV files.
The script reports all repetitions plus median, minimum, maximum and processed
features per second.

Latest measured run: [results from 2026-09-02](results/RESULTS_2026-09-02.md).

## Run on a public dataset

Only use a dataset whose license and download URL can be published:

```powershell
& "C:\Program Files\QGIS 3.40.5\bin\python-qgis-ltr.bat" `
  "benchmarks\benchmark_stat_geom_qc.py" `
  --input "C:\public-data\buildings.gpkg" `
  --label "Public buildings sample" `
  --profile standard `
  --repeats 3
```

The output stores only the label and base filename, never the full local path.
Record the public source URL, license and checksum separately when publishing.

## Method

1. Start one headless QGIS process.
2. Run a small warm-up analysis to initialize QGIS and GEOS.
3. Construct or load the benchmark layer outside the timed section.
4. Analyze the same unchanged layer at least three times.
5. Verify that the engine found exactly the number of synthetic overlaps injected.
6. Report every duration and use the median as the headline value.
7. Record result counters to prove that each run performed the expected work.

## Rules for publishing results

- Publish the raw JSON and CSV files.
- State plugin, QGIS, Python, GEOS, GDAL and OS versions.
- State CPU, logical-core count and available system memory.
- State profile, thresholds, scenario and feature count.
- Use the median of at least three runs.
- Do not compare results produced with different checks enabled.
- Do not claim that a synthetic grid predicts all real datasets.
- Do not publish private paths, layer names or production statistics.
- Re-run after every algorithmic change; never carry old numbers forward.

## Suggested results table

| Plugin | QGIS | Scenario | Features | Profile | Median | Features/s | Issues |
| --- | --- | --- | ---: | --- | ---: | ---: | ---: |
| 2.9.36 | 3.40.5 | clean | measured | standard | measured | measured | measured |
| 2.9.36 | 3.40.5 | controlled-overlap | measured | standard | measured | measured | measured |

Replace every `measured` value from the generated result files. Never estimate
or invent a missing benchmark.
