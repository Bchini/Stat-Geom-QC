# -*- coding: utf-8 -*-
"""Persistance des paramètres utilisateur via QgsSettings.

Conserve d'une session à l'autre les derniers choix utiles (seuil, contrôles
actifs, tolérance de rognage) afin d'éviter à l'utilisateur de tout re-régler.
La lecture est toujours tolérante : toute valeur absente ou corrompue est
remplacée par un défaut sûr issu de ``constants``.
"""

from qgis.core import QgsSettings

from . import constants as C
from .analysis_engine import AnalysisOptions


def _group_key(key: str) -> str:
    return "%s/%s" % (C.SETTINGS_GROUP, key)


# Contrôles OPT-IN : ils ne sont JAMAIS restaurés à l'état actif.
#
# Ce sont exactement les contrôles de la section « Extras ». Ils ne s'appliquent
# qu'à certains types de données — saisie sur-densifiée, réseaux linéaires — et
# les mémoriser signifiait qu'une case cochée une fois pour essayer revenait
# cochée à chaque ouverture suivante, faisant tourner dans les sessions
# suivantes des contrôles que l'utilisateur n'avait pas demandés. Chaque session
# repart donc de l'état décoché : les activer est un geste explicite, à refaire.
#
# Seule l'ACTIVATION est concernée : les seuils et tolérances associés restent
# mémorisés, pour ne pas avoir à les re-régler.
#
# Quatre contrôles en sont SORTIS en 2.9.49 (petits polygones, trous, vertex
# dupliqués, hauteur des polygones inclus) : ils font désormais partie de la
# section « Options d'analyse », cochée en entier par défaut. Les forcer
# décochés à chaque ouverture irait contre ce défaut — même raisonnement que
# pour les deux exceptions de chevauchement, sorties en 2.9.31.
OPT_IN_CHECK_KEYS = (
    C.SK_SHARP_ANGLES,      # angles aigus (Extras)
    C.SK_CLOSE_VERTICES,    # sommets rapprochés (Extras)
    C.SK_LINE_CROSSINGS,    # croisements de lignes non nodés (Extras)
)

# Contrôles passés d'opt-in à « cochés par défaut » en 2.9.49. Leurs clés sont
# effacées une fois par la migration 10 : un profil pouvait en garder une valeur
# « false » écrite du temps où ils étaient décochés, et ne jamais voir le
# nouveau défaut.
_NEWLY_DEFAULT_ON_KEYS = (
    C.SK_SMALL_POLYGONS,
    C.SK_HOLES,
    C.SK_DUPLICATE_VERTEX,
    C.SK_NESTED_HEIGHT,
)


def _get_float(settings: QgsSettings, key: str, default: float,
               lo: float, hi: float) -> float:
    try:
        value = float(settings.value(_group_key(key), default))
    except (TypeError, ValueError):
        return default
    if value != value:  # NaN
        return default
    return max(lo, min(hi, value))


