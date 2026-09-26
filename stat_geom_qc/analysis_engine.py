# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  STAT GEOM QC - Moteur d'analyse (API native QGIS)                            ║
║                                                                                ║
║  Portage de GISAnalysisEngine (App_Stat_GEOM) vers l'API PyQGIS, sans         ║
║  dépendance à geopandas / matplotlib. Fonctionne directement sur les couches  ║
║  vectorielles chargées dans QGIS.                                             ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import contextlib
import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsLineString,
    QgsMultiPolygon,
    QgsPointXY,
    QgsPolygon,
    QgsProject,
    QgsRectangle,
    QgsSpatialIndex,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsWkbTypes,
)

from . import constants as C


# ═══════════════════════════════════════════════════════════════════════════════
# OPTIONS & RÉSULTATS
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class MeasurementContext:
    """Contexte nécessaire aux mesures ellipsoïdales, capturé UNE fois.

    ``QgsProject.instance()`` est un singleton appartenant au thread principal.
    L'interroger depuis un ``QgsTask`` — ce que faisaient les trois points de
    mesure du moteur — signifie lire l'état du projet pendant que l'utilisateur
    peut le modifier (changement de CRS, chargement d'un autre projet) : au
    mieux des surfaces incohérentes, au pire un plantage de QGIS sans trace
    Python. ``RepairTask`` recevait déjà un ``transform_context`` en paramètre :
    le bon motif existait, il n'était pas appliqué au moteur.

    Cette classe transporte donc les deux valeurs concernées jusqu'au thread
    secondaire. ``from_project()`` doit être appelé depuis le thread PRINCIPAL.
    """

    transform_context: Any = None
    ellipsoid: str = ""

    @staticmethod
    def from_project() -> "MeasurementContext":
        """Instantané du projet courant. À appeler sur le thread principal."""
        try:
            projet = QgsProject.instance()
            return MeasurementContext(projet.transformContext(),
                                      projet.ellipsoid() or "")
        except Exception:
            return MeasurementContext()

    def usable_ellipsoid(self) -> str:
        """Ellipsoïde exploitable, avec repli WGS84 (jamais « NONE »)."""
        return self.ellipsoid if self.ellipsoid and self.ellipsoid != "NONE" else "WGS84"


def _resolve_measurement_context(ctx) -> MeasurementContext:
    """Contexte fourni, ou instantané du projet à défaut.

    Le repli couvre les appels directs (tests, scripts) faits depuis le thread
    principal, où lire le projet est légitime.
    """
    return ctx if isinstance(ctx, MeasurementContext) else MeasurementContext.from_project()


@dataclass
class AnalysisOptions:
    """Paramètres d'exécution de l'analyse."""

    # Petits polygones : contrôle FACULTATIF, décoché par défaut. Ce qui est
    # « trop petit » dépend entièrement du modèle de données (un abri de jardin
    # de 2 m² est légitime, une scorie de numérisation ne l'est pas) et la
    # correction associée SUPPRIME des entités : le déclencher sans demande
    # explicite serait mal venu.
    check_small_polygons: bool = False
    small_polygon_threshold_m2: float = 2.0
    # Contrôles topologiques : détails d'invalidité ET entités multi-parties.
    topology_checks: bool = True
    check_duplicates: bool = True
    # Sommets STRICTEMENT identiques uniquement (désactivé par défaut). Pas de
    # tolérance de quasi-doublon : voir constants.py pour la justification.
    check_duplicate_vertex: bool = False
    check_holes: bool = False             # trous sous seuil (désactivé par défaut)
    # Un trou dont la surface dépasse ce seuil est jugé justifié (cour, îlot…)
    # et n'est ni signalé ni supprimé. 2 m² : au-dessous, c'est de la saisie.
    hole_max_area_m2: float = 2.0
    check_overlaps: bool = True           # chevauchements/intersections entre polygones
    # Faux = signaler TOUS les chevauchements (comportement historique).
    # Vrai = ne pas signaler ceux imputables à un sommet manquant sur la
    # frontière commune (voir overlap_from_missing_vertex) ; ils restent comptés.
    # La forme exigée de la zone commune est FIXE : voir
    # constants.MISSING_VERTEX_RATIO_DEFAULT. L'option se résume à cette case.
    ignore_missing_vertex_overlaps: bool = False
    # Polygones ENTIÈREMENT inclus dans un autre : selon le modèle de données,
    # l'imbrication peut être légitime (bâtiment dans une parcelle) ou fautive.
    # Faux = signalés comme chevauchement (comportement historique).
    # Vrai = ignorés (libellé de l'interface : « ignorer les polygones
    # entièrement inclus »). Nom d'attribut conservé pour la compatibilité.
    allow_contained_polygons: bool = False
    # ── Contrôles « Autres » (validation polygonale avancée, tous désactivés
    # par défaut : ils dépendent fortement du modèle de données) ──
    check_sharp_angles: bool = False      # angles aigus (pointes)
    sharp_angle_min_deg: float = 30.0     # angle intérieur minimal toléré
    check_close_vertices: bool = False    # sommets trop rapprochés
    close_vertex_min_dist_m: float = 0.20  # distance minimale attendue
    # Altitudes d'un polygone entièrement inclus dans un autre : le polygone
    # inclus doit être STRICTEMENT plus haut que son contenant, champ par champ
    # (AGL avec AGL, HEIGHT avec HEIGHT). Contrôle attributaire : il ne tourne
    # que si la couche porte au moins un de ces champs.
    check_nested_height: bool = False
    # Croisements de lignes sans sommet commun (« unnoded line crossing ») :
    # réservé aux couches de LIGNES, silencieux sinon (voir CAT_UNNODED_CROSSING
    # et GeomAnalyzer._detect_unnoded_crossings). Détecte les croisements ENTRE
    # DEUX ENTITÉS DISTINCTES ; un croisement d'une ligne sur elle-même n'est
    # pas vérifié.
    check_line_crossings: bool = False
    # ── Zone d'analyse (AOI), facultative ──
    # ``(xmin, ymin, xmax, ymax)`` exprimé dans ``aoi_crs_authid``, ou None
    # pour analyser toute la couche (comportement par défaut, inchangé).
    #
    # Le rectangle est donné dans le CRS où il a été tracé (celui du canevas)
    # et NON dans celui de la couche : le convertir ici obligerait l'appelant
    # à connaître la couche au moment du tracé, alors qu'elle peut changer
    # entre les deux. La conversion a lieu dans ``_resolve_aoi``, avec le
    # contexte de transformation capturé sur le thread principal.
    #
    # ``aoi_crs_authid`` vide = le rectangle est déjà dans le CRS de la couche.
    aoi_rect: Optional[tuple] = None
    aoi_crs_authid: str = ""
    # Nom du préréglage choisi dans le panneau, purement informatif : le moteur
    # ne s'en sert jamais pour décider, il le recopie dans le résultat pour que
    # le rapport dise sous quel profil il a été produit.
    profile_name: str = ""
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
    # Seuil RÉELLEMENT appliqué à la réparation — peut être resserré par
    # rapport à celui de l'analyse (jamais élargi : voir
    # GeomAnalyzer._compute_small_polygon_removals, qui ne fait que RESTREINDRE
    # le lot déjà repéré, jamais le compléter).
    small_polygon_threshold_m2: float = 2.0
    fix_overlaps: bool = False        # rogner UNIQUEMENT les chevauchements mineurs
    fix_duplicate_vertex: bool = False  # supprimer les sommets dupliqués
    remove_holes: bool = False        # supprimer les trous sous seuil (gros trous conservés)
    explode_multipart: bool = False   # éclater les entités multi-parties en parties simples
    # ── Contrôles « Autres » ──
    fix_sharp_angles: bool = False     # retirer les sommets en pointe
    fix_close_vertices: bool = False   # fusionner les sommets trop rapprochés
    overlap_max_fraction: float = 0.05  # part max de surface rognée pour juger un chevauchement « mineur »
    # Doit refléter le seuil d'analyse pour rester cohérent.
    hole_max_area_m2: float = 2.0
    # Seuil de forme RÉELLEMENT appliqué à l'analyse (0 = option inactive).
    # Repris tel quel pour ne pas rogner un recouvrement que l'analyse a écarté.
    missing_vertex_ratio: float = 0.0
    # Les deux règles de chevauchement de l'analyse, rejouées à l'identique :
    # sans elles, la réparation rognait (ou renvoyait en correction manuelle)
    # des paires que l'analyse avait délibérément déclarées légitimes.
    allow_contained_polygons: bool = False
    equal_is_overlap: bool = False
    sharp_angle_min_deg: float = 30.0
    close_vertex_min_dist_m: float = 0.20

    def any_selected(self) -> bool:
        return bool(
            self.fix_invalid or self.fix_self_intersection or self.remove_null_empty
            or self.remove_duplicates or self.remove_small or self.fix_overlaps
            or self.fix_duplicate_vertex or self.remove_holes or self.explode_multipart
            or self.fix_sharp_angles or self.fix_close_vertices
        )


@dataclass
class RepairStats:
    """Bilan chiffré d'une opération de réparation."""

    fixed_invalid: int = 0        # géométries rendues valides
    still_invalid: int = 0        # géométries encore invalides après make valid
    removed_null_empty: int = 0
    removed_duplicates: int = 0
    removed_small: int = 0
    # Repérés « petits » à l'ANALYSE mais conservés car le seuil de RÉPARATION
    # est plus strict (resserré par l'utilisateur dans l'aperçu) : 0 si les
    # deux seuils sont identiques.
    small_kept_above_threshold: int = 0
    # Repérés « petits » mais dont la surface n'a pas pu être RE-mesurée à la
    # réparation : conservés, mais pour une raison toute autre que « au-dessus
    # du seuil ». Les confondre annonçait un motif faux.
    small_not_measurable: int = 0
    removed_small_area_m2: float = 0.0   # surface totale (m², ellipsoïdale) réellement supprimée
    fixed_overlaps: int = 0       # chevauchements mineurs rognés automatiquement
    manual_overlaps: int = 0      # chevauchements trop importants -> correction manuelle
    manual_overlap_fids: List[int] = field(default_factory=list)  # FID source à revoir
    cleaned_duplicate_vertex: int = 0   # entités dont des sommets doublons ont été retirés
    removed_vertices: int = 0           # nombre total de sommets doublons supprimés
    removed_holes: int = 0              # trous (anneaux intérieurs) supprimés
    features_with_holes_fixed: int = 0  # entités dont au moins un trou a été retiré
    exploded_features: int = 0          # entités multi-parties éclatées
    added_features: int = 0             # entités supplémentaires issues de l'éclatement
    fixed_sharp_angles: int = 0         # entités dont des pointes ont été retirées
    removed_spike_vertices: int = 0     # sommets en pointe supprimés
    skipped_sharp_angles: int = 0       # entités laissées intactes (correction risquée)
    # FID des corrections REFUSÉES (correction manuelle requise, voir
    # rapport de réparation) : mêmes entités que les compteurs skipped_*
    # ci-dessous, mais identifiables — sur le même modèle que
    # ``manual_overlap_fids``.
    skipped_sharp_angle_fids: List[int] = field(default_factory=list)
    cleaned_close_vertices: int = 0     # entités dont des sommets rapprochés ont fusionné
    removed_close_vertices: int = 0     # sommets rapprochés supprimés
    # Corrections REFUSÉES car elles auraient dégradé la géométrie.
    skipped_close_vertices: int = 0
    skipped_close_vertex_fids: List[int] = field(default_factory=list)
    skipped_duplicate_vertex: int = 0
    skipped_duplicate_vertex_fids: List[int] = field(default_factory=list)
    # Entités où il n'y avait simplement RIEN à corriger — distinct d'un refus.
    # Les confondre faisait annoncer « la correction aurait dégradé la
    # géométrie » pour des entités déjà propres (une étape antérieure ayant fait
    # disparaître l'anomalie), soit une accusation fausse portée sur la donnée.
    unchanged_sharp_angles: int = 0
    unchanged_close_vertices: int = 0
    rejected: int = 0             # entités refusées par le fournisseur (anomalie)
    written: int = 0              # entités réellement écrites (comptées côté couche)
    # Attributs renommés pour ne pas entrer en conflit avec la clé primaire du
    # fichier écrit : [(ancien_nom, nouveau_nom), …]. Valeurs conservées.
    renamed_fields: List[tuple] = field(default_factory=list)


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
    contained_pairs: int = 0             # paires dont un polygone est entièrement inclus
    # Paires attribuées à un sommet manquant sur la frontière commune. Comptées
    # uniquement lorsque l'option correspondante est active (le classement n'est
    # alors pas calculé, pour ne rien coûter quand il n'est pas demandé).
    overlap_missing_vertex_pairs: int = 0
    duplicate_vertex_count: int = 0      # entités contenant des sommets dupliqués
    duplicate_vertex_total: int = 0      # nombre total de sommets en doublon détectés
    hole_count: int = 0                  # entités ayant ≥1 trou sous le seuil
    hole_total: int = 0                  # nombre total de trous sous le seuil
    multipart_count: int = 0             # entités composées de plusieurs parties
    sharp_angle_count: int = 0           # entités présentant ≥1 angle aigu
    sharp_angle_total: int = 0           # nombre total de sommets en pointe
    close_vertex_count: int = 0          # entités ayant ≥1 paire de sommets rapprochés
    close_vertex_total: int = 0          # nombre total de paires rapprochées
    # ── Hauteur des polygones entièrement inclus ──
    nested_height_count: int = 0         # polygones inclus dont l'altitude est fautive
    nested_height_pairs: int = 0         # paires (inclus, contenant) en anomalie
    # Paires d'inclusion réellement COMPARÉES (au moins un champ renseigné des
    # deux côtés) et paires laissées sans verdict (valeur vide ou non
    # numérique). Les distinguer évite de lire « 0 anomalie » comme un satisfecit
    # alors que rien n'était comparable.
    nested_pairs_checked: int = 0
    nested_pairs_unknown: int = 0
    invalid_details: List[str] = field(default_factory=list)
    # ── Croisements de lignes non nodés (entre entités DISTINCTES) ──
    unnoded_crossing_count: int = 0   # lignes ayant ≥1 croisement non nodé
    unnoded_crossing_pairs: int = 0   # paires de lignes distinctes concernées


@dataclass
class CorrectnessScore:
    """Score de correctness (qualité globale des données).

    Le score seul ne dit pas sur QUOI il porte : 98 sur deux dimensions mesurées
    et 98 sur onze ne valent pas la même chose. La couverture accompagne donc la
    note partout où elle est affichée.
    """

    score: float = 100.0                       # 0..100
    grade: str = "N/A"                          # Excellent / Bon / ...
    color: str = "#22c55e"                      # couleur associée au grade
    components: Dict[str, float] = field(default_factory=dict)   # sous-scores 0..100
    penalties: Dict[str, float] = field(default_factory=dict)    # pénalités appliquées
    # Couverture : combien de contrôles ont RÉELLEMENT abouti, sur le total
    # connu du plugin. Un contrôle décoché ou non concluant ne compte pas.
    coverage_ran: int = 0
    coverage_total: int = 0
    # Contrôles qui ont tourné SANS pouvoir conclure (mesure impossible, aucune
    # paire comparable…). Distincts des contrôles non demandés : ceux-là on ne
    # les a pas voulus, ceux-ci ont échoué à répondre.
    inconclusive: Dict[str, bool] = field(default_factory=dict)

    def coverage_pct(self) -> float:
        """Part des contrôles aboutis, en pourcentage (0 si total inconnu)."""
        if not self.coverage_total:
            return 0.0
        return round(100.0 * self.coverage_ran / self.coverage_total, 1)


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
    duplicate_vertex_features: int = 0
    hole_features: int = 0
    multipart_features: int = 0
    sharp_angle_features: int = 0
    close_vertex_features: int = 0
    nested_height_features: int = 0
    unnoded_crossing_features: int = 0
    # Champs d'altitude RÉELLEMENT comparés, sous leur nom tel qu'il figure dans
    # la couche (la recherche est insensible à la casse). Vide = le contrôle n'a
    # pas tourné, faute de champ AGL ou HEIGHT.
    nested_height_fields: List[str] = field(default_factory=list)
    # Contrôle AGL/AMSL : interne. ``buildings_agl_over_amsl`` conserve le
    # décompte brut ; seule ``agl_note`` est affichée (note discrète en fin de
    # rapport), et uniquement si les deux champs existent, sont entièrement
    # renseignés et qu'une incohérence est détectée.
    buildings_agl_over_amsl: Any = 0
    agl_note: str = ""
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
    hole_threshold_m2: float = 2.0
    # Seuil de forme réellement appliqué (0 = option « sommet manquant » inactive).
    missing_vertex_ratio: float = 0.0
    # Règle d'imbrication retenue à l'analyse, pour que la réparation applique
    # la même : sans cela elle rognait des paires déclarées légitimes.
    allow_contained_polygons: bool = False
    sharp_angle_min_deg: float = 30.0
    close_vertex_min_dist_m: float = 0.20
    # Identifiants d'entités problématiques, par catégorie (pour sélection / couche d'erreurs)
    flagged: Dict[str, List[int]] = field(default_factory=dict)
    # Localisation des chevauchements : {fid: [(x, y, fid_voisin), …]}. Permet à
    # la couche d'erreurs de pointer l'intersection elle-même plutôt que le
    # centre du polygone. Coordonnées simples (pas d'objet QGIS) : sûres à
    # transporter depuis le thread d'analyse.
    overlap_points: Dict[int, List[tuple]] = field(default_factory=dict)
    # Quels contrôles facultatifs ont RÉELLEMENT tourné pendant cette analyse
    # (clés : voir CHECK_KEYS_BY_CATEGORY). Un compteur à 0 pour un contrôle
    # décoché n'a jamais été calculé — il ne veut rien dire, contrairement à un
    # 0 pour un contrôle actif (couche propre). Sans cette distinction,
    # l'affichage laisserait croire qu'un contrôle non demandé n'a rien trouvé.
    checks_enabled: Dict[str, bool] = field(default_factory=dict)
    # Contrôles qui ont TOURNÉ sans pouvoir conclure : surface non mesurable,
    # aucune paire comparable, altitudes absentes. Leur compteur à 0 ne veut pas
    # dire « conforme » — il veut dire « on ne sait pas ». Sans cette
    # distinction, un contrôle incapable de mesurer quoi que ce soit rendait un
    # sous-score de 100 pour cent, indiscernable d'une couche irréprochable.
    checks_inconclusive: Dict[str, bool] = field(default_factory=dict)
    # Préréglage sous lequel l'analyse a été lancée (« standard », « rapide »…),
    # ou vide si les cases ont été composées à la main. Consigné dans le rapport
    # pour qu'on puisse refaire tourner la même analyse plus tard.
    profile_name: str = ""
    # ── Zone d'analyse (AOI) réellement appliquée ──
    # ``aoi_wkt`` : le rectangle CONVERTI dans le CRS de la couche, en WKT.
    # Vide = aucune restriction, toute la couche a été analysée.
    #
    # Conservé dans le résultat (et non seulement dans les options) parce que
    # trois choses en dépendent APRÈS l'analyse : le rapport, qui doit dire sur
    # quelle zone il porte ; la réparation, qui doit produire le même
    # périmètre que l'analyse ; et la vérification post-réparation, qui doit
    # recontrôler la même zone.
    aoi_wkt: str = ""
    # Entités réellement analysées / total de la couche. Les deux, car « 12 %
    # de chevauchements » ne veut rien dire si l'on ne sait pas que l'analyse
    # ne portait que sur 800 entités d'une couche de 264 000.
    aoi_features: int = 0
    aoi_layer_features: int = 0
    # Photographie des contrôles actifs et de leurs seuils au moment de
    # l'analyse. Un rapport sans ces valeurs est illisible six mois plus tard :
    # on ne sait plus si « 0 petit polygone » vaut pour un seuil de 2 ou de 25 m².
    options_snapshot: Dict[str, Any] = field(default_factory=dict)

    def has_aoi(self) -> bool:
        """L'analyse a-t-elle été restreinte à une zone ?"""
        return bool(self.aoi_wkt)

    def total_issues(self) -> int:
        q = self.quality_report
        return (
            q.null_empty_count
            + q.invalid_count
            + q.small_area_count
            + q.duplicate_count
            + q.overlap_count
            + q.duplicate_vertex_count
            + q.hole_count
            + q.multipart_count
            + q.sharp_angle_count
            + q.close_vertex_count
            + q.nested_height_count
            + q.unnoded_crossing_count
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
            # Zone d'analyse : exportée pour qu'un lecteur programmatique
            # sache que les compteurs ne portent PAS sur toute la couche.
            "aoi": {
                "active": self.has_aoi(),
                "wkt": self.aoi_wkt,
                "features": self.aoi_features,
                "layer_features": self.aoi_layer_features,
            },
            "geometry_types": self.geometry_types,
            "null_geometries": self.null_geometries,
            "invalid_geometries": self.invalid_geometries,
            "polygons_below_threshold": self.polygons_below_threshold,
            "threshold_m2": self.threshold_m2,
            "duplicate_geometries": self.duplicate_geometries,
            "overlapping_features": self.overlapping_features,
            "missing_vertex_ratio": self.missing_vertex_ratio,
            "duplicate_vertex_features": self.duplicate_vertex_features,
            "hole_features": self.hole_features,
            "hole_threshold_m2": self.hole_threshold_m2,
            "multipart_features": self.multipart_features,
            "sharp_angle_features": self.sharp_angle_features,
            "sharp_angle_min_deg": self.sharp_angle_min_deg,
            "close_vertex_features": self.close_vertex_features,
            "close_vertex_min_dist_m": self.close_vertex_min_dist_m,
            "nested_height_features": self.nested_height_features,
            "nested_height_fields": self.nested_height_fields,
            "unnoded_crossing_features": self.unnoded_crossing_features,
            # Contrôle AGL/AMSL interne : seule la note (message destiné au
            # rapport) est exportée. Le décompte brut reste hors des exports pour
            # que ce contrôle demeure discret.
            "agl_note": self.agl_note,
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
                "contained_pairs": self.quality_report.contained_pairs,
                "overlap_missing_vertex_pairs":
                    self.quality_report.overlap_missing_vertex_pairs,
                "duplicate_vertex_count": self.quality_report.duplicate_vertex_count,
                "duplicate_vertex_total": self.quality_report.duplicate_vertex_total,
                "hole_count": self.quality_report.hole_count,
                "hole_total": self.quality_report.hole_total,
                "multipart_count": self.quality_report.multipart_count,
                "sharp_angle_count": self.quality_report.sharp_angle_count,
                "sharp_angle_total": self.quality_report.sharp_angle_total,
                "close_vertex_count": self.quality_report.close_vertex_count,
                "close_vertex_total": self.quality_report.close_vertex_total,
                "nested_height_count": self.quality_report.nested_height_count,
                "nested_height_pairs": self.quality_report.nested_height_pairs,
                "nested_pairs_checked": self.quality_report.nested_pairs_checked,
                "nested_pairs_unknown": self.quality_report.nested_pairs_unknown,
                "invalid_details": self.quality_report.invalid_details,
                "unnoded_crossing_count": self.quality_report.unnoded_crossing_count,
                "unnoded_crossing_pairs": self.quality_report.unnoded_crossing_pairs,
            },
            "correctness": {
                "score": self.correctness.score,
                "grade": self.correctness.grade,
                "components": self.correctness.components,
                "penalties": self.correctness.penalties,
                # Couverture exportée avec la note : un lecteur programmatique
                # doit pouvoir refuser un score dont l'assise est trop mince.
                "coverage_ran": self.correctness.coverage_ran,
                "coverage_total": self.correctness.coverage_total,
                "coverage_pct": self.correctness.coverage_pct(),
                "inconclusive": sorted(self.correctness.inconclusive),
            },
            "profile_name": self.profile_name,
            "options_snapshot": self.options_snapshot,
            "checks_inconclusive": sorted(
                k for k, v in self.checks_inconclusive.items() if v),
            "processing_time": self.processing_time,
            "timestamp": self.timestamp,
            "checks_enabled": self.checks_enabled,
        }


