# Synthetic demo layer

`demo_quality_issues.geojson` is a six-feature polygon layer created for public
screenshots, videos and command-line examples. It contains no external data.

## Deliberate cases

| `demo_id` | Case |
| ---: | --- |
| 1 | Clean reference polygon |
| 2 | One-metre-wide true overlap with the reference footprint |
| 3 | Exact duplicate of the reference footprint |
| 4 | One-square-metre polygon below the default threshold |
| 5 | Invalid bow-tie polygon with a self-intersection |
| 6 | Valid polygon containing a one-square-metre hole |

The exact score can change when scoring rules evolve. Validate categories and
counts rather than pinning promotional material to a permanent score.

Verified with STAT GEOM QC 2.9.36 and QGIS 3.40.5 using the Standard profile:

| Result | Count |
| --- | ---: |
| Features | 6 |
| Invalid geometries | 1 |
| Self-intersections | 1 |
| Overlap pairs | 2 |
| Features in overlaps | 3 |
| Duplicate geometries | 1 |
| Small polygons | 1 |
| Features with a small hole | 1 |
| Total issue count | 7 |
| Check coverage | 70 percent |

The self-intersection is a detail of the invalid geometry and is not added a
second time to the total issue count.

## GUI demo

1. Add `demo_quality_issues.geojson` to a blank QGIS project.
2. Open STAT GEOM QC (every analysis check is enabled by default).
3. Run the analysis.
4. Create the categorized error layer.
5. Click each issue to demonstrate map inspection.
6. Export an HTML report to a temporary public-safe folder.

## Processing demo

```bash
qgis_process plugins enable stat_geom_qc
qgis_process run statgeomqc:analyze -- INPUT=examples/demo_quality_issues.geojson
qgis_process run statgeomqc:errorlayer -- INPUT=examples/demo_quality_issues.geojson OUTPUT=TEMPORARY_OUTPUT
```

If the source checkout is not installed in the active QGIS profile, install the
release ZIP first. Processing only discovers enabled installed plugins.
