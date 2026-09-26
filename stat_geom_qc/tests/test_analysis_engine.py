# -*- coding: utf-8 -*-
"""Tests du moteur d'analyse STAT GEOM QC (exécutables en QGIS headless).

Lancement direct (Windows, QGIS LTR) :
    "C:\\Program Files\\QGIS 3.40.5\\bin\\python-qgis-ltr.bat" \\
        qgis_plugin\\stat_geom_qc\\tests\\test_analysis_engine.py

Le script initialise QGIS lui-même, construit des couches mémoire synthétiques
couvrant les cas demandés (normal, vide, géométrie invalide, champ absent,
annulation, réparation, volume) et vérifie les compteurs produits.
Il est aussi compatible pytest (les fonctions ``test_*`` réutilisent la session
QGIS initialisée par ``_ensure_qgis``).
"""

import os
import sys

# Rendre le paquet « stat_geom_qc » importable (dossier parent du plugin).
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PARENT = os.path.dirname(_PLUGIN_DIR)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant  # noqa: E402

from stat_geom_qc.analysis_engine import (  # noqa: E402
    AnalysisCancelled,
    AnalysisOptions,
    AnalysisResult,
    GeomAnalyzer,
    QualityReport,
    RepairOptions,
    RepairStats,
    check_layer_ready,
    classify_overlap,
    compute_correctness,
    layer_units_per_metre,
    count_parts,
    count_small_holes,
    sliver_ratio,
)
from stat_geom_qc import constants as C  # noqa: E402

_APP = None


def _ensure_qgis():
    global _APP
    if _APP is None:
        prefix = os.environ.get("QGIS_PREFIX_PATH",
                                r"C:/Program Files/QGIS 3.40.5/apps/qgis-ltr")
        QgsApplication.setPrefixPath(prefix, True)
        _APP = QgsApplication([], False)
        _APP.initQgis()
    return _APP


# ── Constructeurs de couches synthétiques ───────────────────────────────────

def _make_layer(geom_type="Polygon", crs="EPSG:2154", fields=None):
    uri = "%s?crs=%s" % (geom_type, crs)
    vl = QgsVectorLayer(uri, "test", "memory")
    pr = vl.dataProvider()
    if fields:
        pr.addAttributes([QgsField(n, t) for n, t in fields])
        vl.updateFields()
    return vl


def _add(vl, wkt, attrs=None):
    f = QgsFeature(vl.fields())
    if wkt is not None:
        f.setGeometry(QgsGeometry.fromWkt(wkt))
    if attrs:
        for k, v in attrs.items():
            f.setAttribute(k, v)
    vl.dataProvider().addFeatures([f])
    vl.updateExtents()


# ── Tests ────────────────────────────────────────────────────────────────────

def test_normal_layer_no_issues():
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,0 10,10 10,10 0,0 0))")
    _add(vl, "POLYGON((20 20,20 30,30 30,30 20,20 20))")
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    assert res.total_features == 2
    assert res.total_issues() == 0
    assert res.correctness.score >= 95.0
    print("OK normal_layer_no_issues (score=%s)" % res.correctness.score)


def test_invalid_self_intersection():
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,2 2,2 0,0 2,0 0))")  # nœud papillon = invalide
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    assert res.quality_report.invalid_count >= 1
    assert res.quality_report.self_intersection_count >= 1
    assert len(res.flagged[C.CAT_INVALID]) >= 1
    print("OK invalid_self_intersection")


def test_null_empty_geometry():
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, None)  # aucune géométrie
    _add(vl, "POLYGON((0 0,0 1,1 1,1 0,0 0))")
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    assert res.quality_report.null_empty_count == 1
    assert len(res.flagged[C.CAT_NULL_EMPTY]) == 1
    print("OK null_empty_geometry")


def test_duplicates():
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,0 5,5 5,5 0,0 0))")
    _add(vl, "POLYGON((0 0,0 5,5 5,5 0,0 0))")  # doublon exact
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    assert res.quality_report.duplicate_count == 1
    print("OK duplicates")


def test_small_polygon():
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,0 1,1 1,1 0,0 0))")  # ~1 m² <= seuil 2.0
    _add(vl, "POLYGON((0 0,0 100,100 100,100 0,0 0))")  # grand
    res = GeomAnalyzer(AnalysisOptions(
        check_small_polygons=True, small_polygon_threshold_m2=2.0)).analyze(vl)
    assert res.quality_report.small_area_count >= 1
    print("OK small_polygon (n=%d)" % res.quality_report.small_area_count)


def test_overlaps():
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,0 2,2 2,2 0,0 0))")
    _add(vl, "POLYGON((1 1,1 3,3 3,3 1,1 1))")  # recouvre le précédent
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    assert res.quality_report.overlap_pairs == 1
    assert res.quality_report.overlap_count == 2
    print("OK overlaps")


def test_agl_hidden_no_fields():
    """Sans champs AGL/AMSL : contrôle interne totalement silencieux."""
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,0 1,1 1,1 0,0 0))")
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    assert res.agl_note == ""
    # Aucune trace dans les avertissements visibles par l'utilisateur.
    assert not any("AGL" in w for w in res.warnings)
    print("OK agl_hidden_no_fields")


def test_agl_hidden_incomplete_fields():
    """Champs présents mais incomplets : aucune note (contrôle non concluant)."""
    _ensure_qgis()
    vl = _make_layer(fields=[("AGL", QVariant.Double), ("AMSL", QVariant.Double)])
    _add(vl, "POLYGON((0 0,0 1,1 1,1 0,0 0))", {"AGL": 30.0, "AMSL": 10.0})
    _add(vl, "POLYGON((2 2,2 3,3 3,3 2,2 2))")  # AGL/AMSL vides
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    assert res.agl_note == "", res.agl_note
    print("OK agl_hidden_incomplete_fields")


def test_agl_hidden_note_when_complete():
    """Champs complets + incohérence : note discrète en fin de rapport.

    Les polygones sont volontairement grands (100 m²) pour qu'aucune autre
    anomalie ne pénalise le score : on isole ainsi l'effet du contrôle caché.
    """
    _ensure_qgis()
    vl = _make_layer(fields=[("AGL", QVariant.Double), ("AMSL", QVariant.Double)])
    _add(vl, "POLYGON((0 0,0 10,10 10,10 0,0 0))", {"AGL": 30.0, "AMSL": 10.0})  # AGL>AMSL
    _add(vl, "POLYGON((20 20,20 30,30 30,30 20,20 20))", {"AGL": 5.0, "AMSL": 40.0})
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    assert res.buildings_agl_over_amsl == 1
    assert "AGL" in res.agl_note and "AMSL" in res.agl_note, res.agl_note
    # Le contrôle caché ne doit PAS peser sur le score ni sur les sous-scores.
    assert C.CAT_AGL not in res.correctness.components
    assert res.correctness.score == 100.0, res.correctness.score
    print("OK agl_hidden_note_when_complete (note=%r)" % res.agl_note)


def test_agl_hidden_no_note_when_consistent():
    """Champs complets et cohérents : aucune note (rien à signaler)."""
    _ensure_qgis()
    vl = _make_layer(fields=[("AGL", QVariant.Double), ("AMSL", QVariant.Double)])
    _add(vl, "POLYGON((0 0,0 1,1 1,1 0,0 0))", {"AGL": 5.0, "AMSL": 40.0})
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    assert res.agl_note == ""
    print("OK agl_hidden_no_note_when_consistent")


def test_check_layer_ready():
    _ensure_qgis()
    assert check_layer_ready(None) == C.MSG_NO_LAYER
    empty = _make_layer()
    assert check_layer_ready(empty) == C.MSG_LAYER_EMPTY
    nogeom = QgsVectorLayer("None?field=id:integer", "attr", "memory")
    assert check_layer_ready(nogeom) == C.MSG_LAYER_NO_GEOM
    ok = _make_layer()
    _add(ok, "POLYGON((0 0,0 1,1 1,1 0,0 0))")
    assert check_layer_ready(ok) is None
    print("OK check_layer_ready")


def test_cancellation():
    _ensure_qgis()
    vl = _make_layer()
    for i in range(10):
        _add(vl, "POLYGON((%d 0,%d 1,%d 1,%d 0,%d 0))" % (i, i, i + 1, i + 1, i))
    raised = False
    try:
        GeomAnalyzer(AnalysisOptions()).analyze(vl, is_canceled=lambda: True)
    except AnalysisCancelled:
        raised = True
    assert raised
    print("OK cancellation")


def test_repair_remove_duplicates_and_fix_invalid():
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,0 5,5 5,5 0,0 0))")
    _add(vl, "POLYGON((0 0,0 5,5 5,5 0,0 0))")   # doublon
    _add(vl, "POLYGON((0 0,2 2,2 0,0 2,0 0))")   # invalide (bowtie)
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    ropts = RepairOptions(fix_invalid=True, remove_duplicates=True)
    mem, stats = GeomAnalyzer.build_repaired_layer(vl, res, ropts)
    assert stats.removed_duplicates == 1
    assert stats.fixed_invalid >= 1
    assert stats.written == 2  # 3 entités - 1 doublon
    assert mem.featureCount() == 2
    print("OK repair (fixed=%d removed_dup=%d written=%d)"
          % (stats.fixed_invalid, stats.removed_duplicates, stats.written))


def test_duplicate_vertex_detection():
    _ensure_qgis()
    vl = _make_layer()
    # sommet initial répété -> 1 sommet en doublon
    _add(vl, "POLYGON((0 0,0 0,0 10,10 10,10 0,0 0))")
    _add(vl, "POLYGON((20 20,20 30,30 30,30 20,20 20))")  # propre
    # Désactivé par défaut : rien ne doit être signalé.
    res_off = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    assert res_off.quality_report.duplicate_vertex_count == 0
    assert res_off.flagged.get(C.CAT_DUPLICATE_VERTEX, []) == []
    # Activé : la première entité seulement est signalée.
    res_on = GeomAnalyzer(
        AnalysisOptions(check_duplicate_vertex=True)).analyze(vl)
    assert res_on.quality_report.duplicate_vertex_count == 1
    assert res_on.quality_report.duplicate_vertex_total == 1
    assert len(res_on.flagged[C.CAT_DUPLICATE_VERTEX]) == 1
    print("OK duplicate_vertex_detection (off=0, on=1)")


def test_duplicate_vertex_repair():
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,0 0,0 10,10 10,10 0,0 0))")
    res = GeomAnalyzer(AnalysisOptions(check_duplicate_vertex=True)).analyze(vl)
    mem, stats = GeomAnalyzer.build_repaired_layer(
        vl, res, RepairOptions(fix_invalid=False, fix_duplicate_vertex=True))
    assert stats.cleaned_duplicate_vertex == 1
    assert stats.removed_vertices == 1
    assert stats.written == 1
    # La géométrie nettoyée ne contient plus de sommet en doublon.
    from stat_geom_qc.analysis_engine import count_duplicate_vertices
    out = next(mem.getFeatures())
    assert count_duplicate_vertices(out.geometry()) == 0
    print("OK duplicate_vertex_repair (1 entité, 1 sommet retiré)")


def test_large_volume_and_progress():
    _ensure_qgis()
    vl = _make_layer()
    feats = []
    for i in range(2000):
        f = QgsFeature(vl.fields())
        f.setGeometry(QgsGeometry.fromWkt(
            "POLYGON((%d 0,%d 1,%d 1,%d 0,%d 0))" % (i * 2, i * 2, i * 2 + 1, i * 2 + 1, i * 2)))
        feats.append(f)
    vl.dataProvider().addFeatures(feats)
    vl.updateExtents()
    seen = {"pct": 0.0}

    def prog(pct, msg=""):
        seen["pct"] = pct

    res = GeomAnalyzer(AnalysisOptions()).analyze(vl, progress_cb=prog)
    assert res.total_features == 2000
    assert seen["pct"] >= 100.0
    print("OK large_volume_and_progress")


# ── Trous (avec seuil) ───────────────────────────────────────────────────────

# 100x100 m avec un petit trou (4 m²) et un grand trou (1600 m²).
_WKT_TWO_HOLES = ("POLYGON((0 0,0 100,100 100,100 0,0 0),"
                  "(10 10,10 12,12 12,12 10,10 10),"
                  "(50 50,50 90,90 90,90 50,50 50))")


def test_holes_disabled_by_default():
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, _WKT_TWO_HOLES)
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    assert res.quality_report.hole_count == 0
    assert not res.flagged.get(C.CAT_HOLE)
    print("OK holes_disabled_by_default")


def test_holes_threshold_ignores_large_holes():
    """Seuil 10 m² : le petit trou est signalé, le grand est jugé justifié."""
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, _WKT_TWO_HOLES)
    res = GeomAnalyzer(
        AnalysisOptions(check_holes=True, hole_max_area_m2=10.0)).analyze(vl)
    assert res.quality_report.hole_count == 1, res.quality_report.hole_count
    assert res.quality_report.hole_total == 1, res.quality_report.hole_total
    assert len(res.flagged[C.CAT_HOLE]) == 1
    # Seuil très large : les deux trous deviennent fautifs.
    res_all = GeomAnalyzer(
        AnalysisOptions(check_holes=True, hole_max_area_m2=100000.0)).analyze(vl)
    assert res_all.quality_report.hole_total == 2, res_all.quality_report.hole_total
    print("OK holes_threshold_ignores_large_holes")


def test_repair_removes_only_small_holes():
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, _WKT_TWO_HOLES)
    res = GeomAnalyzer(
        AnalysisOptions(check_holes=True, hole_max_area_m2=10.0)).analyze(vl)
    ropts = RepairOptions(fix_invalid=False, remove_holes=True, hole_max_area_m2=10.0)
    mem, stats = GeomAnalyzer.build_repaired_layer(vl, res, ropts)
    assert stats.removed_holes == 1, stats.removed_holes
    assert stats.features_with_holes_fixed == 1
    assert stats.written == 1
    # Le grand trou doit subsister dans la géométrie réparée.
    out = next(mem.getFeatures())
    small, total = count_small_holes(out.geometry(), 100000.0)
    assert total == 1, "le grand trou doit être conservé (total=%d)" % total
    print("OK repair_removes_only_small_holes")


# ── Multi-parties ────────────────────────────────────────────────────────────

_WKT_MULTI = ("MULTIPOLYGON(((0 0,0 10,10 10,10 0,0 0)),"
              "((20 20,20 30,30 30,30 20,20 20)))")


def test_multipart_detection_with_topology():
    _ensure_qgis()
    vl = _make_layer(geom_type="MultiPolygon")
    _add(vl, _WKT_MULTI)                                   # 2 parties
    _add(vl, "MULTIPOLYGON(((50 50,50 60,60 60,60 50,50 50)))")  # 1 partie
    res = GeomAnalyzer(AnalysisOptions(topology_checks=True)).analyze(vl)
    assert res.quality_report.multipart_count == 1, res.quality_report.multipart_count
    assert len(res.flagged[C.CAT_MULTIPART]) == 1
    # Contrôles topologiques désactivés -> aucune détection.
    res_off = GeomAnalyzer(AnalysisOptions(topology_checks=False)).analyze(vl)
    assert res_off.quality_report.multipart_count == 0
    print("OK multipart_detection_with_topology")


def test_repair_explodes_multipart():
    _ensure_qgis()
    vl = _make_layer(geom_type="MultiPolygon")
    _add(vl, _WKT_MULTI)  # 1 entité, 2 parties
    res = GeomAnalyzer(AnalysisOptions(topology_checks=True)).analyze(vl)
    ropts = RepairOptions(fix_invalid=False, explode_multipart=True)
    mem, stats = GeomAnalyzer.build_repaired_layer(vl, res, ropts)
    assert stats.exploded_features == 1
    assert stats.added_features == 1
    assert stats.written == 2, stats.written
    assert mem.featureCount() == 2
    for feat in mem.getFeatures():
        assert count_parts(feat.geometry()) == 1
    print("OK repair_explodes_multipart")


# ── Sommets dupliqués : STRICTEMENT identiques uniquement ───────────────────

def test_duplicate_vertex_has_no_tolerance_option():
    """La tolérance de quasi-doublon est supprimée : ni sur les options
    d'analyse, ni sur les options de réparation, ni comme réglage mémorisé.
    Seuls les sommets EXACTEMENT identiques sont détectés."""
    _ensure_qgis()
    opts = AnalysisOptions()
    assert not hasattr(opts, "duplicate_vertex_tolerance_m")
    assert not hasattr(RepairOptions(), "duplicate_vertex_tolerance_m")
    assert not hasattr(C, "SK_VERTEX_TOL")
    assert not hasattr(C, "VERTEX_TOLERANCE_DEFAULT_M")

    vl = _make_layer()
    # Deux sommets distants de 0,05 m : proches mais pas identiques.
    _add(vl, "POLYGON((0 0,0.05 0,10 0,10 10,0 10,0 0))")
    res = GeomAnalyzer(AnalysisOptions(check_duplicate_vertex=True)).analyze(vl)
    assert res.quality_report.duplicate_vertex_count == 0

    # Un vrai doublon (même point deux fois de suite) reste détecté.
    vl2 = _make_layer()
    _add(vl2, "POLYGON((0 0,0 0,10 0,10 10,0 10,0 0))")
    res2 = GeomAnalyzer(AnalysisOptions(check_duplicate_vertex=True)).analyze(vl2)
    assert res2.quality_report.duplicate_vertex_count == 1
    print("OK duplicate_vertex_has_no_tolerance_option")


def test_repair_duplicate_vertex_strict_only():
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,0 0,10 0,10 10,0 10,0 0))")
    res = GeomAnalyzer(AnalysisOptions(check_duplicate_vertex=True)).analyze(vl)
    ropts = RepairOptions(fix_invalid=False, fix_duplicate_vertex=True)
    mem, stats = GeomAnalyzer.build_repaired_layer(vl, res, ropts)
    assert stats.cleaned_duplicate_vertex == 1
    assert stats.removed_vertices == 1, stats.removed_vertices
    print("OK repair_duplicate_vertex_strict_only")


# ── Polygones entièrement inclus (imbrication) ───────────────────────────────

def _nested_layer():
    """Grand polygone + petit polygone ENTIÈREMENT inclus dedans."""
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,0 100,100 100,100 0,0 0))")
    _add(vl, "POLYGON((10 10,10 20,20 20,20 10,10 10))")
    return vl


def test_contained_flagged_as_overlap_by_default():
    _ensure_qgis()
    res = GeomAnalyzer(AnalysisOptions()).analyze(_nested_layer())
    q = res.quality_report
    assert q.contained_pairs == 1, q.contained_pairs
    assert q.overlap_pairs == 1, q.overlap_pairs
    assert q.overlap_count == 2, q.overlap_count
    print("OK contained_flagged_as_overlap_by_default")


def test_contained_accepted_when_allowed():
    _ensure_qgis()
    res = GeomAnalyzer(
        AnalysisOptions(allow_contained_polygons=True)).analyze(_nested_layer())
    q = res.quality_report
    # Toujours compté à titre informatif, mais plus signalé comme anomalie.
    assert q.contained_pairs == 1, q.contained_pairs
    assert q.overlap_pairs == 0, q.overlap_pairs
    assert q.overlap_count == 0, q.overlap_count
    assert not res.flagged.get(C.CAT_OVERLAP)
    print("OK contained_accepted_when_allowed")


def test_partial_overlap_still_flagged_when_contained_allowed():
    """Accepter l'imbrication ne doit PAS masquer les chevauchements partiels."""
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,0 10,10 10,10 0,0 0))")
    _add(vl, "POLYGON((5 5,5 15,15 15,15 5,5 5))")
    res = GeomAnalyzer(
        AnalysisOptions(allow_contained_polygons=True)).analyze(vl)
    q = res.quality_report
    assert q.overlap_pairs == 1, q.overlap_pairs
    assert q.contained_pairs == 0, q.contained_pairs
    print("OK partial_overlap_still_flagged_when_contained_allowed")


def test_classify_overlap_cases():
    _ensure_qgis()
    big = QgsGeometry.fromWkt("POLYGON((0 0,0 100,100 100,100 0,0 0))")
    cases = {
        "POLYGON((10 10,10 20,20 20,20 10,10 10))": "contained",
        "POLYGON((90 90,90 110,110 110,110 90,90 90))": "overlap",
        "POLYGON((100 0,100 100,200 100,200 0,100 0))": None,   # adjacent
        "POLYGON((300 0,300 10,310 10,310 0,300 0))": None,     # disjoint
        "POLYGON((0 0,0 100,100 100,100 0,0 0))": None,         # doublon exact
    }
    for wkt, expected in cases.items():
        got = classify_overlap(big, QgsGeometry.fromWkt(wkt))
        assert got == expected, "%s -> %r (attendu %r)" % (wkt[:24], got, expected)
    print("OK classify_overlap_cases")


def test_point_contact_is_not_an_overlap():
    """Deux polygones qui ne se touchent qu'en UN point sont correctement
    accrochés : aucune surface commune, donc aucune erreur."""
    _ensure_qgis()
    a = QgsGeometry.fromWkt("POLYGON((0 0,10 0,10 10,0 10,0 0))")
    b = QgsGeometry.fromWkt("POLYGON((10 10,20 10,20 20,10 20,10 10))")
    assert classify_overlap(a, b) is None
    print("OK point_contact_is_not_an_overlap")


def _tiny_overlap_layer():
    """Deux carrés se recouvrant sur une bande de 1 mm de large × 10 m."""
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,10 0,10 10,0 10,0 0))")
    _add(vl, "POLYGON((9.999 0,20 0,20 10,9.999 10,9.999 0))")
    return vl


def test_tiny_overlap_is_always_flagged():
    """Règle GeoVectorQualityControl : AUCUNE tolérance. Un recouvrement de
    1 mm est une erreur, et il n'existe plus aucun réglage pour l'écarter."""
    _ensure_qgis()
    opts = AnalysisOptions()
    assert not hasattr(opts, "overlap_min_width_m"), \
        "la tolérance d'accrochage ne doit plus exister"
    res = GeomAnalyzer(opts).analyze(_tiny_overlap_layer())
    q = res.quality_report
    assert q.overlap_pairs == 1, q.overlap_pairs
    assert q.overlap_count == 2, q.overlap_count
    assert res.flagged.get(C.CAT_OVERLAP)
    print("OK tiny_overlap_is_always_flagged")


def test_overlap_rule_is_intersects_and_not_touches():
    """La décision repose sur les deux prédicats DE-9IM de GeoVector, et sur eux
    seuls : ``intersects`` vrai ET ``touches`` faux.

    Chaque cas est vérifié à la fois sur les prédicats bruts et sur le verdict
    du moteur, pour qu'aucun des deux ne puisse dériver de l'autre.
    """
    _ensure_qgis()
    a = QgsGeometry.fromWkt("POLYGON((0 0,10 0,10 10,0 10,0 0))")
    cas = (
        # (voisin, touches attendu, verdict attendu)
        ("POLYGON((10 0,20 0,20 10,10 10,10 0))", True, None),    # mur mitoyen
        ("POLYGON((10 10,20 10,20 20,10 20,10 10))", True, None),  # coin à coin
        ("POLYGON((9.999 0,20 0,20 10,9.999 10,9.999 0))", False, "overlap"),
        ("POLYGON((5 5,6 5,6 6,5 6,5 5))", False, "contained"),
        ("POLYGON((30 0,40 0,40 10,30 10,30 0))", False, None),   # disjoint
    )
    for wkt, touches_attendu, verdict in cas:
        b = QgsGeometry.fromWkt(wkt)
        assert a.touches(b) == touches_attendu, wkt[:28]
        interieurs_se_recoupent = a.intersects(b) and not a.touches(b)
        assert interieurs_se_recoupent == (verdict is not None), wkt[:28]
        assert classify_overlap(a, b) == verdict, wkt[:28]
    print("OK overlap_rule_is_intersects_and_not_touches")


def test_layer_units_per_metre_by_crs():
    """Facteur de conversion mètres -> unités de couche, par type de CRS."""
    _ensure_qgis()
    cases = (
        ("EPSG:2154", 650000, 6860000, 1.0, 0.01),      # Lambert-93, mètres
        ("EPSG:2263", 1000000, 200000, 1 / 0.3048, 0.01),  # pieds US
        ("EPSG:4326", 2.0, 48.0, 1 / 111000.0, 0.02),   # degrés
    )
    for authid, x, y, expected, rel_tol in cases:
        vl = _make_layer(crs=authid)
        _add(vl, "POLYGON((%f %f,%f %f,%f %f,%f %f,%f %f))"
             % (x, y, x + 1, y, x + 1, y + 1, x, y + 1, x, y))
        got = layer_units_per_metre(vl)
        assert abs(got - expected) <= expected * rel_tol, (authid, got, expected)
    print("OK layer_units_per_metre_by_crs")


def test_layer_units_per_metre_falls_back_to_one():
    """Couche vide : repli sûr sur 1 (couche supposée métrique)."""
    _ensure_qgis()
    assert layer_units_per_metre(_make_layer()) == 1.0
    print("OK layer_units_per_metre_falls_back_to_one")


def test_overlap_detection_needs_no_unit_conversion():
    """La règle étant purement topologique, elle ne dépend d'AUCUNE unité.

    Le même recouvrement minuscule doit être signalé en degrés comme en pieds,
    sans que la moindre conversion mètres/unités n'intervienne — c'est ce qui
    rendait le contrôle fragile lorsqu'un seuil métrique était en jeu.
    """
    _ensure_qgis()
    # ~5,5 cm de large en degrés : très en dessous de tout ancien seuil.
    deg = _make_layer(crs="EPSG:4326")
    _add(deg, "POLYGON((2.0000000 48.0,2.0010000 48.0,2.0010000 48.001,"
              "2.0000000 48.001,2.0000000 48.0))")
    _add(deg, "POLYGON((2.0009995 48.0,2.0020000 48.0,2.0020000 48.001,"
              "2.0009995 48.001,2.0009995 48.0))")
    res_deg = GeomAnalyzer(AnalysisOptions()).analyze(deg)
    assert res_deg.crs_is_geographic
    assert res_deg.quality_report.overlap_pairs == 1, \
        res_deg.quality_report.overlap_pairs

    # ~6 cm de large sur une couche en pieds US.
    x, y = 1000000, 200000
    ft = _make_layer(crs="EPSG:2263")
    _add(ft, "POLYGON((%f %f,%f %f,%f %f,%f %f,%f %f))"
         % (x, y, x + 100, y, x + 100, y + 100, x, y + 100, x, y))
    start = x + 100 - 0.2
    _add(ft, "POLYGON((%f %f,%f %f,%f %f,%f %f,%f %f))"
         % (start, y, x + 200, y, x + 200, y + 100, start, y + 100, start, y))
    res_ft = GeomAnalyzer(AnalysisOptions()).analyze(ft)
    assert res_ft.quality_report.overlap_pairs == 1, \
        res_ft.quality_report.overlap_pairs
    print("OK overlap_detection_needs_no_unit_conversion")


def _wgs84_ring_with_gap(gap_deg):
    """Polygone en degrés dont deux sommets consécutifs sont séparés de
    ``gap_deg`` en LATITUDE (≈ 111 000 m par degré)."""
    return ("POLYGON((2.0 48.0,2.001 48.0,2.001 48.001,"
            "2.0005 %.9f,2.0005 48.001,2.0 48.001,2.0 48.0))"
            % (48.001 - gap_deg))


def test_close_vertices_threshold_in_metres_on_geographic_crs():
    """Non-régression : « sommets rapprochés » est un seuil en MÈTRES.

    Sur une couche en degrés, 0,20 « unité » vaudrait 0,2° ≈ 22 km : toutes les
    paires de sommets d'un bâtiment auraient été signalées. Ici deux sommets
    distants de ~11 m ne doivent PAS l'être, alors que ~5 cm doit l'être.
    """
    _ensure_qgis()
    opts = AnalysisOptions(check_close_vertices=True,
                           close_vertex_min_dist_m=0.20)

    loin = _make_layer(crs="EPSG:4326")
    _add(loin, _wgs84_ring_with_gap(1e-4))        # ~11 m entre deux sommets
    res_loin = GeomAnalyzer(opts).analyze(loin)
    assert res_loin.quality_report.close_vertex_count == 0, \
        res_loin.quality_report.close_vertex_total

    proche = _make_layer(crs="EPSG:4326")
    _add(proche, _wgs84_ring_with_gap(5e-7))      # ~5,5 cm entre deux sommets
    res_proche = GeomAnalyzer(opts).analyze(proche)
    assert res_proche.quality_report.close_vertex_count == 1, \
        res_proche.quality_report.close_vertex_total
    print("OK close_vertices_threshold_in_metres_on_geographic_crs")


def test_repair_uses_same_converted_thresholds():
    """La réparation doit fusionner exactement ce que l'analyse a signalé.

    Si la conversion manquait d'un côté, une entité signalée ne serait pas
    corrigée (ou une entité saine serait modifiée).
    """
    _ensure_qgis()
    opts = AnalysisOptions(check_close_vertices=True,
                           close_vertex_min_dist_m=0.20)
    vl = _make_layer(crs="EPSG:4326")
    _add(vl, _wgs84_ring_with_gap(5e-7))
    res = GeomAnalyzer(opts).analyze(vl)
    assert res.quality_report.close_vertex_count == 1
    mem, stats = GeomAnalyzer.build_repaired_layer(
        vl, res, RepairOptions(fix_invalid=False, fix_close_vertices=True,
                               close_vertex_min_dist_m=0.20))
    assert stats.cleaned_close_vertices == 1, stats.cleaned_close_vertices
    assert stats.removed_close_vertices >= 1, stats.removed_close_vertices
    print("OK repair_uses_same_converted_thresholds")


def test_results_are_crs_invariant():
    """Garde-fou central : la MÊME donnée, en Lambert-93 puis reprojetée en
    WGS84, doit produire les MÊMES comptages avec les mêmes seuils en mètres.

    C'est l'invariant que violait le bug d'unités : tous les seuils exprimés en
    mètres (sommets rapprochés, quasi-doublons, surfaces) doivent être
    indépendants du système de coordonnées de la couche. Les chevauchements y
    participent aussi, désormais par construction : leur règle est topologique.
    """
    _ensure_qgis()
    from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform

    x, y = 650000.0, 6860000.0
    wkts = [
        # Bâtiment : un sommet à 5 cm du précédent (sommet rapproché) puis un
        # sommet EXACTEMENT dupliqué (répété tel quel). Abscisses
        # décroissantes -> valide.
        "POLYGON((%f %f,%f %f,%f %f,%f %f,%f %f,%f %f,%f %f,%f %f))" % (
            x, y, x + 10, y, x + 10, y + 10, x + 9.95, y + 10,
            x + 9.80, y + 10, x + 9.80, y + 10, x, y + 10, x, y),
        "POLYGON((%f %f,%f %f,%f %f,%f %f,%f %f))" % (
            x + 100, y, x + 150, y, x + 150, y + 50, x + 100, y + 50, x + 100, y),
        # Chevauche la précédente sur 40 cm de large.
        "POLYGON((%f %f,%f %f,%f %f,%f %f,%f %f))" % (
            x + 149.6, y, x + 200, y, x + 200, y + 50, x + 149.6, y + 50,
            x + 149.6, y),
        # Accrochée correctement à la précédente (bord commun) : pas une erreur.
        "POLYGON((%f %f,%f %f,%f %f,%f %f,%f %f))" % (
            x + 200, y, x + 250, y, x + 250, y + 50, x + 200, y + 50, x + 200, y),
    ]
    opts = dict(check_overlaps=True,
                check_close_vertices=True, close_vertex_min_dist_m=0.20,
                check_duplicate_vertex=True,
                check_holes=True, hole_max_area_m2=2.0,
                small_polygon_threshold_m2=2.0)

    transform = QgsCoordinateTransform(
        QgsCoordinateReferenceSystem("EPSG:2154"),
        QgsCoordinateReferenceSystem("EPSG:4326"),
        QgsProject.instance())

    def analysed(authid, reproject):
        vl = _make_layer(crs=authid)
        for wkt in wkts:
            geom = QgsGeometry.fromWkt(wkt)
            if reproject:
                geom.transform(transform)
            feat = QgsFeature(vl.fields())
            feat.setGeometry(geom)
            vl.dataProvider().addFeatures([feat])
        vl.updateExtents()
        return GeomAnalyzer(AnalysisOptions(**opts)).analyze(vl)

    metric = analysed("EPSG:2154", False)
    geographic = analysed("EPSG:4326", True)

    # Le jeu doit réellement exercer les contrôles, sinon l'invariant est creux.
    assert metric.quality_report.overlap_pairs == 1
    assert metric.quality_report.close_vertex_count == 1
    assert metric.quality_report.duplicate_vertex_count == 1

    for champ in ("overlap_count", "overlap_pairs",
                  "close_vertex_count", "close_vertex_total",
                  "duplicate_vertex_count", "duplicate_vertex_total",
                  "small_area_count", "hole_count", "invalid_count"):
        assert getattr(metric.quality_report, champ) == \
            getattr(geographic.quality_report, champ), champ
    assert metric.correctness.score == geographic.correctness.score
    print("OK results_are_crs_invariant")