class AnalysisCancelled(Exception):
    """Levée lorsque l'utilisateur annule l'analyse."""


def check_layer_ready(layer: Optional[QgsVectorLayer]) -> Optional[str]:
    """Valide qu'une couche est analysable. Renvoie un message d'erreur
    prêt à afficher, ou ``None`` si la couche est prête.

    Règle de validation unique, partagée par l'UI et les tâches, pour éviter
    de lancer un traitement sur une couche absente, invalide, sans fournisseur,
    sans géométrie ou vide.
    """
    if layer is None or not isinstance(layer, QgsVectorLayer):
        return C.MSG_NO_LAYER
    if not layer.isValid():
        return C.MSG_LAYER_INVALID
    provider = layer.dataProvider()
    if provider is None or not provider.isValid():
        return C.MSG_LAYER_INVALID
    try:
        has_geometry = layer.isSpatial()
    except Exception:
        has_geometry = layer.geometryType() != QgsWkbTypes.GeometryType.NullGeometry
    if not has_geometry:
        return C.MSG_LAYER_NO_GEOM
    if layer.featureCount() == 0:
        return C.MSG_LAYER_EMPTY
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# GARDES DÉFENSIVES (appels d'API tolérants aux erreurs)
# ═══════════════════════════════════════════════════════════════════════════════
# Ces helpers encapsulent des appels QGIS susceptibles d'échouer sur une
# géométrie ou un index dégradé. Ils renvoient une valeur de repli explicite,
# ce qui évite de court-circuiter la boucle appelante depuis un gestionnaire
# d'exception : le flux de contrôle reste lisible et l'échec d'une entité
# n'interrompt jamais le lot.


def _safe_index_intersects(index: QgsSpatialIndex, bbox) -> Optional[List[int]]:
    """Candidats d'un index spatial pour une emprise. ``None`` si l'appel échoue."""
    try:
        return index.intersects(bbox)
    except Exception:
        return None


def _safe_intersects(a: QgsGeometry, b: QgsGeometry) -> bool:
    """Teste l'intersection de deux géométries ; ``False`` si l'appel échoue."""
    try:
        return bool(a.intersects(b))
    except Exception:
        return False


def _count_coordinates(geom: QgsGeometry) -> int:
    """Nombre de sommets d'une géométrie (0 si indisponible)."""
    try:
        abstract = geom.constGet()
        return abstract.nCoordinates() if abstract is not None else 0
    except Exception:
        return 0


def _remove_duplicate_nodes(geom: QgsGeometry, tolerance_units: float = 0.0) -> bool:
    """Applique ``removeDuplicateNodes`` avec une tolérance optionnelle.

    ``tolerance_units`` est exprimé dans les UNITÉS DE LA COUCHE (c'est ce
    qu'attend l'epsilon de GEOS) : convertir une tolérance en mètres via
    ``layer_units_per_metre``. 0 -> sommets strictement identiques ; > 0 ->
    sommets consécutifs distants de ≤ tolérance (quasi-doublons). Repli sur la
    signature sans epsilon si la version de QGIS ne l'accepte pas.
    """
    if tolerance_units and tolerance_units > 0:
        try:
            return bool(geom.removeDuplicateNodes(tolerance_units))
        except Exception:
            return bool(geom.removeDuplicateNodes())
    return bool(geom.removeDuplicateNodes())


def count_duplicate_vertices(geom: QgsGeometry, tolerance_units: float = 0.0) -> int:
    """Nombre de sommets dupliqués (ou quasi-dupliqués) d'une géométrie.

    Avec ``tolerance_units`` > 0 (unités de la couche, voir
    ``layer_units_per_metre``), deux sommets consécutifs distants de moins que la
    tolérance comptent comme un doublon. Non destructif : le test est réalisé
    sur une copie. Renvoie 0 si la géométrie est propre, nulle, ponctuelle ou
    non analysable.
    """
    if geom is None or geom.isNull() or geom.isEmpty():
        return 0
    before = _count_coordinates(geom)
    if before <= 1:
        return 0
    try:
        probe = QgsGeometry(geom)
        if not _remove_duplicate_nodes(probe, tolerance_units):
            return 0
        after = _count_coordinates(probe)
    except Exception:
        return 0
    return max(0, before - after)


def count_parts(geom: QgsGeometry) -> int:
    """Nombre de parties d'une géométrie (1 pour une géométrie simple).

    Note : une couche de type Multi* dont les entités n'ont qu'une seule partie
    n'est PAS considérée comme multi-parties — seul ``> 1`` est une anomalie.
    """
    if geom is None or geom.isNull() or geom.isEmpty():
        return 0
    try:
        abstract = geom.constGet()
        return abstract.partCount() if abstract is not None else 0
    except Exception:
        return 0


def explode_parts(geom: QgsGeometry) -> List[QgsGeometry]:
    """Éclate une géométrie multi-parties en géométries simples.

    Renvoie ``[geom]`` si l'éclatement est impossible ou ne produit rien
    d'exploitable (jamais de liste vide : aucune entité n'est perdue).
    """
    if geom is None or geom.isNull() or geom.isEmpty():
        return [geom]
    try:
        parts = geom.asGeometryCollection()
    except Exception:
        return [geom]
    usable = [p for p in parts
              if p is not None and not p.isNull() and not p.isEmpty()]
    return usable or [geom]


def _interior_ring_count(part) -> int:
    """Nombre d'anneaux intérieurs (trous) d'une partie ; 0 si indisponible."""
    try:
        return int(part.numInteriorRings())
    except Exception:
        return 0


def _exterior_ring(part):
    """Anneau extérieur d'une partie polygonale ; ``None`` si non applicable."""
    try:
        return part.exteriorRing()
    except Exception:
        return None


def _interior_ring(part, index: int):
    """Anneau intérieur d'indice donné ; ``None`` si indisponible."""
    try:
        return part.interiorRing(index)
    except Exception:
        return None


def _ring_points(ring) -> List:
    """Sommets d'un anneau (liste fermée) ; liste vide si illisible."""
    try:
        return list(ring.points())
    except Exception:
        return []


def _iter_rings(geom: QgsGeometry):
    """Itère tous les anneaux (extérieurs et intérieurs) d'une géométrie
    polygonale. Ne produit rien pour les autres types de géométrie."""
    if geom is None or geom.isNull() or geom.isEmpty():
        return
    try:
        parts = list(geom.constParts())
    except Exception:
        return
    for part in parts:
        ring = _exterior_ring(part)
        if ring is not None:
            yield ring
        for index in range(_interior_ring_count(part)):
            inner = _interior_ring(part, index)
            if inner is not None:
                yield inner


def _same_xy(a, b) -> bool:
    """Vrai si deux sommets partagent la même position planaire.

    On ignore volontairement Z/M : un anneau fermé dont le dernier sommet ne
    diffère que par son Z reste un anneau fermé, et son sommet initial ne doit
    pas être compté deux fois.
    """
    try:
        return a.x() == b.x() and a.y() == b.y()
    except Exception:
        return False


def _unique_ring_points(points: List) -> List:
    """Sommets d'un anneau sans le sommet de fermeture répété."""
    if len(points) > 1 and _same_xy(points[0], points[-1]):
        return points[:-1]
    return list(points)


def _point_distance(a, b) -> Optional[float]:
    """Distance planaire entre deux sommets ; ``None`` si non calculable."""
    try:
        return float(a.distance(b))
    except Exception:
        return None


def _vertex_angle_deg(a, b, c) -> Optional[float]:
    """Angle (en degrés) formé en ``b`` par les segments ``ba`` et ``bc``.

    ``None`` si l'angle est indéfini (sommets superposés).
    """
    try:
        v1x, v1y = a.x() - b.x(), a.y() - b.y()
        v2x, v2y = c.x() - b.x(), c.y() - b.y()
    except Exception:
        return None
    norm1 = math.hypot(v1x, v1y)
    norm2 = math.hypot(v2x, v2y)
    if norm1 == 0 or norm2 == 0:
        return None
    cosine = (v1x * v2x + v1y * v2y) / (norm1 * norm2)
    cosine = max(-1.0, min(1.0, cosine))
    return math.degrees(math.acos(cosine))


# ── Contrôles « Autres » : détection ────────────────────────────────────────

def _single_sliver_ratio(geom: QgsGeometry) -> Optional[float]:
    """Rapport largeur/longueur du rectangle englobant ORIENTÉ d'UNE partie."""
    if geom is None or geom.isNull() or geom.isEmpty():
        return None
    try:
        result = geom.orientedMinimumBoundingBox()
    except Exception:
        return None
    if not result or len(result) < 5:
        return None
    try:
        width, height = float(result[3]), float(result[4])
    except (TypeError, ValueError):
        return None
    longest = max(width, height)
    if longest <= 0:
        return None
    return min(width, height) / longest


def sliver_ratio(geom: QgsGeometry) -> Optional[float]:
    """Rapport largeur/longueur (0..1) mesurant l'ALLONGEMENT d'une forme.

    Proche de 1 pour une forme compacte (carré, cercle), proche de 0 pour une
    forme en lame. Mesuré sur le rectangle englobant ORIENTÉ, donc indépendant
    de l'orientation : une lame en diagonale donne le même rapport qu'une lame
    alignée sur les axes.

    Le contrôle « polygones filiformes » qui exploitait cette mesure a été
    retiré en 2.9.21 ; elle sert désormais uniquement à la règle du SOMMET
    MANQUANT (voir ``overlap_from_missing_vertex``), pour distinguer un
    recouvrement qui longe un mur d'une vraie morsure dans le bâti.

    Pour une entité MULTI-PARTIES, renvoie le rapport de la partie la moins
    allongée : deux parties compactes mais éloignées auraient un rectangle
    englobant global très allongé, ce qui serait trompeur.

    ``None`` si non calculable.
    """
    if geom is None or geom.isNull() or geom.isEmpty():
        return None
    if count_parts(geom) <= 1:
        return _single_sliver_ratio(geom)
    ratios = [r for r in (_single_sliver_ratio(part) for part in explode_parts(geom))
              if r is not None]
    return max(ratios) if ratios else None


def count_sharp_angles(geom: QgsGeometry, min_angle_deg: float) -> int:
    """Nombre de sommets dont l'angle intérieur est < ``min_angle_deg``.

    Ces « pointes » sont typiquement des artefacts de digitalisation.
    """
    if min_angle_deg <= 0:
        return 0
    total = 0
    for ring in _iter_rings(geom):
        unique = _unique_ring_points(_ring_points(ring))
        count = len(unique)
        if count < 3:
            continue
        for index in range(count):
            angle = _vertex_angle_deg(
                unique[(index - 1) % count], unique[index], unique[(index + 1) % count])
            if angle is not None and angle < min_angle_deg:
                total += 1
    return total


def count_close_vertex_pairs(geom: QgsGeometry, max_dist_units: float,
                             duplicate_tolerance_units: float = 0.0) -> int:
    """Nombre de paires de sommets consécutifs séparés de 0 < d ≤ seuil.

    Les deux seuils sont exprimés dans les UNITÉS DE LA COUCHE (voir
    ``layer_units_per_metre``), comme les distances entre sommets auxquelles ils
    sont comparés.

    Les sommets STRICTEMENT superposés (d = 0) sont exclus : ils relèvent du
    contrôle « sommets dupliqués », qui les compte séparément.
    ``duplicate_tolerance_units`` existe pour la signature générique de cette
    fonction ; l'appelant actuel y passe toujours 0 (sommets dupliqués =
    exactement superposés, sans tolérance réglable).
    """
    if max_dist_units <= 0:
        return 0
    floor = max(0.0, duplicate_tolerance_units)
    total = 0
    for ring in _iter_rings(geom):
        points = _ring_points(ring)
        for index in range(len(points) - 1):
            distance = _point_distance(points[index], points[index + 1])
            if distance is not None and floor < distance <= max_dist_units:
                total += 1
    return total


# ── Contrôles « Autres » : réparation ───────────────────────────────────────

def _filter_ring_spikes(ring, min_angle_deg: float):
    """Reconstruit un anneau sans ses sommets en pointe.

    Renvoie ``(anneau, nb_retirés)`` — ``anneau`` valant ``None`` si l'anneau
    devient dégénéré (< 3 sommets) — ou ``None`` en cas d'échec de lecture.
    """
    points = _ring_points(ring)
    if len(points) < 4:
        cloned = _clone_ring(ring)
        return None if cloned is None else (cloned, 0)
    unique = _unique_ring_points(points)
    count = len(unique)
    kept = []
    removed = 0
    for index in range(count):
        angle = _vertex_angle_deg(
            unique[(index - 1) % count], unique[index], unique[(index + 1) % count])
        if angle is not None and angle < min_angle_deg:
            removed += 1
            continue
        kept.append(unique[index])
    if not removed:
        cloned = _clone_ring(ring)
        return None if cloned is None else (cloned, 0)
    if len(kept) < 3:
        return (None, removed)
    try:
        return (QgsLineString(kept + [kept[0]]), removed)
    except Exception:
        return None


def _clone_ring(ring):
    try:
        return ring.clone()
    except Exception:
        return None


def remove_sharp_angle_vertices(geom: QgsGeometry, min_angle_deg: float):
    """Supprime les sommets en pointe et renvoie ``(géométrie, nb_retirés)``.

    Prudence : la géométrie d'origine est conservée telle quelle si la
    reconstruction échoue, dégénère ou produit une géométrie invalide.
    """
    if geom is None or geom.isNull() or geom.isEmpty() or min_angle_deg <= 0:
        return (geom, 0)
    try:
        parts = list(geom.constParts())
    except Exception:
        return (geom, 0)

    removed = 0
    rebuilt = []
    for part in parts:
        outer = _exterior_ring(part)
        if outer is None:
            return (geom, 0)
        filtered = _filter_ring_spikes(outer, min_angle_deg)
        if filtered is None or filtered[0] is None:
            return (geom, 0)          # anneau extérieur dégénéré -> on ne touche pas
        outer_ring, outer_removed = filtered
        removed += outer_removed

        inner_rings = []
        for index in range(_interior_ring_count(part)):
            inner = _interior_ring(part, index)
            if inner is None:
                return (geom, 0)
            filtered_inner = _filter_ring_spikes(inner, min_angle_deg)
            if filtered_inner is None:
                return (geom, 0)
            inner_ring, inner_removed = filtered_inner
            if inner_ring is None:
                # Un trou qui dégénère serait purement supprimé : ce n'est pas
                # le rôle de ce contrôle. On abandonne la correction de l'entité.
                return (geom, 0)
            removed += inner_removed
            inner_rings.append(inner_ring)

        try:
            poly = QgsPolygon()
            poly.setExteriorRing(outer_ring)
            for inner_ring in inner_rings:
                poly.addInteriorRing(inner_ring)
            rebuilt.append(poly)
        except Exception:
            return (geom, 0)

    if not removed or not rebuilt:
        return (geom, 0)

    new_geom = _assemble_polygons(rebuilt)
    if new_geom is None or new_geom.isNull() or new_geom.isEmpty():
        return (geom, 0)
    try:
        if not new_geom.isGeosValid():
            return (geom, 0)          # sécurité : jamais dégrader la validité
    except Exception:
        return (geom, 0)
    return (new_geom, removed)


def coerce_to_geometry_type(geom: QgsGeometry, target_type) -> Optional[QgsGeometry]:
    """Ne conserve que les parties de ``geom`` compatibles avec ``target_type``.

    ``makeValid()`` peut renvoyer une GeometryCollection hétérogène (par exemple
    Polygon + LineString pour un polygone à arête pendante). Écrire une telle
    géométrie dans une couche typée est REFUSÉ par le fournisseur, qui abandonne
    alors tout le lot. On filtre donc les parties utiles, comme le fait
    l'algorithme natif « Corriger les géométries ».

    Renvoie ``None`` si aucune partie exploitable ne subsiste.
    """
    if geom is None or geom.isNull() or geom.isEmpty():
        return None
    try:
        if QgsWkbTypes.geometryType(geom.wkbType()) == target_type:
            return geom
    except Exception:
        return None
    kept = []
    for part in explode_parts(geom):
        if part is None or part.isNull() or part.isEmpty():
            continue
        try:
            same = QgsWkbTypes.geometryType(part.wkbType()) == target_type
        except Exception:
            same = False
        if same:
            kept.append(part)
    if not kept:
        return None
    if len(kept) == 1:
        return kept[0]
    try:
        collected = QgsGeometry.collectGeometry(kept)
    except Exception:
        return kept[0]
    if collected is None or collected.isNull() or collected.isEmpty():
        return kept[0]
    return collected


