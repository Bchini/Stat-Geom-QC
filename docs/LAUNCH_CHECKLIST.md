# International release checklist

Use this checklist before promoting Processing or any unreleased feature.

## Release integrity

- [ ] `metadata.txt` contains the intended version.
- [ ] The exact source is pushed to GitHub.
- [ ] A Git tag and GitHub Release exist for that version.
- [ ] The release ZIP is built from the tagged commit.
- [ ] The ZIP contains one top-level folder named `stat_geom_qc`.
- [ ] The QGIS repository displays and downloads the same version.
- [ ] README claims match actual defaults and available algorithms.
- [ ] The QGIS security scanner passes.

## Product verification

- [ ] Headless tests pass with QGIS 3.40.5.
- [ ] Smoke tests pass on the current QGIS LTR and latest stable QGIS.
- [ ] GUI analysis, cancellation, error-layer creation and report export work.
- [ ] `statgeomqc:analyze`, `statgeomqc:errorlayer` and `statgeomqc:report` appear
      in the Processing toolbox.
- [ ] The three algorithms run through `qgis_process` without an open project.
- [ ] GeoPackage, Shapefile, GeoJSON and memory layers have been tested.
- [ ] A report identifies disabled and inconclusive checks correctly.

## Public evidence

- [ ] Demo data is synthetic or has a documented public license.
- [ ] Screenshots contain no employer, client, username or internal path.
- [ ] Every benchmark publishes dataset generation, profile and thresholds.
- [ ] Every benchmark publishes QGIS, GEOS, GDAL, OS and hardware details.
- [ ] Results use at least three measured runs and report the median.
- [ ] Marketing distinguishes measured facts from interpretation.

## LinkedIn launch sequence

1. Publish a 25- to 90-second subtitled demo with one clear problem and outcome.
2. Put the official plugin link in the post or first comment, but make it easy to
   find and do not obscure that the project is personal.
3. Ask one technical question that practitioners can answer from experience.
4. Reply to substantive questions with reproducible examples, not promises.
5. Publish a follow-up containing benchmark methods and downloadable results.
6. Invite issue reports with minimal, non-confidential sample data.

## Professional-boundary check

- [ ] The project ownership and license are clear.
- [ ] No employer or client code, data, metrics, branding or confidential method
      appears in the release or promotion.
- [ ] Any applicable employment or intellectual-property policy has been
      reviewed rather than concealed.

Large reach cannot be guaranteed. Trust, reproducibility and a maintained public
repository are more durable adoption signals than impressions alone.