# ── Option « ignorer les chevauchements dus à un sommet manquant » ───────────
# Bâti de référence : carré de 10 m dont le mur droit (x = 10) est un segment
# SANS sommet intermédiaire — l'origine même du problème étudié.
_WKT_BATI = "POLYGON((0 0,10 0,10 10,0 10,0 0))"


def _overlap_result(voisin_wkt, ignore_missing_vertex):
    """Analyse le bâti de référence face à un voisin."""
    vl = _make_layer()
    _add(vl, _WKT_BATI)
    _add(vl, voisin_wkt)
    return GeomAnalyzer(AnalysisOptions(
        ignore_missing_vertex_overlaps=ignore_missing_vertex)).analyze(vl)


# Pointe de 30 cm sur 2 m de mur : rapport mesuré 0,15 — sous le seuil fixe de
# 0,25, donc écartée quand l'option est cochée.
_WKT_POINTE_015 = "POLYGON((10 0,20 0,20 10,10 10,10 6,9.7 5,10 4,10 0))"


def test_missing_vertex_ratio_is_not_user_settable():
    """Le seuil de forme est une CONSTANTE : l'option se résume à une case.

    Ni ``AnalysisOptions`` ni les réglages persistants ne l'exposent plus, et
    aucune borne min/max n'a de raison de subsister.
    """
    _ensure_qgis()
    opts = AnalysisOptions()
    assert not hasattr(opts, "missing_vertex_max_ratio")
    assert not hasattr(C, "SK_MISSING_VERTEX_RATIO")
    assert not hasattr(C, "MISSING_VERTEX_RATIO_MIN")
    assert not hasattr(C, "MISSING_VERTEX_RATIO_MAX")
    assert C.MISSING_VERTEX_RATIO_DEFAULT == 0.25
    # Le seuil fixe s'applique bel et bien : la pointe est écartée.
    q = _overlap_result(_WKT_POINTE_015, True).quality_report
    assert q.overlap_pairs == 0, q.overlap_pairs
    assert q.overlap_missing_vertex_pairs == 1
    print("OK missing_vertex_ratio_is_not_user_settable")


def test_missing_vertex_ratio_recorded_in_result():
    """Le seuil RÉELLEMENT appliqué est mémorisé (0 si l'option est décochée) :
    le rapport et la réparation doivent pouvoir reprendre la même règle."""
    _ensure_qgis()
    actif = _overlap_result(_WKT_POINTE_015, True)
    assert actif.missing_vertex_ratio == C.MISSING_VERTEX_RATIO_DEFAULT
    assert actif.to_dict()["missing_vertex_ratio"] == 0.25

    inactif = _overlap_result(_WKT_POINTE_015, False)
    assert inactif.missing_vertex_ratio == 0.0, inactif.missing_vertex_ratio
    print("OK missing_vertex_ratio_recorded_in_result")


def test_repair_does_not_clip_missing_vertex_overlap():
    """La réparation doit appliquer la même règle que l'analyse : un
    recouvrement écarté comme « sommet manquant » ne doit pas être rogné.

    Montage : le carré central subit DEUX recouvrements, tous deux avec des
    voisins plus grands (donc dominants, donc c'est lui qui serait rogné) :
      * à droite, une bande de 40 cm — vrai chevauchement, à retirer (4 % de sa
        surface, sous le plafond de rognage « mineur ») ;
      * à gauche, une pointe de 30 cm imputable à un sommet manquant, à CONSERVER.
    L'aire finale départage sans ambiguïté : 96,0 si la règle est respectée,
    95,7 si la pointe avait été rognée elle aussi.
    """
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, _WKT_BATI)                                     # 1 : carré 0..10
    _add(vl, "POLYGON((9.6 0,30 0,30 10,9.6 10,9.6 0))")    # 2 : bande de 40 cm
    # 3 : voisin de gauche dont un sommet entre de 30 cm sur 2 m de mur.
    _add(vl, "POLYGON((0 0,-10 0,-10 10,0 10,0 6,0.3 5,0 4,0 0))")
    res = GeomAnalyzer(AnalysisOptions(
        ignore_missing_vertex_overlaps=True)).analyze(vl)
    q = res.quality_report
    assert q.overlap_missing_vertex_pairs == 1, q.overlap_missing_vertex_pairs
    assert q.overlap_pairs == 1, q.overlap_pairs

    mem, stats = GeomAnalyzer.build_repaired_layer(
        vl, res, RepairOptions(
            fix_invalid=False, fix_overlaps=True,
            missing_vertex_ratio=res.missing_vertex_ratio))
    aires = sorted(round(f.geometry().area(), 3) for f in mem.getFeatures())
    assert 96.0 in aires, aires
    assert 95.7 not in aires, aires
    assert stats.fixed_overlaps == 1, stats.fixed_overlaps
    print("OK repair_does_not_clip_missing_vertex_overlap")


def test_missing_vertex_option_off_by_default():
    """Par défaut, TOUS les chevauchements sont signalés."""
    opts = AnalysisOptions()
    assert opts.ignore_missing_vertex_overlaps is False
    # Le cas cible est bien signalé quand l'option est décochée.
    res = _overlap_result(
        "POLYGON((10 0,20 0,20 10,10 10,10 6,9.7 5,10 4,10 0))", False)
    assert res.quality_report.overlap_pairs == 1
    assert res.quality_report.overlap_missing_vertex_pairs == 0
    print("OK missing_vertex_option_off_by_default")


def test_missing_vertex_overlap_is_ignored_when_enabled():
    """Cas cible : un sommet du voisin empiète sur un mur sans sommet."""
    _ensure_qgis()
    for wkt, label in (
            ("POLYGON((10 0,20 0,20 10,10 10,10 6.5,9.95 5,10 3.5,10 0))",
             "5 cm sur 3 m"),
            ("POLYGON((10 0,20 0,20 10,10 10,10 6,9.7 5,10 4,10 0))",
             "30 cm sur 2 m")):
        res = _overlap_result(wkt, True)
        q = res.quality_report
        assert q.overlap_pairs == 0, (label, q.overlap_pairs)
        assert q.overlap_count == 0, label
        assert q.overlap_missing_vertex_pairs == 1, label
        assert not res.flagged.get(C.CAT_OVERLAP), label
    print("OK missing_vertex_overlap_is_ignored_when_enabled")


def test_deep_or_compact_overlap_never_ignored():
    """Garde-fou de forme : une zone commune qui MORD dans le bâti reste
    signalée, même si l'empiètement ne va que dans un sens."""
    _ensure_qgis()
    cases = (
        # Pointe profonde : 2 m sur 2 m de mur (rapport 0,80).
        "POLYGON((10 0,20 0,20 10,10 10,10 6,8 5,10 4,10 0))",
        # Morsure compacte de 1 m × 1 m (rapport 1,0).
        "POLYGON((9 5,20 5,20 6,9 6,9 5))",
    )
    for wkt in cases:
        q = _overlap_result(wkt, True).quality_report
        assert q.overlap_pairs == 1, wkt[:30]
        assert q.overlap_missing_vertex_pairs == 0, wkt[:30]
    print("OK deep_or_compact_overlap_never_ignored")


def test_real_conflicts_never_ignored_even_if_elongated():
    """Garde-fou d'empiètement : sans intrusion de sommet à sens unique, le
    conflit est géométrique et reste signalé — y compris quand la zone commune
    est allongée (rapport 0,20, donc sous le seuil de forme)."""
    _ensure_qgis()
    cases = (
        # Bâtiment décalé de 2 m : aucun sommet n'entre (ils sont SUR le bord).
        "POLYGON((8 0,20 0,20 10,8 10,8 0))",
        # Mur entier décalé de 30 cm : mur mal placé, pas sommet absent.
        "POLYGON((9.7 0,20 0,20 10,9.7 10,9.7 0))",
        # Coin sur coin : les deux s'interpénètrent.
        "POLYGON((8 8,20 8,20 20,8 20,8 8))",
        # Croisement en X : aucun sommet à l'intérieur de l'autre.
        "POLYGON((-5 4,15 4,15 6,-5 6,-5 4))",
    )
    for wkt in cases:
        q = _overlap_result(wkt, True).quality_report
        assert q.overlap_pairs == 1, wkt[:30]
        assert q.overlap_missing_vertex_pairs == 0, wkt[:30]
    print("OK real_conflicts_never_ignored_even_if_elongated")


def test_contained_polygon_not_treated_as_missing_vertex():
    """Une imbrication a TOUS ses sommets chez l'autre : elle satisferait le
    critère d'empiètement à sens unique, mais relève de son option dédiée."""
    _ensure_qgis()
    q = _overlap_result("POLYGON((3 3,6 3,6 6,3 6,3 3))", True).quality_report
    assert q.overlap_missing_vertex_pairs == 0
    assert q.contained_pairs == 1
    assert q.overlap_pairs == 1
    print("OK contained_polygon_not_treated_as_missing_vertex")


def test_perfect_adjacency_unaffected_by_the_option():
    """Une mitoyenneté exacte n'est une erreur dans aucun des deux réglages."""
    _ensure_qgis()
    voisin = "POLYGON((10 0,20 0,20 10,10 10,10 0))"
    for ignore in (False, True):
        q = _overlap_result(voisin, ignore).quality_report
        assert q.overlap_pairs == 0
        assert q.overlap_missing_vertex_pairs == 0
    print("OK perfect_adjacency_unaffected_by_the_option")


def test_repair_clips_every_overlap_without_tolerance():
    """La réparation applique la même règle que l'analyse : plus aucune
    exemption métrique.

    L'entité 1 recouvre l'entité 2 sur 30 cm (3 m²) ET l'entité 3 sur 2 cm
    (0,2 m²). Les deux voisins étant plus grands, ils dominent : les deux
    recouvrements sont retirés, d'où 96,8 m². Une version antérieure aurait
    épargné les 2 cm au titre de la tolérance et laissé 97,0 m².
    """
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,10 0,10 10,0 10,0 0))")            # 1 : rogné
    _add(vl, "POLYGON((9.7 0,30 0,30 10,9.7 10,9.7 0))")      # 2 : dominant
    _add(vl, "POLYGON((-20 0,0.02 0,0.02 10,-20 10,-20 0))")  # 3 : dominant
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    assert res.quality_report.overlap_pairs == 2, res.quality_report.overlap_pairs
    mem, stats = GeomAnalyzer.build_repaired_layer(
        vl, res, RepairOptions(fix_invalid=False, fix_overlaps=True))
    assert stats.fixed_overlaps == 1, stats.fixed_overlaps
    aires = sorted(round(f.geometry().area(), 3) for f in mem.getFeatures())
    assert 96.8 in aires, aires
    assert 97.0 not in aires, aires
    print("OK repair_clips_every_overlap_without_tolerance")


def test_mixed_intersection_is_flagged_as_overlap():
    """Non-régression : intersection MIXTE (frontière commune + pénétration).

    GEOS renvoie ici une GeometryCollection « polygone + lignes ». L'ancien
    contrôle exigeait une intersection de type Polygon et laissait donc passer
    ce chevauchement pourtant réel (2 m²), alors que les deux polygones ne sont
    justement PAS correctement accrochés.
    """
    _ensure_qgis()
    a = "POLYGON((0 0,10 0,10 10,0 10,0 0))"
    # Bord commun en x=10, sauf une encoche qui mord dans A entre y=4 et y=6.
    b = "POLYGON((10 0,20 0,20 10,10 10,10 6,9 6,9 4,10 4,10 0))"
    inter = QgsGeometry.fromWkt(a).intersection(QgsGeometry.fromWkt(b))
    assert inter.area() > 0, "le cas de test doit bien produire une surface"
    assert classify_overlap(QgsGeometry.fromWkt(a),
                            QgsGeometry.fromWkt(b)) == "overlap"
    vl = _make_layer()
    _add(vl, a)
    _add(vl, b)
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    assert res.quality_report.overlap_pairs == 1, res.quality_report.overlap_pairs
    print("OK mixed_intersection_is_flagged_as_overlap")


def test_overlap_points_locate_the_intersection():
    """Le point mémorisé tombe DANS la surface d'intersection, pas au centre
    du polygone — c'est ce que la couche d'erreurs va pointer."""
    _ensure_qgis()
    a = "POLYGON((0 0,100 0,100 100,0 100,0 0))"
    b = "POLYGON((99 0,200 0,200 100,99 100,99 0))"   # bande de 1 x 100 m
    vl = _make_layer()
    _add(vl, a)
    _add(vl, b)
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    inter = QgsGeometry.fromWkt(a).intersection(QgsGeometry.fromWkt(b))
    assert len(res.overlap_points) == 2, res.overlap_points
    for fid, points in res.overlap_points.items():
        assert points, fid
        for x, y, other_fid in points:
            assert other_fid != fid
            pt = QgsGeometry.fromWkt("POINT(%.6f %.6f)" % (x, y))
            assert inter.intersects(pt), (fid, x, y)
    # Le centre du grand polygone, lui, est HORS de l'intersection : c'est bien
    # ce que l'ancien comportement (point représentatif) donnait.
    centre = QgsGeometry.fromWkt(a).pointOnSurface()
    assert not inter.intersects(centre)
    print("OK overlap_points_locate_the_intersection")


def test_overlap_points_respect_the_per_feature_cap():
    """Le nombre de points mémorisés par entité est borné (mémoire)."""
    _ensure_qgis()
    vl = _make_layer()
    # Un grand polygone chevauché par bien plus de voisins que le plafond.
    _add(vl, "POLYGON((0 0,1000 0,1000 10,0 10,0 0))")
    for i in range(C.OVERLAP_POINTS_PER_FEATURE + 8):
        x = i * 10
        _add(vl, "POLYGON((%d 5,%d 5,%d 20,%d 20,%d 5))"
             % (x, x + 8, x + 8, x, x))
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    big = min(res.overlap_points)
    assert len(res.overlap_points[big]) == C.OVERLAP_POINTS_PER_FEATURE, \
        len(res.overlap_points[big])
    print("OK overlap_points_respect_the_per_feature_cap")


# ── Section « Autres » : angles aigus / sommets rapprochés ──────────────────

_WKT_LAME = "POLYGON((0 0,0 1,100 1,100 0,0 0))"               # 1 x 100 m
_WKT_SPIKE = "POLYGON((200 0,210 0,205 0.3,210 10,200 10,200 0))"
_WKT_CLOSE = "POLYGON((300 0,300.2 0,310 0,310 10,300 10,300 0))"
_WKT_CLEAN = "POLYGON((400 0,400 10,410 10,410 0,400 0))"


def _other_checks_layer():
    vl = _make_layer()
    for wkt in (_WKT_LAME, _WKT_SPIKE, _WKT_CLOSE, _WKT_CLEAN):
        _add(vl, wkt)
    return vl


def _other_opts(**kwargs):
    """Options isolant les contrôles « Autres » (seuil petits polygones à 0)."""
    base = dict(small_polygon_threshold_m2=0.0)
    base.update(kwargs)
    return AnalysisOptions(**base)


def test_other_checks_disabled_by_default():
    _ensure_qgis()
    res = GeomAnalyzer(AnalysisOptions()).analyze(_other_checks_layer())
    q = res.quality_report
    assert q.sharp_angle_count == 0 and q.close_vertex_count == 0
    for cat in (C.CAT_SHARP_ANGLE, C.CAT_CLOSE_VERTEX):
        assert not res.flagged.get(cat)
    print("OK other_checks_disabled_by_default")


def test_sharp_angle_detection_and_repair():
    _ensure_qgis()
    vl = _other_checks_layer()
    opts = _other_opts(check_sharp_angles=True, sharp_angle_min_deg=30.0)
    res = GeomAnalyzer(opts).analyze(vl)
    assert res.quality_report.sharp_angle_count == 1
    assert res.quality_report.sharp_angle_total == 1
    ropts = RepairOptions(fix_invalid=False, fix_sharp_angles=True,
                          sharp_angle_min_deg=30.0)
    mem, stats = GeomAnalyzer.build_repaired_layer(vl, res, ropts)
    assert stats.fixed_sharp_angles == 1
    assert stats.removed_spike_vertices == 1
    # La correction ne doit laisser ni pointe ni géométrie invalide.
    after = GeomAnalyzer(opts).analyze(mem)
    assert after.quality_report.sharp_angle_count == 0
    assert after.quality_report.invalid_count == 0
    print("OK sharp_angle_detection_and_repair")


def test_sharp_angle_repair_skips_degenerate():
    """Un triangle trop fin ne doit PAS être dégradé : entité laissée intacte."""
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,100 0,50 0.1,0 0))")
    opts = _other_opts(check_sharp_angles=True, sharp_angle_min_deg=30.0)
    res = GeomAnalyzer(opts).analyze(vl)
    assert res.quality_report.sharp_angle_count == 1
    ropts = RepairOptions(fix_invalid=False, fix_sharp_angles=True,
                          sharp_angle_min_deg=30.0)
    mem, stats = GeomAnalyzer.build_repaired_layer(vl, res, ropts)
    assert stats.fixed_sharp_angles == 0
    assert stats.skipped_sharp_angles == 1
    assert stats.written == 1
    print("OK sharp_angle_repair_skips_degenerate")


def test_close_vertices_detection_and_repair():
    _ensure_qgis()
    vl = _other_checks_layer()
    opts = _other_opts(check_close_vertices=True, close_vertex_min_dist_m=0.5)
    res = GeomAnalyzer(opts).analyze(vl)
    assert res.quality_report.close_vertex_count == 1
    assert res.quality_report.close_vertex_total == 1
    ropts = RepairOptions(fix_invalid=False, fix_close_vertices=True,
                          close_vertex_min_dist_m=0.5)
    mem, stats = GeomAnalyzer.build_repaired_layer(vl, res, ropts)
    assert stats.cleaned_close_vertices == 1
    assert stats.removed_close_vertices == 1
    after = GeomAnalyzer(opts).analyze(mem)
    assert after.quality_report.close_vertex_count == 0
    print("OK close_vertices_detection_and_repair")


def test_close_vertices_excludes_exact_duplicates():
    """Sommets STRICTEMENT superposés -> contrôle « dupliqués », pas « rapprochés »."""
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,0 0,0 10,10 10,10 0,0 0))")
    res = GeomAnalyzer(_other_opts(
        check_close_vertices=True, close_vertex_min_dist_m=0.5,
        check_duplicate_vertex=True)).analyze(vl)
    assert res.quality_report.close_vertex_count == 0, "doublon exact mal classé"
    assert res.quality_report.duplicate_vertex_count == 1
    print("OK close_vertices_excludes_exact_duplicates")


def test_other_checks_score_components():
    """Les 3 contrôles ne pèsent sur le score que s'ils sont activés."""
    _ensure_qgis()
    vl = _other_checks_layer()
    off = GeomAnalyzer(_other_opts()).analyze(vl)
    for cat in (C.CAT_SHARP_ANGLE, C.CAT_CLOSE_VERTEX):
        assert cat not in off.correctness.components
    on = GeomAnalyzer(_other_opts(
        check_sharp_angles=True,
        check_close_vertices=True, close_vertex_min_dist_m=0.5)).analyze(vl)
    for cat in (C.CAT_SHARP_ANGLE, C.CAT_CLOSE_VERTEX):
        assert cat in on.correctness.components
    assert on.correctness.score < off.correctness.score
    print("OK other_checks_score_components (%.1f -> %.1f)"
          % (off.correctness.score, on.correctness.score))


def test_other_checks_ignore_non_polygons():
    """Les contrôles « Autres » ne s'appliquent qu'aux polygones."""
    _ensure_qgis()
    vl = _make_layer(geom_type="LineString")
    _add(vl, "LINESTRING(0 0,0.1 0,10 0)")
    res = GeomAnalyzer(_other_opts(
        check_sharp_angles=True,
        check_close_vertices=True, close_vertex_min_dist_m=0.5)).analyze(vl)
    q = res.quality_report
    assert q.sharp_angle_count == 0 and q.close_vertex_count == 0
    print("OK other_checks_ignore_non_polygons")


# ── Non-régression : bugs détectés par la revue adversariale ─────────────────

def test_makevalid_collection_does_not_lose_features():
    """« make valid » peut produire une GeometryCollection (Polygon+LineString).

    Écrite telle quelle, elle était REFUSÉE par le fournisseur, qui abandonnait
    TOUT le lot : la couche réparée sortait vide alors que stats.written
    annonçait le contraire. Les parties du bon type doivent être conservées.
    """
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,0 10,10 10,10 0,0 0,20 0,0 0))")   # arête pendante
    _add(vl, "POLYGON((100 0,100 10,110 10,110 0,100 0))")    # saine
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    mem, stats = GeomAnalyzer.build_repaired_layer(vl, res, RepairOptions())
    assert mem.featureCount() == 2, mem.featureCount()
    assert stats.written == mem.featureCount(), stats.written
    assert stats.rejected == 0
    # L'entité saine doit être intacte et toutes les sorties polygonales.
    for feat in mem.getFeatures():
        geom = feat.geometry()
        assert not geom.isNull() and not geom.isEmpty()
        assert (QgsWkbTypes.geometryType(geom.wkbType())
                == QgsWkbTypes.GeometryType.PolygonGeometry)
    print("OK makevalid_collection_does_not_lose_features")


def test_close_vertex_repair_refuses_collapse():
    """Fusionner les sommets d'un polygone très fin l'effondrait (aire nulle)."""
    _ensure_qgis()
    wkt = "POLYGON((0 0,0.15 0,0.15 50,0 50,0 0))"
    vl = _make_layer()
    _add(vl, wkt)
    opts = _other_opts(check_close_vertices=True, close_vertex_min_dist_m=0.20)
    res = GeomAnalyzer(opts).analyze(vl)
    assert res.quality_report.close_vertex_count == 1
    mem, stats = GeomAnalyzer.build_repaired_layer(
        vl, res, RepairOptions(fix_invalid=False, fix_close_vertices=True,
                               close_vertex_min_dist_m=0.20))
    assert stats.cleaned_close_vertices == 0
    assert stats.skipped_close_vertices == 1
    out = next(mem.getFeatures()).geometry()
    assert out.area() > 0, "la géométrie a été effondrée"
    assert out.isGeosValid()
    print("OK close_vertex_repair_refuses_collapse")


def test_multipart_of_compact_parts_is_not_elongated():
    """Deux parties compactes mais éloignées ne forment pas une lame.

    Le rectangle englobant de l'entité entière est très allongé : sans mesure
    par partie, l'entité était signalée puis SUPPRIMÉE à la réparation.
    """
    _ensure_qgis()
    wkt = ("MULTIPOLYGON(((0 0,0 10,10 10,10 0,0 0)),"
           "((1000 0,1000 10,1010 10,1010 0,1000 0)))")
    assert sliver_ratio(QgsGeometry.fromWkt(wkt)) > 0.5
    vl = _make_layer(geom_type="MultiPolygon")
    _add(vl, wkt)
    print("OK multipart_of_compact_parts_is_not_elongated")


def test_sharp_angle_repair_preserves_real_hole():
    """Le retrait des pointes ne doit jamais faire disparaître un trou réel."""
    _ensure_qgis()
    wkt = ("POLYGON((0 0,100 0,50 0.3,100 100,0 100,0 0),"
           "(40 40,40 60,60 60,60 40,40 40))")
    vl = _make_layer()
    _add(vl, wkt)
    res = GeomAnalyzer(_other_opts(check_sharp_angles=True)).analyze(vl)
    mem, _stats = GeomAnalyzer.build_repaired_layer(
        vl, res, RepairOptions(fix_invalid=False, fix_sharp_angles=True))
    out = next(mem.getFeatures()).geometry()
    assert count_small_holes(out, 1e12)[1] == 1, "le trou a été perdu"
    print("OK sharp_angle_repair_preserves_real_hole")


def test_geographic_crs_warns_about_metre_thresholds():
    _ensure_qgis()
    vl = _make_layer(crs="EPSG:4326")
    _add(vl, "POLYGON((0 0,0 1,1 1,1 0,0 0))")
    res = GeomAnalyzer(AnalysisOptions(
        check_close_vertices=True, close_vertex_min_dist_m=0.2)).analyze(vl)
    assert any("géographique" in w for w in res.warnings), res.warnings
    print("OK geographic_crs_warns_about_metre_thresholds")


def test_agl_note_requires_all_features_populated():
    """Une entité SANS géométrie et sans altitudes rend le contrôle non concluant."""
    _ensure_qgis()
    vl = _make_layer(fields=[("AGL", QVariant.Double), ("AMSL", QVariant.Double)])
    _add(vl, "POLYGON((0 0,0 10,10 10,10 0,0 0))", {"AGL": 30.0, "AMSL": 10.0})
    _add(vl, None)   # ni géométrie ni altitudes
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    assert res.agl_note == "", res.agl_note
    assert not res.flagged.get(C.CAT_AGL)
    assert "buildings_agl_over_amsl" not in res.to_dict()
    print("OK agl_note_requires_all_features_populated")


def test_reserved_fid_field_is_renamed_in_repaired_layer():
    """Un attribut « fid » est renommé, valeurs conservées, ordre inchangé.

    Sans ce renommage, le pilote GeoPackage reprend « fid » comme clé primaire
    et l'écriture échoue dès que la réparation duplique l'identifiant.
    """
    _ensure_qgis()
    vl = _make_layer(fields=[("fid", QVariant.LongLong),
                             ("code", QVariant.String)])
    _add(vl, "POLYGON((0 0,0 10,10 10,10 0,0 0))", {"fid": 7, "code": "a"})
    _add(vl, "POLYGON((20 0,20 10,30 10,30 0,20 0))", {"fid": 8, "code": "b"})
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    mem, stats = GeomAnalyzer.build_repaired_layer(vl, res, RepairOptions())
    names = [f.name() for f in mem.fields()]
    assert names == ["fid_src", "code"], names
    assert stats.renamed_fields == [("fid", "fid_src")], stats.renamed_fields
    rows = sorted((f["fid_src"], f["code"]) for f in mem.getFeatures())
    assert rows == [(7, "a"), (8, "b")], rows
    print("OK reserved_fid_field_is_renamed_in_repaired_layer")


def test_no_rename_without_reserved_field():
    _ensure_qgis()
    vl = _make_layer(fields=[("code", QVariant.String)])
    _add(vl, "POLYGON((0 0,0 10,10 10,10 0,0 0))", {"code": "a"})
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    mem, stats = GeomAnalyzer.build_repaired_layer(vl, res, RepairOptions())
    assert [f.name() for f in mem.fields()] == ["code"]
    assert stats.renamed_fields == []
    print("OK no_rename_without_reserved_field")


def test_rename_avoids_name_already_taken():
    """Si « fid_src » existe déjà, le nouveau nom reste unique."""
    _ensure_qgis()
    vl = _make_layer(fields=[("fid", QVariant.LongLong),
                             ("fid_src", QVariant.String)])
    _add(vl, "POLYGON((0 0,0 10,10 10,10 0,0 0))",
         {"fid": 3, "fid_src": "déjà pris"})
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    mem, stats = GeomAnalyzer.build_repaired_layer(vl, res, RepairOptions())
    assert [f.name() for f in mem.fields()] == ["fid_src2", "fid_src"]
    feat = next(mem.getFeatures())
    assert feat["fid_src2"] == 3 and feat["fid_src"] == "déjà pris"
    print("OK rename_avoids_name_already_taken")


def test_explode_multipart_writes_to_geopackage():
    """Non-régression : éclatement + écriture GPKG d'une source à champ « fid ».

    Reproduit l'échec observé en production (« UNIQUE constraint failed:
    <couche>.fid ») : chaque partie héritait du même identifiant source.
    """
    _ensure_qgis()
    import tempfile
    from qgis.core import (QgsCoordinateTransformContext, QgsVectorFileWriter)

    vl = _make_layer(geom_type="MultiPolygon",
                     fields=[("fid", QVariant.LongLong)])
    for i in range(3):
        x = i * 100
        _add(vl, "MULTIPOLYGON(((%d 0,%d 0,%d 10,%d 10,%d 0)),"
                 "((%d 0,%d 0,%d 10,%d 10,%d 0)))"
                 % (x, x + 10, x + 10, x, x,
                    x + 40, x + 50, x + 50, x + 40, x + 40),
             {"fid": i + 1})
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    mem, stats = GeomAnalyzer.build_repaired_layer(
        vl, res, RepairOptions(explode_multipart=True))
    assert stats.written == 6, stats.written

    tmp_dir = tempfile.mkdtemp(prefix="sgqc_")
    out_path = os.path.join(tmp_dir, "repare.gpkg")
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = "repare"
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        mem, out_path, QgsCoordinateTransformContext(), opts)
    assert result[0] == QgsVectorFileWriter.WriterError.NoError, result[1]

    reread = QgsVectorLayer(out_path, "relu", "ogr")
    assert reread.isValid()
    assert reread.featureCount() == 6, reread.featureCount()
    # Le fichier possède sa propre clé primaire ; l'identifiant source subsiste.
    names = [f.name() for f in reread.fields()]
    assert "fid_src" in names, names
    assert sorted(f["fid_src"] for f in reread.getFeatures()) == [1, 1, 2, 2, 3, 3]
    print("OK explode_multipart_writes_to_geopackage")


def test_hole_defaults_unchecked_and_two_square_metres():
    """Contrôle des trous : décoché par défaut, seuil par défaut à 2 m²."""
    _ensure_qgis()
    opts = AnalysisOptions()
    assert opts.check_holes is False
    assert opts.hole_max_area_m2 == 2.0, opts.hole_max_area_m2
    assert C.HOLE_THRESHOLD_DEFAULT_M2 == 2.0
    print("OK hole_defaults_unchecked_and_two_square_metres")


def test_hole_default_threshold_selects_small_holes_only():
    """Avec le seuil par défaut : trou de 1 m² signalé, trou de 4 m² ignoré.

    Coordonnées prises DANS l'emprise réelle du Lambert-93 : la surface étant
    mesurée sur l'ellipsoïde, des coordonnées hors emprise (x=110, y=10) sont
    fortement déformées et fausseraient la comparaison au seuil.
    """
    _ensure_qgis()
    x0, y0 = 650000, 6860000
    vl = _make_layer()
    _add(vl, "POLYGON((%d %d,%d %d,%d %d,%d %d,%d %d),"
             "(%d %d,%d %d,%d %d,%d %d,%d %d))"
         % (x0, y0, x0, y0 + 50, x0 + 50, y0 + 50, x0 + 50, y0, x0, y0,
            x0 + 10, y0 + 10, x0 + 10, y0 + 11, x0 + 11, y0 + 11,
            x0 + 11, y0 + 10, x0 + 10, y0 + 10))          # trou de 1 m²
    x1 = x0 + 200
    _add(vl, "POLYGON((%d %d,%d %d,%d %d,%d %d,%d %d),"
             "(%d %d,%d %d,%d %d,%d %d,%d %d))"
         % (x1, y0, x1, y0 + 50, x1 + 50, y0 + 50, x1 + 50, y0, x1, y0,
            x1 + 10, y0 + 10, x1 + 10, y0 + 12, x1 + 12, y0 + 12,
            x1 + 12, y0 + 10, x1 + 10, y0 + 10))          # trou de 4 m²
    res = GeomAnalyzer(AnalysisOptions(check_holes=True)).analyze(vl)
    assert res.hole_threshold_m2 == 2.0, res.hole_threshold_m2
    assert res.quality_report.hole_total == 1, res.quality_report.hole_total
    assert res.quality_report.hole_count == 1, res.quality_report.hole_count
    print("OK hole_default_threshold_selects_small_holes_only")


