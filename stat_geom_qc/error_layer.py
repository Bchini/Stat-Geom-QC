# -*- coding: utf-8 -*-
"""Construction de la couche de POINTS d'erreur, et sa symbologie.

Pourquoi ce module existe
-------------------------
Cette logique vivait entièrement dans le panneau, mêlée à des boîtes de dialogue
et à un ajout au projet courant. Elle est pourtant purement géométrique, et deux
appelants en ont désormais besoin : le panneau, qui charge la couche dans le
projet et la stylise, et les algorithmes Processing, qui écrivent le résultat
dans une sortie sans jamais toucher au projet ni afficher quoi que ce soit.

Aucune fonction d'ici n'affiche de message, n'ajoute de couche au projet, ni ne
lit ``QgsProject.instance()``. C'est ce qui les rend utilisables depuis un thread
de fond Processing, où aucune de ces trois choses n'est permise — et sous
``qgis_process``, où il n'y a même pas de projet ouvert.

Localisation des points
-----------------------
Un point d'erreur ne doit pas tomber « quelque part sur l'entité » : il doit
désigner l'endroit à corriger. Trois cas, par ordre de précision décroissante :

  1. invalidité / auto-intersection -> un point sur CHAQUE nœud fautif signalé
     par GEOS, c'est-à-dire la position exacte du défaut ;
  2. chevauchement -> un point DANS la surface d'intersection, relevé pendant
     l'analyse, jamais le centre du polygone (qui peut être à des dizaines de
     mètres du conflit) ;
  3. tout le reste (petit polygone, doublon, trou, multi-parties, sommets) -> un
     point représentatif garanti SUR l'entité, l'anomalie portant sur l'entité
     entière.
"""

import contextlib
import os

from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsGeometry,
    QgsMarkerSymbol,
    QgsPalLayerSettings,
    QgsPointXY,
    QgsRendererCategory,
    QgsStyle,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtGui import QColor

from . import constants as C


def _field_types():
    """Types de champ acceptés par ``QgsField`` sur CETTE version de QGIS.

    ``QMetaType`` est la voie recommandée — ``QVariant.Type`` est déprécié côté
    QGIS, et absent de PyQt6 — mais la surcharge ``QgsField(str, QMetaType.Type)``
    n'existe qu'à partir de QGIS 3.38, alors que le plugin annonce 3.16+.

    On ne peut pas trancher sur le simple import de ``QMetaType`` : il réussit
    très bien sous PyQt5, y compris sur les QGIS où la surcharge n'existe pas. On
    construit donc réellement un champ pour savoir, ce qui reste juste quelle que
    soit la version d'introduction — plutôt que de coder en dur un numéro de
    version qu'il faudrait maintenir.

    Sans cette détection, chaque champ créé émet un ``DeprecationWarning`` : sans
    conséquence dans l'interface, mais qui pollue la sortie de ``qgis_process``,
    laquelle a vocation à être lue par un script.
    """
    from qgis.PyQt.QtCore import QVariant
    try:
        from qgis.PyQt.QtCore import QMetaType
        QgsField("sonde", QMetaType.Type.LongLong)
    except Exception:  # pragma: no cover - QGIS < 3.38 ou Qt sans QMetaType
        return (QVariant.LongLong, QVariant.String)
    return (QMetaType.Type.LongLong, QMetaType.Type.QString)


FIELD_TYPE_LONGLONG, FIELD_TYPE_STRING = _field_types()

# Champs de la couche d'erreurs, dans l'ordre. ``qc_error`` porte la LISTE
# lisible des anomalies du point ; ``qc_class`` n'en porte qu'UNE, la plus grave,
# et sert de clé au rendu catégorisé — classer sur la liste produirait une classe
# par combinaison d'anomalies, donc une légende ingérable.
def error_layer_fields():
    """Champs de la couche d'erreurs, reconstruits à chaque appel.

    Renvoyer une liste neuve évite qu'un appelant modifie par inadvertance la
    définition partagée par les autres.
    """
    return [
        QgsField("src_fid", FIELD_TYPE_LONGLONG),
        QgsField("qc_error", FIELD_TYPE_STRING),
        QgsField(C.ERROR_CLASS_FIELD, FIELD_TYPE_STRING),
        QgsField("qc_detail", FIELD_TYPE_STRING),
    ]