def _get_bool(settings: QgsSettings, key: str, default: bool) -> bool:
    raw = settings.value(_group_key(key), default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        return default        # valeur inattendue : on ne devine pas
    try:
        return bool(int(raw))
    except (TypeError, ValueError):
        return default


def migrate_settings() -> None:
    """Aligne les réglages mémorisés sur les défauts d'une nouvelle version.

    Sans cela, un défaut modifié resterait invisible : la valeur de la version
    précédente, enregistrée à la première analyse, continuerait d'être relue.

    Version 2 : le seuil du trou « fautif » passe de 10 m² à 2 m². La valeur
    n'est remplacée que si elle vaut EXACTEMENT l'ancien défaut — un seuil
    choisi délibérément par l'utilisateur est donc conservé.

    Version 3 : les activations de contrôles opt-in mémorisées par les versions
    précédentes sont effacées, sinon une case cochée lors d'une ancienne session
    resterait cochée à l'ouverture.

    Version 4 : la tolérance d'accrochage des chevauchements n'existe plus (la
    règle est topologique, sans seuil). Sa clé est effacée pour ne pas laisser un
    réglage orphelin dans le fichier de configuration.

    Version 5 : idem pour le seuil de forme du « sommet manquant », désormais une
    constante — l'option se résume à une case à cocher.

    Version 6 : idem pour la tolérance de quasi-doublon des sommets ; seuls les
    sommets STRICTEMENT identiques sont désormais détectés.

    Version 7 : le contrôle « polygones filiformes » est retiré du plugin ; son
    activation et son seuil sont effacés.

    Version 8 : seconde passe sur le seuil de trou, et nettoyage de la clé
    orpheline du contrôle « bâtiments ».

    Version 9 : les deux exceptions de chevauchement deviennent cochées par
    défaut ; les valeurs enregistrées par les versions précédentes sont
    effacées, sans quoi le nouveau défaut resterait invisible.

    Version 10 : les préréglages disparaissent, toute la section « Options
    d'analyse » est cochée par défaut. Quatre contrôles quittent l'opt-in
    (``_NEWLY_DEFAULT_ON_KEYS``) ; leurs clés sont effacées une fois, pour la
    même raison qu'en version 9.
    """
    s = QgsSettings()
    try:
        version = int(s.value(_group_key(C.SK_SETTINGS_VERSION), 1))
    except (TypeError, ValueError):
        version = 1
    if version >= C.SETTINGS_VERSION:
        return
    if version < 2:
        stored = s.value(_group_key(C.SK_HOLE_THRESHOLD), None)
        if stored is not None:
            try:
                is_legacy_default = (float(stored)
                                     == C.HOLE_THRESHOLD_LEGACY_DEFAULT_M2)
            except (TypeError, ValueError):
                is_legacy_default = True   # valeur illisible : on repart du défaut
            if is_legacy_default:
                s.setValue(_group_key(C.SK_HOLE_THRESHOLD),
                           float(C.HOLE_THRESHOLD_DEFAULT_M2))
    if version < 3:
        for key in OPT_IN_CHECK_KEYS:
            s.remove(_group_key(key))
    if version < 4:
        s.remove(_group_key(C.SK_OVERLAP_MIN_WIDTH_LEGACY))
    if version < 5:
        s.remove(_group_key(C.SK_MISSING_VERTEX_RATIO_LEGACY))
    if version < 6:
        s.remove(_group_key(C.SK_VERTEX_TOL_LEGACY))
    if version < 7:
        s.remove(_group_key(C.SK_SLIVERS_LEGACY))
        s.remove(_group_key(C.SK_SLIVER_RATIO_LEGACY))
    if version < 8:
        # Deuxième passe sur le seuil de trou. La migration 2 ne s'appliquait
        # qu'aux profils venant d'une version antérieure à elle ; un profil déjà
        # au-delà a conservé son 10 m² indéfiniment et n'a donc jamais vu le
        # défaut de 2 m². Même prudence qu'en version 2 : seule la valeur égale
        # à l'ancien défaut est remplacée, un seuil choisi délibérément (15,
        # 25…) est conservé.
        stored = s.value(_group_key(C.SK_HOLE_THRESHOLD), None)
        if stored is not None:
            try:
                est_ancien_defaut = (float(stored)
                                     == C.HOLE_THRESHOLD_LEGACY_DEFAULT_M2)
            except (TypeError, ValueError):
                est_ancien_defaut = True   # valeur illisible : retour au défaut
            if est_ancien_defaut:
                s.setValue(_group_key(C.SK_HOLE_THRESHOLD),
                           float(C.HOLE_THRESHOLD_DEFAULT_M2))
        # Clé orpheline du contrôle « bâtiments » (AGL/AMSL), devenu interne et
        # sans case à cocher depuis la 2.7 : elle traînait dans les profils.
        s.remove(_group_key(C.SK_BUILDINGS_LEGACY))
    if version < 9:
        # Les deux exceptions de chevauchement deviennent cochées par défaut.
        # Un profil qui en garde une valeur enregistrée — « false », écrit du
        # temps où elles étaient décochées — continuerait de la relire et ne
        # verrait jamais le nouveau défaut. On efface donc les deux clés une
        # fois ; le prochain enregistrement repartira du choix de l'utilisateur.
        s.remove(_group_key(C.SK_ALLOW_CONTAINED))
        s.remove(_group_key(C.SK_IGNORE_MISSING_VERTEX))
    if version < 10:
        # Quatre contrôles passent d'opt-in à cochés par défaut. Les versions
        # précédentes effaçaient leur clé à chaque enregistrement, mais une
        # valeur « false » a pu subsister (profil figé sur une vieille version) :
        # elle serait relue et le nouveau défaut ne se verrait jamais.
        for key in _NEWLY_DEFAULT_ON_KEYS:
            s.remove(_group_key(key))
    s.setValue(_group_key(C.SK_SETTINGS_VERSION), int(C.SETTINGS_VERSION))


def load_options() -> AnalysisOptions:
    """Reconstitue les options d'analyse depuis la configuration persistante.

    Les défauts ne sont PAS ceux de ``AnalysisOptions()`` mais ceux du panneau
    (``C.UI_DEFAULT_CHECKS``) : le moteur reste conservateur pour les usages
    programmatiques, là où le panneau ouvre avec toute la section « Options
    d'analyse » cochée.
    """
    migrate_settings()
    s = QgsSettings()
    defauts = C.UI_DEFAULT_CHECKS
    return AnalysisOptions(
        small_polygon_threshold_m2=_get_float(
            s, C.SK_THRESHOLD, C.THRESHOLD_DEFAULT_M2,
            C.THRESHOLD_MIN_M2, C.THRESHOLD_MAX_M2),
        topology_checks=_get_bool(s, C.SK_TOPOLOGY, defauts["topology"]),
        check_overlaps=_get_bool(s, C.SK_OVERLAPS, defauts["overlaps"]),
        # Exceptions de chevauchement : cochées par défaut, et mémorisées comme
        # les autres cases une fois que l'utilisateur y a touché.
        allow_contained_polygons=_get_bool(
            s, C.SK_ALLOW_CONTAINED, defauts["allow_contained"]),
        ignore_missing_vertex_overlaps=_get_bool(
            s, C.SK_IGNORE_MISSING_VERTEX, defauts["ignore_missing_vertex"]),
        check_duplicates=_get_bool(s, C.SK_DUPLICATES, defauts["duplicates"]),
        check_small_polygons=_get_bool(
            s, C.SK_SMALL_POLYGONS, defauts["small"]),
        check_duplicate_vertex=_get_bool(
            s, C.SK_DUPLICATE_VERTEX, defauts["duplicate_vertex"]),
        check_holes=_get_bool(s, C.SK_HOLES, defauts["holes"]),
        hole_max_area_m2=_get_float(
            s, C.SK_HOLE_THRESHOLD, C.HOLE_THRESHOLD_DEFAULT_M2,
            C.HOLE_THRESHOLD_MIN_M2, C.HOLE_THRESHOLD_MAX_M2),
        check_nested_height=_get_bool(
            s, C.SK_NESTED_HEIGHT, defauts["nested_height"]),
        # Contrôles « Extras » : opt-in, toujours décochés à l'ouverture, quelle
        # que soit la session précédente (voir OPT_IN_CHECK_KEYS).
        check_sharp_angles=False,
        sharp_angle_min_deg=_get_float(
            s, C.SK_SHARP_ANGLE_DEG, C.SHARP_ANGLE_DEFAULT_DEG,
            C.SHARP_ANGLE_MIN_DEG, C.SHARP_ANGLE_MAX_DEG),
        check_close_vertices=False,
        close_vertex_min_dist_m=_get_float(
            s, C.SK_CLOSE_VERTEX_DIST, C.CLOSE_VERTEX_DEFAULT_M,
            C.CLOSE_VERTEX_MIN_M, C.CLOSE_VERTEX_MAX_M),
        check_line_crossings=False,
    )


def save_options(options: AnalysisOptions) -> None:
    """Enregistre les options d'analyse pour la prochaine session.

    L'ACTIVATION des contrôles opt-in (``OPT_IN_CHECK_KEYS``) n'est volontairement
    pas enregistrée : chaque session repart de l'état décoché. Leurs seuils, en
    revanche, sont mémorisés comme les autres réglages.
    """
    s = QgsSettings()
    s.setValue(_group_key(C.SK_THRESHOLD), float(options.small_polygon_threshold_m2))
    s.setValue(_group_key(C.SK_TOPOLOGY), bool(options.topology_checks))
    s.setValue(_group_key(C.SK_OVERLAPS), bool(options.check_overlaps))
    s.setValue(_group_key(C.SK_DUPLICATES), bool(options.check_duplicates))
    s.setValue(_group_key(C.SK_ALLOW_CONTAINED),
               bool(options.allow_contained_polygons))
    s.setValue(_group_key(C.SK_IGNORE_MISSING_VERTEX),
               bool(options.ignore_missing_vertex_overlaps))
    s.setValue(_group_key(C.SK_HOLE_THRESHOLD), float(options.hole_max_area_m2))
    s.setValue(_group_key(C.SK_SHARP_ANGLE_DEG), float(options.sharp_angle_min_deg))
    s.setValue(_group_key(C.SK_CLOSE_VERTEX_DIST), float(options.close_vertex_min_dist_m))
    # Contrôles sortis de l'opt-in en 2.9.49 : mémorisés comme les autres cases,
    # un décochage délibéré doit survivre à la fermeture du panneau.
    s.setValue(_group_key(C.SK_SMALL_POLYGONS), bool(options.check_small_polygons))
    s.setValue(_group_key(C.SK_HOLES), bool(options.check_holes))
    s.setValue(_group_key(C.SK_DUPLICATE_VERTEX),
               bool(options.check_duplicate_vertex))
    s.setValue(_group_key(C.SK_NESTED_HEIGHT), bool(options.check_nested_height))
    # Ménage : une activation mémorisée par une version antérieure ne doit pas
    # survivre dans le fichier de configuration.
    for key in OPT_IN_CHECK_KEYS:
        s.remove(_group_key(key))


def has_stored_options() -> bool:
    """Ce profil a-t-il déjà enregistré des options d'analyse ?

    ``save_options`` écrit ``SK_TOPOLOGY`` à chaque analyse : sa présence
    signifie que l'utilisateur a déjà lancé le plugin sur ce profil, donc que
    l'état restauré traduit des choix et non des valeurs par défaut.
    """
    return QgsSettings().value(_group_key(C.SK_TOPOLOGY), None) is not None


def load_last_dir() -> str:
    """Dernier dossier utilisé dans les dialogues de fichiers (vide si aucun)."""
    value = QgsSettings().value(_group_key(C.SK_LAST_DIR), "")
    return value if isinstance(value, str) else ""


def save_last_dir(path: str) -> None:
    """Mémorise le dossier d'un chemin de fichier pour la prochaine fois."""
    if not path:
        return
    import os
    directory = path if os.path.isdir(path) else os.path.dirname(path)
    if directory:
        QgsSettings().setValue(_group_key(C.SK_LAST_DIR), directory)


def load_overlap_tolerance_pct() -> float:
    """Dernière tolérance de rognage des chevauchements (en %)."""
    return _get_float(
        QgsSettings(), C.SK_OVERLAP_TOL, C.OVERLAP_TOLERANCE_DEFAULT_PCT,
        C.OVERLAP_TOLERANCE_MIN_PCT, C.OVERLAP_TOLERANCE_MAX_PCT)


def save_overlap_tolerance_pct(value_pct: float) -> None:
    QgsSettings().setValue(_group_key(C.SK_OVERLAP_TOL), float(value_pct))
