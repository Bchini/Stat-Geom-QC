# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════╗
║  STAT GEOM QC - Moteur d'analyse (API native QGIS)                 ║
║                                                                    ║
║  Portage de GISAnalysisEngine (App_Stat_GEOM) vers l'API PyQGIS, sans ║
║  dépendance à geopandas / matplotlib. Fonctionne directement sur les couches  ║
║  vectorielles chargées dans QGIS.                                     ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from qgis.core import (
    Qgis,
    QgsDistanceArea,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsProject,
    QgsSpatialIndex,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsWkbTypes,
)


# ════════════════════════════════════════════════════════════════
# OPTIONS & RÉSULTATS
# ════════════════════════════════════════════════════════════════


@dataclass
class AnalysisOptions:
    """Paramètres d'exécution de l'analyse."""

    small_polygon_threshold_m2: float = 2.0
    topology_checks: bool = True
    check_duplicates: bool = True
    check_buildings: bool = False         # comparaison AGL vs AMSL (désactivé par défaut)
    check_overlaps: bool = True           # chevauchements/intersections entre polygones
    overlap_max_features: int = 100000    # au-delà, contrôle des chevauchements ignoré
    preview_limit_rows: int = 200
    invalid_details_limit: int = 20


@dataclass
class RepairOptions:
    """Types de corrections à appliquer lors de la réparation géométrique.

    Chaque option correspond à une case à cocher de l'interface : l'utilisateur
    choisit précisément quelles anomalies sont corrigées automatiquement.
    """

    fix_invalid: bool = True          # géométries invalides -> make valid
    fix_self_intersection: bool = False  # auto-intersections -> make valid (sous-ensemble d'invalides)
    remove_null_empty: bool = False   # supprimer les entités à géométrie nulle/vide
    remove_duplicates: bool = False   # supprimer les doublons (conserver la 1re occurrence)
    remove_small: bool = False        # supprimer les petits polygones (≤ seuil m²)
    fix_overlaps: bool = False        # rogner UNIQUEMENT les chevauchements mineurs
    overlap_max_fraction: float = 0.05  # part max de surface rognée pour juger un chevauchement « mineur »

    def any_selected(self) -> bool:
        return bool(
            self.fix_invalid or self.fix_self_intersection or self.remove_null_empty
            or self.remove_duplicates or self.remove_small or self.fix_overlaps
        )


@dataclass
class RepairStats:
    """Bilan chiffré d'une opération de réparation."""

    fixed_invalid: int = 0        # géométries rendues valides
    still_invalid: int = 0        # géométries encore invalides après make valid
    removed_null_empty: int = 0
    removed_duplicates: int = 0
    removed_small: int = 0
    fixed_overlaps: int = 0       # chevauchements mineurs rognés automatiquement
    manual_overlaps: int = 0      # chevauchements trop importants -> correction manuelle
    manual_overlap_fids: List[int] = field(default_factory=list)  # FID source à revoir
    written: int = 0              # entités écrites dans le nouveau fichier

    def total_changes(self) -> int:
        return (
            self.fixed_invalid + self.removed_null_empty
            + self.removed_duplicates + self.removed_small + self.fixed_overlaps
        )


@dataclass
class QualityReport:
    """Métriques détaillées de qualité géométrique."""

    valid_count: int = 0
    invalid_count: int = 0
    null_empty_count: int = 0            # géométries nulles OU vides (type unifié)
    duplicate_count: int = 0
    small_area_count: int = 0
    self_intersection_count: int = 0
    overlap_count: int = 0               # entités en chevauchement avec ≥1 autre
    overlap_pairs: int = 0               # nombre de paires de polygones en intersection
    invalid_details: List[str] = field(default_factory=list)


@dataclass
class CorrectnessScore:
    """Score de correctness (qualité globale des données)."""

    score: float = 100.0                       # 0..100
    grade: str = "N/A"                          # Excellent / Bon / ...
    color: str = "#22c55e"                      # couleur associée au grade
    components: Dict[str, float] = field(default_factory=dict)   # sous-scores 0..100
    penalties: Dict[str, float] = field(default_factory=dict)    # pénalités appliquées