def build_output_fields(fields) -> "tuple":
    """Champs de la couche réparée, avec les identifiants réservés renommés.

    Un attribut nommé ``fid`` (GeoPackage) ou ``ogc_fid`` (SQLite/PostGIS) est
    repris par le pilote OGR comme CLÉ PRIMAIRE du fichier écrit. Or la
    réparation renumérote les entités : l'éclatement des multi-parties produit
    plusieurs entités portant le même identifiant d'origine. L'écriture échouait
    alors en bloc (« UNIQUE constraint failed: <couche>.fid ») et aucun fichier
    exploitable n'était produit.

    Ces attributs sont donc renommés (``fid`` -> ``fid_src``) : la valeur
    d'origine — précieuse pour remonter à l'entité source — est conservée, et le
    fichier de sortie reçoit sa propre clé primaire, générée par le pilote.

    L'ORDRE et le NOMBRE de champs sont préservés à l'identique : les attributs
    des entités sont recopiés par position (``setAttributes``).

    Renvoie ``(QgsFields, [(ancien_nom, nouveau_nom), …])``.
    """
    out = QgsFields()
    renamed: List[tuple] = []
    taken = {f.name().lower() for f in fields}
    for src in fields:
        if src.name().lower() not in C.RESERVED_FID_FIELDS:
            out.append(src)
            continue
        # Nom de repli unique, même si « fid_src » existe déjà dans la source.
        base = src.name() + C.RESERVED_FID_SUFFIX
        candidate, n = base, 1
        while candidate.lower() in taken:
            n += 1
            candidate = "%s%d" % (base, n)
        taken.add(candidate.lower())
        new_field = QgsField(src)
        new_field.setName(candidate)
        out.append(new_field)
        renamed.append((src.name(), candidate))
    return out, renamed


def is_safe_replacement(original: QgsGeometry, candidate: QgsGeometry) -> bool:
    """Vrai si ``candidate`` peut remplacer ``original`` sans le dégrader.

    Refuse un candidat vide, une surface effondrée (aire nulle alors que
    l'originale en avait), ou une géométrie devenue invalide alors que
    l'originale était valide. Garantit qu'une réparation n'abîme jamais une
    entité saine (cf. fusion de sommets, qui peut réduire un anneau à moins de
    trois sommets distincts).
    """
    if candidate is None or candidate.isNull() or candidate.isEmpty():
        return False
    try:
        original_area = original.area()
        candidate_area = candidate.area()
    except Exception:
        return False
    if original_area > 0 and candidate_area <= 0:
        return False
    try:
        if original.isGeosValid() and not candidate.isGeosValid():
            return False
    except Exception:
        return False
    return True


def _assemble_polygons(polygons) -> Optional[QgsGeometry]:
    """Assemble une liste de QgsPolygon en une géométrie simple ou multiple."""
    try:
        if len(polygons) == 1:
            return QgsGeometry(polygons[0])
        multi = QgsMultiPolygon()
        for poly in polygons:
            multi.addGeometry(poly)
        return QgsGeometry(multi)
    except Exception:
        return None


def _ring_area_m2(ring, measure_area: Optional[Callable[[QgsGeometry], Optional[float]]],
                  planar_is_m2: bool = True):
    """Surface d'un anneau (trou), en m². ``None`` si non mesurable.

    L'anneau est promu en polygone pour pouvoir être mesuré. On privilégie la
    mesure ellipsoïdale fournie par l'appelant.

    ``planar_is_m2`` : le repli planaire (``geom.area()``) ne renvoie des m²
    que si l'unité de la couche EST le mètre. Sur une couche en EPSG:4326 il
    renvoie des degrés carrés — comparés à un seuil en m², un trou de
    1e-4 deg² (~1 200 m²) se lisait « 0,0001 m² », donc sous n'importe quel
    seuil, donc COMBLÉ. Hors couche métrique, mieux vaut donc ne rien
    conclure : l'appelant conserve alors le trou et ne le compte pas.
    """
    try:
        poly = QgsPolygon()
        poly.setExteriorRing(ring.clone())
        geom = QgsGeometry(poly)
    except Exception:
        return None
    if measure_area is not None:
        area = measure_area(geom)
        if area is not None:
            return area
    if not planar_is_m2:
        return None
    try:
        return geom.area()
    except Exception:
        return None


def _difference_or_none(geom: QgsGeometry,
                        other: QgsGeometry) -> Optional[QgsGeometry]:
    """``geom`` moins ``other``, avec une SECONDE tentative sur copies valides.

    GEOS refuse une opération booléenne sur une géométrie invalide. Sans cette
    reprise, l'échec était comptabilisé « chevauchement trop important, à
    corriger à la main » — un motif faux : le rognage était simplement
    impossible en l'état, et il redevient possible une fois la géométrie rendue
    valide (ce que la réparation fait de toute façon juste après, quand la case
    correspondante est cochée).

    Renvoie ``None`` si les deux tentatives échouent ; l'appelant traite alors
    le cas en correction manuelle, ce qui est cette fois justifié.

    Le repli passe par un helper à sentinelle plutôt que par un
    ``try/except: pass`` : ce motif est refusé par le contrôle de sécurité de
    plugins.qgis.org (Bandit B110), qui n'honore pas les annotations « nosec ».
    """
    def _diff(a, b):
        try:
            return a.difference(b)
        except Exception:
            return None

    resultat = _diff(geom, other)
    if resultat is not None:
        return resultat
    valide_a = _validated_for_area(geom)
    valide_b = _validated_for_area(other)
    if valide_a is None or valide_b is None:
        return None
    return _diff(valide_a, valide_b)


def _validated_for_area(geom: QgsGeometry) -> Optional[QgsGeometry]:
    """Copie rendue valide de ``geom``, utilisable pour MESURER une surface.

    Destinée au seul critère « petit polygone ». Sur une géométrie invalide, la
    somme des aires signées des anneaux peut s'annuler (auto-intersection en
    papillon : deux lobes d'orientations opposées), et un polygone de 50 m² se
    mesure alors à 0 m². Sans cette validation, il était compté « petit » puis
    supprimé par la réparation — une perte de données pour une anomalie par
    ailleurs réparable.

    Ne conserve que les parties SURFACIQUES : ``makeValid`` peut renvoyer une
    GeometryCollection mêlant polygones et lignes, dont les parties linéaires
    n'ont aucune surface à mesurer. Renvoie ``None`` si la validation échoue :
    l'appelant abandonne alors le critère de surface pour cette entité plutôt
    que de se fier à une mesure dénuée de sens.
    """
    try:
        fixed = geom.makeValid()
    except Exception:
        return None
    if fixed is None or fixed.isNull() or fixed.isEmpty():
        return None
    return coerce_to_geometry_type(fixed, QgsWkbTypes.GeometryType.PolygonGeometry)


def count_small_holes(geom: QgsGeometry, max_area_m2: float,
                      measure_area: Optional[Callable] = None,
                      planar_is_m2: bool = True):
    """(nb de trous ≤ seuil, nb total de trous) d'une géométrie polygonale.

    Les trous plus grands que ``max_area_m2`` sont considérés comme justifiés
    (cour intérieure, îlot, plan d'eau…) et ne sont pas comptés comme fautifs.

    ``planar_is_m2`` : voir ``_ring_area_m2``. À faux, un trou dont la mesure
    ellipsoïdale échoue n'est pas compté plutôt que jugé sur des unités qui ne
    sont pas des m².
    """
    if geom is None or geom.isNull() or geom.isEmpty():
        return (0, 0)
    try:
        parts = list(geom.constParts())
    except Exception:
        return (0, 0)
    small = 0
    total = 0
    for part in parts:
        for index in range(_interior_ring_count(part)):
            total += 1
            area = _ring_area_m2(part.interiorRing(index), measure_area,
                                 planar_is_m2)
            if area is not None and area <= max_area_m2:
                small += 1
    return (small, total)


def remove_small_holes(geom: QgsGeometry, max_area_m2: float,
                       measure_area: Optional[Callable] = None,
                       planar_is_m2: bool = True):
    """Supprime les trous ≤ seuil et renvoie (géométrie, nb de trous retirés).

    Les trous dépassant le seuil sont PRÉSERVÉS. La géométrie d'origine est
    renvoyée inchangée si aucun trou n'est concerné ou en cas d'échec.
    """
    if geom is None or geom.isNull() or geom.isEmpty():
        return (geom, 0)
    try:
        parts = list(geom.constParts())
    except Exception:
        return (geom, 0)

    removed = 0
    rebuilt = []
    for part in parts:
        n_rings = _interior_ring_count(part)
        if n_rings == 0:
            try:
                rebuilt.append(part.clone())
            except Exception:
                return (geom, 0)
            continue
        keep = []
        for index in range(n_rings):
            ring = part.interiorRing(index)
            area = _ring_area_m2(ring, measure_area, planar_is_m2)
            if area is not None and area <= max_area_m2:
                removed += 1
            else:
                try:
                    keep.append(ring.clone())
                except Exception:
                    return (geom, 0)
        try:
            poly = QgsPolygon()
            poly.setExteriorRing(part.exteriorRing().clone())
            for ring in keep:
                poly.addInteriorRing(ring)
            rebuilt.append(poly)
        except Exception:
            return (geom, 0)

    if not removed or not rebuilt:
        return (geom, 0)

    new_geom = _assemble_polygons(rebuilt)
    if new_geom is None or new_geom.isNull() or new_geom.isEmpty():
        return (geom, 0)
    return (new_geom, removed)


OVERLAP_NONE = None            # pas de recouvrement d'intérieurs
OVERLAP_PARTIAL = "overlap"    # recouvrement partiel
OVERLAP_CONTAINED = "contained"  # un polygone entièrement inclus dans l'autre
# Recouvrement imputable à un SOMMET MANQUANT sur la frontière commune : un
# sommet d'un bâti empiète sur une arête de l'autre, laquelle n'a pas de sommet
# à cet endroit pour l'accrocher. Voir ``overlap_from_missing_vertex``.
OVERLAP_MISSING_VERTEX = "missing_vertex"


def _has_vertex_strictly_inside(geom: QgsGeometry, other: QgsGeometry) -> bool:
    """Vrai si au moins un sommet de ``geom`` est STRICTEMENT dans ``other``.

    « Strictement » exclut les sommets posés exactement sur la frontière de
    ``other`` : ceux-là sont correctement accrochés et n'empiètent sur rien.
    Sortie dès le premier sommet trouvé — seule la présence compte.
    """
    try:
        for vertex in geom.vertices():
            point = QgsGeometry.fromPointXY(QgsPointXY(vertex.x(), vertex.y()))
            if other.contains(point):
                return True
    except Exception:
        return False
    return False


def _overlap_is_filiform(surface: Optional[QgsGeometry],
                         max_ratio: float) -> bool:
    """La zone commune longe-t-elle la frontière au lieu d'y mordre ?

    ``surface`` à ``None`` — ou d'aire nulle — signifie que l'intersection n'a
    AUCUNE partie surfacique : GEOS l'a réduite à une ligne, à des points, ou à
    un polygone d'aire nulle. C'est le cas limite du recouvrement filiforme, une
    zone commune d'épaisseur nulle, et non l'inverse.

    C'est précisément ce qui arrive sur deux murs mitoyens dont les coordonnées
    diffèrent dans les derniers bits : ``touches`` est faux (les intérieurs se
    recoupent d'un milliardième de millimètre carré) mais l'overlay ne parvient
    plus à en former un polygone. Traiter ces cas comme des conflits compacts
    revenait à leur refuser l'imputation à un sommet manquant — donc à les
    signaler malgré l'option cochée.
    """
    if surface is None:
        return True
    try:
        if surface.area() <= 0:
            return True
    except Exception:
        return True
    ratio = sliver_ratio(surface)
    return ratio is not None and ratio <= max_ratio


def overlap_from_missing_vertex(g: QgsGeometry, og: QgsGeometry,
                               surface: Optional[QgsGeometry],
                               max_ratio: float) -> bool:
    """Vrai si le recouvrement s'explique par un SOMMET MANQUANT.

    Cas visé : deux bâtis censés être mitoyens. L'un porte un sommet qui devrait
    être accroché sur l'arête de l'autre, mais cette arête n'a aucun sommet à cet
    endroit — le sommet se retrouve donc légèrement à l'intérieur du voisin et
    crée un recouvrement en lame de couteau le long du mur commun.

    DEUX conditions, toutes deux nécessaires :

    1. Empiètement À SENS UNIQUE : les sommets d'un seul des deux polygones sont
       à l'intérieur de l'autre. Si les deux s'interpénètrent (coin sur coin) ou
       si aucun sommet n'entre (arêtes qui se croisent en X), le conflit est
       géométrique et non imputable à un sommet absent.

    2. Recouvrement FILIFORME (rapport largeur/longueur ≤ ``max_ratio``) : la
       zone commune longe la frontière au lieu de mordre dans le bâti. Une
       intersection DÉGÉNÉRÉE — réduite par GEOS à une ligne, à des points ou à
       une aire nulle — satisfait cette condition par définition : son épaisseur
       est nulle (voir ``_overlap_is_filiform``).

    La condition 2 est la garde essentielle. Sans elle, un bâtiment entièrement
    décalé de 2 m dans son voisin satisferait la condition 1 (ses sommets sont
    dans le voisin, l'inverse est faux) et serait ignoré à tort, alors que c'est
    une erreur majeure. Avec elle, un tel recouvrement reste compact, donc
    signalé.
    """
    if max_ratio <= 0:
        return False
    if not _overlap_is_filiform(surface, max_ratio):
        return False          # zone commune trop compacte : vrai conflit
    a_intrudes = _has_vertex_strictly_inside(g, og)
    b_intrudes = _has_vertex_strictly_inside(og, g)
    return a_intrudes != b_intrudes


def _is_vertex_of(geom: QgsGeometry, point: QgsPointXY,
                  tol: float = C.LINE_CROSSING_VERTEX_TOLERANCE) -> bool:
    """Vrai si ``point`` coïncide EXACTEMENT avec un sommet de ``geom``.

    Tolérance minuscule et fixe : elle absorbe l'imprécision flottante d'un
    aller-retour par GEOS, jamais l'écart d'un vrai croisement non nodé (voir
    ``constants.LINE_CROSSING_VERTEX_TOLERANCE``).
    """
    try:
        for v in geom.vertices():
            if abs(v.x() - point.x()) <= tol and abs(v.y() - point.y()) <= tol:
                return True
    except Exception:
        return False
    return False


def unnoded_crossing_points(a: QgsGeometry, b: QgsGeometry) -> List[tuple]:
    """Points où deux lignes DISTINCTES se croisent/touchent sans sommet commun.

    La jonction existe géométriquement (les tracés se rencontrent), mais aucune
    des deux lignes n'y a de sommet : un outil réseau (routage, connexité) ne
    peut pas s'y accrocher, même si la carte donne l'illusion d'un carrefour.

    Un chevauchement COLINÉAIRE — l'intersection est une ligne, pas des points
    isolés — n'est pas ce défaut et n'est jamais renvoyé ici : c'est une
    superposition de tracé, pas une jonction manquée.

    Best-effort : toute erreur GEOS est absorbée et renvoie une liste vide,
    comme le reste des contrôles topologiques de ce module.
    """
    try:
        inter = a.intersection(b)
    except Exception:
        return []
    if inter is None or inter.isNull() or inter.isEmpty():
        return []
    try:
        gtype = QgsWkbTypes.geometryType(inter.wkbType())
    except Exception:
        return []
    if gtype != QgsWkbTypes.GeometryType.PointGeometry:
        return []
    try:
        raw_pts = list(inter.asMultiPoint()) if inter.isMultipart() else [inter.asPoint()]
    except Exception:
        return []
    return [(p.x(), p.y()) for p in raw_pts
            if not (_is_vertex_of(a, p) and _is_vertex_of(b, p))]


def feature_in_aoi(geom, aoi_geom) -> bool:
    """Cette géométrie appartient-elle à la zone d'analyse ?

    RÈGLE UNIQUE, volontairement partagée par ``GeomAnalyzer.analyze`` et
    ``GeomAnalyzer.build_repaired_layer`` : le test était écrit deux fois, et
    deux écritures d'une même règle finissent toujours par diverger — la
    réparation aurait alors produit un extrait d'un périmètre différent de
    celui que l'analyse a contrôlé.

    « Intersecte », et non « est contenue » : un bâtiment à cheval sur le bord
    est retenu ENTIER. Le contraire laisserait les entités de bordure
    contrôlées par personne lors d'un découpage en tuiles.

    Une géométrie absente n'appartient à aucune zone : sans coordonnées, elle
    relève de la tuile qui la porte, pas de celle-ci. Un test qui échoue, en
    revanche, GARDE l'entité : une anomalie analysée en trop se voit et se
    discute, une anomalie écartée en silence, non.
    """
    if aoi_geom is None:
        return True
    if geom is None or geom.isNull() or geom.isEmpty():
        return False
    try:
        return bool(aoi_geom.intersects(geom))
    except Exception:
        return True


def _geometry_type_of(feat):
    """Type de géométrie d'une entité (``QgsWkbTypes.GeometryType``), ou None.

    Utilisé par la passe de voisinage de la zone d'analyse, qui doit ranger
    chaque voisin dans le bon index sans refaire tous les contrôles.
    """
    try:
        if not feat.hasGeometry():
            return None
        geom = feat.geometry()
        if geom is None or geom.isNull():
            return None
        return QgsWkbTypes.geometryType(geom.wkbType())
    except Exception:
        return None


def resolve_aoi_rect(layer: QgsVectorLayer, rect, crs_authid: str, ctx=None):
    """Convertit un rectangle d'AOI dans le CRS de la COUCHE.

    ``rect`` : ``(xmin, ymin, xmax, ymax)`` ou ``QgsRectangle``, exprimé dans
    ``crs_authid`` (le CRS où l'utilisateur l'a tracé — celui du canevas).
    ``crs_authid`` vide, inconnu ou identique à celui de la couche : le
    rectangle est pris tel quel.

    La conversion passe par ``transformBoundingBox`` et NON par les quatre
    coins : QGIS y densifie les bords avant de projeter. Sans cela, un
    rectangle tracé en Web Mercator et reprojeté en Lambert-93 se refermerait
    à l'intérieur de la zone visée, et les entités les plus proches des bords
    — justement celles qu'on regarde — sortiraient de l'analyse.

    ``ctx`` : ``MeasurementContext`` capturé sur le thread PRINCIPAL. Comme
    pour les mesures, le projet ne doit pas être interrogé depuis un thread
    secondaire.

    Renvoie un ``QgsRectangle`` dans le CRS de la couche, ou ``None`` si la
    conversion est impossible ou le rectangle dégénéré — l'appelant doit alors
    renoncer à la restriction et le DIRE, jamais analyser une zone vide en
    silence.
    """
    if rect is None:
        return None
    try:
        if isinstance(rect, QgsRectangle):
            source_rect = QgsRectangle(rect)
        else:
            xmin, ymin, xmax, ymax = (float(v) for v in rect)
            source_rect = QgsRectangle(xmin, ymin, xmax, ymax)
    except Exception:
        return None
    source_rect.normalize()
    if source_rect.isEmpty() or source_rect.width() <= 0 or source_rect.height() <= 0:
        return None

    target = layer.crs()
    if not crs_authid or not target.isValid():
        return source_rect
    try:
        source = QgsCoordinateReferenceSystem(crs_authid)
    except Exception:
        return source_rect
    if not source.isValid() or source.authid() == target.authid():
        return source_rect

    mctx = _resolve_measurement_context(ctx)
    try:
        tr = QgsCoordinateTransform(source, target, mctx.transform_context)
        converted = tr.transformBoundingBox(source_rect)
    except Exception:
        # Transformation refusée : ne PAS retomber sur le rectangle non
        # converti, qui désignerait une zone du globe sans rapport.
        return None
    if converted is None or converted.isEmpty():
        return None
    return converted


def layer_planar_area_is_m2(layer: QgsVectorLayer) -> bool:
    """L'unité de la couche est-elle le mètre ?

    Seule réponse « oui » autorise à lire une surface planaire
    (``QgsGeometry.area()``) comme des m². Sur un CRS géographique elle vaut
    des degrés carrés, sur un CRS en pieds des pieds carrés : la comparer à un
    seuil en m² donne un verdict faux de plusieurs ordres de grandeur.

    Repli PRUDENT à faux : mieux vaut ne pas conclure que conclure avec la
    mauvaise unité (voir ``_ring_area_m2``).
    """
    try:
        return layer.crs().mapUnits() == QgsUnitTypes.DistanceUnit.DistanceMeters
    except Exception:
        return False