def primary_category(cats):
    """Catégorie RETENUE quand un point cumule plusieurs anomalies.

    La plus grave l'emporte, selon un ordre fixe et documenté
    (``C.CATEGORY_PRIORITY``) : un choix déterministe vaut mieux qu'un « premier
    arrivé » qui dépendrait de l'ordre d'itération des catégories, et ferait
    changer la couleur d'une même donnée d'une version à l'autre.

    Une catégorie inconnue de l'ordre ne fait pas échouer le rendu : elle est
    prise en dernier recours, telle quelle.
    """
    if not cats:
        return ""
    for cle in C.CATEGORY_PRIORITY:
        if cle in cats:
            return cle
    return cats[0]


def _first_validation_node(geom):
    """Nœuds d'erreur signalés par GEOS, ou liste vide.

    Best-effort : une géométrie que le validateur refuse d'analyser ne doit pas
    priver l'utilisateur des autres points d'erreur du lot.
    """
    erreurs = []
    with contextlib.suppress(Exception):
        erreurs = list(geom.validateGeometry())
    sorties = []
    for err in erreurs:
        with contextlib.suppress(Exception):
            if err.hasWhere():
                sorties.append((QgsGeometry.fromPointXY(QgsPointXY(err.where())),
                                err.what() or ""))
    return sorties


def _representative_point(geom):
    """Point garanti SUR l'entité (``pointOnSurface``, sinon centroïde).

    Le centroïde seul ne suffit pas : sur une forme concave ou multi-parties, il
    peut tomber en dehors de l'entité qu'il est censé désigner.
    """
    for nom in ("pointOnSurface", "centroid"):
        with contextlib.suppress(Exception):
            pt = getattr(geom, nom)()
            if pt is not None and not pt.isNull() and not pt.isEmpty():
                return pt
    return None


