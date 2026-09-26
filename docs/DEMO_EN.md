# STAT GEOM QC - 90-second English demo

This script is designed for a 16:9 screen recording with English narration and
captions. Use a synthetic or clearly licensed public dataset. Do not display
employer, client or confidential layer names, file paths, attributes or project
bookmarks.

## Demo promise

In 90 seconds, show that STAT GEOM QC turns a vague geometry-quality problem
into three actionable outputs: a measured score, exact issue locations and an
auditable report.

## Before recording

1. Publish the source version containing the Processing provider, then confirm
   that the QGIS repository serves that same version.
2. Create a clean QGIS profile with only STAT GEOM QC enabled.
3. Set QGIS to a neutral theme and hide personal toolbars and recent paths.
4. Load [`examples/demo_quality_issues.geojson`](../examples/demo_quality_issues.geojson),
   the public synthetic fixture included in this repository.
5. Prepare an empty output folder with no personal or company name in its path.
6. Record at 1920x1080, 30 fps, with the cursor enlarged to 125 percent.

This is an English narration and caption script. Do not claim that the plugin UI
itself is localized into English until all visible interface and report strings
have actually been translated and tested.

## Shot list and narration

| Time | Screen action | English voice-over |
| --- | --- | --- |
| 0-07 s | Start on a polygon layer. Briefly zoom into an invisible overlap. | "A vector layer can look perfect and still fail in production." |
| 07-16 s | Open STAT GEOM QC from the Vector menu. | "STAT GEOM QC checks geometry quality inside QGIS, without external dependencies." |
| 16-28 s | Select the layer. Point to the enabled checks. | "Choose a layer: every analysis check is on by default. Every requested check and threshold is recorded, so the result can be reproduced later." |
| 28-39 s | Click the analysis button. Show progress, then the score and coverage. | "One run returns a weighted correctness score, its check coverage and a clear breakdown of the issues found." |
| 39-53 s | Open the quality details and click one issue. Zoom to it. | "The plugin does not stop at a counter. Select an issue and jump directly to the geometry that needs attention." |
| 53-64 s | Create the point error layer and show its categorized legend. | "Create an error layer to review, filter or share every location as normal QGIS data." |
| 64-74 s | Open the report preview and scroll through checks and warnings. | "Open the report in the browser, or export it as HTML or PDF, with the checks, thresholds, warnings and inconclusive checks." |
| 74-83 s | Open the Processing toolbox and search STAT GEOM QC. | "The same engine also runs in Processing, batch models and qgis_process for automated quality gates." |
| 83-90 s | End card with plugin name and official URL. | "STAT GEOM QC is free and open source. Install it from the official QGIS plugin repository." |

## On-screen captions

Use no more than four words per caption:

```text
Measure quality
Locate every issue
Repair safely
Automate with Processing
Free and open source
```

## End card

```text
STAT GEOM QC
Vector quality control for QGIS
plugins.qgis.org/plugins/stat_geom_qc/
```

## Editing rules

- Keep cuts under eight seconds except while the analysis is running.
- Accelerate waiting time, but display the real elapsed time in a caption.
- Never call a score a certification or promise that automatic repair is safe
  for every data model.
- Never publish a benchmark number without linking its dataset, parameters and
  environment.
- Add burned-in English subtitles because most LinkedIn videos start muted.

## Short 25-second cut

```text
A layer can look perfect and still contain invalid geometries, duplicates and
overlaps. STAT GEOM QC measures the problem, shows which checks actually ran,
locates every issue on the map and exports an auditable report. The original
layer is never modified during repair. It also runs in QGIS Processing and
qgis_process. Free and open source on the official QGIS plugin repository.
```