def layer_metres_per_unit_range(layer: QgsVectorLayer, ctx=None) -> tuple:
    """``(minimum, maximum)`` de mètres-terrain par unité de la couche.

    Indispensable pour tout seuil exprimé en mètres : les opérations géométriques
    (``buffer``, ``boundingBox``, distance entre sommets, epsilon de fusion)
    travaillent dans les unités de la COUCHE, pas en mètres. Sans conversion, un
    seuil de 0,1 m vaudrait 0,1 degré (~11 km) sur une couche en EPSG:4326, ou
    0,1 pied (~3 cm) sur une couche en pieds américains.

    Deux valeurs, car sur un CRS géographique le facteur DÉPEND DE LA DIRECTION :
    à 48° de latitude, un degré vaut ~111 km du nord au sud mais ~75 km d'est en
    ouest. Aucun scalaire unique n'est donc juste dans toutes les directions, et
    l'appelant doit choisir selon ce qu'il risque (voir les deux usages dans
    ``analyze``) :

      * ``maximum`` -> conversion la plus PETITE en unités : un seuil qui écarte
        ou qui modifie la donnée (tolérance, epsilon de fusion) ne dépassera
        jamais ce que l'utilisateur a demandé ;
      * ``minimum`` -> conversion la plus GRANDE en unités : un seuil de
        DÉTECTION ne laissera jamais passer une anomalie sous prétexte de
        l'orientation.

    Sur un CRS projeté les deux directions coïncident et la question ne se pose
    pas. Renvoie ``(1.0, 1.0)`` si la mesure est impossible : repli sûr sur une
    couche supposée métrique, jamais d'exception propagée.

    ``ctx`` : ``MeasurementContext`` capturé sur le thread principal. À défaut,
    le projet courant est lu — acceptable seulement depuis le thread principal.
    """
    mctx = _resolve_measurement_context(ctx)
    try:
        crs = layer.crs()
        extent = layer.extent()
        if extent is None or extent.isEmpty():
            return (1.0, 1.0)
        centre = extent.center()
        da = QgsDistanceArea()
        da.setSourceCrs(crs, mctx.transform_context)
        da.setEllipsoid(mctx.usable_ellipsoid())
        # Pas de sonde adapté à l'unité : ~100 m en degrés, 1 unité sinon.
        step = 1e-3 if crs.isGeographic() else 1.0
        factors = []
        for dx, dy in ((step, 0.0), (0.0, step)):
            raw = da.measureLine(
                QgsPointXY(centre.x(), centre.y()),
                QgsPointXY(centre.x() + dx, centre.y() + dy))
            metres = da.convertLengthMeasurement(
                raw, QgsUnitTypes.DistanceUnit.DistanceMeters)
            factors.append(metres / step)
        usable = [f for f in factors if f > 0]
        if not usable:
            return (1.0, 1.0)
        return (min(usable), max(usable))
    except Exception:
        return (1.0, 1.0)


def layer_units_per_metre(layer: QgsVectorLayer, ctx=None) -> float:
    """Unités de la couche pour 1 mètre, en conversion PRUDENTE.

    Retient le plus grand facteur mètres/unité (voir
    ``layer_metres_per_unit_range``) : destiné aux seuils qui écartent des cas ou
    qui modifient la géométrie, lesquels ne doivent jamais aller au-delà de ce
    que l'utilisateur a demandé.
    """
    return 1.0 / layer_metres_per_unit_range(layer, ctx)[1]


def layer_units_per_metre_wide(layer: QgsVectorLayer, ctx=None) -> float:
    """Unités de la couche pour 1 mètre, en conversion LARGE.

    Retient le plus petit facteur mètres/unité : destiné aux seuils de DÉTECTION,
    afin qu'une anomalie ne soit jamais manquée à cause de l'orientation. Sur un
    CRS géographique, cela peut signaler un peu au-delà du seuil demandé dans la
    direction nord-sud — surdétecter est préférable à taire une anomalie.
    """
    return 1.0 / layer_metres_per_unit_range(layer, ctx)[0]


def _xy_via(geom: QgsGeometry, func_name: str):
    """``(x, y)`` du point produit par ``func_name``, ou ``None``.

    Helper renvoyant une sentinelle plutôt qu'un ``try/except: continue`` dans
    la boucle appelante : ce motif est refusé par le contrôle de sécurité de
    plugins.qgis.org (Bandit B112), qui n'honore pas les annotations « nosec ».
    """
    try:
        pt_geom = getattr(geom, func_name)()
        if pt_geom is None or pt_geom.isNull() or pt_geom.isEmpty():
            return None
        pt = pt_geom.asPoint()
        return (pt.x(), pt.y())
    except Exception:
        return None


def _inner_point_xy(geom: QgsGeometry):
    """Couple ``(x, y)`` situé à l'intérieur de ``geom``, ou ``None``.

    ``pointOnSurface`` garantit un point DANS la surface (contrairement au
    centroïde, qui peut tomber dehors sur une forme concave ou multi-parties).
    """
    if geom is None or geom.isNull() or geom.isEmpty():
        return None
    for func_name in ("pointOnSurface", "centroid"):
        xy = _xy_via(geom, func_name)
        if xy is not None:
            return xy
    return None


def classify_overlap_detailed(g: QgsGeometry, og: QgsGeometry,
                              missing_vertex_ratio: float = 0.0,
                              equal_is_overlap: bool = False):
    """Qualifie la relation entre deux polygones ET localise le recouvrement.

    RÈGLE DE DÉCISION — ``intersects`` ET NON ``touches``. C'est la règle exacte
    de GeoVectorQualityControl, relevée dans son binaire : ``QgsGeometry::
    intersects`` suivi de ``QgsGeometry::touches``, sans aucun seuil métrique.

    Deux prédicats DE-9IM suffisent, et aucune tolérance n'intervient :

      * ``touches`` est vrai quand les deux géométries se rencontrent SANS que
        leurs intérieurs se recoupent — frontière commune, contact en un point.
        C'est un accrochage correct, jamais une erreur ;
      * ``intersects`` sans ``touches`` signifie donc « les intérieurs se
        recoupent », ce qui pour deux polygones implique une surface commune
        strictement positive. Le moindre recouvrement est une erreur, y compris
        d'un millimètre carré.

    Le cas MIXTE — frontière partagée sur une partie du tracé et pénétration
    réelle ailleurs — donne ``touches`` faux : il est donc bien détecté, alors
    qu'un test portant sur le type de la géométrie d'intersection le ratait
    (GEOS renvoie une GeometryCollection « polygone + lignes »).

    La surface d'intersection n'est plus calculée pour DÉCIDER, seulement pour
    LOCALISER le problème et pour évaluer l'option « sommet manquant ». Si son
    extraction échoue, le chevauchement reste signalé sans point : le verdict
    appartient aux prédicats, jamais à un calcul annexe.

    ``equal_is_overlap`` : à vrai, deux géométries STRICTEMENT identiques sont
    signalées comme recouvrement au lieu d'être déléguées au contrôle des
    doublons. À activer quand ce dernier est décoché, sans quoi une
    superposition totale n'est vue par personne.

    Renvoie ``(genre, point)`` où ``genre`` vaut :
      * ``None``               -> pas de recouvrement d'intérieurs (disjoints,
                                  simple contact de frontière, ou doublon exact
                                  délégué au contrôle des doublons) ;
      * ``OVERLAP_CONTAINED``  -> l'un des deux est ENTIÈREMENT inclus dans
                                  l'autre (imbrication) ;
      * ``OVERLAP_MISSING_VERTEX`` -> imputable à un sommet manquant sur la
                                  frontière commune (voir
                                  ``overlap_from_missing_vertex``) ; évalué
                                  seulement si ``missing_vertex_ratio`` > 0 ;
      * ``OVERLAP_PARTIAL``    -> recouvrement partiel classique.

    ``point`` est un couple ``(x, y)`` situé sur la zone commune (``None`` si non
    localisable), afin que la couche d'erreurs pointe l'endroit exact du
    chevauchement et non le centre des polygones. On le prend dans la partie
    surfacique quand elle existe, sinon sur l'intersection telle quelle — une
    ligne localise le mur en litige aussi bien qu'un polygone. Ce sont des
    flottants simples, sûrs à transporter entre threads.

    Toute erreur d'API GEOS est absorbée et interprétée comme « pas de
    recouvrement », afin qu'une géométrie fautive n'interrompe pas le lot.
    """
    try:
        if not g.intersects(og):
            return (OVERLAP_NONE, None)
        if g.equals(og):
            # Doublon exact : normalement comptabilisé par le contrôle des
            # doublons, qui le décrit mieux. Mais ce contrôle est DÉCOCHABLE :
            # sans ce garde-fou, deux polygones parfaitement superposés
            # devenaient invisibles des deux côtés à la fois (0 doublon, 0
            # chevauchement, score 100). Classé en recouvrement PARTIEL et non
            # en imbrication, sinon l'option « ignorer les polygones inclus »
            # les masquerait de nouveau.
            if not equal_is_overlap:
                return (OVERLAP_NONE, None)
            return (OVERLAP_PARTIAL, _inner_point_xy(g))
        if g.touches(og):
            return (OVERLAP_NONE, None)   # accrochage correct, pas une erreur
        # À partir d'ici le verdict est acquis. La suite ne sert qu'à qualifier
        # et à localiser : ne garder que les parties SURFACIQUES de
        # l'intersection, une intersection mixte étant une GeometryCollection.
        surface = None
        inter = g.intersection(og)
        if inter is not None and not inter.isNull() and not inter.isEmpty():
            surface = coerce_to_geometry_type(
                inter, QgsWkbTypes.GeometryType.PolygonGeometry)
            if surface is not None and surface.area() <= 0:
                surface = None
        if g.within(og) or og.within(g):
            kind = OVERLAP_CONTAINED
        elif overlap_from_missing_vertex(g, og, surface, missing_vertex_ratio):
            # Imbrication écartée d'abord : un polygone inclus a TOUS ses sommets
            # chez l'autre, ce qui satisferait le critère d'empiètement à sens
            # unique sans être un problème de sommet manquant.
            #
            # La règle est évaluée MÊME sans partie surfacique (``surface`` à
            # None). Exiger une surface mesurable écartait d'office plus de la
            # moitié des paires d'un jeu de bâti mitoyen réel : GEOS y réduit
            # l'intersection à une ligne dès que le recouvrement tient dans le
            # bruit du flottant, et ces paires — les plus filiformes qui soient —
            # étaient alors classées en conflit compact, donc signalées malgré
            # l'option cochée.
            kind = OVERLAP_MISSING_VERTEX
        else:
            kind = OVERLAP_PARTIAL
        # Localisation : à défaut de partie surfacique, un point de
        # l'intersection linéaire situe tout aussi bien le problème — sans quoi
        # la couche d'erreurs n'aurait aucun point à poser sur ces paires.
        locator = surface if surface is not None else inter
        return (kind, _inner_point_xy(locator) if locator is not None else None)
    except Exception:
        return (OVERLAP_NONE, None)


def containment_pair(g: QgsGeometry, og: QgsGeometry, fid_g, fid_og):
    """Ordonne deux polygones en ``(inclus, contenant)``, ou ``(None, None)``.

    L'inclusion est celle de ``QgsGeometry.within`` : TOUTE la géométrie tient
    dans l'autre, frontière comprise. Un simple chevauchement partiel n'en est
    pas une et ne concerne donc pas ce contrôle.

    Deux géométries qui se contiennent MUTUELLEMENT occupent la même emprise :
    c'est un doublon, que le contrôle des doublons décrit mieux, et non une
    imbrication. Sans cette exclusion, comparer leurs altitudes signalerait
    systématiquement une anomalie — une valeur n'est jamais strictement
    supérieure à elle-même — sur un problème qui n'est pas celui-là.

    Le critère est bien le containment mutuel et non ``equals``, qui compare la
    LISTE DES SOMMETS : deux tracés du même contour dont l'un porte un sommet
    colinéaire de plus ne sont pas « equals », et le contrôle des doublons ne
    les rattrape pas non plus (il hache le WKB normalisé, qui diffère). La paire
    passait donc pour une imbrication et son altitude était déclarée fautive à
    coup sûr. Le containment mutuel couvre les deux cas d'un seul test, et coûte
    un appel GEOS de moins.

    Toute erreur d'API GEOS est absorbée : une géométrie fautive écarte la
    paire au lieu d'interrompre le lot.
    """
    try:
        dans_og = g.within(og)
        dans_g = og.within(g)
    except Exception:
        return (None, None)
    if dans_og and dans_g:
        return (None, None)       # même emprise : doublon, pas imbrication
    if dans_og:
        return (fid_g, fid_og)
    if dans_g:
        return (fid_og, fid_g)
    return (None, None)


def compare_nested_heights(inner_values, outer_values, labels):
    """Champs d'altitude en faute pour un couple (polygone inclus, contenant).

    RÈGLE — la valeur du polygone INCLUS doit être STRICTEMENT supérieure à
    celle de son contenant, champ par champ et jamais d'un champ à l'autre :
    AGL se compare à AGL, HEIGHT à HEIGHT. Égalité comprise dans la faute, le
    « strictement » étant la demande.

    Un champ vide ou non numérique d'un seul côté rend la comparaison
    impossible POUR CE CHAMP : il est passé, jamais compté comme conforme —
    sans quoi une couche à moitié renseignée afficherait un sans-faute qu'elle
    n'a pas mérité.

    Renvoie ``(champs_en_faute, comparaison_concluante)``. ``concluante`` est
    faux quand aucun champ n'était comparable des deux côtés.
    """
    faults = []
    conclusive = False
    for i, label in enumerate(labels):
        inner = inner_values[i] if inner_values else None
        outer = outer_values[i] if outer_values else None
        if inner is None or outer is None:
            continue
        conclusive = True
        if inner <= outer:
            faults.append(label)
    return (faults, conclusive)


def classify_overlap(g: QgsGeometry, og: QgsGeometry,
                     missing_vertex_ratio: float = 0.0,
                     equal_is_overlap: bool = False) -> Optional[str]:
    """Genre de recouvrement entre deux polygones (voir
    ``classify_overlap_detailed``, dont ceci n'expose que le premier terme)."""
    return classify_overlap_detailed(
        g, og, missing_vertex_ratio, equal_is_overlap)[0]


# ═══════════════════════════════════════════════════════════════════════════════
# SCORE DE CORRECTNESS
# ═══════════════════════════════════════════════════════════════════════════════

# Pondérations et barèmes désormais centralisés dans ``constants`` afin de
# partager une source unique avec l'UI et les rapports.
_WEIGHTS = C.WEIGHTS
_GRADE_BANDS = C.GRADE_BANDS