def test_contained_polygons_rejected_by_default():
    """« Accepter les polygones entièrement inclus » est décoché par défaut :
    une inclusion totale est donc signalée comme chevauchement."""
    _ensure_qgis()
    opts = AnalysisOptions()
    assert opts.allow_contained_polygons is False
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,0 100,100 100,100 0,0 0))")
    _add(vl, "POLYGON((10 10,10 20,20 20,20 10,10 10))")   # entièrement inclus
    res = GeomAnalyzer(opts).analyze(vl)
    assert res.quality_report.contained_pairs == 1, res.quality_report.contained_pairs
    assert len(res.flagged[C.CAT_OVERLAP]) == 2, res.flagged[C.CAT_OVERLAP]
    # Et si l'utilisateur cochait la case, l'inclusion serait acceptée.
    res2 = GeomAnalyzer(AnalysisOptions(allow_contained_polygons=True)).analyze(vl)
    assert not res2.flagged.get(C.CAT_OVERLAP), res2.flagged.get(C.CAT_OVERLAP)
    print("OK contained_polygons_rejected_by_default")


_ANALYSE_OPTIONS = ("topology", "overlaps", "duplicates", "duplicate_vertex",
                    "small", "holes", "nested_height")
_EXTRAS = ("sharp_angles", "close_vertices", "line_crossings")
_OVERLAP_EXCEPTIONS = ("ignore_missing_vertex", "allow_contained")


def test_single_default_mode_checks_every_analysis_option():
    """Un seul mode, sans nom : tout « Options d'analyse », rien d'« Extras ».

    Les trois préréglages « Rapide / Standard / Approfondi » ont été retirés en
    2.9.49 : ils demandaient de choisir un mode avant d'avoir vu la donnée, et
    deux d'entre eux n'existaient que pour retirer des contrôles. Ce test fige
    ce qui les remplace — un dictionnaire unique de défauts — pour qu'une
    modification future soit délibérée et non accidentelle. Voir
    ``test_engine_defaults_stay_conservative`` pour l'autre versant : le moteur,
    lui, reste conservateur pour les usages programmatiques.
    """
    for cle in _ANALYSE_OPTIONS:
        assert C.UI_DEFAULT_CHECKS[cle] is True, cle
    for cle in _EXTRAS:
        assert C.UI_DEFAULT_CHECKS[cle] is False, cle
    # Aucune case oubliée ni en trop : le dict couvre EXACTEMENT le panneau.
    attendu = set(_ANALYSE_OPTIONS) | set(_EXTRAS) | set(_OVERLAP_EXCEPTIONS)
    assert set(C.UI_DEFAULT_CHECKS) == attendu, sorted(C.UI_DEFAULT_CHECKS)
    # Les deux seuils affichés par ce mode, eux aussi épinglés : le panneau
    # annonce « seuil 2 m² » et le comblement de trou s'aligne dessus.
    assert C.THRESHOLD_DEFAULT_M2 == 2.0
    assert C.HOLE_THRESHOLD_DEFAULT_M2 == 2.0
    # Plus aucune trace des préréglages, ni dans les constantes ni dans le
    # panneau : un reste inutilisé se remettrait à vivre à la première reprise.
    from stat_geom_qc.stat_geom_dockwidget import StatGeomDockWidget as D
    for parti in ("PRESETS", "PRESET_ORDER", "PRESET_LABELS", "PRESET_DEFAULT",
                  "PRESET_TOOLTIPS"):
        assert not hasattr(C, parti), parti
    for parti in ("_apply_preset", "_current_preset_key", "_refresh_preset_chips"):
        assert not hasattr(D, parti), parti
    print("OK single_default_mode_checks_every_analysis_option")


def test_nested_height_is_an_analysis_option_not_an_extra():
    """La hauteur des polygones inclus a quitté « Extras » pour les options.

    Ce n'est pas un contrôle de forme réservé à un type de saisie : c'est une
    incohérence d'ALTITUDE — une faute de donnée au même titre qu'un
    chevauchement. Elle est donc cochée par défaut et mémorisée comme les
    autres cases.

    Elle reste en revanche absente de ``CHECKS_ON_BY_DEFAULT``, et ce n'est pas
    une contradiction : le contrôle ne peut tourner que si la couche porte AGL
    ou HEIGHT, si bien que l'y inscrire collerait « 1 contrôle non exécuté » à
    chaque analyse d'une couche sans ces champs, sans recours possible.
    """
    from stat_geom_qc import settings as sg_settings

    assert C.UI_DEFAULT_CHECKS["nested_height"] is True
    assert C.SK_NESTED_HEIGHT not in sg_settings.OPT_IN_CHECK_KEYS
    assert "nested_height" not in C.CHECKS_ON_BY_DEFAULT
    print("OK nested_height_is_an_analysis_option_not_an_extra")


def test_overlap_exceptions_checked_by_default():
    """Les deux EXCEPTIONS de chevauchement restent cochées par défaut.

    Sur du bâti mitoyen réel elles dominent largement : un jeu de 58 000
    bâtiments donne 1 177 paires en recouvrement, dont 1 139 imputables à un
    sommet manquant et 10 à une imbrication — 28 vrais conflits noyés dans 97 %
    de bruit d'accrochage. Les livrer décochées rendait la liste d'erreurs
    inexploitable par défaut.
    """
    for cle in _OVERLAP_EXCEPTIONS:
        assert C.UI_DEFAULT_CHECKS[cle] is True, cle
    assert C.OVERLAP_EXCEPTIONS_ON_BY_DEFAULT is True
    print("OK overlap_exceptions_checked_by_default")


def test_ui_defaults_restore_overlap_exceptions_checked():
    """À l'ouverture du panneau, les deux exceptions doivent revenir cochées.

    Elles ne figurent plus parmi les contrôles opt-in remis à zéro à chaque
    session : ce mécanisme n'a de sens que pour une case dont l'état par défaut
    est décoché.
    """
    _ensure_qgis()
    from qgis.core import QgsSettings
    from stat_geom_qc import settings as sg_settings

    assert C.SK_ALLOW_CONTAINED not in sg_settings.OPT_IN_CHECK_KEYS
    assert C.SK_IGNORE_MISSING_VERTEX not in sg_settings.OPT_IN_CHECK_KEYS

    cles = (C.SK_ALLOW_CONTAINED, C.SK_IGNORE_MISSING_VERTEX,
            C.SK_SETTINGS_VERSION)
    full = {k: "%s/%s" % (C.SETTINGS_GROUP, k) for k in cles}
    s = QgsSettings()
    backup = {k: s.value(v, None) for k, v in full.items()}
    try:
        # Profil venant d'une version où les deux cases étaient décochées : la
        # valeur « false » y est enregistrée. Sans migration, elle continuerait
        # d'être relue et le nouveau défaut resterait invisible — exactement le
        # piège déjà rencontré avec le seuil de trou.
        s.setValue(full[C.SK_ALLOW_CONTAINED], False)
        s.setValue(full[C.SK_IGNORE_MISSING_VERTEX], False)
        s.setValue(full[C.SK_SETTINGS_VERSION], 8)

        opts = sg_settings.load_options()
        assert opts.allow_contained_polygons is True, "migration 9 non appliquée"
        assert opts.ignore_missing_vertex_overlaps is True

        # Une fois la migration passée, un choix EXPLICITE est bien respecté.
        sg_settings.save_options(AnalysisOptions(
            allow_contained_polygons=False,
            ignore_missing_vertex_overlaps=False))
        opts2 = sg_settings.load_options()
        assert opts2.allow_contained_polygons is False
        assert opts2.ignore_missing_vertex_overlaps is False
    finally:
        s2 = QgsSettings()
        for key, value in backup.items():
            if value is None:
                s2.remove(full[key])
            else:
                s2.setValue(full[key], value)
    print("OK ui_defaults_restore_overlap_exceptions_checked")


def test_engine_defaults_stay_conservative():
    """Le MOTEUR, lui, ne masque toujours rien sans qu'on le lui demande.

    ``AnalysisOptions()`` est ce que voient les scripts et les tests : y glisser
    une exception implicite ferait disparaître des chevauchements d'analyses
    programmatiques sans que personne ne l'ait demandé. Seul le PANNEAU propose
    ces deux cases cochées.
    """
    d = AnalysisOptions()
    assert d.ignore_missing_vertex_overlaps is False
    assert d.allow_contained_polygons is False
    print("OK engine_defaults_stay_conservative")


def test_opt_in_checks_never_restored_as_active():
    """Reproduit le cas signalé : une case cochée puis analysée revenait cochée.

    Ne vaut plus que pour les contrôles d'« Extras ». Quatre contrôles en sont
    sortis en 2.9.49 (petits polygones, trous, vertex dupliqués, hauteur des
    polygones inclus) : ils appartiennent à « Options d'analyse », cochée en
    entier par défaut, et sont mémorisés comme les autres cases — sans quoi un
    décochage délibéré serait annulé à chaque ouverture.
    """
    _ensure_qgis()
    from qgis.core import QgsSettings
    from stat_geom_qc import settings as sg_settings

    group_keys = (list(sg_settings.OPT_IN_CHECK_KEYS)
                  + list(sg_settings._NEWLY_DEFAULT_ON_KEYS)
                  + [C.SK_SETTINGS_VERSION, C.SK_HOLE_THRESHOLD])
    full = {k: "%s/%s" % (C.SETTINGS_GROUP, k) for k in group_keys}
    s = QgsSettings()
    backup = {k: s.value(v, None) for k, v in full.items()}
    try:
        # Une session précédente avait tout coché et un seuil personnalisé.
        for key in sg_settings.OPT_IN_CHECK_KEYS:
            s.setValue(full[key], True)
        # Et un profil figé sur une vieille version garde « false » pour les
        # quatre contrôles devenus cochés par défaut : la migration 10 doit
        # effacer ces valeurs, sinon le nouveau défaut resterait invisible.
        for key in sg_settings._NEWLY_DEFAULT_ON_KEYS:
            s.setValue(full[key], False)
        s.setValue(full[C.SK_HOLE_THRESHOLD], 7.5)
        s.setValue(full[C.SK_SETTINGS_VERSION], 2)   # avant la migration v3

        opts = sg_settings.load_options()
        # Extras : jamais restaurés actifs, même cochés la session précédente.
        assert opts.check_sharp_angles is False
        assert opts.check_close_vertices is False
        assert opts.check_line_crossings is False
        # Options d'analyse : cochées, migration 10 appliquée.
        assert opts.check_holes is True, "migration 10 non appliquée"
        assert opts.check_duplicate_vertex is True
        assert opts.check_small_polygons is True
        assert opts.check_nested_height is True
        # Les seuils, eux, sont bien conservés.
        assert opts.hole_max_area_m2 == 7.5, opts.hole_max_area_m2
        # Les clés d'activation des Extras ont été purgées du fichier.
        s2 = QgsSettings()
        leftover = [k for k in sg_settings.OPT_IN_CHECK_KEYS
                    if s2.value(full[k], None) is not None]
        assert not leftover, leftover

        # Une analyse lancée avec un Extra actif ne le réenregistre pas.
        sg_settings.save_options(AnalysisOptions(check_sharp_angles=True))
        s3 = QgsSettings()
        leftover = [k for k in sg_settings.OPT_IN_CHECK_KEYS
                    if s3.value(full[k], None) is not None]
        assert not leftover, leftover
        assert sg_settings.load_options().check_sharp_angles is False

        # En revanche, DÉCOCHER une option d'analyse doit survivre à la
        # fermeture du panneau : c'est un choix délibéré, pas une case activée
        # par curiosité.
        sg_settings.save_options(AnalysisOptions(check_holes=False))
        assert sg_settings.load_options().check_holes is False
    finally:
        s4 = QgsSettings()
        for key, value in backup.items():
            if value is None:
                s4.remove(full[key])
            else:
                s4.setValue(full[key], value)
    print("OK opt_in_checks_never_restored_as_active")


def test_settings_migration_updates_legacy_hole_threshold():
    """L'ancien défaut (10 m²) mémorisé est remplacé ; un choix explicite non.

    Les réglages réels du profil QGIS sont sauvegardés puis restaurés.
    """
    _ensure_qgis()
    from qgis.core import QgsSettings
    from stat_geom_qc import settings as sg_settings

    key_thr = "%s/%s" % (C.SETTINGS_GROUP, C.SK_HOLE_THRESHOLD)
    key_ver = "%s/%s" % (C.SETTINGS_GROUP, C.SK_SETTINGS_VERSION)
    s = QgsSettings()
    backup = (s.value(key_thr, None), s.value(key_ver, None))
    try:
        # Cas 1 : ancien défaut mémorisé, aucune version -> migré.
        s.setValue(key_thr, 10.0)
        s.remove(key_ver)
        sg_settings.migrate_settings()
        assert float(QgsSettings().value(key_thr)) == 2.0
        assert int(QgsSettings().value(key_ver)) == C.SETTINGS_VERSION

        # Cas 2 : valeur choisie par l'utilisateur -> conservée.
        s2 = QgsSettings()
        s2.setValue(key_thr, 25.0)
        s2.remove(key_ver)
        sg_settings.migrate_settings()
        assert float(QgsSettings().value(key_thr)) == 25.0

        # Cas 3 : migration déjà faite -> aucune réécriture.
        s3 = QgsSettings()
        s3.setValue(key_thr, 10.0)
        s3.setValue(key_ver, C.SETTINGS_VERSION)
        sg_settings.migrate_settings()
        assert float(QgsSettings().value(key_thr)) == 10.0
    finally:
        s4 = QgsSettings()
        for key, value in zip((key_thr, key_ver), backup):
            if value is None:
                s4.remove(key)
            else:
                s4.setValue(key, value)
    print("OK settings_migration_updates_legacy_hole_threshold")


def test_settings_migration_purges_removed_overlap_settings():
    """Les clés des deux réglages supprimés sont effacées (migrations 4 et 5).

    Sans cela, des réglages devenus inopérants resteraient indéfiniment dans le
    fichier de configuration du profil.
    """
    _ensure_qgis()
    from qgis.core import QgsSettings
    from stat_geom_qc import settings as sg_settings

    keys = ("%s/%s" % (C.SETTINGS_GROUP, C.SK_OVERLAP_MIN_WIDTH_LEGACY),
            "%s/%s" % (C.SETTINGS_GROUP, C.SK_MISSING_VERTEX_RATIO_LEGACY))
    key_ver = "%s/%s" % (C.SETTINGS_GROUP, C.SK_SETTINGS_VERSION)
    s = QgsSettings()
    backup = tuple(s.value(k, None) for k in keys + (key_ver,))
    try:
        for k in keys:
            s.setValue(k, 0.1)
        s.setValue(key_ver, 3)
        sg_settings.migrate_settings()
        for k in keys:
            assert QgsSettings().value(k, None) is None, "clé non purgée : %s" % k
        assert int(QgsSettings().value(key_ver)) == C.SETTINGS_VERSION
        # Une analyse ne doit jamais réécrire ces clés.
        sg_settings.save_options(AnalysisOptions())
        for k in keys:
            assert QgsSettings().value(k, None) is None, "clé réécrite : %s" % k
    finally:
        s2 = QgsSettings()
        for key, value in zip(keys + (key_ver,), backup):
            if value is None:
                s2.remove(key)
            else:
                s2.setValue(key, value)
    print("OK settings_migration_purges_removed_overlap_settings")


def test_checkbox_assets_shipped_and_referenced():
    """Les pictogrammes de coche existent, sont référencés, et ont un repli.

    L'état coché d'une case repose sur un fichier livré avec le plugin : s'il
    disparaissait du paquet, toutes les cases paraîtraient vides sans aucune
    erreur. Ce test verrouille donc les trois points : les fichiers sont là, la
    feuille de style les cite entre guillemets (un chemin d'installation peut
    contenir des espaces), et leur absence bascule sur l'ancien carré rempli.
    """
    _ensure_qgis()
    import os
    from stat_geom_qc import theme as sg_theme

    for dark in (True, False):
        colors = sg_theme.colors_for(dark)
        for key in ("check_icon", "check_ghost"):
            chemin = colors[key]
            assert os.path.isfile(chemin), chemin
            assert "\\" not in chemin, "séparateurs Windows refusés par url() QSS"
        assert sg_theme.checkbox_assets_available(colors)
        qss = sg_theme.build_stylesheet(colors)
        assert 'image:url("%s")' % colors["check_icon"] in qss
        assert 'image:url("%s")' % colors["check_ghost"] in qss

        # Repli : pictogrammes introuvables -> carré rempli, aucune url().
        absents = dict(colors, check_icon="/introuvable/a.svg",
                       check_ghost="/introuvable/b.svg")
        assert not sg_theme.checkbox_assets_available(absents)
        repli = sg_theme.build_stylesheet(absents)
        assert "image:url(" not in repli
        assert "background:%s; border-color:%s" % (colors["check"],
                                                   colors["check"]) in repli
    print("OK checkbox_assets_shipped_and_referenced")


def test_checks_enabled_reflects_actual_options():
    """``AnalysisResult.checks_enabled`` doit refléter EXACTEMENT quels
    contrôles ont tourné pendant cette analyse — c'est sur cette info que
    l'affichage décide de montrer un compteur ou « Non demandé »."""
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,10 0,10 10,0 10,0 0))")

    # Préréglage « Rapide » : chevauchements décochés, le reste au minimum.
    rapide = GeomAnalyzer(AnalysisOptions(
        topology_checks=True, check_overlaps=False, check_duplicates=True,
        check_duplicate_vertex=False, check_holes=False,
        check_sharp_angles=False,
        check_close_vertices=False,
        check_nested_height=False)).analyze(vl)
    attendu = {
        "self_intersection": True, "multipart": True, "overlap": False,
        "duplicate": True, "small": False, "duplicate_vertex": False,
        "hole": False, "sharp_angle": False, "close_vertex": False,
        "nested_height": False, "unnoded_crossing": False,
    }
    assert rapide.checks_enabled == attendu, rapide.checks_enabled
    assert rapide.to_dict()["checks_enabled"] == attendu

    # Préréglage « Approfondi » : tout coché. La couche porte un champ AGL,
    # sans quoi le contrôle de hauteur se marquerait lui-même non exécuté.
    vl_agl = _make_layer(fields=[("AGL", QVariant.Double)])
    _add(vl_agl, "POLYGON((0 0,10 0,10 10,0 10,0 0))", {"AGL": 12.0})
    approfondi = GeomAnalyzer(AnalysisOptions(
        topology_checks=True, check_overlaps=True, check_duplicates=True,
        check_small_polygons=True, check_duplicate_vertex=True,
        check_holes=True, check_sharp_angles=True,
        check_close_vertices=True,
        check_nested_height=True,
        check_line_crossings=True)).analyze(vl_agl)
    checks = dict(approfondi.checks_enabled)
    # Couche exclusivement polygonale : le contrôle de croisement de lignes ne
    # peut matériellement pas tourner, quelle que soit la demande — seule
    # exception attendue sur une couche « tout coché ».
    assert checks.pop("unnoded_crossing") is False
    assert all(checks.values()), approfondi.checks_enabled
    print("OK checks_enabled_reflects_actual_options")


def test_report_shows_not_requested_instead_of_zero():
    """Un contrôle décoché ne doit JAMAIS afficher 0 : ce serait lu comme
    « rien trouvé » alors que le contrôle n'a simplement pas tourné. Vérifié
    sur les trois sorties destinées à l'utilisateur (tableau Qt, rapport HTML,
    résumé Qt) — le CSV/JSON gardent le compte brut mais exposent
    ``checks_enabled`` pour qu'un lecteur programmatique fasse la distinction.
    """
    _ensure_qgis()
    from stat_geom_qc import report as sg_report

    vl = _make_layer()
    _add(vl, "POLYGON((0 0,10 0,10 10,0 10,0 0))")
    res = GeomAnalyzer(AnalysisOptions(
        check_overlaps=False, check_duplicate_vertex=False,
        check_holes=False,
        check_sharp_angles=False, check_close_vertices=False)).analyze(vl)
    assert res.quality_report.overlap_count == 0
    assert res.quality_report.hole_count == 0

    html_out = sg_report.build_html(res)
    assert sg_report.NOT_REQUESTED in html_out
    # Un contrôle TOUJOURS actif (petits polygones) doit continuer d'afficher
    # sa valeur normalement, jamais "Non demandé".
    assert "≤ %g m²" % res.threshold_m2 in html_out

    qt_out = sg_report.build_qt_summary(res)
    assert sg_report.NOT_REQUESTED in qt_out

    # Contrôle actif mais résultat propre : un vrai 0 doit rester un 0.
    res_actif = GeomAnalyzer(AnalysisOptions(check_holes=True)).analyze(vl)
    assert res_actif.quality_report.hole_count == 0
    html_actif = sg_report.build_html(res_actif)
    assert ("Entités avec trous" in html_actif)
    print("OK report_shows_not_requested_instead_of_zero")


def test_plugin_version_matches_metadata():
    """La version affichée dans l'en-tête vient de ``metadata.txt`` — jamais
    une valeur dupliquée à la main qui pourrait s'en désynchroniser."""
    _ensure_qgis()
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "metadata.txt")
    with open(path, encoding="utf-8") as fh:
        attendu = next(l.split("=", 1)[1].strip()
                      for l in fh if l.startswith("version="))
    assert C.PLUGIN_VERSION == attendu
    assert C.PLUGIN_VERSION  # jamais vide sur une installation normale
    print("OK plugin_version_matches_metadata")


def test_report_formats_are_html_and_pdf_only():
    """CSV et JSON retirés, PDF ajouté — et le PDF reste sûr.

    Le retrait du CSV supprime du même coup toute la surface d'injection de
    formule tableur (CWE-1236) : plus de cellule, plus d'amorce ``=`` à
    neutraliser. Le test qui la couvrait est donc remplacé par celui-ci, qui
    vérifie deux choses : que les écrivains ont bien disparu (et ne
    reviendront pas par inadvertance), et que le PDF n'ouvre pas une nouvelle
    porte aux métadonnées hostiles.
    """
    _ensure_qgis()
    import tempfile
    from stat_geom_qc import report as sg_report

    # Les formats retirés ne doivent plus exister nulle part.
    for parti in ("write_csv", "write_json", "_csv_safe", "_flat_rows",
                  "_CSV_AMORCES_FORMULE"):
        assert not hasattr(sg_report, parti), parti
    assert hasattr(sg_report, "write_pdf")
    assert hasattr(sg_report, "write_html")
    assert sorted(C.EXPORT_FILTERS) == ["html", "pdf"], sorted(C.EXPORT_FILTERS)

    # Un nom de couche hostile ne doit ni casser le PDF ni s'y injecter.
    for charge in ("=cmd|' /C calc'!A0",
                   "<script>alert(1)</script>",
                   "<img src=x onerror=alert(1)>"):
        vl = QgsVectorLayer("Polygon?crs=EPSG:2154", charge, "memory")
        feat = QgsFeature(vl.fields())
        feat.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0,0 10,10 10,10 0,0 0))"))
        vl.dataProvider().addFeatures([feat])
        vl.updateExtents()
        res = GeomAnalyzer(AnalysisOptions()).analyze(vl)

        chemin = os.path.join(tempfile.mkdtemp(), "r.pdf")
        sg_report.write_pdf(res, chemin)
        assert os.path.isfile(chemin), charge
        # Un PDF réel, pas un fichier vide : l'en-tête et une taille plausible.
        with open(chemin, "rb") as fh:
            tete = fh.read(5)
        assert tete.startswith(b"%PDF"), (charge, tete)
        assert os.path.getsize(chemin) > 8000, (charge, os.path.getsize(chemin))

        # Le PDF est rendu depuis le résumé Qt : c'est là que l'échappement
        # doit tenir, puisque QTextDocument interprète le HTML qu'on lui donne.
        # Vérifié en PARSANT le document, jamais par recherche de motif :
        # « onerror= » apparaît légitimement dans du texte échappé, et une
        # assertion de sous-chaîne y produit un faux positif.
        from html.parser import HTMLParser

        class _Audit(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tags = {}
                self.handlers = []

            def handle_starttag(self, tag, attrs):
                self.tags[tag] = self.tags.get(tag, 0) + 1
                for nom, val in attrs:
                    if nom.lower().startswith("on") or (
                            val and "javascript:" in val.lower()):
                        self.handlers.append((tag, nom, val))

        audit = _Audit()
        audit.feed(sg_report.build_qt_summary(res))
        assert audit.tags.get("script", 0) == 0, (charge, audit.tags)
        assert audit.tags.get("img", 0) == 0, (charge, audit.tags)
        assert not audit.handlers, (charge, audit.handlers)
        # Et l'en-tête que write_pdf ajoute échappe aussi le nom de couche.
        assert "<script>" not in ("STAT GEOM QC — %s" % charge).replace(
            charge, __import__("html").escape(charge))
    print("OK report_formats_are_html_and_pdf_only")


def test_html_report_escapes_hostile_layer_metadata():
    """Sécurité : le rapport HTML s'ouvre dans le navigateur de l'utilisateur.

    Un nom de couche ou de colonne contenant du balisage ne doit produire aucune
    balise exécutable — vérifié en PARSANT le document, pas en cherchant des
    motifs de texte (``onerror=`` apparaît légitimement dans du texte échappé).
    """
    _ensure_qgis()
    from html.parser import HTMLParser
    from qgis.core import QgsField
    from qgis.PyQt.QtCore import QMetaType
    from stat_geom_qc import report as sg_report

    vl = QgsVectorLayer("Polygon?crs=EPSG:2154",
                        '<script>alert("xss")</script>', "memory")
    vl.dataProvider().addAttributes(
        [QgsField('<img src=x onerror=alert(1)>', QMetaType.Type.QString)])
    vl.updateFields()
    feat = QgsFeature(vl.fields())
    feat.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0,0 10,10 10,10 0,0 0))"))
    vl.dataProvider().addFeatures([feat])
    vl.updateExtents()
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)

    class Audit(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags = {}
            self.handlers = []

        def handle_starttag(self, tag, attrs):
            self.tags[tag] = self.tags.get(tag, 0) + 1
            for nom, val in attrs:
                if nom.lower().startswith("on") or (
                        val and "javascript:" in val.lower()):
                    self.handlers.append((tag, nom, val))

    audit = Audit()
    audit.feed(sg_report.build_html(res))
    assert audit.tags.get("script", 0) == 0, audit.tags
    assert audit.tags.get("img", 0) == 0, audit.tags
    assert not audit.handlers, audit.handlers
    print("OK html_report_escapes_hostile_layer_metadata")


def test_score_ignores_checks_that_did_not_run():
    """Un contrôle non exécuté ne doit PAS produire un sous-score à 100 %.

    Non-régression du défaut le plus trompeur : avec les chevauchements et les
    doublons décochés, le score valait 100/100 « Excellent » et le rapport
    affichait « Absence de chevauchement 100 % » — un verdict sur une dimension
    jamais mesurée. Le score ne doit porter que sur ce qui a tourné.
    """
    _ensure_qgis()
    from stat_geom_qc import report as sg_report

    vl = _make_layer()
    _add(vl, "POLYGON((0 0,10 0,10 10,0 10,0 0))")
    _add(vl, "POLYGON((100 0,110 0,110 10,100 10,100 0))")

    res = GeomAnalyzer(AnalysisOptions(
        check_overlaps=False, check_duplicates=False)).analyze(vl)
    comps = res.correctness.components
    assert "overlap" not in comps, comps
    assert "duplicate" not in comps, comps
    assert "invalid" in comps and "null_empty" in comps, comps
    # Les barres du rapport suivent, puisqu'elles bouclent sur components.
    html = sg_report.build_html(res)
    assert "Absence de chevauchement" not in html
    assert "Unicité (doublons)" not in html
    # La mention de couverture partielle accompagne le score. Cinq contrôles
    # manquent ici, et non deux : depuis la 2.9.49, petits polygones, vertex
    # dupliqués et trous sont cochés par défaut dans le panneau et comptent donc
    # dans la couverture attendue — le moteur, lui, les laisse décochés.
    assert sg_report.skipped_note(res).strip() == "5 contrôles non exécutés."

    # Contre-épreuve : tout coché -> les dimensions reviennent, pas de mention.
    res_full = GeomAnalyzer(AnalysisOptions(
        check_overlaps=True, check_duplicates=True,
        check_small_polygons=True, check_duplicate_vertex=True,
        check_holes=True)).analyze(vl)
    assert "overlap" in res_full.correctness.components
    assert "duplicate" in res_full.correctness.components
    assert sg_report.skipped_note(res_full) == "", sg_report.skipped_note(res_full)
    print("OK score_ignores_checks_that_did_not_run")


def test_overlap_check_has_no_feature_limit():
    """AUCUN plafond de nombre de polygones ne doit exister.

    Un garde-fou interne abandonnait le contrôle au-delà de 100 000 polygones —
    donc sur les couches où il est le plus utile — sans que l'utilisateur ait
    jamais choisi cette limite, et en affichant un avertissement déroutant.
    """
    _ensure_qgis()
    opts = AnalysisOptions()
    assert not hasattr(opts, "overlap_max_features"), \
        "le plafond de volume ne doit plus exister"

    vl = _make_layer()
    _add(vl, "POLYGON((0 0,10 0,10 10,0 10,0 0))")
    _add(vl, "POLYGON((9 0,20 0,20 10,9 10,9 0))")   # vrai chevauchement
    res = GeomAnalyzer(AnalysisOptions(check_overlaps=True)).analyze(vl)
    assert res.checks_enabled["overlap"] is True
    assert res.quality_report.overlap_pairs == 1
    assert not any("dépassent la limite" in w for w in res.warnings), res.warnings
    print("OK overlap_check_has_no_feature_limit")


def test_overlap_check_marked_not_run_when_index_unavailable():
    """Le SEUL abandon possible reste l'index spatial indisponible.

    Dans ce cas le contrôle doit être marqué non exécuté : afficher « 0 »
    laisserait croire à une couche propre alors que rien n'a été examiné.
    """
    _ensure_qgis()
    from stat_geom_qc import analysis_engine as ae
    from stat_geom_qc import report as sg_report

    vl = _make_layer()
    _add(vl, "POLYGON((0 0,10 0,10 10,0 10,0 0))")
    _add(vl, "POLYGON((9 0,20 0,20 10,9 10,9 0))")

    vrai_index = ae.QgsSpatialIndex

    class IndexIndisponible(object):
        Flag = vrai_index.Flag

        def __init__(self, *a, **k):
            raise RuntimeError("index spatial simulé indisponible")

    ae.QgsSpatialIndex = IndexIndisponible
    try:
        # Tous les autres contrôles attendus par défaut sont demandés : la
        # mention isole ainsi le SEUL abandon, celui des chevauchements.
        res = GeomAnalyzer(AnalysisOptions(
            check_overlaps=True, check_small_polygons=True,
            check_duplicate_vertex=True, check_holes=True)).analyze(vl)
    finally:
        ae.QgsSpatialIndex = vrai_index

    assert res.checks_enabled["overlap"] is False
    assert "overlap" not in res.correctness.components
    assert res.errors, "l'échec doit être signalé, pas absorbé"
    assert sg_report.skipped_note(res).strip() == "1 contrôle non exécuté."
    assert sg_report.NOT_REQUESTED in sg_report.build_html(res)
    print("OK overlap_check_marked_not_run_when_index_unavailable")


def test_summary_cards_hide_checks_that_did_not_run():
    """Les cartes de l'onglet Synthèse ne doivent pas afficher « 0 » pour un
    contrôle non exécuté : le premier écran vu contredisait l'onglet Qualité,
    qui affichait « Non demandé » sur la même analyse."""
    _ensure_qgis()
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtWidgets import QLabel
    from stat_geom_qc.stat_geom_dockwidget import StatGeomDockWidget
    from stat_geom_qc.widgets import MetricCard

    class _Iface(object):
        def mainWindow(self):
            return None

        def mapCanvas(self):
            return None

    vl = _make_layer()
    _add(vl, "POLYGON((0 0,10 0,10 10,0 10,0 0))")
    _add(vl, "POLYGON((9 0,20 0,20 10,9 10,9 0))")

    dock = StatGeomDockWidget(_Iface())
    try:
        res = GeomAnalyzer(AnalysisOptions(
            topology_checks=False, check_overlaps=False,
            check_duplicates=False)).analyze(vl)
        dock._display(res)
        dock.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        dock.show()
        libelles = set()
        for card in dock.findChildren(MetricCard):
            textes = [w.text() for w in card.findChildren(QLabel)]
            if len(textes) >= 2:
                libelles.add(textes[-1])
        for absent in ("Auto-inters.", "Chevauch.", "Doublons"):
            assert absent not in libelles, "%s affichée alors que non exécutée" % absent
        # Les contrôles toujours calculés restent présents.
        for present in ("Entités", "Nulles/Vides", "Invalides"):
            assert present in libelles, present
    finally:
        dock.deleteLater()
    print("OK summary_cards_hide_checks_that_did_not_run")


def test_invalid_geometry_not_judged_on_its_area():
    """PERTE DE DONNÉES : la surface d'une géométrie invalide n'est pas fiable.

    Sur une auto-intersection en papillon, les aires signées des deux lobes
    s'annulent : un polygone de 50 m² se mesurait à 0 m², était compté « petit
    polygone » et se faisait SUPPRIMER par la réparation. La mesure se fait
    désormais sur une copie rendue valide.
    """
    _ensure_qgis()
    PAPILLON = "POLYGON((0 0,10 10,10 0,0 10,0 0))"      # ~50 m² réels
    vl = _make_layer()
    _add(vl, PAPILLON)
    res = GeomAnalyzer(AnalysisOptions(check_small_polygons=True, small_polygon_threshold_m2=2.0)).analyze(vl)
    assert res.quality_report.invalid_count == 1
    assert res.quality_report.small_area_count == 0, "encore compté petit"
    assert not res.flagged.get("small")

    # La réparation ne doit pas la supprimer, et doit la rendre valide.
    mem, _stats = GeomAnalyzer.build_repaired_layer(
        vl, res, RepairOptions(fix_invalid=True, remove_small=True))
    assert mem.featureCount() == 1, "entité supprimée à tort"
    aires = [round(f.geometry().area(), 1) for f in mem.getFeatures()]
    assert aires == [50.0], aires

    # Non-régression : un petit polygone VRAIMENT petit reste signalé, valide
    # comme invalide.
    for wkt, libelle in (("POLYGON((0 0,1 0,1 1,0 1,0 0))", "valide 1 m²"),
                         ("POLYGON((0 0,1 1,1 0,0 1,0 0))", "invalide ~0,5 m²")):
        petit = _make_layer()
        _add(petit, wkt)
        r = GeomAnalyzer(AnalysisOptions(check_small_polygons=True, small_polygon_threshold_m2=2.0)).analyze(petit)
        assert r.quality_report.small_area_count == 1, libelle
    print("OK invalid_geometry_not_judged_on_its_area")


def test_exact_duplicates_visible_when_duplicate_check_off():
    """Deux polygones identiques ne doivent JAMAIS devenir invisibles.

    Le contrôle des chevauchements délègue les doublons exacts au contrôle des
    doublons — lequel est décochable. Sans rattrapage, une superposition totale
    n'était vue par personne : 0 anomalie, score 100 « Excellent ».
    """
    _ensure_qgis()
    CARRE = "POLYGON((0 0,10 0,10 10,0 10,0 0))"
    vl = _make_layer()
    _add(vl, CARRE)
    _add(vl, CARRE)

    res = GeomAnalyzer(AnalysisOptions(
        check_duplicates=False, check_overlaps=True)).analyze(vl)
    q = res.quality_report
    assert q.overlap_pairs == 1, q.overlap_pairs
    assert q.overlap_count == 2, q.overlap_count
    assert res.total_issues() > 0, "superposition totale invisible"
    assert res.correctness.score < 100.0

    # Non-régression : contrôle des doublons actif -> c'est lui qui décrit le
    # cas, le chevauchement ne le compte pas une seconde fois.
    res2 = GeomAnalyzer(AnalysisOptions(
        check_duplicates=True, check_overlaps=True)).analyze(vl)
    assert res2.quality_report.duplicate_count == 1
    assert res2.quality_report.overlap_pairs == 0, "compté deux fois"
    print("OK exact_duplicates_visible_when_duplicate_check_off")


def test_repair_replays_contained_rule_of_analysis():
    """La réparation doit rejouer « ignorer les polygones inclus ».

    Sinon un bâtiment inclus dans sa parcelle était soustrait de celle-ci —
    donc entièrement effacé — puis annoncé comme « chevauchement majeur »,
    alors que l'analyse avait déclaré cette imbrication légitime.
    """
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,100 0,100 100,0 100,0 0))")      # parcelle
    _add(vl, "POLYGON((10 10,20 10,20 20,10 20,10 10))")    # bâti inclus
    _add(vl, "POLYGON((99 0,140 0,140 100,99 100,99 0))")   # vrai chevauchement

    res = GeomAnalyzer(AnalysisOptions(
        check_overlaps=True, allow_contained_polygons=True)).analyze(vl)
    assert res.allow_contained_polygons is True
    assert res.quality_report.contained_pairs == 1

    mem, stats = GeomAnalyzer.build_repaired_layer(
        vl, res, RepairOptions(
            fix_invalid=False, fix_overlaps=True,
            allow_contained_polygons=res.allow_contained_polygons))
    aires = sorted(round(f.geometry().area(), 1) for f in mem.getFeatures())
    # Le bâti inclus (100 m²) doit être INTACT, pas effacé.
    assert 100.0 in aires, aires
    assert mem.featureCount() == 3, mem.featureCount()
    print("OK repair_replays_contained_rule_of_analysis")


