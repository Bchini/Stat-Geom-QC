# Professional use cases

These examples explain where STAT GEOM QC adds value without replacing domain
rules, visual review or contractual acceptance procedures.

## 1. Building-footprint delivery gate

### Situation

A production team receives or generates building footprints from several
sources. The layer looks plausible at normal map scale, but invalid rings,
duplicates, small artifacts and true overlaps can break extrusion, tiling or
spatial joins later in the pipeline.

### Workflow

1. Run the default analysis (every *Analysis options* check) on the delivery layer.
2. Read score and check coverage together.
3. Export the categorized point error layer.
4. Review true overlap locations and any inconclusive checks.
5. Apply only approved repairs to a new GeoPackage.
6. Re-run the same checks and thresholds on the repaired output.
7. Archive both reports with the delivery package.

### Acceptance evidence

Record the plugin version, QGIS version, enabled checks, thresholds, CRS, feature count,
coverage and remaining issue counts. A threshold such as "score >= 95" is only
meaningful when these parameters are fixed in the delivery specification.

## 2. Cadastral or land-cover topology review

### Situation

Adjacent polygons are expected to form a coverage. Manual inspection misses
small intersections, while a raw topological check can produce a review queue
too large to process efficiently.

### Workflow

1. Decide whether contained polygons are valid for this data model.
2. Decide whether the named missing-vertex overlap exception is acceptable.
3. Run overlap detection with those decisions recorded.
4. Create the point error layer and filter it by `qc_class`.
5. Assign the remaining points to editors or review them by geographic zone.
6. Treat automatic overlap clipping as a proposed edit, not ground truth.

### Important limitation

STAT GEOM QC checks overlaps but is not a complete coverage validator. If the
specification also forbids gaps or requires shared-boundary equality, combine it
with QGIS coverage-validation algorithms and document both results.

## 3. Automated nightly quality monitoring

### Situation

A GeoPackage or exported database layer is rebuilt every night. The team needs
to detect regressions before publishing downstream products, without opening a
QGIS desktop session.

### Workflow

Enable the plugin once for the QGIS profile used by the automation account:

```bash
qgis_process plugins enable stat_geom_qc
```

Run the analysis, then write the reports:

```bash
qgis_process run statgeomqc:analyze -- INPUT=nightly-buildings.gpkg
qgis_process run statgeomqc:report -- INPUT=nightly-buildings.gpkg OUTPUT_HTML=qc-report.html OUTPUT_PDF=qc-report.pdf
```

Read score, coverage and issue counters from the outputs of
`statgeomqc:analyze` (the reports are for people, not for parsing). Fail the
pipeline only against rules agreed for a fixed set of checks, for example:

```text
coverage must equal 100 percent for mandatory checks
invalid geometries must equal 0
null or empty geometries must equal 0
true overlap count must not increase from the approved baseline
```

### Operational note

The Processing algorithms are autonomous. Calling `analyze`, `errorlayer` and
`report` in one model performs the analysis three times. Schedule only the
outputs required by the pipeline, or account for that cost in runtime budgets.

## What the plugin should not be sold as

- It is not a replacement for a data-product specification.
- Its score is not comparable across different sets of checks or thresholds.
- A geometry can be valid according to GEOS and still be semantically wrong.
- Automatic repair cannot determine legal ownership or editing priority.
- Height checks apply only when their field meaning matches the documented rule.