def build_error_features(layer, result, cap=None, max_per_feature=None):
    """Entités ponctuelles localisant chaque anomalie de ``result``.

    Renvoie ``(features, tronque)``. ``tronque`` dit si le plafond a mordu : un
    appelant doit pouvoir l'ANNONCER, une troncature muette se lisant comme une
    liste exhaustive.

    Une anomalie de PAIRE (chevauchement de polygones, croisement de lignes non
    node) produit UN SEUL point, portant les identifiants des deux entites :
    c'est un seul defaut, a un seul endroit. Le moteur en garde la trace des
    deux cotes pour la surbrillance, mais l'inventaire ne doit pas le compter
    deux fois.

    Ne crée aucune couche et n'affiche rien : c'est l'appelant qui décide quoi
    faire du résultat (couche mémoire pour le panneau, sortie Processing pour un
    algorithme).
    """
    if cap is None:
        cap = C.ERROR_LAYER_CAP
    if max_per_feature is None:
        max_per_feature = C.OVERLAP_POINTS_PER_FEATURE

    fid_to_errors = {}
    fid_to_cats = {}
    for cat in C.GEOM_CATEGORIES:
        for fid in result.flagged.get(cat, []):
            fid_to_errors.setdefault(fid, []).append(
                C.CATEGORY_LABELS.get(cat, cat))
            fid_to_cats.setdefault(fid, []).append(cat)
    if not fid_to_errors:
        return ([], False)

    validity_fids = (set(result.flagged.get(C.CAT_INVALID, []))
                     | set(result.flagged.get(C.CAT_SELF_INTERSECTION, [])))
    overlap_points = getattr(result, "overlap_points", None) or {}

    champs = error_layer_fields()
    # Un QgsFields est nécessaire pour construire des QgsFeature typées.
    from qgis.core import QgsFields
    fields = QgsFields()
    for f in champs:
        fields.append(f)

    def point_feature(point_geom, fid, labels, classe, detail):
        nf = QgsFeature(fields)
        nf.setGeometry(point_geom)
        nf.setAttributes([fid, labels, classe, detail])
        return nf

    feats = []
    # Troncature RÉELLE, et non « le compte a atteint le plafond » : tomber
    # pile sur le plafond sans avoir rien laissé de côté annonçait une liste
    # écourtée qui était en fait complète — un doute inutile jeté sur un
    # inventaire d'anomalies exact.
    tronque = False
    # Paires deja localisees, sous forme (fid_min, fid_max) : un chevauchement
    # entre A et B est UNE anomalie, a un seul endroit. Le moteur en garde la
    # trace des deux cotes (voir GeomAnalyzer._detect_overlaps.remember), ce
    # dont la surbrillance a besoin ; la couche d'erreurs, elle, ne doit pas
    # empiler deux points superposes pour le meme defaut.
    paires_vues = set()
    request = QgsFeatureRequest().setFilterFids(list(fid_to_errors.keys()))
    for src_feat in layer.getFeatures(request):
        if len(feats) >= cap:
            tronque = True
            break
        if not src_feat.hasGeometry():
            continue
        geom = src_feat.geometry()
        if geom is None or geom.isNull() or geom.isEmpty():
            continue
        fid = src_feat.id()
        labels = ", ".join(fid_to_errors.get(fid, []))
        classe = primary_category(fid_to_cats.get(fid, []))

        localises = 0
        # 1) Nœuds d'erreur exacts pour les problèmes de validité.
        if fid in validity_fids:
            for pt_geom, detail in _first_validation_node(geom)[:max_per_feature]:
                if len(feats) >= cap:
                    tronque = True
                    break
                feats.append(point_feature(pt_geom, fid, labels, classe, detail))
                localises += 1

        # 2) Lieu exact de chaque chevauchement ou croisement de ligne non nodé,
        #    avec le FID du voisin en cause : c'est là qu'il faut regarder pour
        #    corriger la saisie. ``classe`` (et non une catégorie fixée en dur)
        #    car ce même dictionnaire porte aussi bien des chevauchements de
        #    polygones que des croisements de lignes (voir
        #    GeomAnalyzer._detect_unnoded_crossings) — les confondre étiquetterait
        #    un croisement de lignes comme un chevauchement.
        for x, y, other_fid in overlap_points.get(fid, []):
            paire = (min(fid, other_fid), max(fid, other_fid))
            if paire in paires_vues:
                # Le partenaire a deja porte le point de cette paire. On compte
                # quand meme l'entite comme LOCALISEE, sinon l'etape 3 lui
                # ajouterait un point representatif et le doublon reviendrait
                # par la fenetre.
                localises += 1
                continue
            if len(feats) >= cap:
                tronque = True
                break
            paires_vues.add(paire)
            feats.append(point_feature(
                QgsGeometry.fromPointXY(QgsPointXY(x, y)), fid, labels,
                classe,
                "entités %s et %s" % (paire[0], paire[1])))
            localises += 1

        # 3) Sinon, point représentatif : l'anomalie porte sur l'entité entière.
        if localises == 0:
            if len(feats) >= cap:
                tronque = True
            else:
                pt_geom = _representative_point(geom)
                if pt_geom is not None:
                    feats.append(point_feature(pt_geom, fid, labels, classe, ""))

    return (feats, tronque)


def build_error_layer(layer, result, cap=None):
    """Couche MÉMOIRE de points d'erreur, ou ``None`` s'il n'y a rien à placer.

    Le CRS est posé sur l'objet couche et pas seulement passé dans l'URI : celle
    -ci ne sait transporter qu'un code d'autorité, et un CRS lu depuis un WKT que
    PROJ ne rattache à aucun EPSG a un ``authid()`` vide — la couche héritait
    alors silencieusement d'EPSG:4326, soit des coordonnées de grille locale
    écrites comme du WGS 84.

    Renvoie ``(couche, tronque)``. N'ajoute rien au projet et ne stylise pas :
    voir ``style_error_layer``.
    """
    feats, tronque = build_error_features(layer, result, cap=cap)
    if not feats:
        return (None, False)

    authid = layer.crs().authid()
    uri = "Point" + ("?crs=%s" % authid if authid else "")
    mem = QgsVectorLayer(uri, "QC erreurs — %s" % layer.name(), "memory")
    pr = mem.dataProvider()
    if not mem.isValid() or pr is None:
        return (None, False)
    if layer.crs().isValid():
        mem.setCrs(layer.crs())
    pr.addAttributes(error_layer_fields())
    mem.updateFields()
    pr.addFeatures(feats)
    mem.updateExtents()
    # Un point d'erreur refusé par le fournisseur disparaît de l'inventaire
    # sans rien dire : la couche se lirait comme la liste EXHAUSTIVE des
    # anomalies. Le signaler par le même drapeau que la troncature — c'est le
    # même sens pour l'utilisateur : « cette couche n'est pas complète ».
    if mem.featureCount() < len(feats):
        tronque = True
    return (mem, tronque)


