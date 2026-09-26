# -*- coding: utf-8 -*-
"""Constantes centralisées de STAT GEOM QC.

Regroupe en un seul point : identité du plugin, catégories d'anomalies,
libellés, pondérations du score, formats de fichiers, pilotes d'écriture,
clés de configuration persistante et messages utilisateur réutilisés.

Objectif : éviter la dispersion des « valeurs magiques » et des chaînes de
caractères dans les autres modules (UI, moteur, rapports, tâches).
"""

# ── Identité ──────────────────────────────────────────────────────────────
PLUGIN_TITLE = "STAT GEOM QC"
MENU_NAME = "STAT GEOM QC"


def _read_plugin_version() -> str:
    """Numéro de version tel que déclaré dans ``metadata.txt``.

    Lu à l'exécution plutôt que dupliqué en dur : source unique avec ce que
    plugins.qgis.org affiche, jamais désynchronisé d'un oubli de mise à jour.

    Cette fonction est appelée à l'IMPORT du module : une exception y empêcherait
    le chargement de tout le plugin. Elle absorbe donc n'importe quelle
    défaillance (fichier absent, illisible, encodage inattendu) et renvoie une
    chaîne vide — l'en-tête masque alors simplement l'étiquette de version.
    """
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("version="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        return ""
    return ""


PLUGIN_VERSION = _read_plugin_version()

# ── Catégories d'anomalies ──────────────────────────────────────────────────
# Clés stables utilisées dans AnalysisResult.flagged et dans toute l'UI.
CAT_NULL_EMPTY = "null_empty"
CAT_INVALID = "invalid"
CAT_SELF_INTERSECTION = "self_intersection"
CAT_OVERLAP = "overlap"
CAT_SMALL = "small"
CAT_DUPLICATE = "duplicate"
CAT_DUPLICATE_VERTEX = "duplicate_vertex"
CAT_HOLE = "hole"
CAT_MULTIPART = "multipart"
# ── Contrôles « Autres » (validation polygonale avancée) ──
CAT_SHARP_ANGLE = "sharp_angle"      # angles aigus / pointes de digitalisation
CAT_CLOSE_VERTEX = "close_vertex"    # sommets trop rapprochés (non superposés)
# Altitudes d'un polygone ENTIÈREMENT INCLUS dans un autre : le polygone inclus
# doit être strictement plus haut que celui qui le contient (superstructure sur
# un toit, corps de bâtiment sur une emprise). Contrôle attributaire, donc
# tributaire de la présence des champs — voir NESTED_HEIGHT_FIELDS.
CAT_NESTED_HEIGHT = "nested_height"
# Croisement ou contact entre deux ENTITÉS LIGNE DISTINCTES sans sommet commun
# au point de rencontre (croix, T) : le réseau n'est topologiquement pas
# connecté à cet endroit, même si le tracé se superpose visuellement. Réservé
# aux couches de lignes ; un croisement d'une ligne sur ELLE-MÊME n'est pas
# vérifié par ce contrôle (voir GeomAnalyzer._detect_unnoded_crossings).
CAT_UNNODED_CROSSING = "unnoded_crossing"
# Contrôle INTERNE non exposé dans l'interface : la cohérence AGL/AMSL est
# vérifiée silencieusement et ne ressort que sous forme d'une note discrète en
# fin de rapport (voir AnalysisResult.agl_note). Volontairement absent de
# ALL_CATEGORIES / GEOM_CATEGORIES / WEIGHTS / COMPONENT_LABELS.
CAT_AGL = "agl"

CATEGORY_LABELS = {
    CAT_NULL_EMPTY: "Nulle/Vide",
    CAT_INVALID: "Invalide",
    CAT_SELF_INTERSECTION: "Auto-intersection",
    CAT_OVERLAP: "Chevauchement",
    CAT_SMALL: "Petit polygone",
    CAT_DUPLICATE: "Doublon",
    CAT_DUPLICATE_VERTEX: "Sommets dupliqués",
    CAT_HOLE: "Trou",
    CAT_MULTIPART: "Multi-parties",
    CAT_SHARP_ANGLE: "Angle aigu",
    CAT_CLOSE_VERTEX: "Sommets rapprochés",
    CAT_NESTED_HEIGHT: "Hauteur imbriquée",
    CAT_UNNODED_CROSSING: "Croisement non nodé",
}

# Champs d'altitude comparés entre un polygone inclus et son contenant, dans cet
# ordre. La recherche est INSENSIBLE À LA CASSE (voir GeomAnalyzer._find_field) :
# « AGL », « agl » et « Agl » désignent le même champ. Chaque champ présent est
# comparé à son homologue — AGL avec AGL, HEIGHT avec HEIGHT — jamais l'un avec
# l'autre : ils ne mesurent pas la même chose.
NESTED_HEIGHT_FIELDS = ("AGL", "HEIGHT")

# Pictogrammes des contrôles (préfixes des libellés d'options).
# Symboles typographiques et non emoji : ils héritent de la couleur du texte et
# se rendent proprement en thème clair comme en thème sombre, là où un emoji
# s'affiche en monochrome et jure avec le reste du panneau.
ICON_SHARP_ANGLE = "∠"     # angle
ICON_CLOSE_VERTEX = "↔"    # distance entre deux sommets
# Flèche simple et non « ↥ » (U+21A5) : cette dernière est absente de Segoe UI
# et se rendait en glyphe de repli minuscule, illisible à côté de son libellé.
ICON_NESTED_HEIGHT = "↑"   # élévation attendue du polygone inclus
ICON_UNNODED_CROSSING = "⊗"  # croisement de lignes sans sommet commun

# Toutes les catégories (union pour la sélection d'entités).
ALL_CATEGORIES = (
    CAT_NULL_EMPTY, CAT_INVALID, CAT_SELF_INTERSECTION,
    CAT_OVERLAP, CAT_SMALL, CAT_DUPLICATE, CAT_DUPLICATE_VERTEX,
    CAT_HOLE, CAT_MULTIPART,
    CAT_SHARP_ANGLE, CAT_CLOSE_VERTEX, CAT_NESTED_HEIGHT,
    CAT_UNNODED_CROSSING,
)
# Catégories dont les entités possèdent une géométrie exploitable
# (pour la couche d'erreurs : les nulles/vides n'ont pas de géométrie).
GEOM_CATEGORIES = (
    CAT_INVALID, CAT_SELF_INTERSECTION, CAT_OVERLAP,
    CAT_SMALL, CAT_DUPLICATE, CAT_DUPLICATE_VERTEX,
    CAT_HOLE, CAT_MULTIPART,
    CAT_SHARP_ANGLE, CAT_CLOSE_VERTEX, CAT_NESTED_HEIGHT,
    CAT_UNNODED_CROSSING,
)

# ── Score de correctness ────────────────────────────────────────────────────
# Pondération des pénalités (proportion d'entités concernées -> pénalité).
# Les problèmes critiques (géométries nulles/vides/invalides) pèsent le plus.
WEIGHTS = {
    CAT_NULL_EMPTY: 1.00,
    CAT_INVALID: 1.00,
    CAT_OVERLAP: 0.70,
    CAT_DUPLICATE: 0.60,
    CAT_SMALL: 0.40,
    # Trou sous seuil : artefact de saisie probable (les grands trous, souvent
    # justifiés, sont exclus par le seuil et ne pénalisent donc pas le score).
    CAT_HOLE: 0.40,
    # Multi-parties : souvent volontaire selon le modèle de données, d'où une
    # pondération modérée.
    CAT_MULTIPART: 0.30,
    # Les vertex dupliqués sont une anomalie de propreté (« hygiène ») plutôt
    # qu'une erreur bloquante : pondération volontairement faible.
    CAT_DUPLICATE_VERTEX: 0.25,
    # Contrôles « Autres » : anomalies de forme/saisie, pénalités modérées.
    CAT_SHARP_ANGLE: 0.30,
    CAT_CLOSE_VERTEX: 0.20,
    # Hauteur d'un polygone inclus : une inversion d'altitude est une vraie
    # erreur de donnée (le volume inclus passerait sous celui qui le porte),
    # mais elle ne rend pas la géométrie inexploitable — même poids qu'un trou.
    CAT_NESTED_HEIGHT: 0.40,
    # Réseau non connecté à cet endroit précis : grave pour l'usage réseau
    # (routage, topologie), mais la géométrie de CHAQUE ligne prise seule reste
    # exploitable — d'où un poids proche de celui d'un chevauchement, sans
    # l'atteindre.
    CAT_UNNODED_CROSSING: 0.60,
}

# (seuil, libellé, couleur) évalués du plus haut au plus bas.
GRADE_BANDS = [
    (95.0, "Excellent", "#16a34a"),
    (85.0, "Bon", "#22c55e"),
    (70.0, "Correct", "#eab308"),
    (50.0, "Faible", "#f97316"),
    (0.0, "Critique", "#ef4444"),
]

# Grade rendu quand AUCUNE entité n'a été analysée (zone d'analyse vide, par
# exemple). Ce n'est ni une bonne ni une mauvaise note : il n'y en a pas. Sans
# ce cas à part, tous les compteurs à 0 donnaient 100/100 « Excellent » — un
# satisfecit délivré sans avoir rien regardé. Gris neutre à dessein : ni vert
# rassurant, ni rouge accusateur.
GRADE_NOT_EVALUATED = "Non évalué"
GRADE_NOT_EVALUATED_COLOR = "#94a3b8"

# Libellés des sous-scores (rapports HTML/Qt et barres de l'UI).
COMPONENT_LABELS = {
    CAT_NULL_EMPTY: "Géométries non nulles/vides",
    CAT_INVALID: "Géométries valides",
    CAT_OVERLAP: "Absence de chevauchement",
    CAT_DUPLICATE: "Unicité (doublons)",
    CAT_SMALL: "Surfaces > seuil",
    CAT_HOLE: "Absence de trous",
    CAT_MULTIPART: "Entités mono-partie",
    CAT_DUPLICATE_VERTEX: "Sommets sans doublon",
    CAT_SHARP_ANGLE: "Absence d'angles aigus",
    CAT_CLOSE_VERTEX: "Sommets bien espacés",
    CAT_NESTED_HEIGHT: "Hauteurs imbriquées cohérentes",
    CAT_UNNODED_CROSSING: "Croisements de lignes nodés",
}

# ── Fichiers ────────────────────────────────────────────────────────────────
# Filtre du sélecteur de fichier source.
SOURCE_FILE_FILTER = "GIS (*.shp *.tab *.geojson *.gpkg *.kml *.json);;Tous (*.*)"
# Filtres d'enregistrement (réparation).
REPAIR_FILE_FILTER = "GeoPackage (*.gpkg);;Shapefile (*.shp);;GeoJSON (*.geojson)"
# Filtres d'export de rapport.
EXPORT_FILTERS = {"html": "HTML (*.html)", "pdf": "PDF (*.pdf)"}

# Pilote d'écriture OGR selon l'extension du fichier de sortie.
DRIVER_BY_EXT = {
    ".shp": "ESRI Shapefile",
    ".gpkg": "GPKG",
    ".geojson": "GeoJSON",
    ".json": "GeoJSON",
    ".kml": "KML",
    ".tab": "MapInfo File",
}
DEFAULT_DRIVER = "GPKG"
WRITABLE_EXTENSIONS = (".gpkg", ".shp", ".geojson", ".json")

# Noms d'attributs que les pilotes OGR réservent à la clé primaire (« fid » pour
# GeoPackage, « ogc_fid » pour les bases SQLite/PostGIS). Un attribut source
# portant l'un de ces noms est repris comme identifiant unique à l'écriture : dès
# que la réparation renumérote les entités (éclatement des multi-parties, qui
# duplique l'identifiant d'origine), l'écriture échoue en bloc sur
# « UNIQUE constraint failed ». Ces attributs sont donc renommés dans la couche
# réparée — la valeur d'origine est conservée, seul le nom change.
RESERVED_FID_FIELDS = ("fid", "ogc_fid")
RESERVED_FID_SUFFIX = "_src"

# Nombre maximal d'entités écrites dans une couche d'erreurs mémoire.
ERROR_LAYER_CAP = 50000
# Symbole appliqué à la couche d'erreurs, pris dans la bibliothèque de styles
# QGIS plutôt que recopié : c'est l'entrée que l'utilisateur voit dans la
# symbologie de la couche.
ERROR_SYMBOL_NAME = "dot red"

# ── Couche d'erreurs : symbologie catégorisée par type d'anomalie ───────────
# Le champ ``qc_error`` porte la LISTE des anomalies d'un point (« Chevauchement,
# Doublon ») : catégoriser dessus produirait une classe par combinaison, donc une
# légende illisible. Un champ dédié porte donc UNE catégorie, la plus grave, et
# c'est lui qui sert de clé au rendu catégorisé.
ERROR_CLASS_FIELD = "qc_class"

# Ordre de gravité DÉCROISSANTE, qui décide de la catégorie retenue quand un
# point cumule plusieurs anomalies. L'auto-intersection passe avant l'invalidité
# générique : c'est le même défaut, mais nommé précisément — dire « invalide » à
# la place perdrait l'information la plus utile pour corriger.
CATEGORY_PRIORITY = (
    CAT_NULL_EMPTY,
    CAT_SELF_INTERSECTION,
    CAT_INVALID,
    CAT_UNNODED_CROSSING,
    CAT_OVERLAP,
    CAT_NESTED_HEIGHT,
    CAT_DUPLICATE,
    CAT_SMALL,
    CAT_HOLE,
    CAT_MULTIPART,
    CAT_SHARP_ANGLE,
    CAT_DUPLICATE_VERTEX,
    CAT_CLOSE_VERTEX,
)

# Trois familles de couleurs, pour que la carte se lise sans consulter la
# légende : ROUGE = la géométrie est fautive ou en conflit avec une voisine,
# JAUNE = la forme est suspecte mais la géométrie reste exploitable, CYAN = le
# défaut est au niveau des sommets, sans effet sur la forme visible. Les nuances
# à l'intérieur d'une famille restent distinguables l'une de l'autre.
ERROR_COLORS = {
    CAT_NULL_EMPTY: "120,10,15",
    CAT_SELF_INTERSECTION: "168,20,40",
    CAT_INVALID: "214,25,28",
    CAT_UNNODED_CROSSING: "232,60,140",
    CAT_OVERLAP: "240,85,45",
    CAT_NESTED_HEIGHT: "196,30,105",
    CAT_DUPLICATE: "228,150,15",
    CAT_SMALL: "246,182,20",
    CAT_HOLE: "250,208,62",
    CAT_MULTIPART: "203,146,42",
    CAT_SHARP_ANGLE: "236,226,86",
    CAT_DUPLICATE_VERTEX: "18,178,190",
    CAT_CLOSE_VERTEX: "76,206,214",
}

# Points de chevauchement mémorisés pendant l'analyse (localisation exacte de
# chaque intersection, voir AnalysisResult.overlap_points). Double plafond pour
# borner la mémoire sur une couche très fautive : par entité, et au total.
OVERLAP_POINTS_PER_FEATURE = 25
OVERLAP_POINTS_TOTAL_CAP = ERROR_LAYER_CAP

# ── Zone d'analyse (AOI) ────────────────────────────────────────────────────
# Rectangle tracé sur le canevas (ou étendue visible reprise telle quelle) qui
# RESTREINT l'analyse. Facultatif : sans AOI, toute la couche est analysée,
# exactement comme avant.
#
# Règle d'appartenance : une entité est dans la zone dès qu'elle INTERSECTE
# l'AOI, même partiellement. Un bâtiment à cheval sur le bord est donc analysé
# EN ENTIER — le contraire laisserait les entités de bordure contrôlées par
# personne lors d'un découpage en tuiles.
AOI_COLOR = "0,150,255"
AOI_FILL_ALPHA = 40             # remplissage très léger : la donnée reste lisible
AOI_WIDTH = 2
# Aucune persistance volontairement : un AOI tracé la semaine dernière et
# restauré en silence restreindrait une analyse sans que rien ne le dise. Il
# est donc oublié à la fermeture du panneau (voir _restore_settings).

# Croisements de lignes : tolérance (unités de la couche) pour décider si un
# point d'intersection GEOS coïncide EXACTEMENT avec un sommet existant des
# deux lignes. Fixe et minuscule à dessein — le point d'un vrai croisement non
# nodé s'écarte de tout sommet de bien plus que l'imprécision flottante,
# jamais d'aussi peu (voir _is_vertex_of).
LINE_CROSSING_VERTEX_TOLERANCE = 1e-9

# ── Bornes des paramètres (règles de validation UI) ─────────────────────────
THRESHOLD_MIN_M2 = 0.0
THRESHOLD_MAX_M2 = 1_000_000.0
THRESHOLD_DEFAULT_M2 = 2.0
OVERLAP_TOLERANCE_MIN_PCT = 0.1
OVERLAP_TOLERANCE_MAX_PCT = 50.0
OVERLAP_TOLERANCE_DEFAULT_PCT = 5.0

# ── Chevauchements : aucune tolérance ────────────────────────────────────────
# La règle est purement topologique — « intersects et non touches », celle de
# GeoVectorQualityControl : le moindre recouvrement d'intérieurs est une erreur,
# un contact de frontière n'en est pas une. Aucun seuil métrique n'intervient,
# donc aucune constante de tolérance ici (voir classify_overlap_detailed).

# Un recouvrement n'est imputé à un « sommet manquant » que si la zone commune
# est ALLONGÉE : rapport largeur/longueur ≤ ce seuil, mesuré sur son rectangle
# englobant orienté. 0,25 signifie « au moins quatre fois plus longue que
# profonde » — la zone longe le mur au lieu de mordre dans le bâti.
#
# Valeur FIXE, volontairement pas exposée à l'utilisateur : l'option se résume
# donc à une case à cocher. Le réglage n'apportait rien de décidable — repères
# mesurés (banc d'essai) : pointe de 30 cm sur 2 m de mur = 0,15 ; pointe de 2 m
# sur 2 m = 0,80 ; morsure compacte de 1 m × 1 m = 1,00. 0,25 sépare nettement la
# lame de saisie de la vraie morsure, sans zone grise à arbitrer.
MISSING_VERTEX_RATIO_DEFAULT = 0.25
# Trous : seuil de surface AU-DESSOUS duquel un trou est jugé fautif. Les trous
# plus grands sont considérés comme justifiés (cour intérieure, îlot, etc.).
HOLE_THRESHOLD_MIN_M2 = 0.0
HOLE_THRESHOLD_MAX_M2 = 1_000_000.0
# Un trou de moins de 2 m² relève de la saisie (clic parasite, anneau résiduel) ;
# au-delà, il s'agit presque toujours d'un vide légitime — cour, îlot, plan
# d'eau. Le seuil reste réglable, et le contrôle décoché par défaut.
HOLE_THRESHOLD_DEFAULT_M2 = 2.0
# Défaut des versions 2.9.6 à 2.9.8, migré une seule fois (voir settings).
HOLE_THRESHOLD_LEGACY_DEFAULT_M2 = 10.0
# Sommets dupliqués : STRICTEMENT identiques uniquement, aucune tolérance
# réglable (supprimée en 2.9.21 — voir settings.migrate_settings, version 6).
# Angles aigus : angle intérieur minimal toléré, en degrés.
SHARP_ANGLE_MIN_DEG = 1.0
SHARP_ANGLE_MAX_DEG = 90.0
SHARP_ANGLE_DEFAULT_DEG = 30.0
# Sommets trop rapprochés : distance minimale attendue entre deux sommets
# consécutifs (les sommets STRICTEMENT superposés relèvent du contrôle
# « sommets dupliqués », d'où une distance strictement positive).
CLOSE_VERTEX_MIN_M = 0.001
CLOSE_VERTEX_MAX_M = 100.0
CLOSE_VERTEX_DEFAULT_M = 0.20

# ── Clés de configuration persistante (QgsSettings) ─────────────────────────
SETTINGS_GROUP = "stat_geom_qc"
SK_THRESHOLD = "threshold_m2"
SK_TOPOLOGY = "check_topology"
SK_OVERLAPS = "check_overlaps"
SK_ALLOW_CONTAINED = "allow_contained_polygons"
SK_DUPLICATES = "check_duplicates"
SK_SMALL_POLYGONS = "check_small_polygons"
SK_DUPLICATE_VERTEX = "check_duplicate_vertex"
# Ancienne tolérance de quasi-doublon, supprimée en 2.9.21 : la clé n'est plus
# lue, seulement effacée par la migration 6.
SK_VERTEX_TOL_LEGACY = "duplicate_vertex_tolerance_m"
SK_HOLES = "check_holes"
SK_HOLE_THRESHOLD = "hole_max_area_m2"
# Contrôle « polygones filiformes », supprimé en 2.9.21 : ces deux clés ne
# sont plus lues, seulement effacées par la migration 7.
# Contrôle « bâtiments » (AGL/AMSL) devenu interne en 2.7 : plus de case à
# cocher, la clé n'est plus lue, seulement effacée par la migration 8.
SK_BUILDINGS_LEGACY = "check_buildings"
SK_SLIVERS_LEGACY = "check_slivers"
SK_SLIVER_RATIO_LEGACY = "sliver_ratio_max"
SK_NESTED_HEIGHT = "check_nested_height"
SK_LINE_CROSSINGS = "check_line_crossings"
SK_SHARP_ANGLES = "check_sharp_angles"
SK_SHARP_ANGLE_DEG = "sharp_angle_min_deg"
SK_CLOSE_VERTICES = "check_close_vertices"
SK_CLOSE_VERTEX_DIST = "close_vertex_min_dist_m"
SK_OVERLAP_TOL = "overlap_tolerance_pct"
# Ancienne tolérance d'accrochage, supprimée en 2.9.19 : la clé n'est plus lue,
# seulement effacée par la migration 4 pour ne pas laisser de résidu.
SK_OVERLAP_MIN_WIDTH_LEGACY = "overlap_min_width_m"
SK_IGNORE_MISSING_VERTEX = "ignore_missing_vertex_overlaps"
# Ancien seuil de forme réglable, supprimé en 2.9.20 : la clé n'est plus lue,
# seulement effacée par la migration 5.
SK_MISSING_VERTEX_RATIO_LEGACY = "missing_vertex_max_ratio"
SK_LAST_DIR = "last_dir"           # dernier dossier utilisé dans les dialogues
SK_SETTINGS_VERSION = "settings_version"   # sert aux migrations de réglages
# Rappel ajouté à l'infobulle de chaque contrôle facultatif : son activation
# n'est pas mémorisée d'une session à l'autre (voir settings.OPT_IN_CHECK_KEYS).
TIP_OPT_IN_RESET = ("\nContrôle facultatif : il repart décoché à chaque "
                    "ouverture du panneau.")
# Numéro de schéma des réglages persistants. 2 = nouveau seuil de trou par
# défaut (2 m² au lieu de 10 m²). 3 = les activations de contrôles opt-in ne
# sont plus mémorisées (voir settings.OPT_IN_CHECK_KEYS). 4 = la tolérance
# d'accrochage des chevauchements est supprimée, sa clé est effacée. 5 = idem
# pour le seuil de forme du « sommet manquant », désormais fixe. 6 = idem pour
# la tolérance de quasi-doublon des sommets, sommets STRICTEMENT identiques
# uniquement désormais. 7 = le contrôle « polygones filiformes » est retiré.
# 8 = seconde passe sur le seuil de trou (les profils déjà au-delà de la
# version 2 avaient gardé 10 m² sans jamais voir le défaut de 2 m²), et
# nettoyage de la clé orpheline du contrôle « bâtiments ».
# 9 = les deux exceptions de chevauchement passent à « cochées par défaut ».
# Les profils qui en gardaient une valeur enregistrée — « false », écrit par
# une version où elles étaient décochées — n'auraient jamais vu ce défaut :
# leurs clés sont effacées une fois, comme au passage du seuil de trou.
# 10 = les préréglages disparaissent au profit d'un mode unique où TOUTE la
# section « Options d'analyse » est cochée. Quatre contrôles quittent donc
# l'opt-in (petits polygones, trous, vertex dupliqués, hauteur des polygones
# inclus) : leurs clés sont effacées une fois pour que le nouveau défaut se
# voie, puis elles sont mémorisées comme les autres cases.
SETTINGS_VERSION = 10


def fmt_int(value) -> str:
    """Formate un entier avec séparateur de milliers français (espace insécable).

    Exemple : 12345 -> « 12 345 ». Toute valeur non entière est rendue telle
    quelle (repli sûr).
    """
    try:
        return format(int(value), ",").replace(",", " ")
    except (TypeError, ValueError):
        return str(value)

# ── Messages utilisateur réutilisés ─────────────────────────────────────────
MSG_NO_LAYER = "Sélectionnez d'abord une couche vectorielle."
MSG_NO_RESULT = "Lancez d'abord une analyse."
MSG_LAYER_INVALID = "La couche sélectionnée est invalide ou illisible."
MSG_LAYER_NO_GEOM = "La couche ne contient pas de géométrie (couche attributaire seule)."
MSG_LAYER_EMPTY = "La couche ne contient aucune entité à analyser."
MSG_LAYER_MISMATCH = (
    "La couche sélectionnée ne correspond pas au dernier résultat.\n"
    "Relancez l'analyse sur cette couche."
)
MSG_BUSY = "Une analyse est déjà en cours. Attendez la fin ou annulez-la."
MSG_HINT_START = "Sélectionnez une couche puis lancez l'analyse."
MSG_AOI_NONE = "Aucune zone — toute la couche est analysée."
MSG_AOI_DRAW_HINT = (
    "Tracez le rectangle sur la carte par cliquer-glisser. "
    "Échap annule."
)
MSG_AOI_EMPTY = (
    "La zone tracée ne contient aucune entité de cette couche.\n\n"
    "Élargissez la zone, ou décochez « Restreindre à une zone » pour "
    "analyser toute la couche."
)
MSG_AOI_NO_CANVAS = (
    "Le tracé de zone demande un canevas de carte. "
    "Utilisez « Étendue visible » ou décochez la restriction."
)

# ── Réglages par défaut du panneau ───────────────────────────────────────────
# Les trois préréglages « Rapide / Standard / Approfondi » ont été RETIRÉS en
# 2.9.49. Ils demandaient à l'utilisateur de choisir un mode avant même d'avoir
# vu la donnée, et deux d'entre eux n'existaient que pour retirer des contrôles.
# Il n'y a plus qu'un mode, sans nom affiché : tous les contrôles de la section
# « Options d'analyse » sont cochés, aucun de ceux d'« Extras » ne l'est. Qui
# veut moins décoche, ce qui est un geste plus direct que de chercher quel
# préréglage contient quoi.
#
# Dimensions d'analyse ACTIVES PAR DÉFAUT (clés de AnalysisResult.checks_enabled).
#
# Sert à la mention accolée au score (voir report.skipped_note) : seule
# l'absence d'un de ces contrôles réduit la couverture de façon notable et
# mérite d'être signalée. Les contrôles d'« Extras » (angles aigus, sommets
# rapprochés, croisements de lignes) restent décochés dans le cas courant : les
# mentionner à chaque analyse produirait un avertissement permanent, donc
# ignoré — l'inverse du but recherché.
#
# ``nested_height`` est coché par défaut dans le panneau mais reste ABSENT
# d'ici, et ce n'est pas un oubli : il ne peut tourner que si la couche porte
# AGL ou HEIGHT, si bien qu'une couche sans ces champs — le cas courant hors
# bâti 3D — afficherait « 1 contrôle non exécuté » à CHAQUE analyse, sans que
# rien ne puisse y remédier. C'est exactement l'avertissement permanent que
# cette liste cherche à éviter. Son absence reste consignée honnêtement par la
# ligne de couverture (voir report.coverage_text) et par le tableau du rapport,
# qui écrit « Non demandé » en toutes lettres.
CHECKS_ON_BY_DEFAULT = (
    "self_intersection", "multipart", "overlap", "duplicate",
    "small", "duplicate_vertex", "hole",
)

# Les deux EXCEPTIONS du bloc chevauchements ne sont pas des contrôles : elles
# retirent une famille de cas des erreurs signalées.
#
# Actives par défaut depuis la 2.9.31. Sur du bâti mitoyen réel, ces deux
# familles dominent largement : un jeu de 58 000 bâtiments donne 1 177 paires en
# recouvrement, dont 1 139 imputables à un sommet manquant et 10 à une
# imbrication — soit 28 vrais conflits noyés dans 97 % de bruit d'accrochage.
# Les laisser décochées revenait à livrer par défaut une liste d'erreurs
# inexploitable. Décocher reste à un clic pour qui veut tout voir.
OVERLAP_EXCEPTIONS_ON_BY_DEFAULT = True

# État par défaut de CHAQUE case du panneau — source unique, lue par
# ``settings.load_options`` (profil vierge) et par « ↺ Réinitialiser »
# (``StatGeomDockWidget._reset_options``).
#
# Les quatre premières lignes sont la section « Options d'analyse » : tout y est
# coché. La section « Extras » suit : rien n'y est coché, ces contrôles ne
# s'appliquent qu'à certains types de données (saisie sur-densifiée, réseaux
# linéaires) et leur activation reste un geste explicite, non mémorisé d'une
# session à l'autre (voir settings.OPT_IN_CHECK_KEYS).
#
# ``AnalysisOptions()`` reste volontairement plus conservateur (rien d'opt-in
# coché) : c'est le défaut des usages PROGRAMMATIQUES — Processing, scripts —
# où personne ne voit de case à cocher et où un contrôle non demandé ne doit pas
# s'exécuter en silence. Ici, au contraire, les cases sont sous les yeux.
UI_DEFAULT_CHECKS = {
    # Options d'analyse
    "topology": True,
    "overlaps": True,
    "duplicates": True,
    "duplicate_vertex": True,
    # Détecter n'est pas corriger : la suppression des petits polygones et le
    # comblement des trous restent des cases du dialogue de réparation, jamais
    # implicites.
    "small": True,
    "holes": True,
    # Contrôle attributaire : il ne tourne que si la couche porte AGL ou HEIGHT,
    # et le signale sinon. Le cocher par défaut ne coûte donc rien sur une
    # couche qui n'a pas ces champs.
    "nested_height": True,
    # Les deux exceptions de chevauchement (voir ci-dessus).
    "ignore_missing_vertex": OVERLAP_EXCEPTIONS_ON_BY_DEFAULT,
    "allow_contained": OVERLAP_EXCEPTIONS_ON_BY_DEFAULT,
    # Extras
    "sharp_angles": False,
    "close_vertices": False,
    "line_crossings": False,
}