def test_measurement_context_captured_not_read_from_project():
    """Les mesures ellipsoïdales doivent passer par un contexte EXPLICITE.

    Le moteur interrogeait ``QgsProject.instance()`` — un singleton du thread
    principal — depuis le thread d'un QgsTask. Le contexte est désormais
    capturé puis transporté, et les tâches le font dans leur constructeur.
    """
    _ensure_qgis()
    from stat_geom_qc.analysis_engine import MeasurementContext
    from stat_geom_qc import tasks as sg_tasks

    ctx = MeasurementContext.from_project()
    assert ctx.usable_ellipsoid(), "un ellipsoïde exploitable est attendu"
    # Jamais « NONE » : ce serait refusé par setEllipsoid.
    assert MeasurementContext(None, "NONE").usable_ellipsoid() == "WGS84"
    assert MeasurementContext(None, "").usable_ellipsoid() == "WGS84"

    # Le contexte fourni est bien celui utilisé (et non le projet).
    vl = _make_layer(crs="EPSG:2154")
    _add(vl, "POLYGON((650000 6860000,650010 6860000,650010 6860010,"
             "650000 6860010,650000 6860000))")
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl, measurement_ctx=ctx)
    assert res.total_features == 1

    # Les deux tâches capturent le contexte dès la construction.
    tache = sg_tasks.AnalysisTask(vl.clone(), AnalysisOptions())
    assert isinstance(tache._mctx, MeasurementContext)
    print("OK measurement_context_captured_not_read_from_project")


def test_skipped_counters_distinguish_refusal_from_nothing_to_do():
    """« Laissée intacte » ne doit pas accuser à tort une entité déjà propre.

    Les compteurs confondaient « correction refusée car dégradante » et « plus
    rien à corriger », alors que le message affirme la dégradation comme un
    fait. Deux compteurs distincts désormais.
    """
    _ensure_qgis()
    stats = RepairStats()
    for champ in ("skipped_sharp_angles", "skipped_close_vertices",
                  "unchanged_sharp_angles", "unchanged_close_vertices"):
        assert hasattr(stats, champ), champ

    # Entité signalée « angle aigu » dont la pointe a disparu avant l'étape :
    # comptée « sans correction nécessaire », pas « refusée ».
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,10 0,10 10,0 10,0 0))")   # aucun angle aigu
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    res.flagged[C.CAT_SHARP_ANGLE] = [1]             # signalement forcé
    _mem, st = GeomAnalyzer.build_repaired_layer(
        vl, res, RepairOptions(fix_invalid=False, fix_sharp_angles=True,
                               sharp_angle_min_deg=30.0))
    assert st.skipped_sharp_angles == 0, st.skipped_sharp_angles
    assert st.unchanged_sharp_angles == 1, st.unchanged_sharp_angles
    print("OK skipped_counters_distinguish_refusal_from_nothing_to_do")


def test_migration_resets_stale_hole_threshold():
    """Un profil bloqué sur l'ancien seuil de trou (10 m²) revient à 2 m².

    La migration 2 ne touchait que les profils venant d'AVANT elle ; un profil
    déjà au-delà gardait 10 m² indéfiniment sans jamais voir le défaut. Un seuil
    choisi délibérément (autre que l'ancien défaut) doit rester intact.
    """
    _ensure_qgis()
    from qgis.core import QgsSettings
    from stat_geom_qc import settings as sg_settings

    key_thr = "%s/%s" % (C.SETTINGS_GROUP, C.SK_HOLE_THRESHOLD)
    key_ver = "%s/%s" % (C.SETTINGS_GROUP, C.SK_SETTINGS_VERSION)
    key_bat = "%s/%s" % (C.SETTINGS_GROUP, C.SK_BUILDINGS_LEGACY)
    s = QgsSettings()
    backup = (s.value(key_thr, None), s.value(key_ver, None), s.value(key_bat, None))
    try:
        # Profil « à jour » (version 7) mais resté sur 10 m², avec la clé
        # orpheline du contrôle bâtiments.
        s.setValue(key_thr, 10.0)
        s.setValue(key_ver, 7)
        s.setValue(key_bat, True)
        sg_settings.migrate_settings()
        assert float(QgsSettings().value(key_thr)) == 2.0
        assert QgsSettings().value(key_bat, None) is None, "clé orpheline gardée"
        assert int(QgsSettings().value(key_ver)) == C.SETTINGS_VERSION

        # Un seuil délibéré n'est PAS écrasé.
        s2 = QgsSettings()
        s2.setValue(key_thr, 25.0)
        s2.setValue(key_ver, 7)
        sg_settings.migrate_settings()
        assert float(QgsSettings().value(key_thr)) == 25.0

        # Idempotence : relancer ne retouche rien.
        s3 = QgsSettings()
        s3.setValue(key_thr, 10.0)
        sg_settings.migrate_settings()
        assert float(QgsSettings().value(key_thr)) == 10.0, \
            "migration rejouée alors que la version est à jour"
    finally:
        s4 = QgsSettings()
        for key, value in zip((key_thr, key_ver, key_bat), backup):
            if value is None:
                s4.remove(key)
            else:
                s4.setValue(key, value)
    print("OK migration_resets_stale_hole_threshold")


# ── Hauteur des polygones entièrement inclus ────────────────────────────────
# Géométries partagées : GRAND contient PETIT, entièrement.
_NEST_OUTER = "POLYGON((0 0,0 100,100 100,100 0,0 0))"
_NEST_INNER = "POLYGON((10 10,10 20,20 20,20 10,10 10))"


def _height_layer(fields, outer_attrs, inner_attrs):
    vl = _make_layer(fields=fields)
    _add(vl, _NEST_OUTER, outer_attrs)
    _add(vl, _NEST_INNER, inner_attrs)
    return vl


def _nested_result(vl):
    return GeomAnalyzer(AnalysisOptions(
        check_nested_height=True, check_overlaps=False)).analyze(vl)


def test_nested_height_flags_inner_not_strictly_higher():
    """Le polygone INCLUS doit être strictement plus haut que son contenant.

    Égal ou plus bas : anomalie. Et c'est bien l'inclus qui est signalé, pas
    le contenant — c'est sa valeur qui doit dépasser l'autre.
    """
    _ensure_qgis()
    for agl_inner, cas in ((5.0, "plus bas"), (30.0, "égal")):
        vl = _height_layer([("AGL", QVariant.Double)],
                           {"AGL": 30.0}, {"AGL": agl_inner})
        res = _nested_result(vl)
        q = res.quality_report
        assert q.nested_height_count == 1, (cas, q.nested_height_count)
        assert q.nested_height_pairs == 1, (cas, q.nested_height_pairs)
        assert q.nested_pairs_checked == 1, (cas, q.nested_pairs_checked)
        flags = res.flagged[C.CAT_NESTED_HEIGHT]
        assert len(flags) == 1, (cas, flags)
        # FID 2 = le petit polygone, ajouté en second.
        assert flags == [2], (cas, flags)
    print("OK nested_height_flags_inner_not_strictly_higher")


def test_nested_height_accepts_inner_strictly_higher():
    """Cas conforme : rien n'est signalé, mais la paire est bien comparée."""
    _ensure_qgis()
    vl = _height_layer([("AGL", QVariant.Double)],
                       {"AGL": 10.0}, {"AGL": 10.001})
    q = _nested_result(vl).quality_report
    assert q.nested_height_count == 0, q.nested_height_count
    assert q.nested_pairs_checked == 1, q.nested_pairs_checked
    assert q.nested_pairs_unknown == 0, q.nested_pairs_unknown
    print("OK nested_height_accepts_inner_strictly_higher")


def test_nested_height_fields_are_case_insensitive():
    """« agl » / « Height » doivent être reconnus au même titre que AGL/HEIGHT."""
    _ensure_qgis()
    vl = _height_layer([("agl", QVariant.Double), ("Height", QVariant.Double)],
                       {"agl": 30.0, "Height": 30.0},
                       {"agl": 5.0, "Height": 5.0})
    res = _nested_result(vl)
    assert res.checks_enabled["nested_height"] is True
    assert res.nested_height_fields == ["agl", "Height"], res.nested_height_fields
    assert res.quality_report.nested_height_count == 1
    print("OK nested_height_fields_are_case_insensitive")


def test_nested_height_compares_each_field_to_its_own():
    """AGL se compare à AGL et HEIGHT à HEIGHT, jamais l'un avec l'autre.

    Ici AGL est conforme mais HEIGHT ne l'est pas : la paire doit être
    signalée. Croiser les champs laisserait passer l'erreur.
    """
    _ensure_qgis()
    vl = _height_layer([("AGL", QVariant.Double), ("HEIGHT", QVariant.Double)],
                       {"AGL": 10.0, "HEIGHT": 50.0},
                       {"AGL": 20.0, "HEIGHT": 40.0})
    q = _nested_result(vl).quality_report
    assert q.nested_height_count == 1, q.nested_height_count
    print("OK nested_height_compares_each_field_to_its_own")


def test_nested_height_ignores_mere_overlap():
    """Un chevauchement PARTIEL n'est pas une inclusion : hors périmètre."""
    _ensure_qgis()
    vl = _make_layer(fields=[("AGL", QVariant.Double)])
    _add(vl, "POLYGON((0 0,0 10,10 10,10 0,0 0))", {"AGL": 30.0})
    _add(vl, "POLYGON((5 5,5 15,15 15,15 5,5 5))", {"AGL": 1.0})
    q = _nested_result(vl).quality_report
    assert q.nested_pairs_checked == 0, q.nested_pairs_checked
    assert q.nested_height_count == 0, q.nested_height_count
    print("OK nested_height_ignores_mere_overlap")


def test_nested_height_ignores_exact_duplicates():
    """Deux géométries identiques se contiennent mutuellement.

    C'est un DOUBLON, pas une imbrication : les compter ici signalerait une
    anomalie de hauteur systématique (une valeur n'est jamais > à elle-même)
    sur un problème que le contrôle des doublons décrit mieux.
    """
    _ensure_qgis()
    vl = _make_layer(fields=[("AGL", QVariant.Double)])
    _add(vl, _NEST_INNER, {"AGL": 7.0})
    _add(vl, _NEST_INNER, {"AGL": 7.0})
    q = _nested_result(vl).quality_report
    assert q.nested_pairs_checked == 0, q.nested_pairs_checked
    assert q.nested_height_count == 0, q.nested_height_count
    print("OK nested_height_ignores_exact_duplicates")


def test_nested_height_missing_value_is_never_conforming():
    """Altitude vide d'un côté : paire NON vérifiable, jamais tenue pour saine."""
    _ensure_qgis()
    vl = _height_layer([("AGL", QVariant.Double)], {"AGL": 30.0}, None)
    q = _nested_result(vl).quality_report
    assert q.nested_pairs_unknown == 1, q.nested_pairs_unknown
    assert q.nested_pairs_checked == 0, q.nested_pairs_checked
    assert q.nested_height_count == 0, q.nested_height_count
    print("OK nested_height_missing_value_is_never_conforming")


def test_nested_height_without_fields_is_marked_not_run():
    """Sans champ AGL ni HEIGHT le contrôle ne peut pas tourner.

    Il doit se marquer NON exécuté et le dire — un 0 se lirait comme
    « aucune anomalie » sur une couche jamais examinée.
    """
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, _NEST_OUTER)
    _add(vl, _NEST_INNER)
    res = _nested_result(vl)
    assert res.checks_enabled["nested_height"] is False
    assert res.nested_height_fields == []
    assert any("AGL" in w and "HEIGHT" in w for w in res.warnings), res.warnings
    assert C.CAT_NESTED_HEIGHT not in res.correctness.components
    print("OK nested_height_without_fields_is_marked_not_run")


def test_nested_height_runs_independently_of_overlap_settings():
    """Ni le contrôle des chevauchements ni son exception « polygones inclus »
    ne doivent influer sur ce contrôle : la première retire ces paires des
    ERREURS de chevauchement, elle ne dit rien de leurs altitudes."""
    _ensure_qgis()
    for overlaps, allow in ((False, False), (True, False), (True, True)):
        vl = _height_layer([("AGL", QVariant.Double)],
                           {"AGL": 30.0}, {"AGL": 5.0})
        res = GeomAnalyzer(AnalysisOptions(
            check_nested_height=True, check_overlaps=overlaps,
            allow_contained_polygons=allow)).analyze(vl)
        assert res.quality_report.nested_height_count == 1, (overlaps, allow)
    print("OK nested_height_runs_independently_of_overlap_settings")


def test_nested_height_absent_when_not_requested():
    """Contrôle décoché : aucun compteur, et surtout aucun sous-score à 100 %."""
    _ensure_qgis()
    vl = _height_layer([("AGL", QVariant.Double)], {"AGL": 30.0}, {"AGL": 5.0})
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    assert res.checks_enabled["nested_height"] is False
    assert res.quality_report.nested_height_count == 0
    assert not res.flagged.get(C.CAT_NESTED_HEIGHT)
    assert C.CAT_NESTED_HEIGHT not in res.correctness.components
    print("OK nested_height_absent_when_not_requested")


def test_nested_height_enters_score_and_reports():
    """Le contrôle demandé pèse sur le score et apparaît dans les sorties."""
    _ensure_qgis()
    vl = _height_layer([("AGL", QVariant.Double)], {"AGL": 30.0}, {"AGL": 5.0})
    from stat_geom_qc import report as sg_report
    res = _nested_result(vl)
    assert C.CAT_NESTED_HEIGHT in res.correctness.components
    assert res.correctness.components[C.CAT_NESTED_HEIGHT] < 100.0
    assert res.correctness.score < 100.0
    assert res.total_issues() >= 1
    for txt in (sg_report.build_html(res), sg_report.build_qt_summary(res)):
        assert "Polygones inclus pas assez hauts" in txt
        segment = txt.split("Polygones inclus pas assez hauts")[1][:200]
        assert sg_report.NOT_REQUESTED not in segment, segment
        # Le champ réellement comparé est rappelé : sans lui, un résultat
        # obtenu sur le seul AGL se lirait comme un verdict sur AGL ET HEIGHT.
        assert "(AGL)" in segment, segment
    assert res.to_dict()["nested_height_features"] == 1
    print("OK nested_height_enters_score_and_reports")


def test_nested_height_not_requested_in_reports():
    """Contrôle décoché : les rapports affichent « Non demandé », pas 0."""
    _ensure_qgis()
    vl = _height_layer([("AGL", QVariant.Double)], {"AGL": 30.0}, {"AGL": 5.0})
    from stat_geom_qc import report as sg_report
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    for txt in (sg_report.build_html(res), sg_report.build_qt_summary(res)):
        segment = txt.split("Polygones inclus pas assez hauts")[1][:200]
        assert sg_report.NOT_REQUESTED in segment, segment
    print("OK nested_height_not_requested_in_reports")


def test_html_report_puts_warnings_right_after_indicators():
    """Les avertissements suivent immédiatement la section « Indicateurs ».

    Ils disent en toutes lettres ce que les compteurs se contentent de
    chiffrer ; relégués en fin de page, ils n'étaient lus qu'après coup.
    """
    import re

    from stat_geom_qc import report as sg_report
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,2 2,2 0,0 2,0 0))")     # invalide -> au moins 1 avert.
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    assert res.warnings, "le cas de test doit produire un avertissement"

    titres = re.findall(r"<h2>(.*?)</h2>", sg_report.build_html(res))
    i_ind = next(i for i, t in enumerate(titres) if "Indicateurs" in t)
    i_avt = next(i for i, t in enumerate(titres) if "Avertissements" in t)
    assert i_avt == i_ind + 1, titres
    print("OK html_report_puts_warnings_right_after_indicators")


def test_degenerate_intersection_counts_as_filiform():
    """Une intersection SANS partie surfacique est le cas limite du filiforme.

    Sur du bâti mitoyen réel, GEOS réduit l'intersection à une ligne dès que le
    recouvrement tient dans le bruit du flottant : ``touches`` est faux (les
    intérieurs se recoupent d'un milliardième de millimètre carré) mais
    l'overlay ne forme plus de polygone. Traiter ces cas comme des conflits
    COMPACTS leur refusait l'imputation à un sommet manquant, et les faisait
    signaler malgré l'option cochée — 665 paires sur 1177 dans le jeu de test
    de Sydney.
    """
    from stat_geom_qc.analysis_engine import _overlap_is_filiform
    _ensure_qgis()
    assert _overlap_is_filiform(None, 0.25) is True
    vide = QgsGeometry.fromWkt("POLYGON((0 0,1 0,1 0,0 0,0 0))")   # aire nulle
    assert _overlap_is_filiform(vide, 0.25) is True
    compact = QgsGeometry.fromWkt("POLYGON((0 0,0 1,1 1,1 0,0 0))")
    assert _overlap_is_filiform(compact, 0.25) is False
    print("OK degenerate_intersection_counts_as_filiform")


def test_missing_vertex_rule_applies_without_measurable_surface():
    """La règle « sommet manquant » doit être évaluée même sans surface.

    Le sens unique de l'empiètement reste le seul juge : sans surface mesurable,
    la condition de forme est acquise, pas contournée.
    """
    from stat_geom_qc.analysis_engine import overlap_from_missing_vertex
    _ensure_qgis()
    # A porte un sommet à l'intérieur de B ; B n'en a aucun dans A.
    a = QgsGeometry.fromWkt("POLYGON((0 0,0 10,5 10,5.001 5,5 0,0 0))")
    b = QgsGeometry.fromWkt("POLYGON((5 0,5 10,10 10,10 0,5 0))")
    assert overlap_from_missing_vertex(a, b, None, 0.25) is True
    # Empiètement des DEUX côtés : conflit géométrique, jamais imputé.
    c = QgsGeometry.fromWkt("POLYGON((5 0,4.999 5,5 10,10 10,10 0,5 0))")
    assert overlap_from_missing_vertex(a, c, None, 0.25) is False
    # Option décochée (ratio 0) : rien n'est jamais imputé.
    assert overlap_from_missing_vertex(a, b, None, 0.0) is False
    print("OK missing_vertex_rule_applies_without_measurable_surface")


def test_degenerate_overlap_still_flagged_when_option_off():
    """Le correctif ne doit RIEN taire quand l'option est décochée.

    La règle reste « intersects et non touches », sans tolérance : un
    recouvrement infime est une erreur tant que l'utilisateur n'a pas demandé
    d'écarter les sommets manquants.
    """
    from stat_geom_qc.analysis_engine import (OVERLAP_MISSING_VERTEX,
                                              OVERLAP_PARTIAL,
                                              classify_overlap)
    _ensure_qgis()
    a = QgsGeometry.fromWkt("POLYGON((0 0,0 10,5 10,5.001 5,5 0,0 0))")
    b = QgsGeometry.fromWkt("POLYGON((5 0,5 10,10 10,10 0,5 0))")
    assert classify_overlap(a, b, 0.0) == OVERLAP_PARTIAL
    assert classify_overlap(a, b, C.MISSING_VERTEX_RATIO_DEFAULT) \
        == OVERLAP_MISSING_VERTEX
    print("OK degenerate_overlap_still_flagged_when_option_off")


def test_overlap_point_falls_back_to_linear_intersection():
    """Sans partie surfacique, la couche d'erreurs doit tout de même pointer
    la zone en litige : un point de l'intersection linéaire la situe."""
    from stat_geom_qc.analysis_engine import classify_overlap_detailed
    _ensure_qgis()
    a = QgsGeometry.fromWkt("POLYGON((0 0,0 10,5 10,5.001 5,5 0,0 0))")
    b = QgsGeometry.fromWkt("POLYGON((5 0,5 10,10 10,10 0,5 0))")
    _kind, xy = classify_overlap_detailed(a, b, 0.0)
    assert xy is not None, "aucun point de localisation"
    assert 4.9 <= xy[0] <= 5.2, xy
    print("OK overlap_point_falls_back_to_linear_intersection (xy=%.4f,%.4f)" % xy)


def test_metadata_parses_like_plugins_qgis_org():
    """``metadata.txt`` doit survivre au parseur de plugins.qgis.org.

    Le site lit le fichier avec ``configparser`` et son interpolation par
    défaut : tout ``%`` isolé y devient une erreur de syntaxe — et elle ne se
    déclenche qu'à la LECTURE d'une valeur, pas au chargement du fichier. Un
    « 97 % de bruit » glissé dans le changelog a ainsi fait échouer un envoi de
    la 2.9.31 alors que le paquet était par ailleurs valide.

    Le test rejoue donc le parcours complet : chargement, puis accès à chaque
    valeur de chaque section.
    """
    import configparser
    import os
    import re

    chemin = os.path.join(_PLUGIN_DIR, "metadata.txt")
    parser = configparser.ConfigParser()          # interpolation par défaut
    with open(chemin, encoding="utf-8") as fh:
        parser.read_file(fh)
    for section in parser.sections():
        for cle in parser[section]:
            parser[section][cle]                  # peut lever Interpolation*

    assert parser.has_section("general")
    general = parser["general"]
    for cle in ("name", "qgisMinimumVersion", "description", "about", "version",
                "author", "email", "repository", "tracker", "homepage",
                "changelog"):
        assert general.get(cle), "champ obligatoire vide ou absent : %s" % cle

    # Le champ n'expose QUE la version courante (règle du dépôt) : aucune
    # autre entrée de version ne doit y réapparaître.
    version = general["version"]
    changelog = general["changelog"]
    assert changelog.lstrip().startswith(version), changelog[:80]

    # Une SEULE en-tête de version. Sans ce garde-fou le champ dérive : à force
    # de consolider des versions non publiées les unes sous les autres, il avait
    # atteint 7 868 caractères et rassemblait six versions, là où le dépôt
    # impose de ne présenter que la dernière.
    entetes = [ligne.strip() for ligne in changelog.splitlines()
               if re.fullmatch(r"\d+\.\d+\.\d+", ligne.strip())]
    assert entetes == [version], entetes
    assert len(changelog) < 5000, (
        "changelog de %d caractères : il recommence à accumuler" % len(changelog))

    # Un pourcent isolé ferait échouer le téléversement (le site interpole).
    assert "%%" in changelog or "%" not in changelog, \
        "pourcent non échappé dans le changelog"
    print("OK metadata_parses_like_plugins_qgis_org (v%s, changelog %d car.)"
          % (version, len(changelog)))


def test_overlap_pass_skipped_when_check_is_off():
    """Le contrôle des chevauchements ne doit RIEN produire quand il est décoché.

    Régression de la 2.9.29 : l'index spatial est aussi construit pour le
    contrôle de hauteur des polygones inclus, et le lancement de la passe des
    chevauchements ne testait que la présence de cet index. Cocher « Hauteur des
    polygones inclus » en ayant décoché « Chevauchements » exécutait donc la
    passe la plus coûteuse, remplissait les compteurs, les avertissements, la
    couche d'erreurs et jusqu'à la case « rogner les chevauchements mineurs » du
    dialogue de réparation — pour un contrôle explicitement refusé.
    """
    _ensure_qgis()
    vl = _make_layer(fields=[("AGL", QVariant.Double)])
    _add(vl, "POLYGON((0 0,0 100,100 100,100 0,0 0))", {"AGL": 12.0})
    _add(vl, "POLYGON((10 10,10 20,20 20,20 10,10 10))", {"AGL": 30.0})
    _add(vl, "POLYGON((200 200,200 210,210 210,210 200,200 200))", {"AGL": 5.0})
    _add(vl, "POLYGON((205 205,205 215,215 215,215 205,205 205))", {"AGL": 5.0})

    res = GeomAnalyzer(AnalysisOptions(
        check_nested_height=True, check_overlaps=False)).analyze(vl)
    q = res.quality_report
    assert res.checks_enabled["overlap"] is False
    assert q.overlap_count == 0, q.overlap_count
    assert q.overlap_pairs == 0, q.overlap_pairs
    assert q.contained_pairs == 0, q.contained_pairs
    assert not res.flagged.get(C.CAT_OVERLAP), res.flagged.get(C.CAT_OVERLAP)
    assert not res.overlap_points, res.overlap_points
    assert res.to_dict()["overlapping_features"] == 0
    assert not [w for w in res.warnings
                if "chevauch" in w.lower() or "imbriqu" in w.lower()], res.warnings
    # Le contrôle réellement demandé, lui, a bien tourné.
    assert res.checks_enabled["nested_height"] is True
    assert q.nested_pairs_checked == 1, q.nested_pairs_checked
    print("OK overlap_pass_skipped_when_check_is_off")