# ── Symbologie ──────────────────────────────────────────────────────────────

def error_symbol():
    """Symbole « dot red » de la bibliothèque QGIS, ou son équivalent.

    On prend le symbole tel que QGIS le définit plutôt que d'en recopier les
    valeurs : l'utilisateur retrouve exactement l'entrée de ses styles, et le
    jour où elle change avec QGIS, la couche d'erreurs suit. Une bibliothèque
    amputée ou renommée ne doit pas pour autant laisser la couche sans
    symbologie, d'où le repli sur un cercle rouge de mêmes teintes.
    """
    with contextlib.suppress(Exception):
        symbole = QgsStyle.defaultStyle().symbol(C.ERROR_SYMBOL_NAME)
        if symbole is not None:
            return symbole
    return QgsMarkerSymbol.createSimple({
        "name": "circle",
        "color": "219,30,42,255",
        "size": "4",
        "outline_color": "128,17,25,255",
        "outline_width": "0.4",
    })


def category_symbol(cle):
    """Symbole d'une catégorie, décliné depuis « dot red ».

    On repart du symbole de la bibliothèque pour garder exactement sa forme et sa
    taille, et on n'en change que la couleur : la couche reste cohérente avec les
    autres styles de l'utilisateur, et les catégories ne diffèrent que par ce qui
    doit les distinguer.
    """
    symbole = error_symbol()
    couleur = C.ERROR_COLORS.get(cle)
    if symbole is not None and couleur:
        with contextlib.suppress(Exception):
            r, v, b = (int(x) for x in couleur.split(","))
            symbole.setColor(QColor(r, v, b))
    return symbole


def categorized_renderer(mem, classes=None):
    """Rendu catégorisé sur ``qc_class``, une classe par type d'anomalie.

    Ne déclare QUE les catégories réellement présentes : une légende de onze
    entrées dont neuf vides serait plus difficile à lire que le symbole unique
    qu'elle remplace. L'ordre suit la gravité, si bien que la légende se lit du
    plus grave au plus bénin.

    ``classes`` impose la liste au lieu de la lire dans la couche. Indispensable
    pour fabriquer un STYLE sans données : la couche sonde de
    ``write_style_sidecar`` est vide, donc sans cette surcharge le rendu
    retombait sur le symbole unique et le fichier rouvert dans QGIS s'affichait
    en points gris uniformes.

    Renvoie ``None`` si le rendu ne peut pas être construit — l'appelant garde
    alors le symbole unique, plutôt qu'une couche sans symbologie.
    """
    index = mem.fields().indexOf(C.ERROR_CLASS_FIELD)
    if index < 0:
        return None
    presentes = set(classes) if classes else mem.uniqueValues(index)
    classes = [c for c in C.CATEGORY_PRIORITY if c in presentes]
    # Une valeur inattendue ne doit pas devenir invisible sur la carte.
    classes += sorted(str(v) for v in presentes
                      if v and str(v) not in classes
                      and str(v) not in C.CATEGORY_PRIORITY)
    if not classes:
        return None
    categories = []
    for cle in classes:
        symbole = category_symbol(cle)
        if symbole is None:
            continue
        categories.append(QgsRendererCategory(
            cle, symbole, C.CATEGORY_LABELS.get(cle, cle)))
    if not categories:
        return None
    return QgsCategorizedSymbolRenderer(C.ERROR_CLASS_FIELD, categories)