def compute_correctness(result: AnalysisResult,
                        include_duplicate_vertex: bool = False,
                        include_holes: bool = False,
                        include_multipart: bool = False,
                        include_sharp_angles: bool = False,
                        include_close_vertices: bool = False,
                        include_nested_height: bool = False,
                        include_line_crossings: bool = False,
                        include_overlap: bool = True,
                        include_duplicate: bool = True,
                        include_small: bool = True) -> CorrectnessScore:
    """Calcule un score de qualité 0..100 à partir des métriques détectées.

    Modèle par pénalités : chaque catégorie de problème retranche une part
    proportionnelle au ratio d'entités concernées, pondérée par sa gravité.

    RÈGLE ESSENTIELLE — un contrôle qui n'a PAS tourné n'entre ni dans le score
    ni dans les sous-scores. Sans cela, son ratio valait 0 et produisait un
    sous-score à 100 % : le rapport affichait « Absence de chevauchement 100 % »
    et une note « 100/100 Excellent » pour un contrôle décoché, ce qui laissait
    croire à une donnée saine alors que rien n'avait été mesuré. Chaque
    dimension facultative a donc son ``include_*``, y compris les chevauchements
    et les doublons qui en étaient dépourvus.

    Seuls ``null_empty`` et ``invalid`` sont inconditionnels : ils n'ont pas de
    case à décocher et sont toujours calculés.

    Le contrôle AGL/AMSL est interne (silencieux) et n'entre volontairement PAS
    dans le score : il ne doit pas faire varier la note sans explication
    visible pour l'utilisateur.
    """
    # AUCUNE entité analysée (zone d'analyse vide, par exemple) : il n'y a
    # rien à noter. Tous les compteurs valent 0, donc toutes les pénalités
    # aussi, donc le score sortait à 100/100 « Excellent » — le pire faux
    # positif possible : un satisfecit délivré sans avoir rien regardé. On
    # rend explicitement une note NON ÉVALUÉE, avec une couverture nulle et
    # chaque contrôle déclaré marqué sans conclusion.
    if result.total_features <= 0:
        declares = dict(getattr(result, "checks_enabled", {}) or {})
        return CorrectnessScore(
            score=0.0,
            grade=C.GRADE_NOT_EVALUATED,
            color=C.GRADE_NOT_EVALUATED_COLOR,
            components={},
            penalties={},
            coverage_ran=0,
            coverage_total=len(declares),
            inconclusive={cle: True for cle, actif in declares.items() if actif},
        )

    total = max(result.total_features, 1)
    q = result.quality_report
    # Un contrôle NON CONCLUANT est traité comme un contrôle non exécuté : il
    # n'entre ni dans le score ni dans les sous-scores. Sans cela, un contrôle
    # incapable de mesurer quoi que ce soit rendait un ratio de 0, donc un
    # sous-score de 100 pour cent — indiscernable d'une couche irréprochable.
    doute = dict(getattr(result, "checks_inconclusive", {}) or {})

    def retenu(demande: bool, cle: str) -> bool:
        return bool(demande) and not doute.get(cle)

    include_small = retenu(include_small, "small")
    include_overlap = retenu(include_overlap, "overlap")
    include_duplicate = retenu(include_duplicate, "duplicate")
    include_duplicate_vertex = retenu(include_duplicate_vertex,
                                      C.CAT_DUPLICATE_VERTEX)
    include_holes = retenu(include_holes, C.CAT_HOLE)
    include_multipart = retenu(include_multipart, C.CAT_MULTIPART)
    include_sharp_angles = retenu(include_sharp_angles, C.CAT_SHARP_ANGLE)
    include_close_vertices = retenu(include_close_vertices, C.CAT_CLOSE_VERTEX)
    include_nested_height = retenu(include_nested_height, C.CAT_NESTED_HEIGHT)
    include_line_crossings = retenu(include_line_crossings, C.CAT_UNNODED_CROSSING)

    ratios = {
        "null_empty": q.null_empty_count / total,
        "invalid": q.invalid_count / total,
    }
    if include_small:
        ratios["small"] = q.small_area_count / total
    if include_overlap:
        ratios["overlap"] = q.overlap_count / total
    if include_duplicate:
        ratios["duplicate"] = q.duplicate_count / total
    if include_duplicate_vertex:
        ratios[C.CAT_DUPLICATE_VERTEX] = q.duplicate_vertex_count / total
    if include_holes:
        ratios[C.CAT_HOLE] = q.hole_count / total
    if include_multipart:
        ratios[C.CAT_MULTIPART] = q.multipart_count / total
    if include_sharp_angles:
        ratios[C.CAT_SHARP_ANGLE] = q.sharp_angle_count / total
    if include_close_vertices:
        ratios[C.CAT_CLOSE_VERTEX] = q.close_vertex_count / total
    if include_nested_height:
        ratios[C.CAT_NESTED_HEIGHT] = q.nested_height_count / total
    if include_line_crossings:
        ratios[C.CAT_UNNODED_CROSSING] = q.unnoded_crossing_count / total

    penalties = {k: round(min(1.0, r) * _WEIGHTS[k] * 100.0, 2) for k, r in ratios.items()}
    # Même règle que le score global (voir plus bas), appliquée ICI par
    # dimension : sur une grande couche, un ratio infime (2 trous sur 58 000
    # entités) arrondit à 100,0 % au premier décimal alors que l'anomalie
    # existe bel et bien. Sans ce plafond, le rapport affichait « Absence de
    # trous : 100 % » juste au-dessus d'un compteur de 2 trous détectés — la
    # sous-note contredisait le chiffre qu'elle est censée résumer.
    components = {}
    for k, r in ratios.items():
        comp = round(max(0.0, 1.0 - min(1.0, r)) * 100.0, 1)
        if r > 0 and comp >= 100.0:
            comp = 99.9
        components[k] = comp

    total_penalty = sum(penalties.values())
    score = round(max(0.0, min(100.0, 100.0 - total_penalty)), 1)

    # Règle : un score parfait (100/100) est réservé aux données sans AUCUNE
    # anomalie. Dès qu'une erreur subsiste — même en proportion infime, où
    # l'arrondi des pénalités ramènerait le total à 0 — le score est plafonné
    # sous 100 pour ne jamais afficher « 100/100 » avec des erreurs.
    issues_present = (
        q.null_empty_count or q.invalid_count
        or (include_small and q.small_area_count)
        or (include_overlap and q.overlap_count)
        or (include_duplicate and q.duplicate_count)
        or (include_duplicate_vertex and q.duplicate_vertex_count)
        or (include_holes and q.hole_count)
        or (include_multipart and q.multipart_count)
        or (include_sharp_angles and q.sharp_angle_count)
        or (include_close_vertices and q.close_vertex_count)
        or (include_nested_height and q.nested_height_count)
        or (include_line_crossings and q.unnoded_crossing_count)
    )
    if issues_present and score >= 100.0:
        score = 99.9

    # Un contrôle qui a tourné SANS conclure interdit également le 100 : la note
    # ne peut pas être parfaite quand une dimension demandée n'a pu être ni
    # confirmée ni infirmée. C'est le cas d'une surface non mesurable ou d'un
    # contrôle d'altitude sans une seule paire comparable — l'absence d'anomalie
    # y est une ignorance, pas un satisfecit.
    if doute and any(doute.values()) and score >= 100.0:
        score = 99.9

    # Couverture : sur les contrôles connus du plugin, combien ont abouti. Le
    # dénominateur est le nombre de contrôles DÉCLARÉS pour cette analyse
    # (checks_enabled), et non un total théorique, afin que la couverture reste
    # comparable d'une version à l'autre même si un contrôle est ajouté.
    declares = dict(getattr(result, "checks_enabled", {}) or {})
    coverage_total = len(declares)
    coverage_ran = sum(1 for cle, actif in declares.items()
                       if actif and not doute.get(cle))

    grade, color = "Critique", "#ef4444"
    for threshold, label, col in _GRADE_BANDS:
        if score >= threshold:
            grade, color = label, col
            break

    return CorrectnessScore(
        score=score, grade=grade, color=color, components=components,
        penalties=penalties, coverage_ran=coverage_ran,
        coverage_total=coverage_total,
        inconclusive={k: True for k, v in doute.items() if v},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# MOTEUR D'ANALYSE
# ═══════════════════════════════════════════════════════════════════════════════


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
    def _read_preview(layer: QgsVectorLayer, columns: List[str],
                      limit: int, aoi_rect=None, aoi_geom=None) -> List[List[str]]:
        """Échantillon d'attributs (N premières entités) en une passe limitée.

        Tolérant : un attribut illisible devient une chaîne vide et
        n'interrompt jamais la lecture.

        ``aoi_rect`` / ``aoi_geom`` : sous zone d'analyse, l'échantillon ne
        montre QUE des entités de la zone. Il lisait les N premières de la
        couche entière, si bien que l'onglet « Attributs » affichait des
        entités qu'aucun contrôle n'avait examinées — sur une couche triée
        géographiquement, il pouvait même n'en montrer AUCUNE de la zone.
        """
        rows: List[List[str]] = []
        if limit <= 0 or not columns:
            return rows
        try:
            request = QgsFeatureRequest().setLimit(limit)
        except Exception:
            request = QgsFeatureRequest()
        if aoi_rect is not None:
            # Le plafond doit compter les entités RETENUES, pas les entités
            # lues : setLimit s'applique avant le test exact, il faut donc lire
            # plus large et s'arrêter sur le compte des lignes gardées.
            try:
                request = QgsFeatureRequest().setFilterRect(aoi_rect)
            except Exception:
                request = QgsFeatureRequest()
        for feat in layer.getFeatures(request):
            if len(rows) >= limit:
                break
            if aoi_geom is not None:
                try:
                    geom = feat.geometry() if feat.hasGeometry() else None
                except Exception:
                    geom = None
                if not feature_in_aoi(geom, aoi_geom):
                    continue
            row = []
            for name in columns:
                try:
                    row.append(_attr_str(feat[name]))
                except Exception:
                    row.append("")
            rows.append(row)
        return rows

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        """Valeur numérique FINIE d'un attribut, ``None`` sinon.

        ``float()`` accepte ``nan``, ``inf`` et ``-inf`` : sans le filtre
        ``isfinite``, un NaN d'altitude passait pour un nombre et rendait la
        comparaison ``inclus <= contenant`` fausse — la paire était donc
        comptée COMPARÉE et CONFORME (voir ``compare_nested_heights``). Une
        valeur non finie ne dit rien : elle doit rendre la paire non
        vérifiable, jamais conforme.
        """
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            try:
                number = float(str(value).replace(",", ".").strip())
            except (TypeError, ValueError):
                return None
        return number if math.isfinite(number) else None

    def _build_area_calculator(self, layer: QgsVectorLayer,
                               result: AnalysisResult, ctx=None):
        """Configure un QgsDistanceArea pour des surfaces en m² (ellipsoïdal).

        ``ctx`` : ``MeasurementContext`` capturé sur le thread principal, pour
        ne pas interroger le projet depuis un thread secondaire.
        """
        mctx = _resolve_measurement_context(ctx)
        da = QgsDistanceArea()
        try:
            da.setSourceCrs(layer.crs(), mctx.transform_context)
            da.setEllipsoid(mctx.usable_ellipsoid())
        except Exception as exc:  # pragma: no cover - dépend de la version QGIS
            result.warnings.append("Calcul de surface dégradé : %s" % exc)
        return da

    def _measure_area_m2(self, da: QgsDistanceArea, geom: QgsGeometry) -> Optional[float]:
        try:
            raw = da.measureArea(geom)
            return da.convertAreaMeasurement(raw, QgsUnitTypes.AreaUnit.AreaSquareMeters)
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
    def _detect_overlaps(index, polygon_fids, check_cancel, progress_cb,
                         allow_contained=False, missing_vertex_ratio=0.0,
                         equal_is_overlap=False, scope=None):
        """Détecte les paires de polygones dont les *intérieurs* se recouvrent.

        Règle : ``intersects`` et non ``touches``, sans aucune tolérance (voir
        ``classify_overlap_detailed``). Les contacts par simple frontière et les
        doublons exacts sont donc exclus, le moindre recouvrement est signalé.

        ``scope`` : sous zone d'analyse (AOI), l'ensemble des fid RÉELLEMENT
        analysés. ``polygon_fids`` et l'index contiennent alors en plus les
        VOISINS immédiats, hors zone — indispensables pour voir un
        chevauchement au bord de la zone, mais qui ne doivent rien apporter au
        bilan par eux-mêmes. Une paire n'est donc comptée que si au moins un de
        ses membres est dans la zone, et seuls les membres DANS la zone sont
        signalés : l'entité du voisinage est ce qui explique l'erreur, pas une
        erreur à corriger dans cette tuile. ``None`` = aucune restriction
        (toute la couche est dans la zone).

        ``allow_contained`` : si vrai, les polygones ENTIÈREMENT inclus dans un
        autre (imbrication) sont considérés comme légitimes et ne sont donc pas
        signalés comme chevauchement. Ils restent comptés à titre informatif.

        ``missing_vertex_ratio`` > 0 active l'attribution à un sommet manquant :
        ces paires sont comptées à part et ne sont pas signalées comme erreur.
        À 0, aucun classement supplémentaire n'est calculé — coût nul quand
        l'option n'est pas demandée.

        Renvoie ``(fids concernés, nb de paires, nb de paires imbriquées,
        nb de paires par sommet manquant, points)`` où ``points`` associe à
        chaque fid la liste des ``(x, y, fid_voisin)`` localisant chaque
        recouvrement — ce qui permet à la couche d'erreurs de pointer
        l'intersection elle-même.
        """
        overlap_fids = set()
        pairs = 0
        contained_pairs = 0
        missing_vertex_pairs = 0
        points = {}
        stored_points = 0

        def remember(a, b, xy):
            """Mémorise le lieu du recouvrement pour les DEUX entités de la
            paire, sous double plafond (par entité et au total) : sur une couche
            très fautive, la liste ne doit pas croître sans limite en mémoire."""
            nonlocal stored_points
            if xy is None:
                return
            for owner, other in ((a, b), (b, a)):
                if stored_points >= C.OVERLAP_POINTS_TOTAL_CAP:
                    return
                bucket = points.setdefault(owner, [])
                if len(bucket) >= C.OVERLAP_POINTS_PER_FEATURE:
                    continue
                bucket.append((xy[0], xy[1], other))
                stored_points += 1

        n = len(polygon_fids)
        for i, fid in enumerate(polygon_fids):
            if i % 200 == 0:
                check_cancel()
                if n:
                    progress_cb(i / n)
            g = index.geometry(fid)
            if g is None or g.isNull():
                continue
            candidates = _safe_index_intersects(index, g.boundingBox())
            if candidates is None:
                continue
            for cand in candidates:
                if cand <= fid:
                    continue  # chaque paire traitée une seule fois (évite aussi self)
                og = index.geometry(cand)
                if og is None or og.isNull():
                    continue
                # Doublon exact déjà comptabilisé séparément : le classement
                # l'exclut, comme les simples contacts de frontière.
                kind, xy = classify_overlap_detailed(
                    g, og, missing_vertex_ratio, equal_is_overlap)
                if kind is OVERLAP_NONE:
                    continue
                # Paire entièrement hors zone : elle n'existe ici que parce
                # qu'un voisin a été indexé. Écartée AVANT tout compteur, y
                # compris ceux des paires « informatives » (sommet manquant,
                # imbrication) : ils portent sur la zone analysée, pas sur son
                # pourtour.
                if scope is not None and fid not in scope and cand not in scope:
                    continue
                if kind == OVERLAP_MISSING_VERTEX:
                    missing_vertex_pairs += 1   # sommet manquant : non signalé
                    continue
                if kind == OVERLAP_CONTAINED:
                    contained_pairs += 1
                    if allow_contained:
                        continue      # imbrication acceptée par l'utilisateur
                pairs += 1
                for member in (fid, cand):
                    if scope is None or member in scope:
                        overlap_fids.add(member)
                remember(fid, cand, xy)
        return (overlap_fids, pairs, contained_pairs,
                missing_vertex_pairs, points)

    @staticmethod
    def _detect_unnoded_crossings(index, line_fids, check_cancel, progress_cb,
                                  scope=None):
        """Détecte les croisements/contacts entre LIGNES DISTINCTES sans sommet
        commun (voir ``unnoded_crossing_points``).

        Réservé au réseau ENTRE entités : un croisement d'une ligne sur
        elle-même (auto-intersection) n'est pas vérifié par ce contrôle — voir
        la docstring de ``constants.CAT_UNNODED_CROSSING``.

        ``scope`` : même rôle que dans ``_detect_overlaps`` — sous zone
        d'analyse, restreint le bilan aux paires dont au moins un membre est
        dans la zone, et ne signale que les membres qui y sont.

        Même forme de retour que ``_detect_overlaps`` : ``(fids concernés, nb
        de paires, points)`` où ``points`` associe à chaque fid la liste des
        ``(x, y, fid_voisin)`` localisant chaque croisement.
        """
        flagged = set()
        pairs = 0
        points = {}
        stored_points = 0
        seen_pairs = set()

        def remember(a, b, xy):
            nonlocal stored_points
            for owner, other in ((a, b), (b, a)):
                if stored_points >= C.OVERLAP_POINTS_TOTAL_CAP:
                    return
                bucket = points.setdefault(owner, [])
                if len(bucket) >= C.OVERLAP_POINTS_PER_FEATURE:
                    continue
                bucket.append((xy[0], xy[1], other))
                stored_points += 1

        n = len(line_fids)
        for i, fid in enumerate(line_fids):
            if i % 200 == 0:
                check_cancel()
                if n:
                    progress_cb(i / n)
            g = index.geometry(fid)
            if g is None or g.isNull():
                continue
            candidates = _safe_index_intersects(index, g.boundingBox())
            if candidates is None:
                continue
            for cand in candidates:
                if cand == fid:
                    continue  # auto-intersection : hors périmètre de ce contrôle
                # Paire entièrement hors zone : le voisin n'est indexé que
                # pour expliquer un croisement AVEC la zone, pas pour en
                # rapporter entre voisins.
                if scope is not None and fid not in scope and cand not in scope:
                    continue
                pair_key = (fid, cand) if fid < cand else (cand, fid)
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                og = index.geometry(cand)
                if og is None or og.isNull():
                    continue
                if not _safe_intersects(g, og):
                    continue
                crossing_pts = unnoded_crossing_points(g, og)
                if not crossing_pts:
                    continue
                pairs += 1
                for member in (fid, cand):
                    if scope is None or member in scope:
                        flagged.add(member)
                for xy in crossing_pts:
                    remember(fid, cand, xy)
        return (flagged, pairs, points)

    @staticmethod
    def _detect_nested_height(index, polygon_fids, values, labels,
                              check_cancel, progress_cb, scope=None):
        """Vérifie l'altitude des polygones ENTIÈREMENT INCLUS dans un autre.

        Le polygone inclus doit être strictement plus haut que celui qui le
        contient, champ par champ (voir ``compare_nested_heights``). Une
        superstructure posée sur un toit, un corps de bâtiment sur son emprise :
        si l'inclus est au même niveau ou plus bas, les altitudes ont été
        interverties ou l'une des deux est fausse.

        Ce balayage est INDÉPENDANT de celui des chevauchements : il tourne même
        si ce contrôle est décoché, et n'est pas affecté par l'option « ignorer
        les polygones entièrement inclus » — celle-ci retire ces paires de la
        liste des chevauchements, elle ne dit rien de leurs altitudes.

        Chaque paire n'est visitée qu'une fois (``cand > fid``), les deux sens
        d'inclusion étant testés à ce moment-là.

        ``values`` associe à chaque fid le tuple des altitudes lues, aligné sur
        ``labels``. Renvoie ``(fids des polygones inclus fautifs, paires
        fautives, paires comparées, paires non concluantes, fautes par champ)``.

        ``scope`` : sous zone d'analyse, seul le polygone INCLUS décide. C'est
        lui que ce contrôle met en cause et lui seul qui serait à corriger :
        une superstructure hors zone posée sur un bâtiment de la zone relève
        de la tuile qui la porte, pas de celle-ci. Compter la paire ici la
        ferait apparaître deux fois au découpage.
        """
        flagged = set()
        bad_pairs = 0
        checked = 0
        unknown = 0
        by_field = {label: 0 for label in labels}

        n = len(polygon_fids)
        for i, fid in enumerate(polygon_fids):
            if i % 200 == 0:
                check_cancel()
                if n:
                    progress_cb(i / n)
            g = index.geometry(fid)
            if g is None or g.isNull():
                continue
            candidates = _safe_index_intersects(index, g.boundingBox())
            if candidates is None:
                continue
            for cand in candidates:
                if cand <= fid:
                    continue
                og = index.geometry(cand)
                if og is None or og.isNull():
                    continue
                inner_fid, outer_fid = containment_pair(g, og, fid, cand)
                if inner_fid is None:
                    continue
                # C'est le polygone INCLUS qui est mis en cause : hors zone,
                # la paire ne concerne pas cette analyse.
                if scope is not None and inner_fid not in scope:
                    continue
                faults, conclusive = compare_nested_heights(
                    values.get(inner_fid), values.get(outer_fid), labels)
                if not conclusive:
                    unknown += 1
                    continue
                checked += 1
                if faults:
                    bad_pairs += 1
                    # Seul le polygone INCLUS est signalé : c'est sa valeur qui
                    # doit dépasser celle du contenant. Marquer aussi le
                    # contenant noierait la donnée à corriger — un grand
                    # bâtiment portant dix superstructures fautives apparaîtrait
                    # dix fois, et sa propre altitude est peut-être la bonne.
                    flagged.add(inner_fid)
                    for label in faults:
                        by_field[label] += 1
        return (flagged, bad_pairs, checked, unknown, by_field)

    # -- Analyse principale -----------------------------------------------------

    def analyze(
        self,
        layer: QgsVectorLayer,
        progress_cb: Optional[Callable[[float, str], None]] = None,
        is_canceled: Optional[Callable[[], bool]] = None,
        measurement_ctx=None,
    ) -> AnalysisResult:
        """Analyse complète d'une couche. Peut lever AnalysisCancelled.

        ``measurement_ctx`` : ``MeasurementContext`` capturé sur le thread
        PRINCIPAL. Obligatoire en pratique dès que l'analyse tourne dans un
        ``QgsTask`` : sans lui, les mesures ellipsoïdales interrogeraient le
        singleton ``QgsProject`` depuis un thread secondaire.
        """
        import time

        started = time.time()
        opt = self.options
        mctx = _resolve_measurement_context(measurement_ctx)

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
        # Mémorisé pour l'affichage : un compteur resté à 0 parce que le
        # contrôle est décoché ne doit pas se lire comme « rien trouvé ».
        result.checks_enabled = {
            "self_intersection": opt.topology_checks,
            "multipart": opt.topology_checks,
            "overlap": opt.check_overlaps,
            "duplicate": opt.check_duplicates,
            "small": opt.check_small_polygons,
            "duplicate_vertex": opt.check_duplicate_vertex,
            "hole": opt.check_holes,
            "sharp_angle": opt.check_sharp_angles,
            "close_vertex": opt.check_close_vertices,
            # Rectifié plus bas si la couche ne porte aucun champ d'altitude :
            # le contrôle ne peut alors pas tourner, et son compteur à 0 ne doit
            # pas se lire comme « aucune anomalie ».
            "nested_height": opt.check_nested_height,
            # Rectifié plus bas si la couche ne contient aucune entité de
            # type ligne : le contrôle ne peut alors pas tourner.
            "unnoded_crossing": opt.check_line_crossings,
        }
        # Un contrôle peut TOURNER sans conclure : rempli au fil de l'analyse.
        result.checks_inconclusive = {}
        # Profil et réglages consignés tels quels : un rapport sans eux est
        # illisible plus tard — « 0 petit polygone » ne veut rien dire si l'on ne
        # sait plus si le seuil valait 2 ou 25 m².
        result.profile_name = opt.profile_name or ""
        result.options_snapshot = {
            "small_polygon_threshold_m2": opt.small_polygon_threshold_m2,
            "hole_max_area_m2": opt.hole_max_area_m2,
            "sharp_angle_min_deg": opt.sharp_angle_min_deg,
            "close_vertex_min_dist_m": opt.close_vertex_min_dist_m,
            "ignore_missing_vertex_overlaps": bool(
                opt.ignore_missing_vertex_overlaps),
            "allow_contained_polygons": bool(opt.allow_contained_polygons),
            # Zone d'analyse : consignée même quand elle est absente. « 0
            # chevauchement » ne veut pas dire la même chose sur une couche
            # entière et sur un quartier de 800 bâtiments, et six mois plus
            # tard rien ne permettrait de le savoir.
            "aoi_active": opt.aoi_rect is not None,
        }

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

        # Contrôle AGL/AMSL : INTERNE et silencieux (aucune option d'interface).
        # Il n'est tenté que si les deux champs existent, et ne produit une note
        # que s'ils sont ENTIÈREMENT renseignés (cf. agl_complete plus bas).
        agl_field = self._find_field(layer, "AGL")
        amsl_field = self._find_field(layer, "AMSL")
        buildings_enabled = bool(agl_field and amsl_field)

        # Hauteur des polygones inclus : contrôle ATTRIBUTAIRE, donc tributaire
        # de la couche. On ne retient que les champs d'altitude réellement
        # présents (recherche insensible à la casse). Aucun des deux : le
        # contrôle ne peut pas tourner — on le dit, et on le marque non exécuté
        # plutôt que de laisser un 0 passer pour un sans-faute.
        nested_fields = [(label, self._find_field(layer, label))
                         for label in C.NESTED_HEIGHT_FIELDS]
        nested_fields = [(label, name) for label, name in nested_fields if name]
        nested_labels = [label for label, _ in nested_fields]
        nested_enabled = bool(opt.check_nested_height and nested_fields)
        if opt.check_nested_height and not nested_fields:
            result.checks_enabled["nested_height"] = False
            result.warnings.append(
                "Hauteur des polygones inclus non contrôlée : la couche ne "
                "contient ni champ « %s »." % " » ni champ « ".join(
                    C.NESTED_HEIGHT_FIELDS))
        if nested_enabled:
            result.nested_height_fields = [name for _, name in nested_fields]

        total = layer.featureCount()
        if total < 0:  # certains fournisseurs renvoient -1
            total = None
        result.total_features = total or 0
        result.aoi_layer_features = total or 0

        # ── Zone d'analyse (AOI) ──
        # Résolue ICI, avant toute lecture : elle change le lot d'entités, donc
        # tous les compteurs. Convertie dans le CRS de la couche (voir
        # resolve_aoi_rect) et conservée en WKT dans le résultat, parce que le
        # rapport, la réparation et la revérification post-réparation doivent
        # tous porter sur la MÊME zone que l'analyse.
        aoi_rect = None
        aoi_geom = None
        if opt.aoi_rect is not None:
            aoi_rect = resolve_aoi_rect(layer, opt.aoi_rect,
                                        opt.aoi_crs_authid, mctx)
            if aoi_rect is None:
                # Renoncer, mais le DIRE : analyser toute la couche en croyant
                # n'en analyser qu'un quartier serait le pire des deux mondes.
                result.warnings.append(
                    "Zone d'analyse ignorée : le rectangle n'a pas pu être "
                    "converti dans le système de coordonnées de la couche — "
                    "TOUTE la couche a été analysée.")
            else:
                aoi_geom = QgsGeometry.fromRect(aoi_rect)
                result.aoi_wkt = aoi_geom.asWkt()

        da = self._build_area_calculator(layer, result, mctx)
        can_measure_area = True
        # Le repli planaire des surfaces de trous n'est licite que sur une
        # couche en mètres (voir layer_planar_area_is_m2).
        planar_is_m2 = layer_planar_area_is_m2(layer)

        # Collecteurs
        q = result.quality_report
        geom_types: Dict[str, int] = {}
        wkb_index: Dict[bytes, List[int]] = {}
        flagged = {
            "null_empty": [], "invalid": [], "self_intersection": [],
            "small": [], "duplicate": [], "overlap": [],
            C.CAT_DUPLICATE_VERTEX: [], C.CAT_HOLE: [], C.CAT_MULTIPART: [],
            C.CAT_SHARP_ANGLE: [], C.CAT_CLOSE_VERTEX: [],
            C.CAT_NESTED_HEIGHT: [], C.CAT_UNNODED_CROSSING: [],
            C.CAT_AGL: [],
        }
        invalid_details: List[str] = []
        self_intersections = 0
        # Entités dont GEOS n'a pas pu dire si elles étaient valides : ni
        # valides, ni invalides. Comptées à part pour pouvoir l'ANNONCER.
        validity_failures = 0
        # Entités de type POLYGONE réellement rencontrées, indépendamment de
        # l'index spatial : sans polygone, les contrôles purement polygonaux
        # n'ont rien pu examiner et ne doivent pas se lire « 0 anomalie ».
        polygon_seen = 0
        # Idem pour les lignes. Compté à part de ``line_fids``, qui contient
        # aussi les VOISINES hors zone : sans ce compteur, une zone sans
        # aucune ligne mais entourée de lignes verrait le contrôle des
        # croisements « tourner » et rendre 0, lu comme un sans-faute.
        line_seen = 0
        # Sous zone d'analyse : entités lues mais HORS zone, mises de côté pour
        # être indexées comme simples voisines (voir la passe de voisinage
        # après la boucle). Vide sans AOI.
        neighbour_feats: List[QgsFeature] = []
        # Les fid réellement analysés, seule assise des compteurs. None sans
        # AOI utilisable : les détecteurs de paires n'ont alors rien à
        # restreindre (une AOI abandonnée faute de conversion revient bien à
        # « pas d'AOI », y compris ici).
        scope_fids = set() if aoi_geom is not None else None
        # Rectangle englobant des entités RÉELLEMENT analysées. Sert deux fois :
        # à cadrer la passe de voisinage (une entité de la zone peut dépasser
        # loin hors du rectangle de l'AOI, et son voisin encore plus loin), et
        # à renseigner result.bounds — annoncer l'étendue de toute la couche
        # après une analyse restreinte serait trompeur.
        scope_bbox = None
        agl_over = 0
        agl_complete = True      # faux dès qu'un AGL/AMSL est vide ou non numérique
        duplicate_vertex_total = 0
        hole_total = 0
        sharp_angle_total = 0
        close_vertex_total = 0
        result.hole_threshold_m2 = opt.hole_max_area_m2
        # Seuil de forme effectif : la valeur fixe quand l'option est cochée,
        # 0 sinon. Mémorisé pour que le rapport ET la réparation reprennent
        # exactement la même règle que l'analyse.
        result.missing_vertex_ratio = (C.MISSING_VERTEX_RATIO_DEFAULT
                                       if opt.ignore_missing_vertex_overlaps
                                       else 0.0)
        result.allow_contained_polygons = opt.allow_contained_polygons
        result.sharp_angle_min_deg = opt.sharp_angle_min_deg
        result.close_vertex_min_dist_m = opt.close_vertex_min_dist_m
        # Mesure ellipsoïdale réutilisée pour la surface des trous.
        measure_hole_area = (lambda g: self._measure_area_m2(da, g)) if opt.check_holes else None

        # Les seuils de DISTANCE sont saisis en mètres, mais les comparaisons
        # géométriques (distance entre sommets, epsilon de fusion) se font dans
        # les unités de la couche. Facteurs calculés UNE fois ici.
        #
        # Deux conversions, car l'erreur acceptable n'est pas la même :
        #   * PRUDENTE pour ce qui modifie la donnée — l'epsilon de fusion des
        #     quasi-doublons, qui ne doit jamais aller plus loin que demandé ;
        #   * LARGE pour la pure DÉTECTION des sommets rapprochés, afin de ne
        #     jamais taire une anomalie à cause de l'orientation du segment.
        #
        # Les chevauchements n'y figurent plus : leur règle est purement
        # topologique (intersects/touches), donc insensible aux unités.
        # Sommets dupliqués : plus de tolérance réglable, donc plus de
        # conversion à faire — 0 dans les unités de la couche vaut 0 partout.
        vertex_tol_units = 0.0
        close_dist_units = (opt.close_vertex_min_dist_m
                           * layer_units_per_metre_wide(layer, mctx))

        # Index spatial des polygones. Il sert à DEUX contrôles indépendants —
        # les chevauchements et la hauteur des polygones inclus — d'où un index
        # construit dès que l'un des deux est demandé, et non plus au seul titre
        # des chevauchements.
        overlap_index = None
        polygon_fids: List[int] = []
        # Altitudes des polygones indexés : {fid: (valeur par champ…)}, aligné
        # sur ``nested_labels``. Collecté dans la boucle principale, où les
        # attributs sont déjà lus — une seconde passe sur la couche coûterait
        # une lecture complète de plus.
        nested_values: Dict[int, tuple] = {}
        if opt.check_overlaps or nested_enabled:
            try:
                overlap_index = QgsSpatialIndex(QgsSpatialIndex.Flag.FlagStoreFeatureGeometries)
            except Exception as exc:
                # Sans index, les contrôles concernés ne tourneront pas du tout.
                # Le dire, et surtout les marquer NON exécutés : sinon le rapport
                # afficherait « 0 chevauchement » pour une couche jamais
                # examinée.
                overlap_index = None
                if opt.check_overlaps:
                    result.checks_enabled["overlap"] = False
                    result.errors.append(
                        "Chevauchements non contrôlés : index spatial "
                        "indisponible (%s)." % exc)
                if nested_enabled:
                    nested_enabled = False
                    result.checks_enabled["nested_height"] = False
                    result.errors.append(
                        "Hauteur des polygones inclus non contrôlée : index "
                        "spatial indisponible (%s)." % exc)

        # Index spatial des LIGNES : contrôle séparé de celui des polygones,
        # réservé aux entités de type ligne (voir CAT_UNNODED_CROSSING).
        line_index = None
        line_fids: List[int] = []
        if opt.check_line_crossings:
            try:
                line_index = QgsSpatialIndex(QgsSpatialIndex.Flag.FlagStoreFeatureGeometries)
            except Exception as exc:
                line_index = None
                result.checks_enabled["unnoded_crossing"] = False
                result.errors.append(
                    "Croisements de lignes non contrôlés : index spatial "
                    "indisponible (%s)." % exc)

        # Aperçu attributaire : passe séparée et limitée (attributs complets),
        # pour ne PAS charger tous les attributs de toutes les entités dans la
        # boucle principale. Celle-ci ne lit alors que la géométrie (et AGL/AMSL
        # si le contrôle bâtiments est actif) — nettement plus rapide sur les
        # tables à nombreux champs.
        preview_rows = self._read_preview(
            layer, result.preview_columns, opt.preview_limit_rows,
            aoi_rect, aoi_geom)

        # Requête principale optimisée : géométrie + attributs strictement utiles.
        # AGL peut être réclamé par les deux contrôles à la fois (interne
        # AGL/AMSL et hauteur des inclus) : la liste est dédoublonnée sans
        # perdre l'ordre, un même champ demandé deux fois étant refusé par
        # certains fournisseurs.
        wanted_attrs: List[str] = []
        if buildings_enabled:
            wanted_attrs.extend([agl_field, amsl_field])
        if nested_enabled:
            wanted_attrs.extend(name for _, name in nested_fields)
        seen_attrs = set()
        wanted_attrs = [n for n in wanted_attrs
                        if not (n in seen_attrs or seen_attrs.add(n))]
        main_request = QgsFeatureRequest()
        try:
            if wanted_attrs:
                main_request.setSubsetOfAttributes(wanted_attrs, layer.fields())
            else:
                main_request.setNoAttributes()
        except Exception:
            main_request = QgsFeatureRequest()

        # ── Zone d'analyse (AOI), facultative ──
        # ``setFilterRect`` fait le gros du tri côté fournisseur (index
        # spatial) : sur une couche de 264 000 bâtiments restreinte à un
        # quartier, seules quelques centaines d'entités sont même lues. Le
        # test précis vient ensuite, entité par entité : le rectangle d'un
        # objet peut couper l'AOI alors que l'objet lui-même ne la touche pas.
        if aoi_rect is not None:
            with contextlib.suppress(Exception):
                main_request.setFilterRect(aoi_rect)
            # Le compte de la couche cesse d'être le dénominateur : sous AOI,
            # il surestimerait massivement. On le met à None, ce qui bascule
            # la progression en mode « n entités analysées » et fait recaler
            # result.total_features sur le compte RÉEL en fin de boucle — sans
            # quoi tous les ratios du score seraient divisés par le total de
            # la couche entière, donc écrasés vers zéro.
            total = None

        def index_feature(feat, gtype):
            """Range une entité dans l'index spatial qui la concerne.

            Appelée pour les entités ANALYSÉES (boucle principale) comme pour
            les VOISINES de la zone (passe de voisinage) : les deux doivent
            entrer dans le même index, sinon un chevauchement au bord de la
            zone n'aurait qu'un seul de ses deux membres et resterait invisible.
            Une entité non indexable est simplement écartée de ces contrôles.
            """
            if (overlap_index is not None
                    and gtype == QgsWkbTypes.GeometryType.PolygonGeometry):
                with contextlib.suppress(Exception):
                    overlap_index.addFeature(feat)
                    polygon_fids.append(feat.id())
                    # Altitudes retenues ici et non dans une passe séparée : le
                    # balayage d'inclusion travaille sur l'index, qui ne porte
                    # que les géométries. Une valeur illisible devient None et
                    # rendra simplement la comparaison non concluante.
                    if nested_enabled:
                        nested_values[feat.id()] = tuple(
                            self._to_float(feat[name])
                            for _, name in nested_fields)
            if (line_index is not None
                    and gtype == QgsWkbTypes.GeometryType.LineGeometry):
                with contextlib.suppress(Exception):
                    line_index.addFeature(feat)
                    line_fids.append(feat.id())

        report(5, "Lecture des entités…")

        processed = 0
        denom = total if total else 1
        for feat in layer.getFeatures(main_request):
            if processed % 500 == 0:
                check_cancel()
                if total:
                    report(5 + 80.0 * processed / denom, "Analyse %d/%d…" % (processed, total))
                else:
                    report(50, "Analyse %d entités…" % processed)

            fid = feat.id()

            try:
                geom = feat.geometry() if feat.hasGeometry() else None
            except Exception:
                geom = None

            # --- Appartenance à la zone d'analyse (AOI) ---
            # ``setFilterRect`` n'a trié que sur les RECTANGLES ENGLOBANTS : le
            # rectangle d'une entité peut couper l'AOI sans que l'entité la
            # touche (une diagonale, un bâtiment en L). Test exact ici.
            #
            # Une entité hors zone n'est pas jetée : elle est INDEXÉE quand
            # même, plus bas, comme simple VOISINE. Sans cela, un
            # chevauchement entre une entité de la zone et sa voisine juste
            # dehors ne serait vu par personne — précisément au bord, là où
            # l'on découpe le travail en tuiles.
            if aoi_geom is not None:
                in_aoi = feature_in_aoi(geom, aoi_geom)
                if not in_aoi:
                    neighbour_feats.append(QgsFeature(feat))
                    continue
                scope_fids.add(fid)
                if geom is not None and not geom.isNull():
                    with contextlib.suppress(Exception):
                        bb = geom.boundingBox()
                        if scope_bbox is None:
                            scope_bbox = QgsRectangle(bb)
                        else:
                            scope_bbox.combineExtentWith(bb)

            # --- Contrôle interne AGL > AMSL (silencieux, note en fin de rapport) ---
            # Évalué AVANT le filtre des géométries nulles : le critère « champs
            # entièrement renseignés » doit porter sur TOUTES les entités, sinon
            # une entité sans géométrie et sans altitudes passerait inaperçue et
            # la note serait émise à tort.
            if buildings_enabled:
                try:
                    agl = self._to_float(feat[agl_field])
                    amsl = self._to_float(feat[amsl_field])
                except Exception:
                    agl = amsl = None
                if agl is None or amsl is None:
                    # Champ vide ou non numérique -> contrôle non concluant :
                    # aucune note ne sera émise.
                    agl_complete = False
                elif agl > amsl:
                    agl_over += 1
                    flagged[C.CAT_AGL].append(fid)

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
            # Un échec de GEOS ne veut PAS dire « valide » : compter l'entité
            # dans valid_count la déclarait conforme sur la foi d'un contrôle
            # qui n'a pas abouti. Elle est ici laissée hors des deux compteurs
            # et le contrôle est marqué non concluant — le score cesse alors de
            # se prononcer sur cette dimension.
            is_valid = True
            try:
                is_valid = geom.isGeosValid()
            except Exception:
                validity_failures += 1
                result.checks_inconclusive["invalid"] = True
                is_valid = None      # ni valide, ni invalide : indéterminé
            if not is_valid and is_valid is not None:
                q.invalid_count += 1
                flagged["invalid"].append(fid)
                if opt.topology_checks:
                    msg, is_self = self._first_validation_error(geom)
                    if is_self:
                        self_intersections += 1
                        flagged["self_intersection"].append(fid)
                    if msg and len(invalid_details) < opt.invalid_details_limit:
                        invalid_details.append("FID %s : %s" % (fid, msg))
            elif is_valid:
                q.valid_count += 1

            # --- Petits polygones ---
            # La surface d'une géométrie INVALIDE ne veut rien dire : sur une
            # auto-intersection en « papillon », les deux lobes ont des
            # orientations opposées et leurs aires signées s'annulent — un
            # polygone de 50 m² est mesuré à 0 m², donc compté « petit », donc
            # SUPPRIMÉ si l'option de suppression est cochée. On mesure donc sur
            # une copie rendue valide ; si la validation échoue, le critère de
            # surface est abandonné pour cette entité (elle est de toute façon
            # déjà signalée comme invalide).
            gtype = QgsWkbTypes.geometryType(geom.wkbType())
            if gtype == QgsWkbTypes.GeometryType.PolygonGeometry:
                polygon_seen += 1
            elif gtype == QgsWkbTypes.GeometryType.LineGeometry:
                line_seen += 1
            if (opt.check_small_polygons and can_measure_area
                    and gtype == QgsWkbTypes.GeometryType.PolygonGeometry):
                measurable = geom if is_valid else _validated_for_area(geom)
                area = (self._measure_area_m2(da, measurable)
                        if measurable is not None else None)
                if area is None and is_valid:
                    can_measure_area = False
                    # Le contrôle s'arrête ici pour toutes les entités
                    # suivantes : son compteur ne mesure plus rien et ne doit
                    # donc pas valoir « conforme ».
                    result.checks_inconclusive["small"] = True
                    result.warnings.append("Surfaces non calculables : petits polygones ignorés.")
                elif area is not None and area <= opt.small_polygon_threshold_m2:
                    q.small_area_count += 1
                    flagged["small"].append(fid)

            # --- Doublons (WKB normalisé, haché) ---
            # Empreinte NON cryptographique servant uniquement à regrouper les
            # géométries identiques. BLAKE2b (rapide, digest 16 o) évite l'alerte
            # « MD5 faible » des scanners. Une géométrie non hachable est ignorée.
            if opt.check_duplicates:
                with contextlib.suppress(Exception):
                    g = QgsGeometry(geom)
                    with contextlib.suppress(Exception):
                        g.normalize()
                    key = hashlib.blake2b(bytes(g.asWkb()), digest_size=16).digest()
                    wkb_index.setdefault(key, []).append(fid)

            # --- Sommets dupliqués / quasi-dupliqués (selon tolérance) ---
            if opt.check_duplicate_vertex:
                dup_vertices = count_duplicate_vertices(
                    geom, vertex_tol_units)
                if dup_vertices:
                    q.duplicate_vertex_count += 1
                    duplicate_vertex_total += dup_vertices
                    flagged[C.CAT_DUPLICATE_VERTEX].append(fid)

            # --- Trous sous seuil (les grands trous sont jugés justifiés) ---
            if opt.check_holes and gtype == QgsWkbTypes.GeometryType.PolygonGeometry:
                small_holes, _all_holes = count_small_holes(
                    geom, opt.hole_max_area_m2, measure_hole_area,
                    planar_is_m2)
                if small_holes:
                    q.hole_count += 1
                    hole_total += small_holes
                    flagged[C.CAT_HOLE].append(fid)

            # --- Entités multi-parties (rattaché aux contrôles topologiques) ---
            if opt.topology_checks and count_parts(geom) > 1:
                q.multipart_count += 1
                flagged[C.CAT_MULTIPART].append(fid)

            # --- Contrôles « Autres » (polygones uniquement) ---
            if gtype == QgsWkbTypes.GeometryType.PolygonGeometry:
                # Angles aigus : pointes de digitalisation.
                if opt.check_sharp_angles:
                    spikes = count_sharp_angles(geom, opt.sharp_angle_min_deg)
                    if spikes:
                        q.sharp_angle_count += 1
                        sharp_angle_total += spikes
                        flagged[C.CAT_SHARP_ANGLE].append(fid)

                # Sommets trop rapprochés (hors sommets strictement superposés).
                if opt.check_close_vertices:
                    close_pairs = count_close_vertex_pairs(
                        geom, close_dist_units,
                        vertex_tol_units if opt.check_duplicate_vertex else 0.0)
                    if close_pairs:
                        q.close_vertex_count += 1
                        close_vertex_total += close_pairs
                        flagged[C.CAT_CLOSE_VERTEX].append(fid)

            # --- Indexation des polygones et des lignes ---
            # Une entité non indexable est simplement écartée de ce contrôle.
            index_feature(feat, gtype)

            processed += 1

        check_cancel()
        report(88, "Consolidation…")

        # Si featureCount() n'était pas fiable, on recale sur le compte réel
        if not total:
            result.total_features = processed

        # ── Voisinage de la zone d'analyse ──
        # Les contrôles de PAIRES (chevauchement, imbrication, croisement de
        # lignes) ne peuvent pas se contenter des entités de la zone : un
        # bâtiment de la zone qui chevauche son voisin juste dehors est une
        # erreur RÉELLE, et personne ne la verrait — précisément au bord, là
        # où l'on découpe le travail en tuiles.
        #
        # Les voisins sont donc INDEXÉS sans être analysés : ils expliquent
        # une erreur de la zone, ils n'en apportent aucune (voir le paramètre
        # ``scope`` des trois détecteurs).
        # L'INDEXATION des voisins n'a de sens que s'il existe un index à
        # remplir — c'est-à-dire si au moins un contrôle de paires tourne.
        if scope_fids is not None and (overlap_index is not None
                                       or line_index is not None):
            report(89, "Voisinage de la zone…")
            seen_fids = set(scope_fids)
            for feat in neighbour_feats:
                check_cancel()
                seen_fids.add(feat.id())
                gtype_n = _geometry_type_of(feat)
                if gtype_n is not None:
                    index_feature(feat, gtype_n)
            neighbour_feats = []      # rendus à la mémoire dès indexés

            # Une entité de la zone peut dépasser LOIN hors du rectangle de
            # l'AOI (une parcelle traversant la tuile) : son propre voisin est
            # alors dehors ET hors du rectangle, donc jamais lu par la requête
            # principale. Deuxième anneau, cadré sur l'étendue réelle des
            # entités analysées, et seulement si elle dépasse l'AOI.
            if scope_bbox is not None and not aoi_rect.contains(scope_bbox):
                ring_request = QgsFeatureRequest()
                # MÊMES attributs que la requête principale : le contrôle de
                # hauteur imbriquée a besoin de l'altitude du polygone
                # CONTENANT, qui peut être un voisin hors zone. Sans elle, la
                # paire serait déclarée « non vérifiable » alors qu'elle est
                # parfaitement comparable.
                with contextlib.suppress(Exception):
                    if wanted_attrs:
                        ring_request.setSubsetOfAttributes(
                            wanted_attrs, layer.fields())
                    else:
                        ring_request.setNoAttributes()
                with contextlib.suppress(Exception):
                    ring_request.setFilterRect(scope_bbox)
                with contextlib.suppress(Exception):
                    for feat in layer.getFeatures(ring_request):
                        if feat.id() in seen_fids:
                            continue
                        check_cancel()
                        seen_fids.add(feat.id())
                        gtype_n = _geometry_type_of(feat)
                        if gtype_n is not None:
                            index_feature(feat, gtype_n)


        # ── Bilan de la zone : HORS de la condition ci-dessus ──
        # Il en dépendait, et décocher les chevauchements, la hauteur imbriquée
        # et les croisements de lignes suffisait alors à faire disparaître
        # l'avertissement « analyse restreinte », à laisser aoi_features à 0 et
        # result.bounds sur la couche entière — le rapport annonçait
        # « Restreinte — 0 entité(s) sur 7 » après en avoir analysé 4. Une
        # restriction muette est précisément ce que cette fonction doit
        # empêcher : le bilan ne dépend donc plus d'aucun contrôle.
        if scope_fids is not None:
            # Étendue annoncée = celle des entités RÉELLEMENT analysées.
            if scope_bbox is not None:
                result.bounds = {
                    "minx": scope_bbox.xMinimum(), "miny": scope_bbox.yMinimum(),
                    "maxx": scope_bbox.xMaximum(), "maxy": scope_bbox.yMaximum(),
                }
            result.aoi_features = processed
            if processed == 0:
                # Zone vide : le DIRE. Un score calculé sur zéro entité est un
                # non-sens, et « 0 anomalie » se lirait comme un sans-faute.
                result.warnings.append(
                    "La zone d'analyse ne contient aucune entité de cette "
                    "couche : aucun contrôle n'a pu porter sur quoi que ce "
                    "soit.")
            else:
                result.warnings.append(
                    "Analyse RESTREINTE à une zone : %s entité(s) sur %s. Les "
                    "compteurs et le score ne portent que sur cette zone."
                    % (C.fmt_int(processed),
                       C.fmt_int(result.aoi_layer_features or processed)))

        # GEOS n'a pas pu conclure sur certaines entités : le contrôle de
        # validité est déjà marqué non concluant, il reste à le DIRE.
        if validity_failures:
            result.warnings.append(
                "Validité indéterminée pour %s entité(s) : le moteur "
                "géométrique n'a pas pu se prononcer — elles ne sont comptées "
                "ni valides ni invalides." % C.fmt_int(validity_failures))

        # Aucun polygone à examiner (dans la couche, ou dans la ZONE si une
        # AOI est active) : les contrôles PUREMENT polygonaux n'ont rien eu à
        # examiner. Les laisser « exécutés » afficherait « 0 anomalie » — lu
        # comme un satisfecit — et les ferait peser dans le score alors qu'ils
        # n'ont mesuré aucune donnée. Même traitement que les croisements de
        # lignes sans ligne (voir plus bas).
        if polygon_seen == 0:
            inapplicables = [key for key in ("overlap", "small", C.CAT_HOLE,
                                             C.CAT_SHARP_ANGLE,
                                             C.CAT_CLOSE_VERTEX,
                                             "nested_height")
                             if result.checks_enabled.get(key)]
            for key in inapplicables:
                result.checks_enabled[key] = False
            if inapplicables:
                nested_enabled = False
                result.warnings.append(
                    "Contrôles polygonaux non applicables : %s ne contient "
                    "aucune entité de type polygone."
                    % ("la zone d'analyse" if scope_fids is not None
                       else "la couche"))

        # Doublons : toutes les entités d'un groupe sauf la première
        dup_count = 0
        for fids in wkb_index.values():
            if len(fids) > 1:
                dup_count += len(fids) - 1
                flagged["duplicate"].extend(fids[1:])
        q.duplicate_count = dup_count

        # Chevauchements / intersections entre polygones
        # AUCUNE limite de nombre de polygones : le contrôle demandé est
        # exécuté quelle que soit la taille de la couche. Un plafond interne
        # abandonnait autrefois l'analyse au-delà de 100 000 polygones — donc
        # précisément sur les couches où le contrôle est le plus utile, et sans
        # que l'utilisateur ait jamais choisi cette limite. La durée reste
        # maîtrisée par l'index spatial (chaque paire n'est vue qu'une fois), et
        # l'analyse reste annulable à tout moment (check_cancel tous les 200
        # polygones) avec une progression affichée.
        # ``opt.check_overlaps`` est indispensable ici : depuis que l'index est
        # aussi construit pour le contrôle de hauteur des polygones inclus, sa
        # seule présence ne signifie plus que les chevauchements ont été
        # demandés. Sans ce test, cocher « Hauteur des polygones inclus » en
        # ayant décoché « Chevauchements » exécutait quand même la passe la plus
        # coûteuse, remplissait les compteurs, les avertissements, la couche
        # d'erreurs et jusqu'à la case « rogner les chevauchements mineurs » du
        # dialogue de réparation — pour un contrôle explicitement refusé.
        if opt.check_overlaps and overlap_index is not None and polygon_fids:
            report(90, "Chevauchements…")
            (overlap_fids, overlap_pairs, contained_pairs,
             missing_vertex_pairs,
             overlap_points) = self._detect_overlaps(
                overlap_index, polygon_fids, check_cancel,
                lambda p: report(90 + 5.0 * p, "Chevauchements…"),
                allow_contained=opt.allow_contained_polygons,
                missing_vertex_ratio=result.missing_vertex_ratio,
                # Le contrôle des doublons est décoché : c'est ici qu'il
                # faut rattraper les superpositions totales, sinon elles
                # ne sont vues par personne.
                equal_is_overlap=not opt.check_duplicates,
                scope=scope_fids,
            )
            q.overlap_count = len(overlap_fids)
            q.overlap_pairs = overlap_pairs
            q.contained_pairs = contained_pairs
            q.overlap_missing_vertex_pairs = missing_vertex_pairs
            flagged["overlap"] = sorted(overlap_fids)
            result.overlap_points = overlap_points

        # Hauteur des polygones entièrement inclus. Balayage SÉPARÉ de celui des
        # chevauchements : il doit tourner même quand ce contrôle est décoché, et
        # il ignore l'option « ignorer les polygones entièrement inclus » — celle
        # -ci écarte ces paires de la liste des chevauchements, elle ne dit rien
        # de la cohérence de leurs altitudes.
        nested_by_field = {}
        if nested_enabled and overlap_index is not None and polygon_fids:
            report(95, "Hauteur des polygones inclus…")
            (nested_fids, nested_bad_pairs, nested_checked,
             nested_unknown, nested_by_field) = self._detect_nested_height(
                overlap_index, polygon_fids, nested_values, nested_labels,
                check_cancel,
                lambda p: report(95 + 3.0 * p, "Hauteur des polygones inclus…"),
                scope=scope_fids)
            q.nested_height_count = len(nested_fids)
            q.nested_height_pairs = nested_bad_pairs
            q.nested_pairs_checked = nested_checked
            q.nested_pairs_unknown = nested_unknown
            flagged[C.CAT_NESTED_HEIGHT] = sorted(nested_fids)
            # Aucune paire COMPARÉE alors qu'il en existait : les altitudes
            # manquaient des deux côtés, « 0 anomalie » serait un satisfecit
            # imméritré. En revanche, aucune paire comparée ET aucune paire non
            # concluante veut dire qu'il n'existe AUCUNE imbrication dans la
            # couche : le contrôle a tourné, il a conclu — il n'y a rien à
            # trouver. Le déclarer non concluant plafonnait une couche
            # parfaitement propre à 99,9.
            if nested_checked == 0 and nested_unknown > 0:
                result.checks_inconclusive[C.CAT_NESTED_HEIGHT] = True

        # Croisements de lignes non nodés (entre entités DISTINCTES). Réservé
        # aux couches de lignes : silencieux si la couche n'en contient aucune,
        # comme la hauteur des polygones inclus l'est en l'absence de champ.
        # ``line_seen`` et non ``line_fids`` : sous zone d'analyse, ce dernier
        # contient aussi les lignes VOISINES hors zone, et une zone sans
        # aucune ligne verrait alors le contrôle « tourner » pour rendre 0.
        if opt.check_line_crossings and line_index is not None:
            if line_seen:
                report(98, "Croisements de lignes…")
                crossing_fids, crossing_pairs, crossing_points = (
                    self._detect_unnoded_crossings(
                        line_index, line_fids, check_cancel,
                        lambda p: report(98 + 2.0 * p, "Croisements de lignes…"),
                        scope=scope_fids))
                q.unnoded_crossing_count = len(crossing_fids)
                q.unnoded_crossing_pairs = crossing_pairs
                flagged[C.CAT_UNNODED_CROSSING] = sorted(crossing_fids)
                for pt_fid, pts in crossing_points.items():
                    result.overlap_points.setdefault(pt_fid, []).extend(pts)
            else:
                result.checks_enabled["unnoded_crossing"] = False
                result.warnings.append(
                    "Croisements de lignes non contrôlés : %s ne contient "
                    "aucune entité de type ligne."
                    % ("la zone d'analyse" if scope_fids is not None
                       else "la couche")
                )

        # Report des compteurs
        result.geometry_types = geom_types
        result.null_geometries = q.null_empty_count
        result.invalid_geometries = q.invalid_count
        result.polygons_below_threshold = q.small_area_count
        result.duplicate_geometries = q.duplicate_count
        result.overlapping_features = q.overlap_count
        q.self_intersection_count = self_intersections
        q.duplicate_vertex_total = duplicate_vertex_total
        result.duplicate_vertex_features = q.duplicate_vertex_count
        q.hole_total = hole_total
        q.sharp_angle_total = sharp_angle_total
        q.close_vertex_total = close_vertex_total
        result.hole_features = q.hole_count
        result.multipart_features = q.multipart_count
        result.sharp_angle_features = q.sharp_angle_count
        result.close_vertex_features = q.close_vertex_count
        result.nested_height_features = q.nested_height_count
        result.unnoded_crossing_features = q.unnoded_crossing_count
        q.invalid_details = invalid_details
        result.flagged = flagged

        # Contrôle interne AGL/AMSL : note discrète, uniquement si les deux
        # champs existent, sont ENTIÈREMENT renseignés et qu'une incohérence
        # est détectée. Sinon, aucune trace visible (contrôle silencieux).
        if buildings_enabled and agl_complete:
            result.buildings_agl_over_amsl = agl_over
            if agl_over:
                result.agl_note = (
                    "Contrôle complémentaire des altitudes : %s entité(s) présentent "
                    "AGL supérieur à AMSL — valeurs à vérifier." % C.fmt_int(agl_over)
                )
        if not result.agl_note:
            # Contrôle non concluant ou sans anomalie : ne laisser aucune trace.
            flagged[C.CAT_AGL] = []

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
        if q.contained_pairs:
            if opt.allow_contained_polygons:
                result.warnings.append(
                    "%d paires de polygones imbriqués (inclusion totale) — "
                    "acceptées selon vos réglages." % q.contained_pairs
                )
            else:
                result.warnings.append(
                    "%d paires de polygones imbriqués (inclusion totale) — "
                    "comptées comme chevauchement." % q.contained_pairs
                )
        if q.small_area_count:
            result.warnings.append(
                "%d polygones ≤ %g m²." % (q.small_area_count, opt.small_polygon_threshold_m2)
            )
        if q.duplicate_count:
            result.warnings.append("%d doublons détectés." % q.duplicate_count)
        if q.duplicate_vertex_count:
            result.warnings.append(
                "%d entités avec des sommets dupliqués (%d sommets en doublon)."
                % (q.duplicate_vertex_count, q.duplicate_vertex_total)
            )
        if q.hole_count:
            result.warnings.append(
                "%d entités avec des trous ≤ %g m² (%d trous)."
                % (q.hole_count, opt.hole_max_area_m2, q.hole_total)
            )
        if q.multipart_count:
            result.warnings.append("%d entités multi-parties." % q.multipart_count)
        if q.sharp_angle_count:
            result.warnings.append(
                "%d entités avec des angles aigus < %g° (%d sommets en pointe)."
                % (q.sharp_angle_count, opt.sharp_angle_min_deg, q.sharp_angle_total)
            )
        if q.close_vertex_count:
            result.warnings.append(
                "%d entités avec des sommets trop rapprochés ≤ %g m (%d paires)."
                % (q.close_vertex_count, opt.close_vertex_min_dist_m, q.close_vertex_total)
            )
        if q.nested_height_count:
            detail = ", ".join("%s : %s" % (label, C.fmt_int(count))
                               for label, count in nested_by_field.items()
                               if count)
            result.warnings.append(
                "%s polygone(s) inclus dans un autre sans être strictement plus "
                "haut que lui (%s paire(s) — %s)."
                % (C.fmt_int(q.nested_height_count),
                   C.fmt_int(q.nested_height_pairs), detail)
            )
        # Des paires imbriquées sans altitude comparable : le dire, sinon un
        # « 0 anomalie » se lirait comme un sans-faute alors que rien n'a pu
        # être comparé.
        if q.nested_pairs_unknown:
            result.warnings.append(
                "%s paire(s) de polygones imbriqués non vérifiables : altitude "
                "absente ou non numérique de part ou d'autre."
                % C.fmt_int(q.nested_pairs_unknown)
            )
        if q.unnoded_crossing_count:
            result.warnings.append(
                "%s ligne(s) en croisement non nodé (%s paire(s) sans sommet "
                "commun au point de rencontre)."
                % (C.fmt_int(q.unnoded_crossing_count),
                   C.fmt_int(q.unnoded_crossing_pairs))
            )
        if result.crs_authid in ("Inconnu", ""):
            result.warnings.append("CRS inconnu : les mesures de surface peuvent être erronées.")
        # Les seuils de DISTANCE sont désormais convertis dans les unités de la
        # couche (voir layer_units_per_metre). Sur un CRS géographique, cette
        # conversion reste approximative : un degré de longitude est plus court
        # qu'un degré de latitude, donc le seuil effectif dépend de
        # l'orientation. Il n'est jamais plus permissif que demandé, mais peut
        # être plus strict — d'où cet avertissement, revu et non supprimé.
        distance_checks_on = (
            opt.check_close_vertices and opt.close_vertex_min_dist_m > 0
        )
        if distance_checks_on and result.crs_is_geographic:
            result.warnings.append(
                "CRS géographique (degrés) : les seuils en mètres (sommets "
                "rapprochés, quasi-doublons) sont convertis en degrés, mais "
                "restent approximatifs selon l'orientation — jamais plus "
                "permissifs que demandé. Reprojetez la couche dans un CRS "
                "métrique pour un contrôle exact."
            )

        result.preview_rows = preview_rows
        # Le score se fonde sur ``checks_enabled`` et NON sur les options
        # demandées : un contrôle coché mais abandonné en cours de route
        # (limite de volume, index spatial indisponible) ne doit pas produire
        # un sous-score à 100 % — c'est justement là que la note tromperait le
        # plus. ``checks_enabled`` reflète ce qui a réellement tourné.
        ran = result.checks_enabled
        result.correctness = compute_correctness(
            result,
            include_overlap=ran.get("overlap", True),
            include_duplicate=ran.get("duplicate", True),
            include_small=ran.get("small", True),
            include_duplicate_vertex=ran.get("duplicate_vertex", False),
            include_holes=ran.get("hole", False),
            include_multipart=ran.get("multipart", False),
            include_sharp_angles=ran.get("sharp_angle", False),
            include_close_vertices=ran.get("close_vertex", False),
            include_nested_height=ran.get("nested_height", False),
            include_line_crossings=ran.get("unnoded_crossing", False),
        )
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
        measurement_ctx=None,
        *,
        dry_run: bool = False,
        only_fids: Optional[List[int]] = None,
    ):
        """Construit une NOUVELLE couche mémoire réparée selon `ropts`.

        ``only_fids`` : ne lit et ne traite QUE ces entités (au lieu de toute
        la couche) — sert à l'APERÇU d'une seule catégorie de correction, dont
        le lot est déjà connu (les FID signalés par l'analyse). Le calcul de
        décision par entité reste EXACTEMENT le même ; seul l'ensemble parcouru
        change. Sans effet sur les catégories dont la décision dépend d'un
        voisin hors du lot (chevauchements) : ``_compute_overlap_fixes``
        construit son propre index scopé au sous-ensemble qui lui est passé,
        et ce sous-ensemble contient TOUJOURS les deux membres de chaque paire
        détectée (voir ``GeomAnalyzer._detect_overlaps``) — restreindre à
        ``only_fids`` == ce même sous-ensemble ne peut donc jamais faire
        disparaître un voisin nécessaire au calcul.

        ``dry_run`` : calcule et compte tout normalement (chaque branche de
        décision ci-dessous tourne à l'identique), mais NE CONSTRUIT ni
        n'écrit aucune sortie. Utilisé pour l'aperçu du dialogue de
        réparation : les compteurs de ``RepairStats`` sont réels, la couche
        mémoire renvoyée est ``None`` — jamais un objet partiel ou trompeur.

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
                                      intacts et signalés (manual_overlap_fids) ;
          * fix_duplicate_vertex   -> retire les sommets STRICTEMENT dupliqués ;
          * remove_holes           -> comble les trous ≤ hole_max_area_m2 ; les
                                      trous plus grands sont CONSERVÉS ;
          * explode_multipart      -> éclate les entités multi-parties en une
                                      entité par partie (attributs recopiés) :
                                      le nombre d'entités écrites augmente ;
          * fix_sharp_angles       -> retire les sommets en pointe (< seuil) en
                                      préservant la validité, sinon l'entité est
                                      laissée intacte (skipped_sharp_angles) ;
          * fix_close_vertices     -> fusionne les sommets séparés de moins que
                                      close_vertex_min_dist_m (léger changement
                                      de tracé, contrairement aux doublons).

        Renvoie (couche_mémoire, RepairStats). L'appelant écrit ensuite cette
        couche dans un fichier (voir StatGeomDockWidget).
        """
        stats = RepairStats()

        # Sortie promue en multi-parties : « make valid » peut transformer un
        # Polygon en MultiPolygon (comme l'algorithme natif « Corriger les
        # géométries »). Un type multi accueille sans perte les entités mono-
        # et multi-parties et évite les avertissements/rejets des pilotes stricts.
        out_wkb = QgsWkbTypes.multiType(layer.wkbType())
        target_gtype = QgsWkbTypes.geometryType(out_wkb)
        wkb_str = QgsWkbTypes.displayString(out_wkb)
        # ``target_gtype`` sert au calcul de décision (make valid : la
        # géométrie corrigée doit survivre à la coercition vers le type de la
        # couche pour compter comme « réparée ») — nécessaire même en aperçu.
        # La couche mémoire elle-même, en revanche, n'a aucune raison d'exister
        # en ``dry_run`` : ce serait construire un contenant pour ne jamais
        # l'écrire, à chaque clic sur un aperçu.
        mem = pr = out_fields = None
        if not dry_run:
            authid = layer.crs().authid()
            uri = wkb_str + ("?crs=%s" % authid if authid else "")
            mem = QgsVectorLayer(uri, "%s — réparé" % layer.name(), "memory")
            pr = mem.dataProvider()
            if not mem.isValid() or pr is None:
                raise RuntimeError(
                    "Impossible de créer la couche mémoire de réparation (type %s)." % wkb_str
                )
            # Le CRS est posé sur l'OBJET couche et pas seulement passé dans
            # l'URI. Celle-ci ne sait transporter qu'un code d'autorité : un
            # CRS lu depuis un WKT que PROJ ne rattache à aucun EPSG a un
            # ``authid()`` vide, et la couche mémoire héritait alors
            # silencieusement d'EPSG:4326 — des coordonnées de grille locale
            # écrites comme du WGS 84, sans un mot. ``setCrs`` accepte
            # n'importe quel CRS valide et ne change rien quand l'URI portait
            # déjà le bon code.
            if layer.crs().isValid():
                mem.setCrs(layer.crs())
            # Champs identiques à la source, hormis les identifiants que le
            # pilote d'écriture réserve à sa clé primaire (voir
            # build_output_fields).
            renamed_fields, renames = build_output_fields(layer.fields())
            pr.addAttributes(renamed_fields.toList())
            mem.updateFields()
            out_fields = mem.fields()
            stats.renamed_fields = renames

        flagged = result.flagged or {}
        # Résolu ICI (avant les *_set) : réutilisé pour les petits polygones
        # comme pour les trous, la mesure doit être IDENTIQUE (ellipsoïdale,
        # en m²) à celle de l'analyse, sinon un seuil inchangé ne désignerait
        # plus les mêmes entités.
        mctx = _resolve_measurement_context(measurement_ctx)
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
        small_set = set()
        if ropts.remove_small:
            small_set = GeomAnalyzer._compute_small_polygon_removals(
                layer, flagged.get("small", []),
                ropts.small_polygon_threshold_m2, stats, mctx)
        dupvertex_set = (set(flagged.get(C.CAT_DUPLICATE_VERTEX, []))
                         if ropts.fix_duplicate_vertex else set())
        hole_set = (set(flagged.get(C.CAT_HOLE, []))
                    if ropts.remove_holes else set())
        # La surface des trous doit être mesurée EXACTEMENT comme à l'analyse
        # (ellipsoïdal, en m²), sinon le seuil ne désignerait pas les mêmes trous.
        measure_hole_area = None
        if ropts.remove_holes:
            area_calc = QgsDistanceArea()
            with contextlib.suppress(Exception):
                area_calc.setSourceCrs(layer.crs(), mctx.transform_context)
                area_calc.setEllipsoid(mctx.usable_ellipsoid())

            def measure_hole_area(ring_geom, _calc=area_calc):  # noqa: F811
                try:
                    raw = _calc.measureArea(ring_geom)
                    return _calc.convertAreaMeasurement(
                        raw, QgsUnitTypes.AreaUnit.AreaSquareMeters)
                except Exception:
                    return None
        # Même règle qu'à l'analyse : le repli planaire n'est licite qu'en
        # mètres, sinon un trou de 1e-4 deg² serait comblé comme « 0,0001 m² ».
        planar_is_m2 = layer_planar_area_is_m2(layer)
        sharp_set = (set(flagged.get(C.CAT_SHARP_ANGLE, []))
                     if ropts.fix_sharp_angles else set())
        close_set = (set(flagged.get(C.CAT_CLOSE_VERTEX, []))
                     if ropts.fix_close_vertices else set())

        # Seuils de distance convertis dans les unités de la couche, en version
        # PRUDENTE : une réparation ne doit jamais modifier la géométrie au-delà
        # de ce que l'utilisateur a demandé. Sur un CRS géographique, la
        # détection des sommets rapprochés utilise à l'inverse une conversion
        # large : une entité peut donc être signalée sans être fusionnée ici —
        # elle est alors comptée parmi les entités laissées intactes, jamais
        # ignorée en silence.
        units_per_m = layer_units_per_metre(layer, mctx)
        # Sommets dupliqués : toujours strictement identiques, aucune tolérance.
        vertex_tol_units = 0.0
        close_dist_units = ropts.close_vertex_min_dist_m * units_per_m

        # Chevauchements : on pré-calcule les rognages MINEURS à appliquer.
        # Les entités par ailleurs supprimées (doublons/petits/nulles) sont
        # exclues pour rester cohérent.
        overlap_fixes = {}
        if ropts.fix_overlaps:
            overlap_set = (set(flagged.get("overlap", []))
                           - (null_set | dup_set | small_set))
            overlap_fixes = GeomAnalyzer._compute_overlap_fixes(
                layer, overlap_set, ropts.overlap_max_fraction, stats,
                missing_vertex_ratio=ropts.missing_vertex_ratio,
                allow_contained=ropts.allow_contained_polygons,
                equal_is_overlap=ropts.equal_is_overlap,
            )

        # ── Périmètre de la sortie ──
        # ``only_fids`` restreint la LECTURE au sous-ensemble demandé (aperçu
        # d'une seule catégorie) ; ``None`` lit toute la couche.
        #
        # Zone d'analyse : la sortie porte EXACTEMENT le même périmètre que
        # l'analyse. Recopier toute la couche livrerait un fichier dont la
        # quasi-totalité n'a jamais été contrôlée, sous un nom qui dit
        # « réparé » ; ne garder que la zone livre un extrait, ce qui est
        # assumé et annoncé dans le bilan.
        aoi_geom = None
        if only_fids is None and getattr(result, "aoi_wkt", ""):
            candidate = QgsGeometry.fromWkt(result.aoi_wkt)
            if candidate is not None and not candidate.isNull():
                aoi_geom = candidate

        if only_fids is not None:
            request = QgsFeatureRequest().setFilterFids(list(only_fids))
            total = len(only_fids)
        else:
            request = QgsFeatureRequest()
            if aoi_geom is not None:
                with contextlib.suppress(Exception):
                    request.setFilterRect(aoi_geom.boundingBox())
            total = result.aoi_features if aoi_geom is not None else (
                layer.featureCount() or 0)
        feature_source = layer.getFeatures(request)
        out_feats: List[QgsFeature] = []
        processed = 0
        for feat in feature_source:
            if is_canceled and is_canceled():
                raise AnalysisCancelled()
            if progress_cb and total and processed % 500 == 0:
                progress_cb(100.0 * processed / total)
            processed += 1
            fid = feat.id()

            # Hors zone d'analyse : ni recopiée, ni corrigée. Le filtre par
            # rectangle ci-dessus n'a trié que les enveloppes ; c'est le même
            # test exact qu'à l'analyse qui décide (voir ``analyze``).
            if aoi_geom is not None:
                geom_zone = feat.geometry() if feat.hasGeometry() else None
                if not feature_in_aoi(geom_zone, aoi_geom):
                    continue

            # Suppressions (prioritaires sur la correction).
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

            # Suppression des trous sous seuil (les grands trous, souvent
            # justifiés, sont conservés intacts).
            if fid in hole_set and geom is not None and not geom.isNull():
                new_geom, removed_holes = remove_small_holes(
                    geom, ropts.hole_max_area_m2, measure_hole_area,
                    planar_is_m2)
                if removed_holes:
                    geom = new_geom
                    stats.removed_holes += removed_holes
                    stats.features_with_holes_fixed += 1

            # Retrait des sommets en pointe (angles aigus). Conservateur : si la
            # reconstruction dégrade la géométrie, l'entité est laissée intacte
            # et comptée dans skipped_sharp_angles.
            if fid in sharp_set and geom is not None and not geom.isNull():
                new_geom, removed_spikes = remove_sharp_angle_vertices(
                    geom, ropts.sharp_angle_min_deg)
                if removed_spikes:
                    geom = new_geom
                    stats.fixed_sharp_angles += 1
                    stats.removed_spike_vertices += removed_spikes
                elif count_sharp_angles(geom, ropts.sharp_angle_min_deg):
                    # La pointe est toujours là et n'a pas pu être retirée sans
                    # dégrader la géométrie : « laissée intacte » est justifié.
                    stats.skipped_sharp_angles += 1
                    stats.skipped_sharp_angle_fids.append(fid)
                else:
                    # Plus rien à corriger : une étape antérieure (rognage d'un
                    # chevauchement, make valid) a déjà fait disparaître la
                    # pointe. Compté à part, car annoncer « la correction aurait
                    # dégradé la géométrie » serait une accusation fausse.
                    stats.unchanged_sharp_angles += 1

            # Fusion des sommets trop rapprochés (modifie légèrement le tracé).
            if fid in close_set and geom is not None and not geom.isNull():
                merged = QgsGeometry(geom)
                merged_count = 0
                try:
                    before = _count_coordinates(merged)
                    if _remove_duplicate_nodes(merged, close_dist_units):
                        merged_count = max(0, before - _count_coordinates(merged))
                except Exception:
                    merged_count = 0
                # Garde-fou : la fusion peut réduire un anneau à moins de trois
                # sommets distincts et l'effondrer (aire nulle). On refuse alors
                # la correction plutôt que de détruire l'entité.
                if merged_count and is_safe_replacement(geom, merged):
                    geom = merged
                    stats.cleaned_close_vertices += 1
                    stats.removed_close_vertices += merged_count
                elif merged_count:
                    # Des sommets AURAIENT fusionné, mais le résultat dégradait
                    # la géométrie (anneau effondré) : correction refusée.
                    stats.skipped_close_vertices += 1
                    stats.skipped_close_vertex_fids.append(fid)
                else:
                    # Rien à fusionner : le seuil de fusion est prudent alors
                    # que la détection est large (voir les deux conversions
                    # d'unités), donc une entité peut être signalée sans qu'il y
                    # ait matière à corriger ici. Ce n'est pas un refus.
                    stats.unchanged_close_vertices += 1

            # Nettoyage des sommets dupliqués (avec tolérance éventuelle).
            # Réalisé AVANT « make valid » : retirer les doublons peut déjà
            # lever certaines invalidités, la forme du tracé restant inchangée.
            if fid in dupvertex_set and geom is not None and not geom.isNull():
                cleaned = QgsGeometry(geom)
                removed_here = 0
                try:
                    before = _count_coordinates(cleaned)
                    if _remove_duplicate_nodes(cleaned, vertex_tol_units):
                        removed_here = max(0, before - _count_coordinates(cleaned))
                except Exception:
                    removed_here = 0
                # Même garde-fou : avec une tolérance > 0, le retrait peut
                # dégrader la géométrie (cf. sommets rapprochés).
                if removed_here and is_safe_replacement(geom, cleaned):
                    geom = cleaned
                    stats.cleaned_duplicate_vertex += 1
                    stats.removed_vertices += removed_here
                elif removed_here:
                    stats.skipped_duplicate_vertex += 1
                    stats.skipped_duplicate_vertex_fids.append(fid)

            # Correction des géométries invalides / auto-intersections (make valid)
            if fid in makevalid_set and geom is not None and not geom.isNull():
                try:
                    fixed = geom.makeValid()
                except Exception:
                    fixed = None
                # « make valid » peut produire une GeometryCollection hétérogène
                # (Polygon + LineString) : on ne garde que les parties du type
                # de la couche, sinon le fournisseur refuserait tout le lot.
                if fixed is not None:
                    fixed = coerce_to_geometry_type(fixed, target_gtype)
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

            # Éclatement des multi-parties : une entité de sortie par partie,
            # attributs recopiés à l'identique. Réalisé en DERNIER pour éclater
            # aussi les multi-parties éventuellement créées par « make valid ».
            out_geoms = [geom]
            if (ropts.explode_multipart and geom is not None
                    and not geom.isNull() and count_parts(geom) > 1):
                parts = explode_parts(geom)
                if len(parts) > 1:
                    out_geoms = parts
                    stats.exploded_features += 1
                    stats.added_features += len(parts) - 1

            if dry_run:
                continue    # décision comptée ; rien à écrire en aperçu

            for out_geom in out_geoms:
                nf = QgsFeature(out_fields)
                nf.setAttributes(feat.attributes())
                # Dernier filet : n'écrire qu'une géométrie du type de la couche.
                # À défaut, on retombe sur la géométrie d'origine (compatible par
                # construction) pour ne jamais perdre l'entité.
                safe_geom = coerce_to_geometry_type(out_geom, target_gtype)
                if safe_geom is None:
                    safe_geom = coerce_to_geometry_type(
                        feat.geometry() if feat.hasGeometry() else None, target_gtype)
                if safe_geom is not None and not safe_geom.isNull():
                    with contextlib.suppress(Exception):
                        safe_geom.convertToMultiType()
                    nf.setGeometry(safe_geom)
                out_feats.append(nf)

        if dry_run:
            return None, stats

        added = pr.addFeatures(out_feats)
        mem.updateExtents()
        # Compter ce que le fournisseur a RÉELLEMENT accepté : annoncer
        # len(out_feats) masquerait un rejet de lot (le fournisseur mémoire
        # abandonne tout le lot dès une géométrie non conforme).
        stats.written = mem.featureCount()
        stats.rejected = max(0, len(out_feats) - stats.written)
        # AUCUNE sortie partielle. Un fichier « réparé » à qui il manque des
        # entités est pire qu'une erreur : rien ne le signale à la relecture,
        # il porte le même nom, il se charge normalement — et l'utilisateur
        # travaille ensuite sur une donnée amputée en la croyant complète.
        # Refuser le lot laisse au moins le choix de décocher la correction
        # fautive et de relancer.
        if out_feats and (stats.rejected or added is False):
            details = ""
            with contextlib.suppress(Exception):
                details = "; ".join(pr.errors()[:2])
            raise RuntimeError(
                "Réparation abandonnée : le fournisseur a refusé %s entité(s) "
                "sur %s. Aucun fichier partiel n'est écrit%s."
                % (C.fmt_int(stats.rejected or len(out_feats)),
                   C.fmt_int(len(out_feats)),
                   " (%s)" % details if details else "")
            )

        return mem, stats

    @staticmethod
    def _compute_small_polygon_removals(layer, small_fids, threshold_m2, stats,
                                        mctx: "MeasurementContext"):
        """Parmi les polygones déjà signalés « petits » à l'ANALYSE, lesquels
        restent ≤ ``threshold_m2`` — le seuil de RÉPARATION, qui peut différer
        de celui de l'analyse.

        Ne peut JAMAIS désigner un FID hors de ``small_fids`` : un seuil de
        réparation plus LARGE ne fait réapparaître personne (il faudrait
        rebalayer toute la couche pour ça, ce que ce contrôle ne fait pas —
        voir ``AnalysisResult.flagged["small"]``, seule source des candidats).
        Un seuil plus STRICT, en revanche, en écarte certains : comptés dans
        ``stats.small_kept_above_threshold``, jamais supprimés en silence.

        Même mesure ellipsoïdale que ``GeomAnalyzer.analyze`` (surface sur une
        copie rendue valide si besoin) : un seuil INCHANGÉ doit désigner
        EXACTEMENT les mêmes entités qu'à l'analyse, sans quoi le compte
        « petits polygones » du panneau et celui de la réparation
        divergeraient pour la même donnée.

        Alimente ``stats.removed_small_area_m2`` (somme des surfaces
        réellement supprimées) comme effet de bord. Renvoie l'ensemble des
        FID CONFIRMÉS pour suppression.
        """
        confirmed = set()
        if not small_fids:
            return confirmed
        area_calc = QgsDistanceArea()
        with contextlib.suppress(Exception):
            area_calc.setSourceCrs(layer.crs(), mctx.transform_context)
            area_calc.setEllipsoid(mctx.usable_ellipsoid())

        def _area(geom):
            try:
                raw = area_calc.measureArea(geom)
                return area_calc.convertAreaMeasurement(
                    raw, QgsUnitTypes.AreaUnit.AreaSquareMeters)
            except Exception:
                return None

        request = QgsFeatureRequest().setFilterFids(list(small_fids))
        for f in layer.getFeatures(request):
            geom = f.geometry() if f.hasGeometry() else None
            if geom is None or geom.isNull() or geom.isEmpty():
                continue
            try:
                valid = geom.isGeosValid()
            except Exception:
                valid = True
            measurable = geom if valid else _validated_for_area(geom)
            area = _area(measurable) if measurable is not None else None
            if area is None:
                # Surface non mesurable : l'entité est conservée, mais dire
                # « conservée car au-dessus du seuil » serait faux — on ne
                # sait justement pas où elle se situe.
                stats.small_not_measurable += 1
            elif area <= threshold_m2:
                confirmed.add(f.id())
                stats.removed_small_area_m2 += area
            else:
                stats.small_kept_above_threshold += 1
        return confirmed

    @staticmethod
    def _compute_overlap_fixes(layer, overlap_fids, max_fraction, stats,
                               missing_vertex_ratio=0.0,
                               allow_contained=False, equal_is_overlap=False):
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

        ``missing_vertex_ratio`` reprend le seuil de forme réellement appliqué à
        l'analyse : un recouvrement écarté comme « sommet manquant » n'ayant pas
        été signalé, il ne doit pas non plus être rogné. Sans cela, une entité
        signalée pour un vrai chevauchement se verrait aussi retirer les
        recouvrements écartés de ses autres voisins.

        Renvoie ``{fid: géométrie_rognée}`` pour les seuls cas mineurs.
        """
        fixes = {}
        if not overlap_fids:
            return fixes
        try:
            index = QgsSpatialIndex(QgsSpatialIndex.Flag.FlagStoreFeatureGeometries)
        except Exception:
            return fixes

        geoms = {}
        areas = {}
        request = QgsFeatureRequest().setFilterFids(list(overlap_fids))
        for f in layer.getFeatures(request):
            g = f.geometry()
            if g is None or g.isNull() or g.isEmpty():
                continue
            if QgsWkbTypes.geometryType(g.wkbType()) != QgsWkbTypes.GeometryType.PolygonGeometry:
                continue
            geoms[f.id()] = QgsGeometry(g)
            areas[f.id()] = g.area()
            with contextlib.suppress(Exception):
                index.addFeature(f)

        for fid, g in geoms.items():
            area = areas.get(fid, 0.0)
            if area <= 0:
                continue
            candidates = _safe_index_intersects(index, g.boundingBox())
            if candidates is None:
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
                if not _safe_intersects(g, og):
                    continue
                # Mêmes règles qu'à l'analyse, sans exception : ni un
                # recouvrement imputé à un sommet manquant, ni une imbrication
                # que l'utilisateur a déclarée légitime, ne sont des motifs de
                # rognage. Sans le second cas, un bâtiment inclus dans sa
                # parcelle était soustrait de celle-ci — donc entièrement
                # effacé, puis annoncé comme « chevauchement majeur ».
                ecartes = [OVERLAP_NONE, OVERLAP_MISSING_VERTEX]
                if allow_contained:
                    ecartes.append(OVERLAP_CONTAINED)
                if classify_overlap(g, og, missing_vertex_ratio,
                                    equal_is_overlap) in ecartes:
                    continue
                dom_union = QgsGeometry(og) if dom_union is None else dom_union.combine(og)

            if dom_union is None or dom_union.isNull():
                continue  # ce polygone est dominant partout : on n'y touche pas

            new_g = _difference_or_none(g, dom_union)

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


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITAIRES
# ═══════════════════════════════════════════════════════════════════════════════


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _attr_str(value: Any) -> str:
    """Convertit une valeur d'attribut en chaîne affichable."""
    with contextlib.suppress(Exception):
        # NULL QVariant de QGIS
        if value is None:
            return ""
        if hasattr(value, "isNull") and value.isNull():
            return ""
    return str(value)