def test_same_footprint_is_never_a_nested_height_anomaly():
    """Deux tracés du MÊME contour ne sont pas une imbrication.

    ``equals()`` compare la liste des sommets : un sommet colinéaire de plus
    suffisait à faire échouer la garde, et comme une valeur n'est jamais
    strictement supérieure à elle-même, l'anomalie de hauteur était
    SYSTÉMATIQUE. Le contrôle des doublons ne rattrapait pas le cas — il hache
    le WKB normalisé, qui diffère. Le critère est donc le containment MUTUEL.
    """
    from stat_geom_qc.analysis_engine import containment_pair
    _ensure_qgis()
    a = QgsGeometry.fromWkt("POLYGON((0 0,0 10,10 10,10 0,0 0))")
    b = QgsGeometry.fromWkt("POLYGON((0 0,0 5,0 10,10 10,10 0,0 0))")
    assert a.equals(b) is False, "le cas de test doit échapper à equals()"
    assert a.within(b) and b.within(a), "même emprise attendue"
    assert containment_pair(a, b, 1, 2) == (None, None)

    vl = _make_layer(fields=[("AGL", QVariant.Double)])
    _add(vl, "POLYGON((0 0,0 10,10 10,10 0,0 0))", {"AGL": 12.0})
    _add(vl, "POLYGON((0 0,0 5,0 10,10 10,10 0,0 0))", {"AGL": 12.0})
    q = GeomAnalyzer(AnalysisOptions(
        check_nested_height=True, check_overlaps=False)).analyze(vl).quality_report
    assert q.nested_pairs_checked == 0, q.nested_pairs_checked
    assert q.nested_height_count == 0, q.nested_height_count

    # Une imbrication RÉELLE reste détectée : un seul sens de within.
    vl2 = _make_layer(fields=[("AGL", QVariant.Double)])
    _add(vl2, "POLYGON((0 0,0 100,100 100,100 0,0 0))", {"AGL": 30.0})
    _add(vl2, "POLYGON((10 10,10 20,20 20,20 10,10 10))", {"AGL": 5.0})
    q2 = GeomAnalyzer(AnalysisOptions(
        check_nested_height=True, check_overlaps=False)).analyze(vl2).quality_report
    assert q2.nested_height_count == 1, q2.nested_height_count
    print("OK same_footprint_is_never_a_nested_height_anomaly")


def test_memory_layers_keep_a_crs_without_authid():
    """Un CRS sans code EPSG ne doit pas être remplacé par WGS 84.

    L'URI du fournisseur mémoire ne transporte qu'un code d'autorité. Un CRS lu
    depuis un WKT que PROJ ne rattache à aucun EPSG a un ``authid()`` vide, et
    la couche mémoire héritait alors silencieusement d'EPSG:4326 : des
    coordonnées de grille locale écrites comme du WGS 84.
    """
    from qgis.core import QgsCoordinateReferenceSystem
    _ensure_qgis()
    wkt = ('PROJCS["Grille_locale_test",GEOGCS["GCS_WGS_1984",'
           'DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
           'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
           'PROJECTION["Transverse_Mercator"],PARAMETER["False_Easting",500000.0],'
           'PARAMETER["False_Northing",0.0],PARAMETER["Central_Meridian",3.7],'
           'PARAMETER["Scale_Factor",0.9999],PARAMETER["Latitude_Of_Origin",0.0],'
           'UNIT["Meter",1.0]]')
    crs = QgsCoordinateReferenceSystem.fromWkt(wkt)
    if not crs.isValid() or crs.authid():
        print("OK memory_layers_keep_a_crs_without_authid (cas indisponible ici)")
        return

    vl = QgsVectorLayer("Polygon", "src", "memory")
    vl.setCrs(crs)
    _add(vl, "POLYGON((0 0,0 10,10 10,10 0,0 0))")
    res = GeomAnalyzer(AnalysisOptions(topology_checks=False)).analyze(vl)
    mem, _stats = GeomAnalyzer.build_repaired_layer(
        vl, res, RepairOptions(fix_invalid=False, remove_null_empty=True))
    assert mem.crs().isValid(), "la couche réparée a perdu son CRS"
    assert mem.crs() == crs, (mem.crs().description(), crs.description())
    assert mem.crs().authid() != "EPSG:4326", "CRS silencieusement remplacé"
    print("OK memory_layers_keep_a_crs_without_authid")


def test_stored_options_are_detectable():
    """``has_stored_options`` distingue un profil vierge d'un profil déjà utilisé.

    C'est ce qui empêche le repli sur « Standard » d'écraser un choix mémorisé.
    Les contrôles opt-in ne sont jamais restaurés actifs, alors que « Standard »
    et « Approfondi » en exigent plusieurs depuis la 2.9.32 : l'état restauré ne
    peut donc plus égaler ces deux préréglages, et un repli inconditionnel se
    déclenchait à presque chaque ouverture, réécrivant les cases réellement
    mémorisées.

    Le test s'arrête à la frontière que ``settings.py`` expose ; le panneau, lui,
    exige une session graphique et n'est pas exerçable dans cette suite.
    """
    _ensure_qgis()
    from qgis.core import QgsSettings
    from stat_geom_qc import settings as sg_settings

    cle = "%s/%s" % (C.SETTINGS_GROUP, C.SK_TOPOLOGY)
    s = QgsSettings()
    backup = s.value(cle, None)
    try:
        s.remove(cle)
        assert sg_settings.has_stored_options() is False, \
            "profil vierge attendu après suppression de la clé"
        sg_settings.save_options(AnalysisOptions(check_duplicates=False))
        assert sg_settings.has_stored_options() is True, \
            "save_options doit rendre le profil « déjà utilisé »"
    finally:
        s2 = QgsSettings()
        if backup is None:
            s2.remove(cle)
        else:
            s2.setValue(cle, backup)
    print("OK stored_options_are_detectable")


def test_error_layer_point_cap_shares_the_engine_constant():
    """Le plafond de points par entité de la couche d'erreurs doit être CELUI du
    moteur : deux littéraux 25 en vis-à-vis se désynchronisent au premier
    réglage de l'un des deux."""
    # Le plafond est désormais un DÉFAUT de la fonction partagée, qui retombe
    # sur la constante du moteur : une seule source de vérité, plus aucun
    # littéral 25 en vis-à-vis qui puisse se désynchroniser.
    _ensure_qgis()
    import inspect
    import io as _io
    import os as _os
    from stat_geom_qc import error_layer as EL
    signature = inspect.signature(EL.build_error_features)
    # ``None`` comme défaut, résolu vers la constante DANS le corps : c'est ce
    # qui garantit qu'un réglage de la constante prenne effet partout, sans
    # qu'un défaut figé dans la signature le contredise.
    assert signature.parameters["max_per_feature"].default is None
    assert signature.parameters["cap"].default is None
    source = _io.open(_os.path.join(_PLUGIN_DIR, "error_layer.py"),
                      encoding="utf-8").read()
    assert "cap = C.ERROR_LAYER_CAP" in source
    assert "max_per_feature = C.OVERLAP_POINTS_PER_FEATURE" in source
    print("OK error_layer_point_cap_shares_the_engine_constant")


# ── Fiabilité du score : couverture, cas non concluants, traçabilité ────────

def _layer_two_clean_polygons():
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,0 10,10 10,10 0,0 0))")
    _add(vl, "POLYGON((50 50,50 60,60 60,60 50,50 50))")
    return vl


def _layer_nested_pair_without_heights():
    """Une imbrication RÉELLE dont aucune altitude n'est lisible.

    C'est le seul cas où le contrôle de hauteur tourne SANS conclure : la
    paire existe, elle est visitée, mais les deux altitudes sont vides. Une
    couche sans aucune imbrication ne relève PAS de ce cas — le contrôle y
    conclut « rien à trouver » (voir
    test_absence_of_nested_pairs_is_a_conclusion_not_an_ignorance).
    """
    vl = _make_layer(fields=[("AGL", QVariant.Double)])
    _add(vl, "POLYGON((0 0,0 100,100 100,100 0,0 0))", {"AGL": None})
    _add(vl, "POLYGON((10 10,10 20,20 20,20 10,10 10))", {"AGL": None})
    return vl


def test_coverage_counts_only_checks_that_concluded():
    """La couverture dit sur COMBIEN de dimensions le score est assis.

    Un score de 100 sur quatre contrôles aboutis et un score de 100 sur onze ne
    valent pas la même chose ; rien ne le laissait voir.
    """
    _ensure_qgis()
    res = GeomAnalyzer(AnalysisOptions()).analyze(_layer_two_clean_polygons())
    c = res.correctness
    assert c.coverage_total == len(res.checks_enabled), c.coverage_total
    attendu = sum(1 for actif in res.checks_enabled.values() if actif)
    assert c.coverage_ran == attendu, (c.coverage_ran, attendu)
    assert 0 < c.coverage_pct() < 100, c.coverage_pct()

    # Tout coché sur une couche qui s'y prête : couverture complète.
    vl = _make_layer(fields=[("AGL", QVariant.Double)])
    _add(vl, "POLYGON((0 0,0 100,100 100,100 0,0 0))", {"AGL": 10.0})
    _add(vl, "POLYGON((10 10,10 20,20 20,20 10,10 10))", {"AGL": 20.0})
    res2 = GeomAnalyzer(AnalysisOptions(
        topology_checks=True, check_overlaps=True, check_duplicates=True,
        check_small_polygons=True, check_duplicate_vertex=True,
        check_holes=True, check_sharp_angles=True, check_close_vertices=True,
        check_nested_height=True, check_line_crossings=True)).analyze(vl)
    # Couche exclusivement polygonale : le contrôle de croisement de lignes ne
    # peut matériellement pas conclure, quelle que soit la demande — seule
    # dimension qui plafonne la couverture en dessous de 100 pour cent ici.
    total = len(res2.checks_enabled)
    attendu_pct = round(100.0 * (total - 1) / total, 1)
    assert res2.correctness.coverage_pct() == attendu_pct, res2.correctness.coverage_pct()
    print("OK coverage_counts_only_checks_that_concluded")


def test_inconclusive_check_is_excluded_and_forbids_a_perfect_score():
    """Un contrôle qui tourne SANS conclure ne peut pas valoir 100 pour cent.

    Cas reproduit : le contrôle de hauteur est demandé sur une couche qui
    CONTIENT une imbrication, mais dont les deux altitudes sont vides — la
    paire est visitée, aucune comparaison n'est possible. Son compteur reste à
    0, mais ce 0 est une ignorance, pas un satisfecit : il ne doit ni produire
    un sous-score parfait, ni laisser la note globale atteindre 100.
    """
    _ensure_qgis()
    res = GeomAnalyzer(AnalysisOptions(
        check_nested_height=True)).analyze(_layer_nested_pair_without_heights())
    q = res.quality_report
    assert q.nested_pairs_checked == 0, q.nested_pairs_checked
    assert q.nested_pairs_unknown > 0, "la paire imbriquée doit avoir été VUE"
    assert q.nested_height_count == 0

    assert res.checks_inconclusive.get(C.CAT_NESTED_HEIGHT) is True
    assert res.checks_enabled["nested_height"] is True, \
        "le contrôle a bien tourné : ce n'est pas un « non demandé »"
    assert C.CAT_NESTED_HEIGHT not in res.correctness.components, \
        "un contrôle sans conclusion ne doit pas produire de sous-score"
    assert res.correctness.score < 100.0, res.correctness.score
    assert C.CAT_NESTED_HEIGHT in res.correctness.inconclusive
    # Et il ne compte pas dans la couverture.
    assert res.correctness.coverage_pct() < 100.0
    print("OK inconclusive_check_is_excluded_and_forbids_a_perfect_score")


def test_reports_distinguish_not_requested_from_not_conclusive():
    """« Non demandé » et « Non concluant » ne doivent pas se confondre.

    Le premier dit « je ne l'ai pas voulu », le second « je n'ai pas pu
    répondre ». Les deux valent mieux qu'un 0, mais ils ne disent pas la même
    chose et l'utilisateur n'agit pas pareil.
    """
    from stat_geom_qc import report as sg_report
    _ensure_qgis()
    # Hauteur demandée mais sans conclusion (imbrication réelle, altitudes
    # vides) ; trous NON demandés.
    res = GeomAnalyzer(AnalysisOptions(
        check_nested_height=True,
        check_holes=False)).analyze(_layer_nested_pair_without_heights())

    for texte in (sg_report.build_html(res), sg_report.build_qt_summary(res)):
        seg_haut = texte.split("Polygones inclus pas assez hauts")[1][:220]
        assert sg_report.NOT_CONCLUSIVE in seg_haut, seg_haut
        assert sg_report.NOT_REQUESTED not in seg_haut, seg_haut
        seg_trous = texte.split("Trous détectés (total)")[1][:220]
        assert sg_report.NOT_REQUESTED in seg_trous, seg_trous
    print("OK reports_distinguish_not_requested_from_not_conclusive")


def test_report_records_profile_checks_and_parameters():
    """Le rapport doit consigner le profil, les contrôles et leurs seuils.

    Sans eux il n'est pas relisible : « 0 petit polygone » ne veut rien dire si
    l'on ne sait plus si le seuil valait 2 ou 25 m², et « 0 chevauchement » ne
    veut rien dire si l'on ne sait plus que les imbrications étaient écartées.
    """
    from stat_geom_qc import report as sg_report
    _ensure_qgis()
    res = GeomAnalyzer(AnalysisOptions(
        profile_name="standard", check_small_polygons=True,
        small_polygon_threshold_m2=25.0,
        allow_contained_polygons=True)).analyze(_layer_two_clean_polygons())

    assert res.profile_name == "standard"
    assert res.options_snapshot["small_polygon_threshold_m2"] == 25.0
    assert res.options_snapshot["allow_contained_polygons"] is True

    lignes = dict(sg_report.configuration_rows(res))
    # Le profil est repris TEL QUEL : les préréglages nommés ont disparu en
    # 2.9.49, seul un appelant programmatique peut encore nommer le sien.
    assert lignes["Profil"] == "standard", lignes["Profil"]
    assert lignes["Seuil petits polygones"] == "25 m²", lignes
    assert lignes["Polygones entièrement inclus"] == "Ignorés", lignes

    for texte in (sg_report.build_html(res), sg_report.build_qt_summary(res)):
        assert "Configuration de l'analyse" in texte
        assert "25 m²" in texte, "le seuil réellement appliqué doit figurer"

    # Export machine : mêmes informations, exploitables par un script.
    d = res.to_dict()
    assert d["profile_name"] == "standard"
    assert d["options_snapshot"]["small_polygon_threshold_m2"] == 25.0
    assert d["correctness"]["coverage_total"] == len(res.checks_enabled)
    assert "coverage_pct" in d["correctness"]
    print("OK report_records_profile_checks_and_parameters")


def test_report_omits_the_profile_line_when_there_is_no_profile():
    """Sans nom de profil, pas de ligne « Profil » — et rien d'inventé.

    Le rapport écrivait « Réglages manuels », ce qui n'avait de sens que face
    aux trois préréglages nommés. Ceux-ci ont disparu en 2.9.49 : la mention
    n'opposerait plus rien à rien. Ce qui a réellement tourné se lit contrôle
    par contrôle dans le tableau, qui écrit « Exécuté » ou « Non demandé ».
    """
    from stat_geom_qc import report as sg_report
    _ensure_qgis()
    res = GeomAnalyzer(AnalysisOptions()).analyze(_layer_two_clean_polygons())
    assert res.profile_name == ""
    lignes = dict(sg_report.configuration_rows(res))
    assert "Profil" not in lignes, lignes
    assert "Réglages manuels" not in sg_report.build_html(res)
    assert "Réglages manuels" not in sg_report.build_qt_summary(res)
    # Le périmètre, lui, reste annoncé dans tous les cas.
    assert lignes["Zone d'analyse"] == "Couche entière"

    # Contre-épreuve : un appelant programmatique qui NOMME son profil le voit
    # bien consigné, sans traduction ni table de correspondance.
    nomme = GeomAnalyzer(AnalysisOptions(
        profile_name="livraison été 2026")).analyze(_layer_two_clean_polygons())
    assert dict(sg_report.configuration_rows(nomme))["Profil"] == "livraison été 2026"
    print("OK report_omits_the_profile_line_when_there_is_no_profile")


def test_invalid_details_still_in_reports_after_tabs_removal():
    """Ce que l'onglet « Qualité » montrait doit se retrouver DANS les rapports.

    Le retrait des onglets (2.9.49) devait DÉPLACER l'information, pas la faire
    disparaître. Vérifié dans les DEUX rendus — celui du navigateur et celui de
    Qt, qui produit le PDF.

    Les anomalies entité par entité et l'aperçu des attributs, un temps ajoutés
    aux rapports pour la même raison, en ont été retirés ensuite (toujours en
    2.9.49) : ce test vérifie donc aussi qu'ils n'y reviennent pas par
    inadvertance.
    """
    _ensure_qgis()
    from stat_geom_qc import report as sg_report

    vl = _make_layer(fields=[("nom", QVariant.String)])
    _add(vl, "POLYGON((0 0,2 2,2 0,0 2,0 0))", {"nom": "papillon"})
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    assert res.quality_report.invalid_details, "le cas de test doit être invalide"

    for rendu in (sg_report.build_html(res), sg_report.build_qt_summary(res)):
        assert "Détails d'invalidité" in rendu
        assert "Self-intersection" in rendu, "le détail réel doit figurer"
        assert "Anomalies par entité" not in rendu
        assert "Aperçu des attributs" not in rendu

    # Le détail d'invalidité garde ses TROIS états : décocher les contrôles
    # topologiques ne doit pas se lire comme « aucune invalidité ».
    sans = GeomAnalyzer(AnalysisOptions(topology_checks=False)).analyze(vl)
    etat, _details = sg_report.invalid_details_state(sans)
    assert etat == "not_requested", etat
    for rendu in (sg_report.build_html(sans), sg_report.build_qt_summary(sans)):
        segment = rendu.split("Détails d'invalidité")[1][:400]
        assert sg_report.NOT_REQUESTED in segment, segment

    for parti in ("anomaly_rows", "preview_rows"):
        assert not hasattr(sg_report, parti), parti
    for parti in ("REPORT_ANOMALY_CAP", "REPORT_PREVIEW_ROWS"):
        assert not hasattr(C, parti), parti
    print("OK invalid_details_still_in_reports_after_tabs_removal")


def test_pdf_report_is_laid_out_at_the_output_resolution():
    """Non-régression du rapport PDF : taille PHYSIQUE du texte sur le papier.

    Deux défauts successifs, tous deux invisibles dans le HTML produit :

      * 2.9.50 — aucun tableau ne fixait sa largeur : Qt les réduisait à leur
        contenu (son sous-ensemble HTML4/CSS2 n'a ni flexbox ni grid), le
        document n'occupait que 17 % de la largeur imprimable ;
      * 2.9.51 — ``QTextDocument.print()`` n'attache l'appareil de sortie que
        si le document n'a PAS de taille de page ; comme on lui en donne une,
        Qt mettait tout en page à ~96 ppp pendant que la page était exprimée en
        pixels à 300 ppp. Mesuré : une ligne de 11 pt sortait à 4,3 pt sur le
        papier. Le rapport était lisible à l'écran dans un rendu sur image et
        illisible dans le vrai PDF — d'où une vérification, ici, qui porte sur
        les MILLIMÈTRES de papier et non sur des pixels de document.
    """
    _ensure_qgis()
    import tempfile
    from qgis.PyQt.QtGui import QFontMetricsF, QPdfWriter
    from stat_geom_qc import report as sg_report

    vl = _make_layer()
    _add(vl, "POLYGON((0 0,2 2,2 0,0 2,0 0))")   # invalide : remplit aussi
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)   # « Détails d'invalidité »
    rendu = sg_report.build_qt_summary(res)

    # 1) Chaque tableau annonce sa largeur pleine (défaut 2.9.50).
    nb_tables = rendu.count("<table")
    assert nb_tables >= 3, "le rendu doit contenir plusieurs tableaux"
    assert rendu.count('width="100%"') == nb_tables, (
        "un tableau au moins ne fixe pas sa largeur — il se réduira à son "
        "contenu")

    # 2) Tailles de police en points, jamais en pixels : à 300 ppp, un « px »
    # ne vaut pas la même chose physiquement qu'à l'écran.
    assert "font-size:11px" not in rendu, "reliquat de l'ancienne unité px"
    assert "font-size:11pt" in rendu

    # 3) Le document est mis en page POUR la sortie (défaut 2.9.51).
    chemin = os.path.join(tempfile.mkdtemp(), "r.pdf")
    writer = QPdfWriter(chemin)
    # Même configuration de page que write_pdf, et non une copie : un test qui
    # configure la page à sa façon ne verrait jamais un défaut de configuration.
    sg_report.configure_pdf_page(writer)
    writer.setResolution(300)
    doc = sg_report.pdf_document(res, writer)

    appareil = doc.documentLayout().paintDevice()
    assert appareil is not None, (
        "aucun appareil de mise en page : Qt calculera à ~96 ppp pour une "
        "page à 300 ppp, et tout le texte sortira ~3x trop petit")
    assert appareil.logicalDpiY() == writer.resolution(), (
        appareil.logicalDpiY(), writer.resolution())

    # La mesure qui compte : une ligne de 11 pt doit faire ~11 pt sur le
    # papier. Le défaut de la 2.9.51 la rendait à 4,3 pt.
    police = doc.defaultFont()
    police.setPointSizeF(11.0)
    hauteur_px = QFontMetricsF(police, appareil).height()
    hauteur_pt = hauteur_px / writer.resolution() * 72.0
    assert 11.0 <= hauteur_pt <= 20.0, (
        "une ligne de 11 pt est rendue à %.1f pt sur le papier" % hauteur_pt)

    # Et le contenu occupe la largeur de page, pas une fraction.
    ratio = doc.idealWidth() / float(writer.width())
    assert ratio > 0.9, (
        "le contenu n'occupe que %.0f %% de la largeur de page" % (ratio * 100))

    # Enfin, le PDF réellement écrit tient sur plusieurs pages : à la bonne
    # échelle, ce rapport ne peut PAS tenir sur une seule page A4 — c'était
    # le symptôme du texte minuscule.
    sg_report.write_pdf(res, chemin)
    assert os.path.getsize(chemin) > 8000
    assert doc.pageCount() >= 2, (
        "document sur %d page(s) : le texte est probablement trop petit"
        % doc.pageCount())
    print("OK pdf_report_is_laid_out_at_the_output_resolution")


def test_pdf_page_setup_survives_pyqt_bindings_that_hide_overloads():
    """L'export PDF ne doit pas dépendre de la surcharge exposée par PyQt.

    Défaut de la 2.9.51, signalé sous QGIS : « setPageSize(self,
    QPagedPaintDevice.PageSize): argument 1 has unexpected type 'QPageSize' ».
    Certaines compilations de PyQt5 redéfinissent ``QPdfWriter.setPageSize``
    avec la seule énumération historique, masquant la surcharge ``QPageSize``.
    La suite tournait sous une liaison qui accepte les deux : elle ne pouvait
    pas le voir. On simule donc les liaisons restrictives, une par une.
    """
    _ensure_qgis()
    import tempfile
    from qgis.PyQt.QtGui import QPageSize, QPdfWriter
    from stat_geom_qc import report as sg_report

    dossier = tempfile.mkdtemp()

    # 1) Liaison normale : la voie sans surcharge ambiguë est retenue.
    w = QPdfWriter(os.path.join(dossier, "a.pdf"))
    assert sg_report.configure_pdf_page(w) == "setPageLayout"
    assert w.pageLayout().pageSize().id() == QPageSize.PageSizeId.A4
    assert abs(w.pageLayout().margins().left() - 12.0) < 0.01

    # 2) Liaison qui refuse QPageSize ET n'expose pas setPageLayout : le
    # repli sur l'énumération historique doit donner la même page.
    class LiaisonRestrictive(object):
        def __init__(self, vrai):
            self._vrai = vrai
            self.appels = []

        def setPageSize(self, taille):
            if isinstance(taille, QPageSize):
                raise TypeError("setPageSize(self, QPagedPaintDevice.PageSize):"
                                " argument 1 has unexpected type 'QPageSize'")
            self.appels.append("enum")
            return self._vrai.setPageSize(QPageSize(QPageSize.PageSizeId.A4))

        def setPageMargins(self, marges, unite):
            self.appels.append("marges")
            return self._vrai.setPageMargins(marges, unite)

    vrai = QPdfWriter(os.path.join(dossier, "b.pdf"))
    faux = LiaisonRestrictive(vrai)
    assert sg_report.configure_pdf_page(faux) == "setPageSize(enum)"
    assert faux.appels == ["enum", "marges"], faux.appels
    assert vrai.pageLayout().pageSize().id() == QPageSize.PageSizeId.A4

    # 3) Et write_pdf lui-même n'appelle plus setPageSize avec un QPageSize.
    import io as _io
    source = _io.open(os.path.join(_PLUGIN_DIR, "report.py"), encoding="utf-8").read()
    corps = source.split("def write_pdf(")[1].split("\ndef ")[0]
    assert "setPageSize(" not in corps, "write_pdf doit passer par configure_pdf_page"
    print("OK pdf_page_setup_survives_pyqt_bindings_that_hide_overloads")


def test_coverage_text_names_the_checks_without_conclusion():
    """La phrase de couverture doit NOMMER les contrôles restés sans réponse."""
    from stat_geom_qc import report as sg_report
    _ensure_qgis()
    res = GeomAnalyzer(AnalysisOptions(
        check_nested_height=True)).analyze(_layer_nested_pair_without_heights())
    texte = sg_report.coverage_text(res)
    assert "Couverture" in texte, texte
    assert "Sans conclusion" in texte, texte
    assert C.CATEGORY_LABELS[C.CAT_NESTED_HEIGHT] in texte, texte
    print("OK coverage_text_names_the_checks_without_conclusion")


# ── Lisibilité de la couche d'erreurs et inspection ─────────────────────────

def test_error_categories_are_fully_declared():
    """Chaque catégorie géométrique doit avoir une couleur ET un rang de gravité.

    Une catégorie oubliée dans l'une des deux tables passerait inaperçue : elle
    hériterait de la couleur de repli et se retrouverait indiscernable des
    autres sur la carte, ou serait classée en dernier sans raison.
    """
    manquantes_couleur = [c for c in C.GEOM_CATEGORIES
                          if c not in C.ERROR_COLORS]
    manquantes_rang = [c for c in C.GEOM_CATEGORIES
                       if c not in C.CATEGORY_PRIORITY]
    assert not manquantes_couleur, manquantes_couleur
    assert not manquantes_rang, manquantes_rang
    # Chaque couleur est un triplet RVB exploitable.
    for cle, valeur in C.ERROR_COLORS.items():
        composantes = [int(x) for x in valeur.split(",")]
        assert len(composantes) == 3, (cle, valeur)
        assert all(0 <= x <= 255 for x in composantes), (cle, valeur)
    # Les couleurs sont DISTINCTES : deux catégories de même teinte ne se
    # distingueraient pas sur la carte, ce qui annule l'intérêt du rendu.
    assert len(set(C.ERROR_COLORS.values())) == len(C.ERROR_COLORS)
    print("OK error_categories_are_fully_declared")


def test_primary_category_picks_the_most_severe():
    """Un point cumulant plusieurs anomalies est classé sur la plus grave.

    Déterministe et documenté : un « premier arrivé » dépendrait de l'ordre
    d'itération des catégories, et la même donnée changerait de couleur d'une
    version à l'autre.
    """
    _ensure_qgis()
    # Source unique de la règle : le raccourci qu'en donnait le panneau a
    # disparu avec l'onglet « Anomalies » (2.9.49). La couche d'erreurs et la
    # liste des rapports s'appuient toutes deux sur cette fonction.
    from stat_geom_qc.error_layer import primary_category as pc
    assert pc([C.CAT_DUPLICATE, C.CAT_OVERLAP]) == C.CAT_OVERLAP
    # L'auto-intersection est plus précise que « invalide » : elle gagne.
    assert pc([C.CAT_INVALID, C.CAT_SELF_INTERSECTION]) == C.CAT_SELF_INTERSECTION
    assert pc([C.CAT_CLOSE_VERTEX, C.CAT_HOLE]) == C.CAT_HOLE
    assert pc([]) == ""
    # Une catégorie inconnue ne fait pas échouer le classement.
    assert pc(["inconnue"]) == "inconnue"
    print("OK primary_category_picks_the_most_severe")


def test_error_layer_is_categorized_by_error_type():
    """La couche d'erreurs porte un rendu CATÉGORISÉ, pas un symbole unique.

    Un point rouge partout disait « il y a un problème » sans dire lequel : sur
    une couche mêlant chevauchements, doublons et sommets, il fallait cliquer
    chaque point. Le rendu ne déclare que les catégories PRÉSENTES, dans l'ordre
    de gravité, pour que la légende reste courte et se lise de haut en bas.
    """
    from qgis.core import QgsCategorizedSymbolRenderer
    _ensure_qgis()
    from stat_geom_qc.stat_geom_dockwidget import StatGeomDockWidget as D

    mem = QgsVectorLayer("Point?crs=EPSG:2154", "QC erreurs", "memory")
    pr = mem.dataProvider()
    pr.addAttributes([QgsField("src_fid", QVariant.LongLong),
                      QgsField("qc_error", QVariant.String),
                      QgsField(C.ERROR_CLASS_FIELD, QVariant.String),
                      QgsField("qc_detail", QVariant.String)])
    mem.updateFields()
    presentes = [C.CAT_OVERLAP, C.CAT_DUPLICATE, C.CAT_CLOSE_VERTEX,
                 C.CAT_SELF_INTERSECTION]
    for i, cle in enumerate(presentes):
        f = QgsFeature(mem.fields())
        f.setGeometry(QgsGeometry.fromWkt("POINT(%d 0)" % i))
        f.setAttributes([i, C.CATEGORY_LABELS[cle], cle, ""])
        pr.addFeatures([f])
    mem.updateExtents()

    D._style_error_layer(mem, label=False)
    renderer = mem.renderer()
    assert isinstance(renderer, QgsCategorizedSymbolRenderer), type(renderer)
    assert renderer.classAttribute() == C.ERROR_CLASS_FIELD

    valeurs = [c.value() for c in renderer.categories()]
    assert set(valeurs) == set(presentes), valeurs
    assert len(valeurs) == len(presentes), "aucune catégorie vide ne doit figurer"
    # Ordre de gravité décroissante.
    rang = {cle: i for i, cle in enumerate(C.CATEGORY_PRIORITY)}
    assert valeurs == sorted(valeurs, key=lambda v: rang[v]), valeurs
    # Chaque catégorie porte le libellé humain, pas la clé technique.
    for cat in renderer.categories():
        assert cat.label() == C.CATEGORY_LABELS[cat.value()], cat.label()
    # Et la couleur déclarée pour ce type.
    for cat in renderer.categories():
        attendue = tuple(int(x) for x in C.ERROR_COLORS[cat.value()].split(","))
        couleur = cat.symbol().color()
        assert (couleur.red(), couleur.green(), couleur.blue()) == attendue, \
            (cat.value(), couleur.name())
    print("OK error_layer_is_categorized_by_error_type")


def test_error_layer_carries_both_the_list_and_the_class():
    """``qc_error`` garde la LISTE lisible, ``qc_class`` porte la clé de rendu.

    Catégoriser sur ``qc_error`` produirait une classe par COMBINAISON
    d'anomalies (« Chevauchement, Doublon » à part de « Chevauchement »), donc
    une légende qui explose. Les deux champs doivent coexister.
    """
    assert C.ERROR_CLASS_FIELD != "qc_error"
    # La définition des champs vit désormais dans ``error_layer``, partagée entre
    # le panneau et les algorithmes Processing : on interroge la source unique
    # plutôt que de chercher un littéral dans un fichier.
    _ensure_qgis()
    from stat_geom_qc import error_layer as EL
    noms = [f.name() for f in EL.error_layer_fields()]
    assert "qc_error" in noms, noms
    assert C.ERROR_CLASS_FIELD in noms, noms
    print("OK error_layer_carries_both_the_list_and_the_class")


# ── Algorithmes QGIS Processing ─────────────────────────────────────────────

_PROC_BASE = {
    "TOPOLOGY": True, "OVERLAPS": True, "IGNORE_MISSING_VERTEX": True,
    "ALLOW_CONTAINED": True, "DUPLICATES": True, "DUPLICATE_VERTEX": True,
    "SMALL": True, "HOLES": True, "SHARP_ANGLES": False,
    "CLOSE_VERTICES": False, "NESTED_HEIGHT": False,
    "SMALL_THRESHOLD": 1.0, "HOLE_THRESHOLD": 2.0,
    "SHARP_ANGLE_DEG": 30.0, "CLOSE_VERTEX_DIST": 0.2,
}