@dataclass
class AnalysisResult:
    """Résultat structuré d'une analyse de couche."""

    layer_name: str = ""
    source: str = ""
    provider: str = ""
    crs_authid: str = ""
    crs_description: str = ""
    crs_is_geographic: bool = False
    total_features: int = 0
    geometry_types: Dict[str, int] = field(default_factory=dict)
    null_geometries: int = 0            # nulles OU vides (type unifié)
    invalid_geometries: int = 0
    polygons_below_threshold: int = 0
    duplicate_geometries: int = 0
    overlapping_features: int = 0
    buildings_agl_over_amsl: Any = 0
    bounds: Dict[str, float] = field(default_factory=dict)
    attribute_info: Dict[str, Any] = field(default_factory=dict)
    preview_columns: List[str] = field(default_factory=list)
    preview_rows: List[List[str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    quality_report: QualityReport = field(default_factory=QualityReport)
    correctness: CorrectnessScore = field(default_factory=CorrectnessScore)
    processing_time: float = 0.0
    timestamp: str = ""
    threshold_m2: float = 2.0
    # Identifiants d'entités problématiques, par catégorie (pour sélection / couche d'erreurs)
    flagged: Dict[str, List[int]] = field(default_factory=dict)

    def total_issues(self) -> int:
        q = self.quality_report
        agl_count = (
            self.buildings_agl_over_amsl
            if isinstance(self.buildings_agl_over_amsl, int)
            else 0
        )
        return (
            q.null_empty_count
            + q.invalid_count
            + q.self_intersection_count
            + q.small_area_count
            + q.duplicate_count
            + q.overlap_count
            + agl_count
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "layer_name": self.layer_name,
            "source": self.source,
            "provider": self.provider,
            "crs": {
                "authid": self.crs_authid,
                "description": self.crs_description,
                "is_geographic": self.crs_is_geographic,
            },
            "total_features": self.total_features,
            "geometry_types": self.geometry_types,
            "null_geometries": self.null_geometries,
            "invalid_geometries": self.invalid_geometries,
            "polygons_below_threshold": self.polygons_below_threshold,
            "threshold_m2": self.threshold_m2,
            "duplicate_geometries": self.duplicate_geometries,
            "overlapping_features": self.overlapping_features,
            "buildings_agl_over_amsl": self.buildings_agl_over_amsl,
            "bounds": self.bounds,
            "attribute_info": self.attribute_info,
            "warnings": self.warnings,
            "errors": self.errors,
            "quality_report": {
                "valid_count": self.quality_report.valid_count,
                "invalid_count": self.quality_report.invalid_count,
                "null_empty_count": self.quality_report.null_empty_count,
                "duplicate_count": self.quality_report.duplicate_count,
                "small_area_count": self.quality_report.small_area_count,
                "self_intersection_count": self.quality_report.self_intersection_count,
                "overlap_count": self.quality_report.overlap_count,
                "overlap_pairs": self.quality_report.overlap_pairs,
                "invalid_details": self.quality_report.invalid_details,
            },
            "correctness": {
                "score": self.correctness.score,
                "grade": self.correctness.grade,
                "components": self.correctness.components,
                "penalties": self.correctness.penalties,
            },
            "processing_time": self.processing_time,
            "timestamp": self.timestamp,
        }


class AnalysisCancelled(Exception):
    """Levée lorsque l'utilisateur annule l'analyse."""


# ════════════════════════════════════════════════════════════════
# SCORE DE CORRECTNESS
# ════════════════════════════════════════════════════════════════

# Pondérations des pénalités (proportion d'entités concernées -> pénalité).
# Les problèmes critiques (géométries nulles/vides/invalides) pèsent le plus.
_WEIGHTS = {
    "null_empty": 1.00,
    "invalid": 1.00,
    "overlap": 0.70,
    "duplicate": 0.60,
    "agl": 0.50,
    "small": 0.40,
}

_GRADE_BANDS = [
    (95.0, "Excellent", "#16a34a"),
    (85.0, "Bon", "#22c55e"),
    (70.0, "Correct", "#eab308"),
    (50.0, "Faible", "#f97316"),
    (0.0, "Critique", "#ef4444"),
]


def compute_correctness(result: AnalysisResult, include_buildings: bool) -> CorrectnessScore:
    """Calcule un score de qualité 0..100 à partir des métriques détectées.

    Modèle par pénalités : chaque catégorie de problème retranche une part
    proportionnelle au ratio d'entités concernées, pondérée par sa gravité.
    """
    total = max(result.total_features, 1)
    q = result.quality_report

    ratios = {
        "null_empty": q.null_empty_count / total,
        "invalid": q.invalid_count / total,
        "duplicate": q.duplicate_count / total,
        "small": q.small_area_count / total,
        "overlap": q.overlap_count / total,
    }
    agl_over = result.buildings_agl_over_amsl if isinstance(result.buildings_agl_over_amsl, int) else 0
    if include_buildings:
        ratios["agl"] = agl_over / total

    penalties = {k: round(min(1.0, r) * _WEIGHTS[k] * 100.0, 2) for k, r in ratios.items()}
    components = {k: round(max(0.0, 1.0 - min(1.0, r)) * 100.0, 1) for k, r in ratios.items()}

    total_penalty = sum(penalties.values())
    score = round(max(0.0, min(100.0, 100.0 - total_penalty)), 1)

    # Règle : un score parfait (100/100) est réservé aux données sans AUCUNE
    # anomalie. Dès qu'une erreur subsiste — même en proportion infime, où
    # l'arrondi des pénalités ramènerait le total à 0 — le score est plafonné
    # sous 100 pour ne jamais afficher « 100/100 » avec des erreurs.
    issues_present = (
        q.null_empty_count or q.invalid_count or q.duplicate_count
        or q.small_area_count or q.overlap_count
        or (include_buildings and agl_over)
    )
    if issues_present and score >= 100.0:
        score = 99.9

    grade, color = "Critique", "#ef4444"
    for threshold, label, col in _GRADE_BANDS:
        if score >= threshold:
            grade, color = label, col
            break

    return CorrectnessScore(
        score=score, grade=grade, color=color, components=components, penalties=penalties
    )


# ════════════════════════════════════════════════════════════════
# MOTEUR D'ANALYSE
# ════════════════════════════════════════════════════════════════


class GeomAnalyzer:
    """Analyse de qualité géométrique d'une couche vectorielle QGIS."""

    def __init__(self, options: Optional[AnalysisOptions] = None):
        self.options = options or AnalysisOptions()

    # -- Aides ------------------------------------------------------------------

    @staticmethod
    def _find_field(layer: QgsVectorLayer, name: str) -> Optional[str]:
        """Retrouve un champ par nom, insensible à la casse."""
        target = name.strip().lower()
        for f in layer.fields():
            if f.name().strip().lower() == target:
                return f.name()
        return None

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            try:
                return float(str(value).replace(",", ".").strip())
            except (TypeError, ValueError):
                return None

    def _build_area_calculator(self, layer: QgsVectorLayer, result: AnalysisResult):
        """Configure un QgsDistanceArea pour des surfaces en m² (ellipsoïdal)."""
        da = QgsDistanceArea()
        try:
            da.setSourceCrs(layer.crs(), QgsProject.instance().transformContext())
            ellipsoid = QgsProject.instance().ellipsoid()
            if not ellipsoid or ellipsoid == "NONE":
                ellipsoid = "WGS84"
            da.setEllipsoid(ellipsoid)
        except Exception as exc:  # pragma: no cover - dépend de la version QGIS
            result.warnings.append("Calcul de surface dégradé : %s" % exc)
        return da

    def _measure_area_m2(self, da: QgsDistanceArea, geom: QgsGeometry) -> Optional[float]:
        try:
            raw = da.measureArea(geom)
            return da.convertAreaMeasurement(raw, QgsUnitTypes.AreaSquareMeters)
        except Exception:
            return None

    @staticmethod
    def _first_validation_error(geom: QgsGeometry):
        """Renvoie (message, est_auto_intersection) pour une géométrie invalide.

        Utilise de préférence le moteur GEOS (messages explicites type
        « Self-intersection ») avec repli sur le validateur interne QGIS.
        """
        errors = []
        try:
            engine = Qgis.GeometryValidationEngine.Geos
            errors = geom.validateGeometry(engine)
        except Exception:
            try:
                errors = geom.validateGeometry()
            except Exception:
                errors = []
        if not errors:
            return ("géométrie invalide", False)
        msg = errors[0].what() or "géométrie invalide"
        # On balaie TOUTES les erreurs : une auto-intersection peut n'être
        # signalée qu'en seconde position (« Ring self-intersection », etc.).
        is_self = False
        for err in errors:
            low = (err.what() or "").lower()
            if ("self" in low) or ("intersect" in low):
                is_self = True
                break
        return (msg, is_self)

    @staticmethod
    def _detect_overlaps(index, polygon_fids, check_cancel, progress_cb):
        """Détecte les paires de polygones dont les *intérieurs* se recouvrent
        (aire d'intersection > 0 → chevauchement/superposition). Les contacts
        par simple frontière (intersection linéaire/ponctuelle) et les doublons
        exacts sont exclus. Renvoie (ensemble de fids concernés, nb de paires)."""
        overlap_fids = set()
        pairs = 0
        n = len(polygon_fids)
        for i, fid in enumerate(polygon_fids):
            if i % 200 == 0:
                check_cancel()
                if n:
                    progress_cb(i / n)
            g = index.geometry(fid)
            if g is None or g.isNull():
                continue
            try:
                candidates = index.intersects(g.boundingBox())
            except Exception:  # nosec B112 - garde défensive API QGIS, entité ignorée
                continue
            for cand in candidates:
                if cand <= fid:
                    continue  # chaque paire traitée une seule fois (évite aussi self)
                og = index.geometry(cand)
                if og is None or og.isNull():
                    continue
                try:
                    if not g.intersects(og):
                        continue
                    if g.equals(og):
                        continue  # doublon exact : déjà comptabilisé séparément
                    inter = g.intersection(og)
                    if (inter is not None and not inter.isNull() and not inter.isEmpty()
                            and QgsWkbTypes.geometryType(inter.wkbType())
                            == QgsWkbTypes.PolygonGeometry
                            and inter.area() > 0):
                        pairs += 1
                        overlap_fids.add(fid)
                        overlap_fids.add(cand)
                except Exception:  # nosec B112 - garde défensive API QGIS, paire ignorée
                    continue
        return overlap_fids, pairs

    # -- Analyse principale -----------------------------------------------------

    def analyze(
        self,
        layer: QgsVectorLayer,
        progress_cb: Optional[Callable[[float, str], None]] = None,
        is_canceled: Optional[Callable[[], bool]] = None,
    ) -> AnalysisResult:
        """Analyse complète d'une couche. Peut lever AnalysisCancelled."""
        import time

        started = time.time()
        opt = self.options

        def report(pct: float, msg: str = ""):
            if progress_cb:
                progress_cb(pct, msg)

        def check_cancel():
            if is_canceled and is_canceled():
                raise AnalysisCancelled()

        result = AnalysisResult()
        result.layer_name = layer.name()
        result.source = layer.source()
        result.provider = layer.dataProvider().name() if layer.dataProvider() else ""
        result.threshold_m2 = opt.small_polygon_threshold_m2
        result.timestamp = _now_iso()

        crs = layer.crs()
        result.crs_authid = crs.authid() or "Inconnu"
        result.crs_description = crs.description() or "Inconnu"
        result.crs_is_geographic = bool(crs.isGeographic())

        # Étendue spatiale
        try:
            ext = layer.extent()
            result.bounds = {
                "minx": ext.xMinimum(),
                "miny": ext.yMinimum(),
                "maxx": ext.xMaximum(),
                "maxy": ext.yMaximum(),
            }
        except Exception as exc:
            result.errors.append("Étendue indisponible : %s" % exc)

        # Attributs
        fields = layer.fields()
        result.attribute_info = {
            "columns": [f.name() for f in fields],
            "dtypes": {f.name(): f.typeName() for f in fields},
        }
        result.preview_columns = [f.name() for f in fields]

        agl_field = self._find_field(layer, "AGL") if opt.check_buildings else None
        amsl_field = self._find_field(layer, "AMSL") if opt.check_buildings else None
        buildings_enabled = bool(agl_field and amsl_field)
        if opt.check_buildings and not buildings_enabled:
            result.buildings_agl_over_amsl = "Attributs AGL/AMSL manquants"

        total = layer.featureCount()
        if total < 0:  # certains fournisseurs renvoient -1
            total = None
        result.total_features = total or 0

        da = self._build_area_calculator(layer, result)
        can_measure_area = True

        # Collecteurs
        q = result.quality_report
        geom_types: Dict[str, int] = {}
        wkb_index: Dict[bytes, List[int]] = {}
        flagged = {
            "null_empty": [], "invalid": [], "self_intersection": [],
            "small": [], "duplicate": [], "overlap": [], "agl": [],
        }
        invalid_details: List[str] = []
        self_intersections = 0
        agl_over = 0
        agl_non_numeric = 0
        preview_rows: List[List[str]] = []

        # Index spatial pour la détection des chevauchements entre polygones.
        overlap_index = None
        polygon_fids: List[int] = []
        if opt.check_overlaps:
            try:
                overlap_index = QgsSpatialIndex(QgsSpatialIndex.FlagStoreFeatureGeometries)
            except Exception:
                overlap_index = None

        report(5, "Lecture des entités…")

        processed = 0
        denom = total if total else 1
        for feat in layer.getFeatures(QgsFeatureRequest()):
            if processed % 500 == 0:
                check_cancel()
                if total:
                    report(5 + 80.0 * processed / denom, "Analyse %d/%d…" % (processed, total))
                else:
                    report(50, "Analyse %d entités…" % processed)

            fid = feat.id()

            # Aperçu attributaire (N premières entités)
            if len(preview_rows) < opt.preview_limit_rows:
                preview_rows.append([_attr_str(feat[name]) for name in result.preview_columns])

            geom = feat.geometry() if feat.hasGeometry() else None

            # --- Géométries nulles ou vides (type de problème unifié) ---
            if geom is None or geom.isNull() or geom.isEmpty():
                q.null_empty_count += 1
                flagged["null_empty"].append(fid)
                processed += 1
                continue

            # --- Type de géométrie ---
            try:
                tname = QgsWkbTypes.displayString(geom.wkbType())
            except Exception:
                tname = "Inconnu"
            geom_types[tname] = geom_types.get(tname, 0) + 1

            # --- Validité topologique ---
            is_valid = True
            try:
                is_valid = geom.isGeosValid()
            except Exception:
                is_valid = True
            if not is_valid:
                q.invalid_count += 1
                flagged["invalid"].append(fid)
                if opt.topology_checks:
                    msg, is_self = self._first_validation_error(geom)
                    if is_self:
                        self_intersections += 1
                        flagged["self_intersection"].append(fid)
                    if msg and len(invalid_details) < opt.invalid_details_limit:
                        invalid_details.append("FID %s : %s" % (fid, msg))
            else:
                q.valid_count += 1

            # --- Petits polygones ---
            gtype = QgsWkbTypes.geometryType(geom.wkbType())
            if gtype == QgsWkbTypes.PolygonGeometry and can_measure_area:
                area = self._measure_area_m2(da, geom)
                if area is None:
                    can_measure_area = False
                    result.warnings.append("Surfaces non calculables : petits polygones ignorés.")
                elif area <= opt.small_polygon_threshold_m2:
                    q.small_area_count += 1
                    flagged["small"].append(fid)

            # --- Doublons (WKB normalisé, haché) ---
            if opt.check_duplicates:
                try:
                    g = QgsGeometry(geom)
                    try:
                        g.normalize()
                    except Exception:  # nosec B110 - normalisation best-effort
                        pass
                    # Empreinte NON cryptographique servant uniquement à
                    # regrouper les géométries identiques. BLAKE2b (rapide,
                    # digest 16 o) évite l'alerte « MD5 faible » des scanners.
                    key = hashlib.blake2b(bytes(g.asWkb()), digest_size=16).digest()
                    wkb_index.setdefault(key, []).append(fid)
                except Exception:  # nosec B110 - géométrie non hachable ignorée
                    pass

            # --- Bâtiments AGL > AMSL ---
            if buildings_enabled:
                agl = self._to_float(feat[agl_field])
                amsl = self._to_float(feat[amsl_field])
                if agl is None or amsl is None:
                    agl_non_numeric += 1
                elif agl > amsl:
                    agl_over += 1
                    flagged["agl"].append(fid)

            # --- Indexation des polygones (chevauchements) ---
            if overlap_index is not None and gtype == QgsWkbTypes.PolygonGeometry:
                try:
                    overlap_index.addFeature(feat)
                    polygon_fids.append(fid)
                except Exception:  # nosec B110 - entité non indexable ignorée
                    pass

            processed += 1

        check_cancel()
        report(88, "Consolidation…")

        # Si featureCount() n'était pas fiable, on recale sur le compte réel
        if not total:
            result.total_features = processed

        # Doublons : toutes les entités d'un groupe sauf la première
        dup_count = 0
        for fids in wkb_index.values():
            if len(fids) > 1:
                dup_count += len(fids) - 1
                flagged["duplicate"].extend(fids[1:])
        q.duplicate_count = dup_count

        # Chevauchements / intersections entre polygones
        if overlap_index is not None and polygon_fids:
            if len(polygon_fids) > opt.overlap_max_features:
                result.warnings.append(
                    "Chevauchements non contrôlés : %d polygones dépassent la limite (%d)."
                    % (len(polygon_fids), opt.overlap_max_features)
                )
            else:
                report(90, "Chevauchements…")
                overlap_fids, overlap_pairs = self._detect_overlaps(
                    overlap_index, polygon_fids, check_cancel,
                    lambda p: report(90 + 5.0 * p, "Chevauchements…"),
                )
                q.overlap_count = len(overlap_fids)
                q.overlap_pairs = overlap_pairs
                flagged["overlap"] = sorted(overlap_fids)

        # Report des compteurs
        result.geometry_types = geom_types
        result.null_geometries = q.null_empty_count
        result.invalid_geometries = q.invalid_count
        result.polygons_below_threshold = q.small_area_count
        result.duplicate_geometries = q.duplicate_count
        result.overlapping_features = q.overlap_count
        q.self_intersection_count = self_intersections
        q.invalid_details = invalid_details
        result.flagged = flagged

        if buildings_enabled:
            result.buildings_agl_over_amsl = agl_over
            if agl_non_numeric:
                result.warnings.append(
                    "%d entités avec AGL/AMSL non numériques ou manquants." % agl_non_numeric
                )

        # Avertissements de synthèse
        if q.null_empty_count:
            result.warnings.append("%d géométries nulles ou vides détectées." % q.null_empty_count)
        if q.invalid_count:
            result.warnings.append("%d géométries invalides détectées." % q.invalid_count)
        if q.self_intersection_count:
            result.warnings.append(
                "%d géométries avec auto-intersection." % q.self_intersection_count
            )
        if q.overlap_count:
            result.warnings.append(
                "%d entités en chevauchement (%d paires en intersection)."
                % (q.overlap_count, q.overlap_pairs)
            )
        if q.small_area_count:
            result.warnings.append(
                "%d polygones ≤ %g m²." % (q.small_area_count, opt.small_polygon_threshold_m2)
            )
        if q.duplicate_count:
            result.warnings.append("%d doublons détectés." % q.duplicate_count)
        if isinstance(result.buildings_agl_over_amsl, int) and result.buildings_agl_over_amsl:
            result.warnings.append("%d bâtiments avec AGL > AMSL." % result.buildings_agl_over_amsl)
        if result.crs_authid in ("Inconnu", ""):
            result.warnings.append("CRS inconnu : les mesures de surface peuvent être erronées.")

        result.preview_rows = preview_rows
        result.correctness = compute_correctness(result, include_buildings=buildings_enabled)
        result.processing_time = time.time() - started
        report(100, "Terminé.")
        return result

    # -- Réparation -------------------------------------------------------------

    @staticmethod
    def build_repaired_layer(
        layer: QgsVectorLayer,
        result: "AnalysisResult",
        ropts: RepairOptions,
        progress_cb: Optional[Callable[[float], None]] = None,
        is_canceled: Optional[Callable[[], bool]] = None,
    ):
        """Construit une NOUVELLE couche mémoire réparée selon `ropts`.

        La couche d'origine n'est jamais modifiée : on recopie toutes les
        entités (attributs conservés) en appliquant les corrections cochées :
          * fix_invalid            -> make valid sur les géométries invalides ;
          * fix_self_intersection  -> make valid sur les auto-intersections
                                      (sous-ensemble des invalides) ;
          * remove_null_empty      -> les entités à géométrie nulle/vide écartées ;
          * remove_duplicates      -> les doublons (hors 1re occurrence) écartés ;
          * remove_small           -> les petits polygones (≤ seuil) écartés ;
          * fix_overlaps           -> rogne UNIQUEMENT les chevauchements mineurs
                                      (surface retirée ≤ overlap_max_fraction) ;
                                      les chevauchements majeurs sont laissés
                                      intacts et signalés (manual_overlap_fids).

        Renvoie (couche_mémoire, RepairStats). L'appelant écrit ensuite cette
        couche dans un fichier (voir StatGeomDockWidget).
        """
        stats = RepairStats()

        wkb_str = QgsWkbTypes.displayString(layer.wkbType())
        authid = layer.crs().authid()
        uri = wkb_str + ("?crs=%s" % authid if authid else "")
        mem = QgsVectorLayer(uri, "%s — réparé" % layer.name(), "memory")
        pr = mem.dataProvider()
        pr.addAttributes(layer.fields().toList())
        mem.updateFields()
        out_fields = mem.fields()

        flagged = result.flagged or {}
        # « make valid » s'applique à l'union des invalides et/ou des
        # auto-intersections, selon les cases cochées (une auto-intersection
        # étant un invalide, la même géométrie n'est traitée qu'une fois).
        makevalid_set = set()
        if ropts.fix_invalid:
            makevalid_set |= set(flagged.get("invalid", []))
        if ropts.fix_self_intersection:
            makevalid_set |= set(flagged.get("self_intersection", []))
        null_set = set(flagged.get("null_empty", [])) if ropts.remove_null_empty else set()
        dup_set = set(flagged.get("duplicate", [])) if ropts.remove_duplicates else set()
        small_set = set(flagged.get("small", [])) if ropts.remove_small else set()

        # Chevauchements : on pré-calcule les rognages MINEURS à appliquer.
        # Les entités par ailleurs supprimées (doublons/petits/nulles) sont
        # exclues pour rester cohérent.
        overlap_fixes = {}
        if ropts.fix_overlaps:
            overlap_set = set(flagged.get("overlap", [])) - (null_set | dup_set | small_set)
            overlap_fixes = GeomAnalyzer._compute_overlap_fixes(
                layer, overlap_set, ropts.overlap_max_fraction, stats
            )

        total = layer.featureCount() or 0
        out_feats: List[QgsFeature] = []
        processed = 0
        for feat in layer.getFeatures():
            if is_canceled and is_canceled():
                raise AnalysisCancelled()
            if progress_cb and total and processed % 500 == 0:
                progress_cb(100.0 * processed / total)
            processed += 1
            fid = feat.id()

            # Suppressions (prioritaires sur la correction)
            if fid in null_set:
                stats.removed_null_empty += 1
                continue
            if fid in dup_set:
                stats.removed_duplicates += 1
                continue
            if fid in small_set:
                stats.removed_small += 1
                continue

            geom = feat.geometry() if feat.hasGeometry() else None

            # Rognage d'un chevauchement mineur (déjà validé comme « mineur »)
            if fid in overlap_fixes:
                geom = overlap_fixes[fid]

            # Correction des géométries invalides / auto-intersections (make valid)
            if fid in makevalid_set and geom is not None and not geom.isNull():
                try:
                    fixed = geom.makeValid()
                except Exception:
                    fixed = None
                if fixed is not None and not fixed.isNull() and not fixed.isEmpty():
                    geom = fixed
                    try:
                        valid_now = geom.isGeosValid()
                    except Exception:
                        valid_now = False
                    if valid_now:
                        stats.fixed_invalid += 1
                    else:
                        stats.still_invalid += 1
                else:
                    stats.still_invalid += 1

            nf = QgsFeature(out_fields)
            nf.setAttributes(feat.attributes())
            if geom is not None and not geom.isNull():
                nf.setGeometry(geom)
            out_feats.append(nf)

        pr.addFeatures(out_feats)
        mem.updateExtents()
        stats.written = len(out_feats)
        return mem, stats

    @staticmethod
    def _compute_overlap_fixes(layer, overlap_fids, max_fraction, stats):
        """Pré-calcule les rognages de chevauchement à appliquer.

        Principe prudent (ne déforme pas les polygones) :
          * pour chaque paire de polygones qui se recouvrent, le **plus grand**
            (surface supérieure, ou égale avec FID plus petit) est « dominant »
            et conserve sa forme ; seul le **plus petit** est rogné de la partie
            commune (``difference``) — donc pas de trou et forme du grand
            inchangée ;
          * le rognage n'est retenu QUE s'il est **mineur** : surface retirée
            ≤ ``max_fraction`` de la surface du polygone. Sinon le polygone est
            laissé intact et son FID est reporté (``manual_overlap_fids``) pour
            une correction manuelle par l'opérateur.

        Renvoie ``{fid: géométrie_rognée}`` pour les seuls cas mineurs.
        """
        fixes = {}
        if not overlap_fids:
            return fixes
        try:
            index = QgsSpatialIndex(QgsSpatialIndex.FlagStoreFeatureGeometries)
        except Exception:
            return fixes

        geoms = {}
        areas = {}
        request = QgsFeatureRequest().setFilterFids(list(overlap_fids))
        for f in layer.getFeatures(request):
            g = f.geometry()
            if g is None or g.isNull() or g.isEmpty():
                continue
            if QgsWkbTypes.geometryType(g.wkbType()) != QgsWkbTypes.PolygonGeometry:
                continue
            geoms[f.id()] = QgsGeometry(g)
            areas[f.id()] = g.area()
            try:
                index.addFeature(f)
            except Exception:  # nosec B110 - indexation spatiale best-effort, entité ignorée
                pass

        for fid, g in geoms.items():
            area = areas.get(fid, 0.0)
            if area <= 0:
                continue
            try:
                candidates = index.intersects(g.boundingBox())
            except Exception:
                candidates = []

            # Union des voisins « dominants » (à soustraire de ce polygone)
            dom_union = None
            for cid in candidates:
                if cid == fid:
                    continue
                og = geoms.get(cid)
                if og is None:
                    continue
                oarea = areas.get(cid, 0.0)
                dominant = (oarea > area) or (oarea == area and cid < fid)
                if not dominant:
                    continue
                try:
                    if not g.intersects(og):
                        continue
                except Exception:  # nosec B112 - test d'intersection best-effort, paire ignorée
                    continue
                dom_union = QgsGeometry(og) if dom_union is None else dom_union.combine(og)

            if dom_union is None or dom_union.isNull():
                continue  # ce polygone est dominant partout : on n'y touche pas

            try:
                new_g = g.difference(dom_union)
            except Exception:
                new_g = None

            # Disparition totale / échec -> changement majeur -> manuel
            if new_g is None or new_g.isNull() or new_g.isEmpty():
                stats.manual_overlaps += 1
                stats.manual_overlap_fids.append(fid)
                continue

            removed = area - new_g.area()
            if removed <= 0:
                continue  # rien de significatif à retirer (contact de frontière)

            if (removed / area) <= max_fraction:
                fixes[fid] = new_g            # mineur -> correction automatique
                stats.fixed_overlaps += 1
            else:
                stats.manual_overlaps += 1    # majeur -> laissé à l'opérateur
                stats.manual_overlap_fids.append(fid)

        return fixes


# ════════════════════════════════════════════════════════════════
# UTILITAIRES
# ════════════════════════════════════════════════════════════════


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _attr_str(value: Any) -> str:
    """Convertit une valeur d'attribut en chaîne affichable."""
    try:
        # NULL QVariant de QGIS
        if value is None:
            return ""
        if hasattr(value, "isNull") and value.isNull():
            return ""
    except Exception:  # nosec B110 - valeur non standard, repli sur str()
        pass
    return str(value)