def write_style_sidecar(chemin, classes=None):
    """Écrit un ``.qml`` à côté d'un fichier de couche d'erreurs.

    Une sortie de fichier (algorithme Processing, ou export manuel) ne porte
    aucune symbologie : rouverte dans QGIS, elle s'affiche en points gris sans
    étiquette, alors que la couche mémoire du panneau est stylisée et
    étiquetée. QGIS cherchant ``<base>.qml`` au chargement d'un fichier, il
    suffit de déposer le style à côté.

    Best-effort et silencieux : un style absent n'empêche pas de lire la
    donnée, et l'écriture peut légitimement échouer (dossier en lecture seule,
    sortie en mémoire plutôt qu'en fichier).

    Renvoie le chemin du ``.qml`` écrit, ou ``None``.
    """
    if not chemin:
        return None
    # Une destination GeoPackage porte souvent « |layername=... » : le style se
    # nomme d'après le FICHIER, pas d'après cette URI.
    fichier = str(chemin).split("|", 1)[0].strip()
    if not fichier or "://" in fichier:
        return None
    base, ext = os.path.splitext(fichier)
    if not ext:
        return None
    qml = base + ".qml"
    with contextlib.suppress(Exception):
        # Couche jetable portant les MÊMES champs : c'est d'eux que dépendent
        # le rendu catégorisé (qc_class) et l'étiquette (qc_error).
        sonde = QgsVectorLayer("Point?crs=EPSG:4326", "sonde_style", "memory")
        sonde.dataProvider().addAttributes(error_layer_fields())
        sonde.updateFields()
        # Sans classes imposées, on couvre TOUTES les anomalies connues : un
        # style écrit à l'aveugle doit savoir colorer ce que le fichier
        # contient, quel qu'il soit.
        style_error_layer(sonde, classes=classes or list(C.CATEGORY_PRIORITY))
        sonde.saveNamedStyle(qml)
        if os.path.isfile(qml):
            return qml
    return None


def style_error_layer(mem, label=True, classes=None):
    """Symbologie CATÉGORISÉE par type d'anomalie, et étiquettes optionnelles.

    Un point rouge unique disait « il y a un problème ici » sans dire lequel :
    sur une couche mêlant chevauchements, doublons et sommets dupliqués, il
    fallait cliquer chaque point. Trois familles de couleurs répondent maintenant
    sans un clic — rouge pour une géométrie fautive ou en conflit, jaune pour une
    forme suspecte, cyan pour un défaut de sommets.

    Best-effort : n'échoue jamais, et se rabat sur le symbole unique si le rendu
    catégorisé ne peut pas être construit.
    """
    applique = False
    with contextlib.suppress(Exception):
        renderer = categorized_renderer(mem, classes)
        if renderer is not None:
            mem.setRenderer(renderer)
            applique = True
    if not applique:
        with contextlib.suppress(Exception):
            symbole = error_symbol()
            if symbole is not None and mem.renderer() is not None:
                mem.renderer().setSymbol(symbole)
    if not label:
        return
    with contextlib.suppress(Exception):
        pal = QgsPalLayerSettings()
        pal.fieldName = "qc_error"
        pal.enabled = True
        # AroundPoint et non OverPoint : l'etiquette se posait PAR-DESSUS le
        # marqueur et masquait le point qu'elle decrit. `dist` l'ecarte de
        # quelques pixels. QGIS ecarte de lui-meme les etiquettes qui se
        # chevauchent, ce qui rend le rendu acceptable meme a fort effectif.
        pal.placement = QgsPalLayerSettings.Placement.AroundPoint
        pal.dist = 2.0
        text_format = QgsTextFormat()
        text_format.setSize(8.0)
        buffer_settings = QgsTextBufferSettings()
        buffer_settings.setEnabled(True)
        buffer_settings.setSize(1.0)
        text_format.setBuffer(buffer_settings)
        pal.setFormat(text_format)
        mem.setLabeling(QgsVectorLayerSimpleLabeling(pal))
        mem.setLabelsEnabled(True)