def _proc_layer_on_disk(dossier):
    """Couche GPKG sur DISQUE : c'est ce que reçoit qgis_process en CLI."""
    from qgis.core import QgsCoordinateTransformContext, QgsVectorFileWriter
    import os as _os
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,2 2,2 0,0 2,0 0))")            # papillon = invalide
    _add(vl, "POLYGON((10 10,10 20,20 20,20 10,10 10))")
    _add(vl, "POLYGON((15 15,15 25,25 25,25 15,15 15))")   # chevauche
    _add(vl, "POLYGON((10 10,10 20,20 20,20 10,10 10))")   # doublon
    chemin = _os.path.join(dossier, "bati.gpkg")
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    QgsVectorFileWriter.writeAsVectorFormatV3(
        vl, chemin, QgsCoordinateTransformContext(), options)
    return chemin


def test_processing_provider_declares_three_algorithms():
    """Le fournisseur expose exactement les trois algorithmes attendus.

    Leurs identifiants sont un contrat : un modèle enregistré par l'utilisateur
    les référence par leur nom, et les renommer casserait ses modèles.
    """
    _ensure_qgis()
    from stat_geom_qc.processing_algs import PROVIDER_ID, StatGeomQCProvider

    prov = StatGeomQCProvider()
    assert prov.id() == PROVIDER_ID == "statgeomqc"
    assert prov.name()
    prov.loadAlgorithms()
    noms = sorted(a.name() for a in prov.algorithms())
    assert noms == ["analyze", "errorlayer", "report"], noms
    for alg in prov.algorithms():
        # createInstance doit rendre une instance NEUVE du même type : Processing
        # s'en sert pour chaque exécution, y compris en traitement par lots.
        clone = alg.createInstance()
        assert type(clone) is type(alg), (type(clone), type(alg))
        assert clone is not alg
        assert alg.displayName() and alg.shortHelpString()
        assert alg.group() and alg.groupId()
    print("OK processing_provider_declares_three_algorithms")


def test_processing_algorithms_stay_threadable():
    """Aucun algorithme ne doit porter FlagNoThreading.

    Décision de conception mesurée : ces algorithmes ne font que LIRE la couche,
    ce qui est sûr depuis un thread de fond — y compris sur une couche vivante
    partagée avec le canevas, vérifié sous accès concurrent. Le drapeau sert à
    protéger les MUTATIONS d'une couche ou du projet ; le poser ici forcerait une
    exécution synchrone et figerait l'interface sans aucun bénéfice.
    """
    _ensure_qgis()
    from qgis.core import QgsProcessingAlgorithm
    from stat_geom_qc.processing_algs import (AnalyzeAlgorithm,
                                              ErrorLayerAlgorithm,
                                              ReportAlgorithm)
    for classe in (AnalyzeAlgorithm, ErrorLayerAlgorithm, ReportAlgorithm):
        alg = classe()
        assert not (alg.flags() & QgsProcessingAlgorithm.FlagNoThreading), \
            "%s ne doit pas interdire le multithread" % classe.__name__
    print("OK processing_algorithms_stay_threadable")


def test_processing_measurement_context_never_reads_the_project():
    """Le contexte de mesure vient du contexte Processing, jamais du projet.

    Sous qgis_process, ``context.project()`` vaut None mais le singleton
    ``QgsProject.instance()`` existe quand même — vide et sans rapport avec les
    données. S'en servir donnerait des surfaces calculées sur le mauvais
    ellipsoïde ; et le lire depuis un thread de fond a déjà provoqué, dans ce
    même moteur, un plantage sans trace Python.
    """
    _ensure_qgis()
    from qgis.core import QgsProcessingContext, QgsProcessingFeedback
    from stat_geom_qc.processing_algs import AnalyzeAlgorithm

    contexte = QgsProcessingContext()
    assert contexte.project() is None, "le test exige un contexte SANS projet"

    alg = AnalyzeAlgorithm()
    alg.initAlgorithm()
    assert alg.prepareAlgorithm({}, contexte, QgsProcessingFeedback()) is True
    mctx = alg._mctx
    assert mctx is not None, "le contexte de mesure doit être capturé"
    # transform_context provient du contexte Processing, et reste utilisable.
    assert mctx.transform_context is not None
    # Ellipsoïde vide -> WGS84, jamais une valeur lue dans un projet étranger.
    assert mctx.usable_ellipsoid() == "WGS84", mctx.usable_ellipsoid()

    # Le module ne doit pas non plus lire le projet en douce. On inspecte
    # l'ARBRE SYNTAXIQUE et non le texte : les docstrings mentionnent
    # légitimement QgsProject pour expliquer pourquoi on ne s'en sert PAS, et
    # une recherche textuelle prendrait cette explication pour un usage.
    import os as _os
    assert not _identifiers_used(
        _os.path.join(_PLUGIN_DIR, "processing_algs.py"), "QgsProject"), \
        "un algorithme Processing ne doit jamais toucher QgsProject"
    print("OK processing_measurement_context_never_reads_the_project")


def test_processing_algorithms_are_usable_without_a_gui():
    """Les algorithmes ne doivent dépendre ni du panneau ni de QtWidgets.

    Sous qgis_process, ``iface`` vaut None et aucune fenêtre n'existe : le
    moindre import du panneau ferait échouer le chargement du plugin en ligne de
    commande — panne signalée dans la nature sur d'autres extensions.
    """
    import io as _io
    import os as _os
    for nom in ("processing_algs.py", "error_layer.py"):
        source = _io.open(_os.path.join(_PLUGIN_DIR, nom), encoding="utf-8").read()
        assert "stat_geom_dockwidget" not in source, nom
        assert "QtWidgets" not in source, nom
        assert "QMessageBox" not in source, nom
        assert "iface" not in source, nom
    print("OK processing_algorithms_are_usable_without_a_gui")


def test_processing_analyze_runs_without_any_project():
    """« Analyser » tourne sans projet ouvert et rend des sorties chaînables."""
    _ensure_qgis()
    import tempfile
    from qgis.core import QgsProcessingContext, QgsProcessingFeedback
    from stat_geom_qc.processing_algs import AnalyzeAlgorithm

    dossier = tempfile.mkdtemp(prefix="sgqc_proc_")
    chemin = _proc_layer_on_disk(dossier)

    alg = AnalyzeAlgorithm()
    alg.initAlgorithm()
    params = dict(_PROC_BASE)
    params["INPUT"] = chemin
    contexte = QgsProcessingContext()
    assert contexte.project() is None
    res, ok = alg.run(params, contexte, QgsProcessingFeedback())
    assert ok, res
    assert res["TOTAL_FEATURES"] == 4, res
    assert res["INVALID"] >= 1, res
    assert res["OVERLAPS"] >= 1, res
    assert res["DUPLICATES"] == 1, res
    assert 0.0 <= res["SCORE"] <= 100.0
    assert res["GRADE"], res
    # La couverture accompagne la note : un score sans son assise trompe.
    assert 0.0 < res["COVERAGE_PCT"] <= 100.0, res
    print("OK processing_analyze_runs_without_any_project (score=%s, couverture=%s)"
          % (res["SCORE"], res["COVERAGE_PCT"]))


def test_processing_error_layer_writes_the_class_field():
    """« Couche des erreurs » écrit les champs attendus, dont la clé de rendu."""
    _ensure_qgis()
    import os as _os
    import tempfile
    from qgis.core import QgsProcessingContext, QgsProcessingFeedback
    from stat_geom_qc.processing_algs import ErrorLayerAlgorithm

    dossier = tempfile.mkdtemp(prefix="sgqc_proc_")
    chemin = _proc_layer_on_disk(dossier)
    sortie = _os.path.join(dossier, "erreurs.gpkg")

    alg = ErrorLayerAlgorithm()
    alg.initAlgorithm()
    params = dict(_PROC_BASE)
    params["INPUT"] = chemin
    params["OUTPUT"] = sortie
    res, ok = alg.run(params, QgsProcessingContext(), QgsProcessingFeedback())
    assert ok, res

    couche = QgsVectorLayer(sortie, "err", "ogr")
    assert couche.isValid(), sortie
    assert couche.featureCount() > 0
    noms = [f.name() for f in couche.fields()]
    for attendu in ("src_fid", "qc_error", C.ERROR_CLASS_FIELD, "qc_detail"):
        assert attendu in noms, (attendu, noms)
    # La clé de rendu est REMPLIE et vaut une catégorie connue : sans elle, la
    # symbologie catégorisée n'aurait rien sur quoi se régler.
    classes = {f[C.ERROR_CLASS_FIELD] for f in couche.getFeatures()}
    assert classes, "aucune catégorie écrite"
    assert classes <= set(C.CATEGORY_PRIORITY), classes
    print("OK processing_error_layer_writes_the_class_field (%s)" % sorted(classes))


def test_processing_report_writes_every_requested_format():
    """« Rapport » écrit les formats demandés, et refuse de ne rien produire."""
    _ensure_qgis()
    import io as _io
    import os as _os
    import tempfile
    from qgis.core import QgsProcessingContext, QgsProcessingFeedback
    from stat_geom_qc.processing_algs import ReportAlgorithm

    dossier = tempfile.mkdtemp(prefix="sgqc_proc_")
    chemin = _proc_layer_on_disk(dossier)

    alg = ReportAlgorithm()
    alg.initAlgorithm()
    params = dict(_PROC_BASE)
    params["INPUT"] = chemin
    params["OUTPUT_HTML"] = _os.path.join(dossier, "r.html")
    params["OUTPUT_PDF"] = _os.path.join(dossier, "r.pdf")
    res, ok = alg.run(params, QgsProcessingContext(), QgsProcessingFeedback())
    assert ok, res
    for cle in ("OUTPUT_HTML", "OUTPUT_PDF"):
        assert _os.path.exists(res[cle]), res
        assert _os.path.getsize(res[cle]) > 0

    # Le PDF doit etre un vrai PDF, pas un fichier vide.
    with open(res["OUTPUT_PDF"], "rb") as _fh:
        assert _fh.read(5).startswith(b"%PDF"), "sortie PDF invalide"

    # Le rapport consigne sa configuration, contrôle par contrôle. Lu dans le
    # HTML, le JSON ayant été retiré en 2.9.48. La ligne « Réglages manuels » a
    # disparu avec les préréglages en 2.9.49 : ce qui a tourné se lit désormais
    # par catégorie, ce qui est plus précis qu'un nom de profil.
    _html_produit = _io.open(res["OUTPUT_HTML"], encoding="utf-8").read()
    assert "Réglages manuels" not in _html_produit
    assert "Configuration de l'analyse" in _html_produit
    assert "Exécuté" in _html_produit, "l'état de chaque contrôle doit figurer"
    assert "1 m²" in _html_produit, "le seuil appliqué doit figurer"

    # Aucun format demandé : l'algorithme doit le dire, pas produire un silence.
    alg2 = ReportAlgorithm()
    alg2.initAlgorithm()
    vides = dict(_PROC_BASE)
    vides["INPUT"] = chemin
    _res2, ok2 = alg2.run(vides, QgsProcessingContext(), QgsProcessingFeedback())
    assert not ok2, "sans sortie demandée, l'algorithme doit échouer clairement"
    print("OK processing_report_writes_every_requested_format")


def test_metadata_declares_the_processing_provider():
    """``hasProcessingProvider=yes`` est indispensable.

    Sans ce drapeau, QGIS ne cherche même pas ``initProcessing`` : les
    algorithmes restent invisibles de qgis_process, sans le moindre message —
    exactement le genre d'échec muet que ce plugin s'interdit ailleurs.
    """
    import configparser
    import io as _io
    import os as _os
    parser = configparser.ConfigParser()
    with _io.open(_os.path.join(_PLUGIN_DIR, "metadata.txt"), encoding="utf-8") as fh:
        parser.read_file(fh)
    assert parser["general"].get("hasProcessingProvider") == "yes"

    # Et le point d'entrée doit exister, sans jamais toucher à l'interface.
    source = _io.open(_os.path.join(_PLUGIN_DIR, "stat_geom_qc.py"),
                      encoding="utf-8").read()
    assert "def initProcessing" in source
    assert "def unloadProcessing" in source
    # Inspection de l'ARBRE SYNTAXIQUE : la docstring d'initProcessing explique
    # justement qu'iface vaut None sans interface, et une recherche textuelle
    # confondrait cette explication avec un usage.
    assert not _attribute_used_in_function(
        _os.path.join(_PLUGIN_DIR, "stat_geom_qc.py"),
        "initProcessing", "iface"), \
        "initProcessing ne doit pas toucher iface : il vaut None sans interface"
    print("OK metadata_declares_the_processing_provider")


def test_panel_delegates_error_layer_to_the_shared_module():
    """Le panneau et les algorithmes partagent UNE construction de couche.

    C'était la seule vraie duplication du paquet : la logique vivait dans le
    panneau, mêlée aux boîtes de dialogue. Deux implémentations divergentes de la
    même règle auraient fini par donner deux couches d'erreurs différentes selon
    qu'on passe par le panneau ou par Processing.
    """
    import io as _io
    import os as _os
    panneau = _io.open(_os.path.join(_PLUGIN_DIR, "stat_geom_dockwidget.py"),
                       encoding="utf-8").read()
    assert "EL.build_error_layer(" in panneau
    assert "EL.style_error_layer(" in panneau
    # Les helpers dupliqués ont bien disparu du panneau.
    for disparu in ("def _error_symbol", "def _categorized_renderer",
                    "def _make_point_feature", "def _validity_error_points"):
        assert disparu not in panneau, disparu
    print("OK panel_delegates_error_layer_to_the_shared_module")


def _identifiers_used(chemin, nom):
    """Le fichier UTILISE-t-il l'identifiant ``nom`` dans son CODE ?

    Analyse l'arbre syntaxique : une mention en docstring ou en commentaire ne
    compte pas. Sans cette distinction, un test interdisant ``QgsProject``
    échouerait sur le commentaire qui explique justement pourquoi on l'évite —
    et la seule façon de le faire passer serait de retirer l'explication, ce qui
    appauvrirait le code au lieu de le protéger.
    """
    import ast
    import io as _io
    arbre = ast.parse(_io.open(chemin, encoding="utf-8").read())
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.Name) and noeud.id == nom:
            return True
        if isinstance(noeud, ast.Attribute) and noeud.attr == nom:
            return True
        if isinstance(noeud, ast.alias) and (noeud.name or "").endswith(nom):
            return True
    return False


def _attribute_used_in_function(chemin, nom_fonction, attribut):
    """L'attribut ``attribut`` est-il lu ou écrit DANS ``nom_fonction`` ?

    Même principe : on regarde le code, pas le texte. La docstring d'une méthode
    peut parfaitement citer l'attribut qu'elle s'interdit de toucher.
    """
    import ast
    import io as _io
    arbre = ast.parse(_io.open(chemin, encoding="utf-8").read())
    for noeud in ast.walk(arbre):
        if not isinstance(noeud, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if noeud.name != nom_fonction:
            continue
        for interne in ast.walk(noeud):
            if isinstance(interne, ast.Attribute) and interne.attr == attribut:
                return True
        return False
    raise AssertionError("fonction %s introuvable dans %s"
                         % (nom_fonction, chemin))


# ── Croisements de lignes non nodés (idée 2) ────────────────────────────────

def test_unnoded_crossing_flags_lines_without_shared_vertex():
    """Une croix (X) sans sommet au point de rencontre : les deux lignes
    DISTINCTES sont signalées, une seule paire comptée."""
    _ensure_qgis()
    vl = _make_layer(geom_type="LineString")
    _add(vl, "LINESTRING(0 5,10 5)")
    _add(vl, "LINESTRING(5 0,5 10)")
    res = GeomAnalyzer(AnalysisOptions(check_line_crossings=True)).analyze(vl)
    q = res.quality_report
    assert q.unnoded_crossing_count == 2, q.unnoded_crossing_count
    assert q.unnoded_crossing_pairs == 1, q.unnoded_crossing_pairs
    assert res.unnoded_crossing_features == 2
    assert res.checks_enabled["unnoded_crossing"] is True
    print("OK unnoded_crossing_flags_lines_without_shared_vertex")


def test_unnoded_crossing_ignores_shared_vertex():
    """Un T dont la branche se termine PILE sur un sommet de la ligne
    traversée n'est pas une jonction non nodée."""
    _ensure_qgis()
    vl = _make_layer(geom_type="LineString")
    _add(vl, "LINESTRING(0 5,5 5,10 5)")   # sommet existant en (5, 5)
    _add(vl, "LINESTRING(5 0,5 5)")        # se termine exactement là
    res = GeomAnalyzer(AnalysisOptions(check_line_crossings=True)).analyze(vl)
    q = res.quality_report
    assert q.unnoded_crossing_count == 0, q.unnoded_crossing_count
    assert q.unnoded_crossing_pairs == 0
    print("OK unnoded_crossing_ignores_shared_vertex")


def test_unnoded_crossing_ignores_self_intersection():
    """Une ligne qui se croise ELLE-MÊME (nœud papillon) n'est pas ce que ce
    contrôle vérifie : réservé aux croisements ENTRE entités distinctes."""
    _ensure_qgis()
    vl = _make_layer(geom_type="LineString")
    _add(vl, "LINESTRING(0 0,10 10,10 0,0 10)")  # se croise sur elle-même
    res = GeomAnalyzer(AnalysisOptions(check_line_crossings=True)).analyze(vl)
    q = res.quality_report
    assert q.unnoded_crossing_count == 0, q.unnoded_crossing_count
    print("OK unnoded_crossing_ignores_self_intersection")


def test_unnoded_crossing_requires_line_features():
    """Sur une couche sans aucune ligne, le contrôle ne peut pas tourner et le
    dit — même logique que la hauteur des polygones inclus sans champ AGL."""
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,10 0,10 10,0 10,0 0))")
    res = GeomAnalyzer(AnalysisOptions(check_line_crossings=True)).analyze(vl)
    assert res.checks_enabled["unnoded_crossing"] is False
    assert res.quality_report.unnoded_crossing_count == 0
    assert any("Croisements de lignes" in w for w in res.warnings), res.warnings
    print("OK unnoded_crossing_requires_line_features")


def test_unnoded_crossing_disabled_by_default():
    _ensure_qgis()
    vl = _make_layer(geom_type="LineString")
    _add(vl, "LINESTRING(0 5,10 5)")
    _add(vl, "LINESTRING(5 0,5 10)")
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    assert res.checks_enabled["unnoded_crossing"] is False
    assert res.quality_report.unnoded_crossing_count == 0
    print("OK unnoded_crossing_disabled_by_default")


def test_unnoded_crossing_enters_score_and_reports():
    """Le contrôle pèse sur le score quand il a tourné, et son état passe dans
    ``checks_enabled``/``to_dict`` comme les autres contrôles facultatifs."""
    _ensure_qgis()
    vl = _make_layer(geom_type="LineString")
    _add(vl, "LINESTRING(0 5,10 5)")
    _add(vl, "LINESTRING(5 0,5 10)")
    res = GeomAnalyzer(AnalysisOptions(check_line_crossings=True)).analyze(vl)
    assert res.correctness.score < 100.0, res.correctness.score
    assert C.CAT_UNNODED_CROSSING in res.correctness.components
    d = res.to_dict()
    assert d["quality_report"]["unnoded_crossing_count"] == 2
    assert d["quality_report"]["unnoded_crossing_pairs"] == 1
    print("OK unnoded_crossing_enters_score_and_reports")


# Idée 5 (couches de rejets/corrections annexes) retirée à la demande de
# l'utilisateur : seul le fichier réparé doit sortir de l'étape de
# réparation. Les quatre tests qui vivaient ici testaient cette
# fonctionnalité désormais supprimée de build_repaired_layer/RepairTask.


# ── Corrections manuelles : FID identifiables (idée 4) ──────────────────────

def test_repair_skipped_fixes_track_fids():
    """Une correction REFUSÉE (aurait dégradé la géométrie) est identifiable
    par FID — pas seulement par un compteur — pour guider une reprise
    manuelle, sur le même modèle que ``manual_overlap_fids``."""
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,100 0,50 0.1,0 0))")  # triangle trop fin
    opts = _other_opts(check_sharp_angles=True, sharp_angle_min_deg=30.0)
    res = GeomAnalyzer(opts).analyze(vl)
    fid = res.flagged[C.CAT_SHARP_ANGLE][0]
    ropts = RepairOptions(fix_invalid=False, fix_sharp_angles=True,
                          sharp_angle_min_deg=30.0)
    mem, stats = GeomAnalyzer.build_repaired_layer(vl, res, ropts)
    assert stats.skipped_sharp_angles == 1
    assert stats.skipped_sharp_angle_fids == [fid]
    print("OK repair_skipped_fixes_track_fids")


# ── Sous-scores : jamais 100 % avec une anomalie, même après arrondi ───────

def test_component_score_never_shows_100_percent_with_issues_present():
    """Sur une très grande couche, un ratio infime (2 anomalies sur 100 000
    entités) arrondit à 100,0 % au premier décimal — bug signalé : « Trous
    détectés : 2 » affiché juste au-dessus de « Absence de trous : 100 % ».

    Vérifié sur TOUTES les dimensions notées, pas seulement les trous : la
    même règle que le score global (jamais 100 avec une anomalie présente)
    doit s'appliquer à chaque sous-score pris isolément.
    """
    _ensure_qgis()
    par_categorie = {
        "small": ("small_area_count", dict(include_small=True)),
        "overlap": ("overlap_count", dict(include_overlap=True)),
        "duplicate": ("duplicate_count", dict(include_duplicate=True)),
        C.CAT_DUPLICATE_VERTEX: ("duplicate_vertex_count",
                                dict(include_duplicate_vertex=True)),
        C.CAT_HOLE: ("hole_count", dict(include_holes=True)),
        C.CAT_MULTIPART: ("multipart_count", dict(include_multipart=True)),
        C.CAT_SHARP_ANGLE: ("sharp_angle_count", dict(include_sharp_angles=True)),
        C.CAT_CLOSE_VERTEX: ("close_vertex_count", dict(include_close_vertices=True)),
        C.CAT_NESTED_HEIGHT: ("nested_height_count", dict(include_nested_height=True)),
        C.CAT_UNNODED_CROSSING: ("unnoded_crossing_count",
                                 dict(include_line_crossings=True)),
    }
    for cle, (attr_compte, kwargs) in par_categorie.items():
        q = QualityReport()
        setattr(q, attr_compte, 2)
        res = AnalysisResult(total_features=100000, quality_report=q,
                             checks_enabled={cle: True})
        score = compute_correctness(res, **kwargs)
        assert score.components[cle] < 100.0, (cle, score.components[cle])
    print("OK component_score_never_shows_100_percent_with_issues_present")


def test_component_score_stays_100_percent_when_truly_clean():
    """Le plafond ne doit mordre QUE si l'anomalie existe : un compteur à 0
    garde légitimement 100 %, sur une grande couche comme sur une petite."""
    _ensure_qgis()
    q = QualityReport()  # tout à 0
    res = AnalysisResult(total_features=100000, quality_report=q,
                         checks_enabled={C.CAT_HOLE: True})
    score = compute_correctness(res, include_holes=True)
    assert score.components[C.CAT_HOLE] == 100.0, score.components[C.CAT_HOLE]
    print("OK component_score_stays_100_percent_when_truly_clean")


# ── Régression : nom jamais lié utilisé dans une méthode Qt (idée 5/2.9.37) ─

