# STAT GEOM QC 2.9.36

Version 2.9.36 brings geometry quality control into QGIS Processing. The same
analysis engine used by the dock panel can now run in graphical models, batch
jobs and headless `qgis_process` pipelines.

## Added

### `statgeomqc:analyze`

Analyzes a vector layer and returns model-friendly outputs including score,
grade, check coverage, feature count, total issue count, invalid geometries,
null or empty geometries, overlaps and duplicates.

### `statgeomqc:errorlayer`

Creates a point output locating detected issues. The output includes the full
issue description and a primary classification field suitable for categorized
QGIS symbology.

### `statgeomqc:report`

Creates HTML, CSV and/or JSON reports. Every requested format is an independent
output, so command-line and model workflows can request only what they need.

## Design decisions

- Processing and the dock panel share the analysis, report and error-layer
  engines; the quality logic is not duplicated.
- Each Processing algorithm performs its own analysis. Running all three in one
  model therefore performs three analyses, avoiding stale identifiers passed
  between independent runs.
- The measurement context is captured before background processing and does not
  depend on an open QGIS project.
- Engine warnings and check coverage are forwarded to Processing feedback so a
  zero counter is not mistaken for a successful check which never ran.

## Verification

- 137 headless tests pass with QGIS 3.40.5 on Windows 11.
- All three algorithms were loaded and executed through `qgis_process` in a
  clean temporary QGIS configuration.
- The included six-feature synthetic demo returned the expected invalid,
  self-intersection, overlap, duplicate, small-polygon and small-hole results.
- A three-run synthetic benchmark found all 99 injected overlap pairs in the
  50,000-feature scenario.

See the [benchmark method and raw results](../benchmarks/README.md) and the
[international release checklist](LAUNCH_CHECKLIST.md).

## Compatibility

- QGIS 3.16 to 3.99
- No external Python dependencies
- Existing GUI workflows remain available

## Release package

Local release candidate:

```text
stat_geom_qc_v2.9.36_release.zip
SHA-256: E83F7DF7E7257F3E6BB96D958E1A0D5A7E1890EA36B8D36A10FA8D0786335BDA
```

Recompute the checksum if the archive is rebuilt for any reason.