def _name_read_but_never_bound(chemin, nom_fonction, nom):
    """``nom`` est-il LU (contexte Load) quelque part dans ``nom_fonction``
    (fonctions imbriquées comprises) sans jamais y être LIÉ (paramètre,
    affectation, cible de for/with/except, alias d'import) ?

    Reproduit précisément le bug signalé : ``_on_repair_completed`` lisait
    ``result.layer_name`` alors que ``result`` n'était assigné nulle part dans
    la méthode — un ``NameError`` invisible en test headless (aucun test
    n'instancie le panneau Qt), qui n'apparaissait qu'à l'usage réel, dans un
    connecteur de signal Qt dont l'exception est silencieusement avalée.

    Une exception PEUT rester légitime si ``nom`` est lié dans une portée
    ENGLOBANTE (fermeture sur une variable d'une méthode extérieure) : ce test
    ne s'applique donc qu'à des méthodes de plus haut niveau qui ne sont pas
    elles-mêmes imbriquées, comme les gestionnaires ``_on_*`` du panneau.
    """
    import ast
    import io as _io
    arbre = ast.parse(_io.open(chemin, encoding="utf-8").read())
    for noeud in ast.walk(arbre):
        if not isinstance(noeud, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if noeud.name != nom_fonction:
            continue
        lu = False
        lie = False
        for interne in ast.walk(noeud):
            if isinstance(interne, ast.Name) and interne.id == nom:
                if isinstance(interne.ctx, ast.Load):
                    lu = True
                else:
                    lie = True
            elif isinstance(interne, ast.arg) and interne.arg == nom:
                lie = True
        return lu and not lie
    raise AssertionError("fonction %s introuvable dans %s"
                         % (nom_fonction, chemin))


def test_on_repair_completed_never_reads_an_unbound_result():
    """Régression : voir ``_name_read_but_never_bound``. La méthode doit
    passer par ``self._result`` — jamais un ``result`` local qui n'existe
    pas dans cette méthode."""
    chemin = os.path.join(_PLUGIN_DIR, "stat_geom_dockwidget.py")
    assert not _name_read_but_never_bound(chemin, "_on_repair_completed", "result"), (
        "'result' est lu sans jamais être lié dans _on_repair_completed "
        "(NameError certain) — utiliser self._result")
    print("OK on_repair_completed_never_reads_an_unbound_result")


def test_repair_path_is_automatic_and_next_to_source():
    """« Réparer » doit démarrer sans demander un chemin de sortie.

    Le résultat reprend le nom du fichier source, ajoute ``_repare`` et reste
    dans le même dossier. Un format source non inscriptible est converti en
    GeoPackage ; une couche mémoire conserve le repli historique.
    """
    _ensure_qgis()
    from stat_geom_qc.stat_geom_dockwidget import StatGeomDockWidget

    build = StatGeomDockWidget._automatic_repair_path
    assert build(r"D:\donnees\batiments.shp", "nom QGIS") == (
        r"D:\donnees\batiments_repare.shp")
    assert build(
        r"D:\donnees\catalogue.gpkg|layername=batiments", "batiments") == (
        r"D:\donnees\catalogue_repare.gpkg")
    assert build(r"D:\donnees\occupation.tab", "occupation") == (
        r"D:\donnees\occupation_repare.gpkg")
    assert build("Polygon?crs=EPSG:2154", "Ma couche", r"D:\sorties") == (
        r"D:\sorties\Ma couche_repare.gpkg")
    assert build(
        "dbname='gis' table=\"public\".\"routes\"", "Routes SQL",
        r"D:\sorties") == r"D:\sorties\Routes SQL_repare.gpkg"
    print("OK repair_path_is_automatic_and_next_to_source")


# ── Aperçu de réparation : dry_run / only_fids / seuil ajustable ───────────

def _small_layer_mixed():
    """3 polygones ≤ 2 m² (analyse) + 1 grand, pour tester un seuil de
    réparation qui RESSERRE le lot déjà repéré à l'analyse.

    Coordonnées Lambert-93 RÉALISTES (région parisienne, x/y ~ 650000/6860000)
    et non des coordonnées jouet proches de (0,0) : la mesure ellipsoïdale de
    l'aire (``QgsDistanceArea``, utilisée par le moteur) réinterprète les
    coordonnées projetées comme un point géographique réel une fois
    ré-inversées — près de (0,0) cela retombe sur un point sans rapport avec
    la France, où le facteur d'échelle local peut diviser l'aire mesurée par
    plus de deux. Des coordonnées plausibles gardent l'aire mesurée fidèle à
    l'aire planaire (écart mesuré < 0,1 %), ce qui est ce que ce test veut
    vérifier — pas un artefact de projection.
    """
    ox, oy = 650000.0, 6860000.0
    vl = _make_layer(fields=[("nom", QVariant.String)])
    _add(vl, "POLYGON((%f %f,%f %f,%f %f,%f %f,%f %f))"
         % (ox, oy, ox, oy + 0.5, ox + 0.5, oy + 0.5, ox + 0.5, oy, ox, oy),
         {"nom": "0.25m2"})
    _add(vl, "POLYGON((%f %f,%f %f,%f %f,%f %f,%f %f))"
         % (ox + 10, oy + 10, ox + 10, oy + 11, ox + 11, oy + 11,
            ox + 11, oy + 10, ox + 10, oy + 10),
         {"nom": "1m2"})
    _add(vl, "POLYGON((%f %f,%f %f,%f %f,%f %f,%f %f))"
         % (ox + 20, oy + 20, ox + 20, oy + 21.9, ox + 21.9, oy + 21.9,
            ox + 21.9, oy + 20, ox + 20, oy + 20),
         {"nom": "3.61m2"})
    _add(vl, "POLYGON((%f %f,%f %f,%f %f,%f %f,%f %f))"
         % (ox, oy, ox, oy + 100, ox + 100, oy + 100, ox + 100, oy, ox, oy),
         {"nom": "grand"})
    return vl


def test_dry_run_matches_real_run_counts():
    """``dry_run=True`` calcule EXACTEMENT les mêmes compteurs qu'une
    réparation réelle, sans rien construire ni écrire."""
    _ensure_qgis()
    vl = _small_layer_mixed()
    opts = AnalysisOptions(check_small_polygons=True, small_polygon_threshold_m2=2.0)
    res = GeomAnalyzer(opts).analyze(vl)
    ropts = RepairOptions(fix_invalid=False, remove_small=True,
                          small_polygon_threshold_m2=2.0)

    mem, stats_reel = GeomAnalyzer.build_repaired_layer(vl, res, ropts)
    _mem_vide, stats_apercu = GeomAnalyzer.build_repaired_layer(
        vl, res, ropts, dry_run=True)

    assert _mem_vide is None, "dry_run ne doit rien construire"
    assert stats_apercu.removed_small == stats_reel.removed_small == 2
    assert mem.featureCount() == stats_reel.written
    print("OK dry_run_matches_real_run_counts")


def test_only_fids_restricts_processing_to_the_subset():
    """``only_fids`` ne traite QUE ces entités : une entité hors du lot n'est
    ni comptée ni corrigée, même si elle serait normalement concernée."""
    _ensure_qgis()
    vl = _small_layer_mixed()
    opts = AnalysisOptions(check_small_polygons=True, small_polygon_threshold_m2=2.0)
    res = GeomAnalyzer(opts).analyze(vl)
    small_fids = sorted(res.flagged.get("small", []))
    assert len(small_fids) == 2, small_fids
    ropts = RepairOptions(fix_invalid=False, remove_small=True,
                          small_polygon_threshold_m2=2.0)

    # Un seul des deux FID passé : un seul doit être compté.
    _mem, stats_un_seul = GeomAnalyzer.build_repaired_layer(
        vl, res, ropts, dry_run=True, only_fids=[small_fids[0]])
    assert stats_un_seul.removed_small == 1, stats_un_seul.removed_small

    # Un FID hors du lot « small » (le grand polygone) : jamais concerné.
    grand_fid = [f.id() for f in vl.getFeatures()
                 if f["nom"] == "grand"][0]
    _mem2, stats_grand = GeomAnalyzer.build_repaired_layer(
        vl, res, ropts, dry_run=True, only_fids=[grand_fid])
    assert stats_grand.removed_small == 0
    print("OK only_fids_restricts_processing_to_the_subset")


def test_small_polygon_threshold_can_only_narrow_the_analysis_set():
    """Le seuil de RÉPARATION peut RESSERRER le lot repéré à l'analyse,
    jamais l'élargir. Compteur dédié pour les entités écartées, jamais une
    suppression silencieuse."""
    _ensure_qgis()
    vl = _small_layer_mixed()
    opts = AnalysisOptions(check_small_polygons=True, small_polygon_threshold_m2=2.0)
    res = GeomAnalyzer(opts).analyze(vl)
    assert res.quality_report.small_area_count == 2   # 0.25 m² et 1 m² (pas 3.61)

    # Seuil INCHANGÉ (2.0) : reproduit exactement l'analyse — non-régression.
    ropts_identique = RepairOptions(fix_invalid=False, remove_small=True,
                                    small_polygon_threshold_m2=2.0)
    _m, stats_identique = GeomAnalyzer.build_repaired_layer(
        vl, res, ropts_identique, dry_run=True)
    assert stats_identique.removed_small == 2
    assert stats_identique.small_kept_above_threshold == 0

    # Seuil RESSERRÉ (0.5) : seul le polygone de 0.25 m² reste.
    ropts_strict = RepairOptions(fix_invalid=False, remove_small=True,
                                 small_polygon_threshold_m2=0.5)
    _m2, stats_strict = GeomAnalyzer.build_repaired_layer(
        vl, res, ropts_strict, dry_run=True)
    assert stats_strict.removed_small == 1, stats_strict.removed_small
    assert stats_strict.small_kept_above_threshold == 1
    assert round(stats_strict.removed_small_area_m2, 2) == 0.25, \
        stats_strict.removed_small_area_m2

    # Seuil ÉLARGI (100) : ne fait PAS réapparaître le 3.61 m², jamais repéré
    # à l'analyse — seul le lot de l'analyse est jamais candidat.
    ropts_large = RepairOptions(fix_invalid=False, remove_small=True,
                                small_polygon_threshold_m2=100.0)
    _m3, stats_large = GeomAnalyzer.build_repaired_layer(
        vl, res, ropts_large, dry_run=True)
    assert stats_large.removed_small == 2, stats_large.removed_small
    print("OK small_polygon_threshold_can_only_narrow_the_analysis_set")


# ── Dialogue de réparation : messages adaptatifs (fonctions pures) ─────────
# Aucune dépendance Qt : ces fonctions ne font que formater du texte à partir
# de compteurs et d'un RepairStats — testables sans _ensure_qgis().

from stat_geom_qc import repair_dialog as RD  # noqa: E402


def test_dialog_msg_none_of_this_type_when_count_zero():
    """Chaque message d'aperçu doit reconnaître un compteur nul AVANT de
    toucher ``stats`` — sinon un ``None`` y ferait une exception (c'est
    exactement le bug reproduit puis corrigé pendant le développement :
    ``refresh_text`` appelait une fonction à 2 arguments avec 1 seul)."""
    for fn in (RD.msg_invalid, RD.msg_self_intersection):
        assert fn(0, None) == RD._NONE_OF_THIS_TYPE
    for fn in (RD.msg_null_empty, RD.msg_duplicate):
        assert fn(0) == RD._NONE_OF_THIS_TYPE
    assert RD.msg_small(0, None, 2.0) == RD._NONE_OF_THIS_TYPE
    assert RD.msg_overlap(0, None, 5.0) == RD._NONE_OF_THIS_TYPE
    assert RD.msg_duplicate_vertex(0, None) == RD._NONE_OF_THIS_TYPE
    assert RD.msg_hole(0, None, 2.0) == RD._NONE_OF_THIS_TYPE
    assert RD.msg_multipart(0, None) == RD._NONE_OF_THIS_TYPE
    assert RD.msg_sharp_angle(0, None, 30.0) == RD._NONE_OF_THIS_TYPE
    assert RD.msg_close_vertex(0, None, 0.2) == RD._NONE_OF_THIS_TYPE
    print("OK dialog_msg_none_of_this_type_when_count_zero")


def test_dialog_msg_estimating_when_stats_not_yet_computed():
    """Compteur non nul mais aperçu pas encore arrivé (``stats=None``) :
    message d'attente, jamais un texte prétendant un résultat."""
    for fn, args in (
        (RD.msg_invalid, (3, None)),
        (RD.msg_small, (3, None, 2.0)),
        (RD.msg_overlap, (3, None, 5.0)),
        (RD.msg_sharp_angle, (3, None, 30.0)),
    ):
        assert fn(*args) == RD._ESTIMATING, (fn.__name__, fn(*args))
    print("OK dialog_msg_estimating_when_stats_not_yet_computed")


def test_dialog_msg_invalid_reports_fixed_and_still_invalid():
    """Message COURT (voir la demande utilisateur de réduire le texte) : le
    détail du mécanisme vit dans l'infobulle de la case, pas ici."""
    stats = RepairStats(fixed_invalid=4, still_invalid=1)
    msg = RD.msg_invalid(5, stats)
    assert "4" in msg and "corrigée" in msg
    assert "1" in msg and "encore invalide" in msg
    # Sans invalide résiduel, aucune mention du reliquat.
    msg_clean = RD.msg_invalid(4, RepairStats(fixed_invalid=4, still_invalid=0))
    assert "encore invalide" not in msg_clean
    print("OK dialog_msg_invalid_reports_fixed_and_still_invalid")


def test_dialog_msg_small_reports_area_and_kept_above_threshold():
    stats = RepairStats(removed_small=2, removed_small_area_m2=1.5,
                        small_kept_above_threshold=3)
    msg = RD.msg_small(5, stats, 2.0)
    assert "2" in msg and "1.5 m²" in msg
    assert "3" in msg and "conservé" in msg
    print("OK dialog_msg_small_reports_area_and_kept_above_threshold")


def test_dialog_msg_overlap_reports_fixed_and_manual():
    stats = RepairStats(fixed_overlaps=6, manual_overlaps=2)
    msg = RD.msg_overlap(8, stats, 5.0)
    assert "6" in msg and "rogné" in msg
    assert "2" in msg and "manuel" in msg
    # Sans chevauchement majeur, aucune mention de reprise manuelle.
    msg_clean = RD.msg_overlap(6, RepairStats(fixed_overlaps=6, manual_overlaps=0), 5.0)
    assert "manuel" not in msg_clean
    print("OK dialog_msg_overlap_reports_fixed_and_manual")


def test_dialog_msg_sharp_angle_reports_fixed_and_skipped():
    stats = RepairStats(fixed_sharp_angles=3, skipped_sharp_angles=1)
    msg = RD.msg_sharp_angle(4, stats, 30.0)
    assert "3" in msg and "corrigée" in msg
    assert "1" in msg and "intacte" in msg
    print("OK dialog_msg_sharp_angle_reports_fixed_and_skipped")


def test_dialog_msg_close_vertex_reports_cleaned_and_skipped():
    stats = RepairStats(cleaned_close_vertices=2, skipped_close_vertices=1)
    msg = RD.msg_close_vertex(3, stats, 0.2)
    assert "2" in msg and "fusionnée" in msg
    assert "1" in msg and "intacte" in msg
    print("OK dialog_msg_close_vertex_reports_cleaned_and_skipped")


def test_dialog_msg_duplicate_vertex_reports_cleaned_and_skipped():
    stats = RepairStats(cleaned_duplicate_vertex=2, skipped_duplicate_vertex=1)
    msg = RD.msg_duplicate_vertex(3, stats)
    assert "2" in msg and "nettoyée" in msg
    assert "1" in msg and "intacte" in msg
    print("OK dialog_msg_duplicate_vertex_reports_cleaned_and_skipped")


def test_dialog_msg_hole_and_multipart():
    hole_msg = RD.msg_hole(2, RepairStats(removed_holes=3), 2.0)
    assert "3" in hole_msg and "comblé" in hole_msg
    multi_msg = RD.msg_multipart(2, RepairStats(added_features=5))
    assert "2" in multi_msg and "5" in multi_msg
    print("OK dialog_msg_hole_and_multipart")


def test_dialog_msg_null_empty_and_duplicate_report_the_count():
    """Aucune tolérance, aucun refus possible pour ces deux-là : le message
    se limite au compte, sans nuance — le caractère inconditionnel de la
    suppression est expliqué dans l'infobulle de la case (voir
    ``_build_standard_section``), pas répété à chaque affichage."""
    assert "4" in RD.msg_null_empty(4)
    assert "4" in RD.msg_duplicate(4)
    assert RD.msg_null_empty(0) == ""
    assert RD.msg_duplicate(0) == ""
    print("OK dialog_msg_null_empty_and_duplicate_report_the_count")


def test_dialog_msg_nested_height_and_unnoded_crossing_explain_no_fix():
    """Les deux contrôles sans correction automatique doivent le dire, même
    en une ligne courte — le détail (pourquoi, comment corriger à la main)
    vit dans l'infobulle du titre (voir ``_build_not_fixable_section``)."""
    for fn in (RD.msg_nested_height, RD.msg_unnoded_crossing):
        assert fn(0) == "Aucun détecté."
        dirty = fn(3)
        assert "3" in dirty
        assert "aucune correction automatique" in dirty
    print("OK dialog_msg_nested_height_and_unnoded_crossing_explain_no_fix")


def test_dialog_fmt_uses_french_thousands_separator():
    assert RD._fmt(12345) == C.fmt_int(12345)
    print("OK dialog_fmt_uses_french_thousands_separator")


class _FakeLabel:
    """Remplace un QLabel pour tester _PreviewRow SANS dépendance Qt."""

    def __init__(self):
        self._text = ""
        self._visible = True

    def setText(self, text):
        self._text = text

    def text(self):
        return self._text

    def setVisible(self, visible):
        self._visible = bool(visible)

    def isVisible(self):
        return self._visible


def test_preview_row_lifecycle_logic():
    """``needs_preview``/``is_ready_for_repair``/``refresh_text`` couvrent les
    quatre familles de ligne (statique, informationnelle, aperçu à zéro,
    aperçu réel) sans qu'une seule ait besoin d'un widget Qt réel."""
    # Statique (nulle/vide, doublon) : toujours prête, jamais d'aperçu attendu.
    static_row = RD._PreviewRow("null_empty", 4, static=True,
                                format_message=RD.msg_null_empty)
    static_row.message_label = _FakeLabel()
    assert not static_row.needs_preview()
    assert static_row.is_ready_for_repair()
    static_row.refresh_text()
    assert "4" in static_row.message_label.text()

    # Informationnelle (fids=None) : jamais d'aperçu, toujours prête.
    info_row = RD._PreviewRow("nested_height", 3, fids=None)
    assert not info_row.needs_preview()
    assert info_row.is_ready_for_repair()

    # Aperçu, compteur nul : rien à attendre, toujours prête.
    empty_row = RD._PreviewRow("overlap", 0, fids=[])
    assert not empty_row.needs_preview()
    assert empty_row.is_ready_for_repair()

    # Aperçu réel, résultat pas encore arrivé : bloque la réparation.
    # ``msg_duplicate_vertex`` (2 arguments, sans tolérance) et non
    # ``msg_overlap`` : ``refresh_text`` appelle TOUJOURS son
    # ``format_message`` avec 2 arguments (count, stats) — c'est le dialogue
    # réel qui enveloppe les messages À tolérance dans une lambda ajoutant le
    # 3ᵉ argument (voir ``_add_dry_run_row``), jamais ``_PreviewRow`` lui-même.
    pending_row = RD._PreviewRow("duplicate_vertex", 2, fids=[1, 2],
                                 format_message=RD.msg_duplicate_vertex)
    pending_row.message_label = _FakeLabel()
    assert pending_row.needs_preview()
    assert not pending_row.is_ready_for_repair()
    pending_row.refresh_text()
    assert pending_row.message_label.text() == RD._ESTIMATING

    # Résultat arrivé : prête. Réglage changé depuis (stale) : plus prête.
    pending_row.stats = RepairStats(cleaned_duplicate_vertex=1, skipped_duplicate_vertex=1)
    pending_row.stale = False
    assert pending_row.is_ready_for_repair()
    pending_row.stale = True
    assert not pending_row.is_ready_for_repair()
    print("OK preview_row_lifecycle_logic")


def test_dialog_discards_stale_preview_generation():
    """Régression directe du correctif de condition de course : un résultat
    calculé pour une GÉNÉRATION antérieure de la ligne (un rafraîchissement
    manuel plus récent a eu lieu entre-temps) doit être IGNORÉ, jamais
    écraser le résultat plus récent déjà affiché."""
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,0 10,10 10,10 0,0 0))")
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    counts = {k: 0 for k in (
        "invalid", "self_intersection", "null_empty", "duplicate", "small",
        "overlap", C.CAT_DUPLICATE_VERTEX, C.CAT_HOLE, C.CAT_MULTIPART,
        C.CAT_SHARP_ANGLE, C.CAT_CLOSE_VERTEX)}
    from stat_geom_qc.repair_dialog import RepairOptionsDialog
    dlg = RepairOptionsDialog(None, vl, counts, res)
    try:
        row = dlg._row("small")
        row.gen = 5   # un rafraîchissement manuel a déjà eu lieu 5 fois
        recent_stats = RepairStats(removed_small=9)
        dlg._on_row_ready("small", recent_stats, expected_gen=5)
        assert row.stats is recent_stats, "résultat à jour rejeté à tort"

        # Un résultat calculé pour la génération 3 (périmée) arrive APRÈS :
        # il ne doit PAS remplacer le résultat de la génération 5.
        stale_stats = RepairStats(removed_small=1)
        dlg._on_row_ready("small", stale_stats, expected_gen=3)
        assert row.stats is recent_stats, "un résultat périmé a écrasé le récent"
    finally:
        dlg._closing = True
        dlg.deleteLater()
    print("OK dialog_discards_stale_preview_generation")


# ── Régressions de l'audit de logique (v2.9.42) ─────────────────────────────

def test_absence_of_nested_pairs_is_a_conclusion_not_an_ignorance():
    """Pas d'imbrication du tout ≠ contrôle sans réponse.

    Une couche parfaitement propre plafonnait à 99,9 : le contrôle de hauteur
    y était déclaré « sans conclusion » du seul fait qu'aucune paire n'avait
    été COMPARÉE — alors qu'il n'existait simplement aucune imbrication à
    comparer. Le contrôle a tourné, il a répondu : rien à trouver.
    """
    _ensure_qgis()
    vl = _make_layer(fields=[("AGL", QVariant.Double)])
    _add(vl, "POLYGON((0 0,0 10,10 10,10 0,0 0))", {"AGL": 5.0})
    _add(vl, "POLYGON((50 50,50 60,60 60,60 50,50 50))", {"AGL": 7.0})
    res = GeomAnalyzer(AnalysisOptions(check_nested_height=True)).analyze(vl)
    q = res.quality_report
    assert q.nested_pairs_checked == 0 and q.nested_pairs_unknown == 0, (
        q.nested_pairs_checked, q.nested_pairs_unknown)
    assert not res.checks_inconclusive.get(C.CAT_NESTED_HEIGHT), \
        "aucune imbrication n'est une CONCLUSION, pas une ignorance"
    assert res.correctness.components.get(C.CAT_NESTED_HEIGHT) == 100.0, \
        res.correctness.components
    assert res.correctness.score == 100.0, res.correctness.score
    print("OK absence_of_nested_pairs_is_a_conclusion_not_an_ignorance")


def test_non_finite_height_is_not_verifiable_never_compliant():
    """NaN / ±Inf en altitude ne doivent pas passer pour des nombres.

    ``float("nan")`` réussit : la valeur était retenue, puis
    ``inclus <= contenant`` valait faux — la paire était donc comptée
    COMPARÉE et CONFORME. Un satisfecit sur une donnée illisible.
    """
    _ensure_qgis()
    assert GeomAnalyzer._to_float("nan") is None
    assert GeomAnalyzer._to_float("inf") is None
    assert GeomAnalyzer._to_float("-inf") is None
    assert GeomAnalyzer._to_float(float("nan")) is None
    assert GeomAnalyzer._to_float("12,5") == 12.5, "les nombres normaux passent"

    vl = _make_layer(fields=[("AGL", QVariant.Double)])
    _add(vl, "POLYGON((0 0,0 100,100 100,100 0,0 0))", {"AGL": float("nan")})
    _add(vl, "POLYGON((10 10,10 20,20 20,20 10,10 10))", {"AGL": float("nan")})
    res = GeomAnalyzer(AnalysisOptions(check_nested_height=True)).analyze(vl)
    q = res.quality_report
    assert q.nested_pairs_checked == 0, "un NaN ne se compare pas"
    assert q.nested_pairs_unknown > 0, "la paire doit être NON VÉRIFIABLE"
    assert res.checks_inconclusive.get(C.CAT_NESTED_HEIGHT) is True
    print("OK non_finite_height_is_not_verifiable_never_compliant")


def test_hole_area_never_falls_back_to_non_metric_units():
    """Le repli planaire ne vaut des m² que sur une couche en mètres.

    Sur EPSG:4326 ``geom.area()`` renvoie des degrés carrés : un trou de
    1e-4 deg² (~1 200 m²) se lisait « 0,0001 m² », donc sous n'importe quel
    seuil, donc COMBLÉ. Sans mesure ellipsoïdale, mieux vaut ne rien
    conclure — le trou est conservé et n'est pas compté.
    """
    _ensure_qgis()
    from stat_geom_qc.analysis_engine import layer_planar_area_is_m2
    troue = QgsGeometry.fromWkt(
        "POLYGON((0 0,0 1,1 1,1 0,0 0),(0.4 0.4,0.4 0.6,0.6 0.6,0.6 0.4,0.4 0.4))")
    # Sans mesure ellipsoïdale (measure_area=None) :
    assert count_small_holes(troue, 1.0, None, True)[0] == 1, \
        "en mètres, le repli planaire reste licite"
    assert count_small_holes(troue, 1.0, None, False)[0] == 0, \
        "hors couche métrique, un trou non mesuré ne doit pas être compté"
    # Et la décision d'unité suit bien le CRS de la couche.
    assert layer_planar_area_is_m2(_make_layer(crs="EPSG:2154")) is True
    assert layer_planar_area_is_m2(_make_layer(crs="EPSG:4326")) is False
    print("OK hole_area_never_falls_back_to_non_metric_units")


def test_polygon_only_checks_are_not_applicable_without_polygons():
    """Sur une couche de lignes, les contrôles polygonaux n'ont rien examiné.

    Les laisser « exécutés » affichait « 0 chevauchement », « 0 petit
    polygone » — lus comme un sans-faute — et les faisait peser dans le
    score. Même traitement que les croisements de lignes sur une couche sans
    lignes : NON APPLICABLE.
    """
    _ensure_qgis()
    vl = _make_layer(geom_type="LineString")
    _add(vl, "LINESTRING(0 0,10 10)")
    _add(vl, "LINESTRING(0 10,10 0)")
    res = GeomAnalyzer(AnalysisOptions(
        check_overlaps=True, check_small_polygons=True, check_holes=True,
        check_sharp_angles=True, check_close_vertices=True)).analyze(vl)
    for cle in ("overlap", "small", C.CAT_HOLE, C.CAT_SHARP_ANGLE,
                C.CAT_CLOSE_VERTEX):
        assert res.checks_enabled.get(cle) is False, cle
        assert cle not in res.correctness.components, cle
    assert any("aucune entité de type polygone" in w for w in res.warnings), \
        res.warnings
    print("OK polygon_only_checks_are_not_applicable_without_polygons")


def test_repair_refuses_a_partial_output():
    """Une sortie AMPUTÉE ne doit jamais passer pour une réparation réussie.

    Seul le rejet TOTAL levait une erreur : un lot partiellement refusé
    produisait un fichier incomplet, chargé et présenté comme réparé — rien
    ne le signalait à la relecture.
    """
    _ensure_qgis()
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,0 10,10 10,10 0,0 0))")
    _add(vl, "POLYGON((20 20,20 30,30 30,30 20,20 20))")
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)

    # Fournisseur simulé qui accepte tout sauf la dernière entité.
    import stat_geom_qc.analysis_engine as AE
    erreur = {}

    class _ProviderPartiel:
        """Enveloppe le fournisseur mémoire et en refuse une entité."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def addFeatures(self, feats):  # noqa: N802 (API QGIS)
            return self._inner.addFeatures(list(feats)[:-1])

    original = AE.QgsVectorLayer

    class _CoucheAmputee(original):
        def dataProvider(self):
            return _ProviderPartiel(super().dataProvider())

    AE.QgsVectorLayer = _CoucheAmputee
    try:
        GeomAnalyzer.build_repaired_layer(vl, res, RepairOptions(fix_invalid=True))
    except RuntimeError as exc:
        erreur["msg"] = str(exc)
    finally:
        AE.QgsVectorLayer = original
    assert erreur.get("msg"), "une sortie partielle a été acceptée"
    assert "abandonn" in erreur["msg"].lower(), erreur["msg"]
    print("OK repair_refuses_a_partial_output")


def test_error_layer_truncation_flag_is_real_truncation():
    """Tomber PILE sur le plafond n'est pas une troncature.

    ``len(feats) >= cap`` annonçait une liste écourtée alors que rien
    n'avait été laissé de côté : un doute inutile jeté sur un inventaire
    exact.
    """
    _ensure_qgis()
    from stat_geom_qc import error_layer as EL
    vl = _make_layer()
    for i in range(3):
        _add(vl, "POLYGON((%d 0,%d 1,%d 1,%d 0,%d 0))"
             % (i * 10, i * 10, i * 10 + 1, i * 10 + 1, i * 10))
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    res.flagged["invalid"] = [f.id() for f in vl.getFeatures()]

    feats, tronque = EL.build_error_features(vl, res, cap=3)
    assert len(feats) == 3 and tronque is False, \
        "trois anomalies pour un plafond de trois : rien n'a été écarté"
    feats, tronque = EL.build_error_features(vl, res, cap=2)
    assert len(feats) == 2 and tronque is True, "là, une anomalie manque"
    print("OK error_layer_truncation_flag_is_real_truncation")


def test_preview_row_options_never_smuggle_make_valid():
    """L'aperçu « isolé » d'une ligne ne doit activer QUE cette ligne.

    ``RepairOptions.fix_invalid`` vaut ``True`` par défaut : chaque aperçu
    appliquait donc aussi un ``make valid``, et la ligne « angles aigus »
    montrait le résultat obtenu sur des géométries rendues valides — que
    l'utilisateur ait coché « invalides » ou non.
    """
    _ensure_qgis()
    from stat_geom_qc.repair_dialog import RepairOptionsDialog
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,0 10,10 10,10 0,0 0))")
    res = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    counts = {k: 0 for k in (
        "invalid", "self_intersection", "null_empty", "duplicate", "small",
        "overlap", C.CAT_DUPLICATE_VERTEX, C.CAT_HOLE, C.CAT_MULTIPART,
        C.CAT_SHARP_ANGLE, C.CAT_CLOSE_VERTEX)}
    dlg = RepairOptionsDialog(None, vl, counts, res)
    try:
        for categorie, attribut in RepairOptionsDialog._FLAG_ATTR.items():
            ropts = dlg._isolated_ropts(categorie, None, None)
            leves = [nom for nom in RepairOptionsDialog._FLAG_ATTR.values()
                     if getattr(ropts, nom)]
            assert leves == [attribut], (categorie, leves)
    finally:
        dlg._closing = True
        dlg.deleteLater()
    print("OK preview_row_options_never_smuggle_make_valid")


def test_detection_thresholds_can_only_be_narrowed_in_the_dialog():
    """Un seuil de DÉTECTION n'est pas élargissable depuis la réparation.

    La réparation ne travaille que sur les entités déjà signalées : élargir
    un tel seuil corrigeait davantage DANS ces entités tout en laissant
    intactes celles qui n'avaient pas franchi le seuil d'analyse — deux
    traitements pour la même donnée. Le maximum est donc verrouillé sur la
    valeur de l'analyse (le pourcentage de rognage, lui, règle le remède et
    non la détection : il reste libre).
    """
    _ensure_qgis()
    from stat_geom_qc.repair_dialog import RepairOptionsDialog
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,0 10,10 10,10 0,0 0))")
    res = GeomAnalyzer(AnalysisOptions(
        check_small_polygons=True, small_polygon_threshold_m2=3.0,
        check_holes=True, hole_max_area_m2=4.0,
        check_sharp_angles=True, sharp_angle_min_deg=25.0,
        check_close_vertices=True, close_vertex_min_dist_m=0.15)).analyze(vl)
    counts = {k: 1 for k in (
        "invalid", "self_intersection", "null_empty", "duplicate", "small",
        "overlap", C.CAT_DUPLICATE_VERTEX, C.CAT_HOLE, C.CAT_MULTIPART,
        C.CAT_SHARP_ANGLE, C.CAT_CLOSE_VERTEX)}
    dlg = RepairOptionsDialog(None, vl, counts, res)
    try:
        plafonds = {"small": 3.0, C.CAT_HOLE: 4.0,
                    C.CAT_SHARP_ANGLE: 25.0, C.CAT_CLOSE_VERTEX: 0.15}
        for categorie, plafond in plafonds.items():
            spin = dlg._row(categorie).spin
            assert spin is not None, categorie
            assert abs(spin.maximum() - plafond) < 1e-9, (categorie, spin.maximum())
        # Le rognage des chevauchements n'est PAS un seuil de détection : il
        # règle l'agressivité du remède et garde toute sa plage.
        assert (dlg._row("overlap").spin.maximum()
                == C.OVERLAP_TOLERANCE_MAX_PCT)
    finally:
        dlg._closing = True
        dlg.deleteLater()
    print("OK detection_thresholds_can_only_be_narrowed_in_the_dialog")


class _IfaceStub(object):
    """iface minimal : le panneau se construit sans fenêtre ni canevas."""

    def mainWindow(self):
        return None

    def mapCanvas(self):
        return None


def test_verify_task_counts_as_busy():
    """La vérification post-réparation OCCUPE le panneau.

    Absente de ``_is_busy``, elle laissait démarrer une analyse ou une
    réparation pendant qu'elle tournait : deux tâches se disputaient alors la
    barre de progression et le même état d'interface.
    """
    _ensure_qgis()
    from stat_geom_qc.stat_geom_dockwidget import StatGeomDockWidget
    dock = StatGeomDockWidget(_IfaceStub())
    try:
        assert dock._is_busy() is False
        dock._verify_task = object()
        assert dock._is_busy() is True, \
            "une vérification en cours doit occuper le panneau"
        dock._verify_task = None
        assert dock._is_busy() is False
    finally:
        dock.deleteLater()
    print("OK verify_task_counts_as_busy")


def test_same_file_is_case_insensitive_on_windows():
    """La garde d'écrasement de la source doit résister à la casse.

    ``os.path.abspath`` seul ne normalise pas la casse : sous Windows,
    « D:\\data\\Foo.gpkg » et « d:\\data\\foo.gpkg » désignent le même fichier
    mais ne se comparaient pas égaux — la garde tombait et la couche source,
    ouverte dans QGIS, se faisait écraser par sa version réparée.
    """
    _ensure_qgis()
    from stat_geom_qc.stat_geom_dockwidget import StatGeomDockWidget as D
    if os.name == "nt":
        assert D._same_file(r"D:\data\Foo.gpkg", r"d:\data\foo.gpkg") is True
    assert D._same_file("/a/b/c.gpkg", "/a/b/c.gpkg") is True
    assert D._same_file("/a/b/c.gpkg", "/a/b/d.gpkg") is False
    assert D._same_file("", "/a/b/c.gpkg") is False
    print("OK same_file_is_case_insensitive_on_windows")


def test_actions_use_the_result_layer_not_the_selected_one():
    """Un FID n'a de sens que pour la couche ANALYSÉE.

    Le défaut d'origine : après une analyse de A puis une sélection de B, les
    FID de A étaient appliqués à B — des entités sans aucun rapport, désignées
    à l'utilisateur comme fautives. Toute action portant des FID doit donc
    passer par la couche DU RÉSULTAT, et se taire quand la sélection porte
    ailleurs.
    """
    _ensure_qgis()
    from stat_geom_qc.stat_geom_dockwidget import StatGeomDockWidget
    a = _make_layer()
    _add(a, "POLYGON((0 0,0 10,10 10,10 0,0 0))")
    b = _make_layer()
    _add(b, "POLYGON((0 0,0 10,10 10,10 0,0 0))")
    # Couches volontairement HORS projet : rien ici n'en a besoin, et inscrire
    # des couches au registre pendant que d'autres panneaux de test vivent
    # encore (leurs listes déroulantes y sont abonnées, et aucune boucle
    # d'événements ne les démonte) fait tomber le processus.
    dock = StatGeomDockWidget(_IfaceStub())
    try:
        dock._result = GeomAnalyzer(AnalysisOptions()).analyze(a)
        dock._result_layer_id = a.id()
        assert dock._layer_matches_result(a) is True
        assert dock._layer_matches_result(b) is False, \
            "B n'est pas la couche du résultat"
        assert dock._layer_matches_result(None) is False
        # La sélection porte sur B : aucune couche à surligner.
        # ``_current_layer`` est substitué plutôt que la liste déroulante :
        # piloter un QgsMapLayerComboBox hors boucle d'événements fait tomber
        # cette suite headless en faute de segmentation, et c'est bien la
        # DÉCISION qu'on vérifie ici, pas le widget.
        dock._current_layer = lambda: b
        assert dock._result_layer() is None, \
            "aucun tracé ne doit être produit sur une autre couche"
        dock._current_layer = lambda: a
        assert dock._result_layer() is a
    finally:
        dock.deleteLater()
    print("OK actions_use_the_result_layer_not_the_selected_one")


def test_panel_shows_only_the_summary():
    """Le panneau ne porte plus qu'une synthèse — et rien ne se perd pour autant.

    Les onglets « Qualité », « Anomalies », « Attributs » et « Rapport » ont été
    retirés en 2.9.49 : ils rejouaient dans 300 px de large ce que les rapports
    rendent en pleine page. Ce test vérifie que les widgets ont bien disparu du
    panneau ; le détail d'invalidité qu'ils portaient reste, lui, consultable
    dans les rapports (voir
    ``test_invalid_details_still_in_reports_after_tabs_removal``).

    La surbrillance d'inspection part avec eux : la liste d'anomalies était son
    seul point d'entrée, et du code inatteignable se serait remis à vivre à la
    première reprise.
    """
    _ensure_qgis()
    from qgis.PyQt.QtWidgets import QTabWidget
    from stat_geom_qc.stat_geom_dockwidget import StatGeomDockWidget

    dock = StatGeomDockWidget(_IfaceStub())
    try:
        assert not dock.findChildren(QTabWidget), "les onglets doivent avoir disparu"
        for parti in ("tabs", "quality_table", "invalid_browser", "anomaly_list",
                      "anomaly_count", "attr_table", "report_browser",
                      "btn_clear_highlight", "_highlight_band", "_anomaly_rows"):
            assert not hasattr(dock, parti), parti
        for parti in ("_fill_quality_table", "_fill_anomaly_list",
                      "_fill_attributes_table", "_on_anomaly_selected",
                      "_highlight_feature", "clear_highlight",
                      "_build_quality_tab", "_build_anomalies_tab",
                      "_build_attributes_tab", "_build_report_tab"):
            assert not hasattr(dock, parti), parti
        # La synthèse, elle, reste : c'est elle qui porte les cartes.
        assert hasattr(dock, "cards_layout")
        assert hasattr(dock, "_build_summary_section")
        # Les actions qui LOCALISENT les anomalies dans QGIS restent en place :
        # ce sont elles qui remplacent la revue à la souris.
        assert dock.btn_errlayer is not None
        assert dock.btn_select is not None
    finally:
        dock.deleteLater()
    print("OK panel_shows_only_the_summary")


def test_editing_the_layer_invalidates_the_result_for_actions():
    """Éditer la couche analysée doit interdire les actions sur ses FID.

    Un fid supprimé peut être RECYCLÉ par le fournisseur : supprimer ou
    réparer sur la foi des identifiants d'un état révolu frappe alors une
    autre entité. Le résultat reste affiché (il dit ce qui a été trouvé),
    mais il n'autorise plus d'action.
    """
    _ensure_qgis()
    from stat_geom_qc.stat_geom_dockwidget import StatGeomDockWidget
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,0 10,10 10,10 0,0 0))")
    _add(vl, "POLYGON((50 50,50 60,60 60,60 50,50 50))")
    # Couche hors projet, même raison que ci-dessus : ``_watch_layer_edits``
    # reçoit l'objet couche directement, le registre n'intervient pas.
    dock = StatGeomDockWidget(_IfaceStub())
    try:
        dock._result = GeomAnalyzer(AnalysisOptions()).analyze(vl)
        dock._result_layer_id = vl.id()
        dock._watch_layer_edits(vl)
        assert dock._result_stale is False

        # Suppression réelle d'une entité, par le chemin d'édition de QGIS :
        # le résultat devient périmé.
        assert vl.startEditing()
        assert vl.deleteFeature(next(vl.getFeatures()).id())
        assert dock._result_stale is True, \
            "une édition de la couche doit périmer le résultat"
        vl.commitChanges()

        # Une nouvelle analyse repart de zéro.
        dock._watch_layer_edits(vl)
        assert dock._result_stale is False
        # Et l'écoute est bien débranchée quand on change de couche.
        dock._watch_layer_edits(None)
        assert vl.startEditing()
        assert vl.deleteFeature(next(vl.getFeatures()).id())
        vl.commitChanges()
        assert dock._result_stale is False, \
            "la couche n'est plus écoutée : plus de péremption"
    finally:
        # Débranchement AVANT tout retrait de couche : une couche qui émet
        # vers un panneau à moitié démonté fait tomber le processus.
        dock._unwatch_layer_edits()
        dock.deleteLater()
    print("OK editing_the_layer_invalidates_the_result_for_actions")


# ── Zone d'analyse (AOI) ────────────────────────────────────────────────────

# Coordonnées RÉALISTES (Lambert-93 région parisienne) : en EPSG:2154, une
# mesure ellipsoïdale près de l'origine applique un facteur d'échelle très
# différent du planaire (~0,48×) et fausserait les seuils de surface.
_AOI_OX, _AOI_OY = 650000.0, 6860000.0


def _aoi_box(x0, y0, x1, y1):
    """WKT d'un rectangle, décalé sur des coordonnées Lambert-93 réalistes."""
    return "POLYGON((%f %f,%f %f,%f %f,%f %f,%f %f))" % (
        _AOI_OX + x0, _AOI_OY + y0, _AOI_OX + x0, _AOI_OY + y1,
        _AOI_OX + x1, _AOI_OY + y1, _AOI_OX + x1, _AOI_OY + y0,
        _AOI_OX + x0, _AOI_OY + y0)


def _aoi_rect(x0, y0, x1, y1):
    return (_AOI_OX + x0, _AOI_OY + y0, _AOI_OX + x1, _AOI_OY + y1)


def _layer_for_aoi():
    """Couche couvrant tous les cas d'appartenance à une zone [0,0]-[100,100].

    Le couple ``a_cheval`` / ``voisin_dehors`` est le cas décisif : leur
    chevauchement est une erreur RÉELLE d'une entité de la zone, et il ne doit
    pas disparaître au prétexte que le second membre est dehors.
    """
    vl = _make_layer(fields=[("nom", QVariant.String)])
    _add(vl, _aoi_box(10, 10, 30, 30), {"nom": "dedans_a"})
    _add(vl, _aoi_box(40, 40, 60, 60), {"nom": "dedans_b"})
    _add(vl, _aoi_box(58, 40, 80, 60), {"nom": "dedans_c_chevauche_b"})
    _add(vl, _aoi_box(95, 10, 130, 30), {"nom": "a_cheval"})
    _add(vl, _aoi_box(128, 10, 160, 30), {"nom": "voisin_dehors"})
    _add(vl, _aoi_box(300, 300, 320, 320), {"nom": "loin_dehors"})
    return vl


def _noms(vl, fids):
    table = {f.id(): f["nom"] for f in vl.getFeatures()}
    return sorted(table[f] for f in fids if f in table)


def test_aoi_restricts_the_analysis_and_the_score_denominator():
    """Sous zone d'analyse, tout doit porter sur la zone — le score compris.

    Le dénominateur des ratios est ``total_features`` : laissé au compte de
    la COUCHE entière, un quartier truffé d'erreurs aurait rendu un score
    quasi parfait (4 erreurs sur 264 000 entités au lieu de 4 sur 800).
    """
    _ensure_qgis()
    vl = _layer_for_aoi()
    opts = AnalysisOptions(check_overlaps=True,
                           aoi_rect=_aoi_rect(0, 0, 100, 100),
                           aoi_crs_authid="EPSG:2154")
    res = GeomAnalyzer(opts).analyze(vl)
    assert res.has_aoi() is True
    assert res.aoi_features == 4, res.aoi_features
    assert res.aoi_layer_features == 6, res.aoi_layer_features
    assert res.total_features == 4, \
        "le score doit se diviser par les entités ANALYSÉES, pas par la couche"
    assert res.aoi_wkt, "la zone appliquée doit être conservée dans le résultat"
    assert any("RESTREINTE" in w for w in res.warnings), res.warnings
    print("OK aoi_restricts_the_analysis_and_the_score_denominator")


def test_aoi_still_sees_overlaps_with_neighbours_outside_the_zone():
    """L'erreur au BORD de la zone ne doit pas disparaître.

    Un bâtiment de la zone qui chevauche son voisin juste dehors est une
    erreur réelle. En n'indexant que les entités de la zone, personne ne la
    voyait — précisément au bord, là où l'on découpe le travail en tuiles.
    Les voisins sont donc indexés SANS être analysés : ils expliquent
    l'erreur, ils n'en apportent aucune.
    """
    _ensure_qgis()
    vl = _layer_for_aoi()
    opts = AnalysisOptions(check_overlaps=True,
                           aoi_rect=_aoi_rect(0, 0, 100, 100),
                           aoi_crs_authid="EPSG:2154")
    res = GeomAnalyzer(opts).analyze(vl)
    q = res.quality_report
    # Deux paires : dedans_b/dedans_c, et a_cheval/voisin_dehors.
    assert q.overlap_pairs == 2, q.overlap_pairs
    signalees = _noms(vl, res.flagged["overlap"])
    assert signalees == ["a_cheval", "dedans_b", "dedans_c_chevauche_b"], signalees
    assert "voisin_dehors" not in signalees, \
        "un voisin hors zone n'est pas une erreur à corriger dans cette tuile"
    assert "loin_dehors" not in signalees
    print("OK aoi_still_sees_overlaps_with_neighbours_outside_the_zone")


def test_aoi_rectangle_is_reprojected_to_the_layer_crs():
    """Une zone tracée dans le CRS du CANEVAS doit viser la bonne région.

    Le canevas peut être en Web Mercator quand la couche est en Lambert-93 :
    sans reprojection, le rectangle désignerait un endroit du globe sans
    aucun rapport. La conversion passe par ``transformBoundingBox``, qui
    densifie les bords — les quatre coins seuls refermeraient la zone à
    l'intérieur de ce qui était visé.
    """
    _ensure_qgis()
    from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                           QgsRectangle)
    from stat_geom_qc.analysis_engine import resolve_aoi_rect
    vl = _layer_for_aoi()
    voulu = QgsRectangle(*_aoi_rect(0, 0, 100, 100))
    tr = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:2154"),
                                QgsCoordinateReferenceSystem("EPSG:4326"),
                                QgsProject.instance().transformContext())
    en_wgs = tr.transformBoundingBox(voulu)

    revenu = resolve_aoi_rect(
        vl, (en_wgs.xMinimum(), en_wgs.yMinimum(),
             en_wgs.xMaximum(), en_wgs.yMaximum()), "EPSG:4326")
    assert revenu is not None
    # Aller-retour : quelques mètres d'écart au plus, jamais un décalage
    # d'échelle ou de continent.
    for attendu, obtenu in ((voulu.xMinimum(), revenu.xMinimum()),
                            (voulu.yMinimum(), revenu.yMinimum()),
                            (voulu.xMaximum(), revenu.xMaximum()),
                            (voulu.yMaximum(), revenu.yMaximum())):
        assert abs(attendu - obtenu) < 50.0, (attendu, obtenu)

    opts = AnalysisOptions(check_overlaps=True,
                           aoi_rect=(en_wgs.xMinimum(), en_wgs.yMinimum(),
                                     en_wgs.xMaximum(), en_wgs.yMaximum()),
                           aoi_crs_authid="EPSG:4326")
    assert GeomAnalyzer(opts).analyze(vl).aoi_features == 4

    # Rectangle dégénéré ou absent : aucune zone, jamais un rectangle plat.
    assert resolve_aoi_rect(vl, None, "") is None
    assert resolve_aoi_rect(vl, _aoi_rect(10, 10, 10, 10), "EPSG:2154") is None
    print("OK aoi_rectangle_is_reprojected_to_the_layer_crs")


def test_empty_aoi_never_yields_a_perfect_score():
    """Zéro entité analysée n'est pas une donnée parfaite.

    Tous les compteurs valent 0, donc toutes les pénalités aussi : le score
    sortait à 100/100 « Excellent » sur une zone vide — un satisfecit délivré
    sans avoir rien regardé. La note doit être explicitement NON ÉVALUÉE.
    """
    _ensure_qgis()
    vl = _layer_for_aoi()
    opts = AnalysisOptions(check_overlaps=True,
                           aoi_rect=_aoi_rect(5000, 5000, 5100, 5100),
                           aoi_crs_authid="EPSG:2154")
    res = GeomAnalyzer(opts).analyze(vl)
    assert res.aoi_features == 0
    assert res.total_features == 0
    c = res.correctness
    assert c.score < 100.0, c.score
    assert c.grade == C.GRADE_NOT_EVALUATED, c.grade
    assert c.coverage_ran == 0, c.coverage_ran
    assert not c.components, "aucune dimension ne peut être notée"
    assert any("aucune entité" in w for w in res.warnings), res.warnings
    print("OK empty_aoi_never_yields_a_perfect_score")


def test_aoi_repair_output_covers_exactly_the_analysed_zone():
    """Le fichier réparé porte le MÊME périmètre que l'analyse.

    Recopier toute la couche livrerait un fichier dont la quasi-totalité n'a
    jamais été contrôlée, sous un nom qui dit « réparé ». La sortie est donc
    un extrait de la zone — assumé, et annoncé dans le bilan.
    """
    _ensure_qgis()
    vl = _layer_for_aoi()
    opts = AnalysisOptions(check_overlaps=True,
                           aoi_rect=_aoi_rect(0, 0, 100, 100),
                           aoi_crs_authid="EPSG:2154")
    res = GeomAnalyzer(opts).analyze(vl)
    mem, stats = GeomAnalyzer.build_repaired_layer(
        vl, res, RepairOptions(fix_invalid=True))
    assert mem is not None
    ecrits = sorted(f["nom"] for f in mem.getFeatures())
    assert ecrits == ["a_cheval", "dedans_a", "dedans_b",
                      "dedans_c_chevauche_b"], ecrits
    assert "voisin_dehors" not in ecrits and "loin_dehors" not in ecrits
    assert stats.rejected == 0
    print("OK aoi_repair_output_covers_exactly_the_analysed_zone")


def test_no_aoi_leaves_every_counter_untouched():
    """Sans zone, RIEN ne doit changer (non-régression de l'existant).

    L'AOI est facultative : le chemin sans zone doit rendre exactement les
    mêmes compteurs, le même score et le même périmètre de sortie qu'avant
    son introduction.
    """
    _ensure_qgis()
    vl = _layer_for_aoi()
    base = AnalysisOptions(check_overlaps=True)
    res = GeomAnalyzer(base).analyze(vl)
    assert res.has_aoi() is False
    assert res.aoi_wkt == "" and res.aoi_features == 0
    assert res.total_features == 6
    assert res.quality_report.overlap_pairs == 2
    assert _noms(vl, res.flagged["overlap"]) == [
        "a_cheval", "dedans_b", "dedans_c_chevauche_b", "voisin_dehors"]
    # Toute la couche ressort de la réparation.
    mem, _stats = GeomAnalyzer.build_repaired_layer(
        vl, res, RepairOptions(fix_invalid=True))
    assert mem.featureCount() == 6, mem.featureCount()
    print("OK no_aoi_leaves_every_counter_untouched")


def test_aoi_is_never_persisted_between_sessions():
    """Une zone ne survit pas à la fermeture du panneau.

    Une zone tracée la semaine dernière et restaurée en silence restreindrait
    une analyse sans que rien ne le dise. ``save_options`` n'écrit donc aucune
    clé d'AOI, et ``load_options`` n'en rend aucune.
    """
    _ensure_qgis()
    from stat_geom_qc import settings as sg_settings
    opts = AnalysisOptions(aoi_rect=_aoi_rect(0, 0, 100, 100),
                           aoi_crs_authid="EPSG:2154")
    sg_settings.save_options(opts)
    relu = sg_settings.load_options()
    assert relu.aoi_rect is None, relu.aoi_rect
    assert relu.aoi_crs_authid == ""
    print("OK aoi_is_never_persisted_between_sessions")


def test_aoi_row_is_off_by_default_and_separate_from_the_checks():
    """La ligne « Restreindre à une zone » vit à part des contrôles.

    Ce n'est pas un contrôle qualité, c'est le PÉRIMÈTRE sur lequel les
    contrôles portent : elle n'entre donc pas dans les défauts du panneau, et
    n'est jamais mémorisée d'une session à l'autre.
    """
    _ensure_qgis()
    from stat_geom_qc.stat_geom_dockwidget import StatGeomDockWidget
    dock = StatGeomDockWidget(_IfaceStub())
    try:
        assert dock.chk_aoi.isChecked() is False, "l'AOI est facultative"
        assert dock._aoi_for_options() == (None, "")
        assert "aoi" not in C.UI_DEFAULT_CHECKS
        # Activer une zone ne touche à AUCUNE case de contrôle.
        avant = dock.chk_overlaps.isChecked(), dock.chk_nested_height.isChecked()
        dock.chk_aoi.setChecked(True)
        dock._set_aoi(QgsRectangle(*_aoi_rect(0, 0, 100, 100)), "EPSG:2154")
        assert (dock.chk_overlaps.isChecked(),
                dock.chk_nested_height.isChecked()) == avant
        rect, authid = dock._aoi_for_options()
        assert rect is not None and authid == "EPSG:2154"
        # Décocher OUBLIE la zone : pas de périmètre fantôme sous une case
        # décochée.
        dock.chk_aoi.setChecked(False)
        assert dock._aoi_for_options() == (None, "")
        assert dock._aoi_rect is None
        # « Réinitialiser » aussi.
        dock.chk_aoi.setChecked(True)
        dock._set_aoi(QgsRectangle(*_aoi_rect(0, 0, 100, 100)), "EPSG:2154")
        dock._reset_options()
        assert dock.chk_aoi.isChecked() is False
        assert dock._aoi_for_options() == (None, "")
    finally:
        dock.deleteLater()
    print("OK aoi_row_is_off_by_default_and_separate_from_the_checks")


def test_aoi_draw_tool_refuses_a_degenerate_rectangle():
    """Un simple clic ne définit pas une zone.

    Un rectangle plat n'aurait sélectionné aucune entité, et rien ne l'aurait
    expliqué à l'utilisateur : l'outil annule plutôt que d'émettre une zone
    vide.

    C'est la DÉCISION qui est vérifiée ici, pas le maniement de la souris :
    instancier un ``QgsMapTool`` réclame une application QGIS graphique, que
    cette suite headless n'a pas (elle se termine par une faute de
    segmentation). Le tracé lui-même se vérifie dans un vrai QGIS.
    """
    _ensure_qgis()
    from stat_geom_qc.aoi_tool import rect_is_usable

    assert rect_is_usable(QgsRectangle(*_aoi_rect(0, 0, 100, 100))) is True
    assert rect_is_usable(None) is False
    # Simple clic : les deux coins sont confondus.
    assert rect_is_usable(QgsRectangle(*_aoi_rect(80, 80, 80, 80))) is False
    # Glissé purement horizontal ou vertical : une bande sans surface.
    assert rect_is_usable(QgsRectangle(*_aoi_rect(0, 50, 100, 50))) is False
    assert rect_is_usable(QgsRectangle(*_aoi_rect(50, 0, 50, 100))) is False
    print("OK aoi_draw_tool_refuses_a_degenerate_rectangle")


# ── Correctifs de l'audit AOI (v2.9.44) ────────────────────────────────────

def test_aoi_summary_never_depends_on_which_checks_are_ticked():
    """Le bilan de la zone doit sortir MEME sans contrôle de paires.

    Il était produit à l'intérieur du bloc qui indexe le voisinage, lequel ne
    tourne que si au moins un contrôle de paires est coché. Décocher les
    chevauchements, la hauteur imbriquée et les croisements de lignes suffisait
    donc à faire disparaître l'avertissement « analyse restreinte », à laisser
    aoi_features à 0 et result.bounds sur la couche entière — et le rapport
    annonçait « Restreinte — 0 entité(s) sur 6 » après en avoir analysé 4.
    Une restriction muette est exactement ce que cette fonction doit empêcher.
    """
    _ensure_qgis()
    from stat_geom_qc import report as sg_report
    vl = _layer_for_aoi()
    zone = _aoi_rect(0, 0, 100, 100)

    bilans = {}
    for nom, opts in (
        ("avec_paires", AnalysisOptions(
            check_overlaps=True, aoi_rect=zone, aoi_crs_authid="EPSG:2154")),
        ("sans_paires", AnalysisOptions(
            check_overlaps=False, check_nested_height=False,
            check_line_crossings=False, aoi_rect=zone,
            aoi_crs_authid="EPSG:2154")),
    ):
        res = GeomAnalyzer(opts).analyze(vl)
        bilans[nom] = res
        assert res.aoi_features == 4, (nom, res.aoi_features)
        assert res.total_features == 4, (nom, res.total_features)
        assert any("RESTREINTE" in w for w in res.warnings), (nom, res.warnings)
        ligne = dict(sg_report.configuration_rows(res))["Zone d'analyse"]
        assert "4" in ligne, (nom, ligne)
        # L'étendue annoncée est celle des entités analysées, pas de la couche.
        assert res.bounds["maxx"] < _AOI_OX + 200, (nom, res.bounds)

    # Les deux chemins doivent décrire le MÊME périmètre.
    a, b = bilans["avec_paires"], bilans["sans_paires"]
    assert a.aoi_features == b.aoi_features
    assert a.bounds == b.bounds
    print("OK aoi_summary_never_depends_on_which_checks_are_ticked")


def test_aoi_preview_shows_only_features_of_the_zone():
    """L'onglet « Attributs » ne doit pas montrer d'entités hors zone.

    L'échantillon lisait les N premières entités de la COUCHE : il affichait
    donc des lignes qu'aucun contrôle n'avait examinées, et sur une couche
    triée géographiquement il pouvait n'en montrer aucune de la zone.
    """
    _ensure_qgis()
    vl = _layer_for_aoi()
    res = GeomAnalyzer(AnalysisOptions(
        aoi_rect=_aoi_rect(0, 0, 100, 100),
        aoi_crs_authid="EPSG:2154")).analyze(vl)
    col = list(res.preview_columns).index("nom")
    noms = sorted(row[col] for row in res.preview_rows)
    assert noms == ["a_cheval", "dedans_a", "dedans_b",
                    "dedans_c_chevauche_b"], noms
    assert not any("dehors" in n for n in noms)

    # Sans zone, l'échantillon reste celui de toute la couche.
    complet = GeomAnalyzer(AnalysisOptions()).analyze(vl)
    assert len(complet.preview_rows) == 6, len(complet.preview_rows)
    print("OK aoi_preview_shows_only_features_of_the_zone")


def test_aoi_membership_rule_is_written_once():
    """Une seule définition de « l'entité est-elle dans la zone ».

    Le test était écrit deux fois — dans analyze() et dans
    build_repaired_layer(). Deux écritures d'une même règle finissent par
    diverger, et la réparation aurait alors produit un extrait d'un périmètre
    différent de celui que l'analyse avait contrôlé.
    """
    _ensure_qgis()
    from stat_geom_qc.analysis_engine import feature_in_aoi
    zone = QgsGeometry.fromRect(QgsRectangle(*_aoi_rect(0, 0, 100, 100)))
    dedans = QgsGeometry.fromWkt(_aoi_box(10, 10, 30, 30))
    cheval = QgsGeometry.fromWkt(_aoi_box(95, 10, 130, 30))
    dehors = QgsGeometry.fromWkt(_aoi_box(300, 300, 320, 320))
    assert feature_in_aoi(dedans, zone) is True
    assert feature_in_aoi(cheval, zone) is True, "a cheval = retenue ENTIERE"
    assert feature_in_aoi(dehors, zone) is False
    assert feature_in_aoi(None, zone) is False, "sans geometrie, aucune zone"
    assert feature_in_aoi(dehors, None) is True, "sans zone, tout est retenu"

    # Et le moteur ne doit plus porter de second test « intersects » sur l'AOI.
    import io as _io
    source = _io.open(os.path.join(_PLUGIN_DIR, "analysis_engine.py"),
                      encoding="utf-8").read()
    for methode in ("def analyze(", "def build_repaired_layer("):
        corps = source.split(methode)[1].split("\n    def ")[0]
        assert "aoi_geom.intersects" not in corps, (
            "%s reecrit la regle au lieu d'appeler feature_in_aoi" % methode)
        assert "feature_in_aoi" in corps, methode
    print("OK aoi_membership_rule_is_written_once")


def test_teardown_releases_the_aoi_band_and_the_layer_signals():
    """Décharger le plugin doit tout rendre à QGIS.

    ``cancel_tasks`` n'effaçait que la surbrillance : le rectangle bleu de la
    zone restait dessiné sur la carte, sans plus rien dans le panneau des
    couches pour l'expliquer ni personne pour l'effacer — le défaut même que sa
    docstring dit corriger. L'outil de tracé et les signaux d'édition de la
    couche analysée restaient eux aussi branchés.
    """
    _ensure_qgis()
    from stat_geom_qc.stat_geom_dockwidget import StatGeomDockWidget
    vl = _make_layer()
    _add(vl, "POLYGON((0 0,0 10,10 10,10 0,0 0))")
    dock = StatGeomDockWidget(_IfaceStub())
    try:
        efface = {"band": False}

        class _BandeFactice:
            def scene(self):
                efface["band"] = True
                return None

        dock._aoi_band = _BandeFactice()
        dock._watch_layer_edits(vl)
        assert dock._watched_layer is vl

        dock.cancel_tasks()

        assert dock._aoi_band is None, "le rectangle de zone survit au dechargement"
        assert efface["band"] is True, "la bande n'a meme pas ete sollicitee"
        assert dock._watched_layer is None, "les signaux d'edition restent branches"
        assert dock._aoi_tool is None and dock._aoi_previous_tool is None
    finally:
        dock._unwatch_layer_edits()
        dock.deleteLater()
    print("OK teardown_releases_the_aoi_band_and_the_layer_signals")


def test_drawing_a_zone_twice_does_not_trap_the_user_in_the_tool():
    """Deux clics sur « Dessiner… » ne doivent pas enfermer dans l'outil.

    Le second clic relisait ``canvas.mapTool()`` — qui renvoyait l'outil de
    tracé LUI-MÊME. Il devenait « l'outil précédent », et le rétablir en fin de
    tracé remettait l'utilisateur en mode zone, sans aucune sortie visible.
    """
    _ensure_qgis()
    from stat_geom_qc.stat_geom_dockwidget import StatGeomDockWidget
    dock = StatGeomDockWidget(_IfaceStub())
    try:
        outil_initial = object()
        outil_de_trace = object()

        class _CanevasFactice:
            def __init__(self):
                self.courant = outil_initial
                self.poses = 0

            def mapTool(self):
                return self.courant

            def setMapTool(self, tool):
                self.poses += 1
                self.courant = tool

        canevas = _CanevasFactice()
        dock._canvas = lambda: canevas
        # Un tracé est déjà en cours.
        dock._aoi_tool = outil_de_trace
        dock._aoi_previous_tool = outil_initial

        dock._draw_aoi()

        assert dock._aoi_tool is outil_de_trace, "un second outil a ete cree"
        assert dock._aoi_previous_tool is outil_initial, \
            "l'outil de trace est devenu son propre outil precedent"
        assert canevas.poses == 0, "le canevas ne devait pas changer d'outil"

        # Et le retour rend bien l'outil d'origine.
        dock._restore_map_tool()
        assert canevas.courant is outil_initial
        assert dock._aoi_tool is None
    finally:
        dock.deleteLater()
    print("OK drawing_a_zone_twice_does_not_trap_the_user_in_the_tool")


def test_verification_after_an_aoi_repair_states_what_it_cannot_recheck():
    """Sous zone, « 0 anomalie restante » ne doit pas être dit sans réserve.

    Le fichier vérifié est l'EXTRAIT de la zone : les voisins n'y figurent pas.
    Un chevauchement de bord — pourtant bien signalé par l'analyse d'origine
    grâce à la passe de voisinage — n'a donc plus de second membre et ne peut
    pas être recontrôlé. L'annoncer « 0 anomalie restante » tout court serait un
    faux « conforme ».
    """
    _ensure_qgis()
    from stat_geom_qc.stat_geom_dockwidget import StatGeomDockWidget
    vl = _layer_for_aoi()
    dock = StatGeomDockWidget(_IfaceStub())
    try:
        class _TacheFactice:
            def __init__(self, res):
                self.result = res

        propre = _make_layer()
        _add(propre, "POLYGON((0 0,0 10,10 10,10 0,0 0))")
        verif = GeomAnalyzer(AnalysisOptions()).analyze(propre)
        assert verif.total_issues() == 0

        # 1) Analyse SANS zone : aucune réserve à formuler.
        dock._result = GeomAnalyzer(AnalysisOptions()).analyze(vl)
        dock._verify_task = _TacheFactice(verif)
        dock._on_verify_completed()
        sans_zone = dock.status_label.text()
        assert "0 anomalie restante" in sans_zone, sans_zone
        assert "serve" not in sans_zone, sans_zone

        # 2) Analyse SOUS zone : la réserve doit accompagner le verdict.
        dock._result = GeomAnalyzer(AnalysisOptions(
            check_overlaps=True, aoi_rect=_aoi_rect(0, 0, 100, 100),
            aoi_crs_authid="EPSG:2154")).analyze(vl)
        assert dock._result.has_aoi()
        dock._verify_task = _TacheFactice(verif)
        dock._on_verify_completed()
        sous_zone = dock.status_label.text()
        assert "serve" in sous_zone, sous_zone
        assert "PAIRE" in sous_zone, sous_zone
    finally:
        dock.deleteLater()
    print("OK verification_after_an_aoi_repair_states_what_it_cannot_recheck")


def test_score_delta_is_hidden_for_a_not_evaluated_grade():
    """Une note « Non évalué » ne se compare pas.

    Elle vaut 0 sans avoir rien mesuré : la comparer à l'analyse précédente
    affichait « −100 pts » en rouge, comme un effondrement de la qualité, alors
    que rien n'avait été regardé.
    """
    _ensure_qgis()
    from stat_geom_qc.stat_geom_dockwidget import StatGeomDockWidget
    vl = _layer_for_aoi()
    dock = StatGeomDockWidget(_IfaceStub())
    try:
        # Zone vide -> note non évaluée.
        vide = GeomAnalyzer(AnalysisOptions(
            aoi_rect=_aoi_rect(5000, 5000, 5100, 5100),
            aoi_crs_authid="EPSG:2154")).analyze(vl)
        assert vide.correctness.grade == C.GRADE_NOT_EVALUATED
        dock._result = vide
        # Sentinelle plutot que isVisible() : sur un panneau jamais
        # affiche, isVisible() vaut TOUJOURS faux et l'assertion ne
        # prouverait rien.
        dock.grade_delta.setText("SENTINELLE")
        dock._update_score_delta(vide.correctness.score, 100.0)
        assert dock.grade_delta.text() == "SENTINELLE", (
            "un ecart a ete calcule contre une note qui n'en est pas "
            "une : %r" % dock.grade_delta.text())

        # Une vraie note, elle, se compare normalement.
        vraie = GeomAnalyzer(AnalysisOptions()).analyze(vl)
        dock._result = vraie
        dock._update_score_delta(80.0, 100.0)
        assert "20" in dock.grade_delta.text(), dock.grade_delta.text()
    finally:
        dock.deleteLater()
    print("OK score_delta_is_hidden_for_a_not_evaluated_grade")


def test_automatic_repair_never_overwrites_without_asking():
    """Une réparation ne remplace pas en silence une réparation précédente.

    Depuis que la sortie est écrite automatiquement à côté de la source, plus
    aucun dialogue d'enregistrement ne prévient qu'un fichier du même nom
    existe : deux réparations d'affilée remplaçaient la première SANS RIEN
    DIRE — y compris si l'utilisateur avait entre-temps travaillé dessus.
    """
    _ensure_qgis()
    import contextlib
    import io as _io
    import tempfile
    from stat_geom_qc.stat_geom_dockwidget import StatGeomDockWidget

    dossier = tempfile.mkdtemp(prefix="sgqc_ecrasement_")
    cible = os.path.join(dossier, "couche_repare.gpkg")
    dock = StatGeomDockWidget(_IfaceStub())
    try:
        # 1) Le fichier n'existe pas : aucune question, le chemin passe tel quel.
        appels = {"n": 0}
        dock._confirm_repair_output.__func__  # la méthode existe bien
        assert dock._confirm_repair_output(cible) == cible
        assert appels["n"] == 0

        # 2) Le fichier existe : la décision revient à l'utilisateur. On
        #    substitue la boîte de dialogue, qui ne peut pas s'ouvrir en test.
        _io.open(cible, "w", encoding="utf-8").write("deja la")

        for choix, attendu in (
            ("ecraser", cible),
            ("nouveau", os.path.join(dossier, "couche_repare_2.gpkg")),
            ("annuler", None),
        ):
            def _fausse_boite(chemin, _c=choix):
                appels["n"] += 1
                if _c == "ecraser":
                    return chemin
                if _c == "nouveau":
                    return StatGeomDockWidget._free_variant(chemin)
                return None

            dock._confirm_repair_output = _fausse_boite
            assert dock._confirm_repair_output(cible) == attendu, choix

        assert appels["n"] == 3, "la garde n'a pas été sollicitée à chaque fois"

        # 3) Le nom de repli proposé est bien LIBRE.
        libre = StatGeomDockWidget._free_variant(cible)
        assert libre.endswith("couche_repare_2.gpkg"), libre
        assert not os.path.exists(libre)
        _io.open(libre, "w", encoding="utf-8").write("occupe aussi")
        assert StatGeomDockWidget._free_variant(cible).endswith(
            "couche_repare_3.gpkg")

        # 4) Et le flux de réparation appelle bien la garde AVANT d'écrire.
        source = _io.open(os.path.join(_PLUGIN_DIR, "stat_geom_dockwidget.py"),
                          encoding="utf-8").read()
        corps = source.split("def _repair_geometries(")[1].split("\n    def ")[0]
        i_garde = corps.find("_confirm_repair_output")
        i_tache = corps.find("RepairTask(")
        assert i_garde != -1, "la garde d'écrasement a disparu du flux"
        assert i_tache != -1 and i_garde < i_tache, \
            "la réparation démarre avant d'avoir demandé quoi que ce soit"
    finally:
        dock.deleteLater()
        for nom in os.listdir(dossier):
            with contextlib.suppress(Exception):
                os.remove(os.path.join(dossier, nom))
        with contextlib.suppress(Exception):
            os.rmdir(dossier)
    print("OK automatic_repair_never_overwrites_without_asking")


def _run_all():
    _ensure_qgis()
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback
            print("FAIL %s : %s" % (t.__name__, exc))
            traceback.print_exc()
    print("\n%d/%d tests réussis." % (len(tests) - failures, len(tests)))
    return failures


if __name__ == "__main__":
    rc = _run_all()
    if _APP is not None:
        _APP.exitQgis()
    sys.exit(1 if rc else 0)
