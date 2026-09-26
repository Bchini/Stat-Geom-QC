# -*- coding: utf-8 -*-
"""Algorithmes QGIS Processing de STAT GEOM QC.

Ce que cela apporte
-------------------
Le panneau ne fonctionne qu'à la main, dans une session QGIS ouverte. Ces trois
algorithmes rendent les mêmes contrôles utilisables là où le panneau ne peut pas
aller : dans le MODELEUR graphique (enchaîner le contrôle avec d'autres
traitements et sauvegarder l'enchaînement), en TRAITEMENT PAR LOTS (le même
contrôle sur une liste de couches d'un coup), et via ``qgis_process`` en ligne de
commande (une chaîne de production nocturne, sans interface ni projet ouvert).

Architecture : un deuxième appelant, pas un intermédiaire
---------------------------------------------------------
Ces algorithmes appellent DIRECTEMENT le moteur (``GeomAnalyzer``,
``error_layer``, ``report``), exactement comme le fait ``tasks.py`` pour le
panneau. Ils sont un appelant de plus, en parallèle, jamais un intermédiaire
entre le panneau et le moteur.

Faire passer le panneau par eux a été étudié puis écarté, sur mesure : le chemin
d'appel usuel de Processing est SYNCHRONE, donc il figerait la fenêtre pendant
toute l'analyse et rendrait le bouton Annuler inopérant ; et l'autre chemin
obligerait le panneau à recréer lui-même la mécanique de tâche de fond qu'il a
déjà. La non-duplication porte donc sur le MOTEUR, que les deux façades appellent
chacune de son côté.

Le piège du thread, et sa parade
--------------------------------
Un algorithme Processing sans ``FlagNoThreading`` s'exécute sur un thread de
FOND. Mesuré : y lire une couche, même vivante et partagée avec le canevas, est
sûr (vérifié sous charge, avec accès concurrent depuis le thread principal). Le
drapeau n'est donc pas nécessaire ici — il sert à protéger les MUTATIONS d'une
couche ou du projet, pas la lecture.

Mais le moteur exige un ``MeasurementContext`` capturé sur le thread PRINCIPAL :
sans lui, il interrogerait le singleton ``QgsProject`` depuis le thread de fond,
ce qui a déjà provoqué par le passé « au pire un plantage de QGIS sans trace
Python ». D'où le ``prepareAlgorithm`` de chaque algorithme — mesuré lui aussi :
il tourne bien sur le thread principal, quand ``processAlgorithm`` tourne sur
celui de fond.

Ici, le contexte est construit depuis ``QgsProcessingContext``, et non depuis le
projet : ``transformContext()`` et ``ellipsoid()`` restent valides même quand
aucun projet n'est ouvert, ce qui est le cas normal sous ``qgis_process``.
"""

import contextlib
import os

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
    QgsProcessingProvider,
    QgsProcessingOutputNumber,
    QgsProcessingOutputString,
)
from qgis.PyQt.QtGui import QIcon

from . import constants as C
from . import error_layer as EL
from . import report as R
from .analysis_engine import (
    AnalysisCancelled,
    AnalysisOptions,
    GeomAnalyzer,
    MeasurementContext,
    check_layer_ready,
)

# Identifiant du fournisseur, préfixe de tous les algorithmes
# (« statgeomqc:analyze », etc.). Stable : le changer casserait les modèles
# enregistrés par les utilisateurs.
PROVIDER_ID = "statgeomqc"


def _measurement_context_from(context):
    """``MeasurementContext`` bâti depuis un ``QgsProcessingContext``.

    Volontairement SANS repli vers ``QgsProject.instance()``. Sous
    ``qgis_process`` sans projet, ce singleton existe quand même mais désigne un
    projet vide sans rapport avec l'exécution : s'en servir donnerait des mesures
    fondées sur un ellipsoïde qui n'est pas celui des données. Mieux vaut
    l'ellipsoïde vide, que ``usable_ellipsoid()`` traduit en WGS84.
    """
    mctx = MeasurementContext()
    with contextlib.suppress(Exception):
        mctx.transform_context = context.transformContext()
    try:
        mctx.ellipsoid = context.ellipsoid() or ""
    except Exception:
        mctx.ellipsoid = ""
    return mctx


class _FeedbackBridge:
    """Relie le retour Processing aux rappels attendus par le moteur.

    Le moteur ne connaît que deux fonctions : une pour la progression, une pour
    savoir s'il doit s'arrêter. Ce pont évite de lui apprendre l'existence de
    Processing — c'est ce qui le garde utilisable par le panneau, par les tests
    headless et par ces algorithmes sans rien changer.
    """

    def __init__(self, feedback):
        self._feedback = feedback
        self._dernier = -1

    def progress(self, pct, message=""):
        if self._feedback is None:
            return
        entier = int(pct)
        # Ne pousser un message que quand il change : sous ``qgis_process``,
        # chaque appel s'imprime sur la sortie standard, et répéter mille fois
        # « Analyse 33000/58055… » noierait les vrais messages.
        if message and entier != self._dernier:
            self._feedback.pushInfo(message)
            self._dernier = entier
        self._feedback.setProgress(max(0.0, min(100.0, float(pct))))

    def is_canceled(self):
        return bool(self._feedback is not None and self._feedback.isCanceled())


class _BaseAlgorithm(QgsProcessingAlgorithm):
    """Socle commun : entrée, options de contrôle, contexte de mesure.

    Les trois algorithmes sont AUTONOMES : chacun relance l'analyse pour son
    propre compte. Faire transiter un résultat de l'un à l'autre dans un modèle
    est impossible — l'objet que produit le moteur ne se sérialise pas dans un
    paramètre — et le simuler exposerait à un décalage d'identifiants entre deux
    exécutions. Un modèle qui enchaîne les trois recalcule donc trois fois, ce
    qui est le prix de la fiabilité.
    """

    INPUT = "INPUT"
    AOI = "AOI"

    # Chaque entrée : (nom du paramètre, libellé, attribut d'AnalysisOptions,
    # valeur par défaut). Miroir des cases du panneau, dans le même ordre.
    CHECKS = (
        ("TOPOLOGY", "Contrôles topologiques (invalidité, multi-parties)",
         "topology_checks", True),
        ("OVERLAPS", "Détecter les chevauchements entre polygones",
         "check_overlaps", True),
        ("IGNORE_MISSING_VERTEX",
         "Ignorer les chevauchements dus à un sommet manquant",
         "ignore_missing_vertex_overlaps", True),
        ("ALLOW_CONTAINED",
         "Ignorer les polygones entièrement inclus dans un autre",
         "allow_contained_polygons", True),
        ("DUPLICATES", "Détecter les doublons", "check_duplicates", True),
        ("DUPLICATE_VERTEX", "Détecter les vertex dupliqués",
         "check_duplicate_vertex", True),
        ("SMALL", "Détecter les petits polygones", "check_small_polygons", True),
        ("HOLES", "Détecter les trous dans les polygones", "check_holes", True),
        ("SHARP_ANGLES", "Détecter les angles aigus", "check_sharp_angles", False),
        ("CLOSE_VERTICES", "Détecter les sommets trop rapprochés",
         "check_close_vertices", False),
        ("NESTED_HEIGHT", "Vérifier la hauteur des polygones inclus (AGL / HEIGHT)",
         "check_nested_height", False),
        ("LINE_CROSSINGS",
         "Détecter les croisements de lignes sans sommet commun (réseau)",
         "check_line_crossings", False),
    )

    # (nom, libellé, attribut, défaut, minimum)
    THRESHOLDS = (
        ("SMALL_THRESHOLD", "Seuil des petits polygones (m²)",
         "small_polygon_threshold_m2", C.THRESHOLD_DEFAULT_M2, C.THRESHOLD_MIN_M2),
        ("HOLE_THRESHOLD", "Seuil du trou « fautif » (m²)",
         "hole_max_area_m2", C.HOLE_THRESHOLD_DEFAULT_M2, C.HOLE_THRESHOLD_MIN_M2),
        ("SHARP_ANGLE_DEG", "Angle minimal toléré (°)",
         "sharp_angle_min_deg", C.SHARP_ANGLE_DEFAULT_DEG, C.SHARP_ANGLE_MIN_DEG),
        ("CLOSE_VERTEX_DIST", "Distance minimale entre sommets (m)",
         "close_vertex_min_dist_m", C.CLOSE_VERTEX_DEFAULT_M, C.CLOSE_VERTEX_MIN_M),
    )

    def __init__(self):
        super().__init__()
        self._mctx = None

    # -- Métadonnées --------------------------------------------------------

    def group(self):
        return "Contrôle qualité"

    def groupId(self):
        return "qualitycontrol"

    def tr(self, texte):
        return texte

    # -- Paramètres ---------------------------------------------------------

    def _add_input(self):
        # VectorLayer et non FeatureSource : le moteur appelle des méthodes qui
        # n'existent que sur une vraie couche (dataProvider, crs, name…).
        # Mesuré : avec une source, il échoue dès le premier appel.
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT, "Couche à contrôler",
            types=[QgsProcessing.SourceType.TypeVectorAnyGeometry]))

    def _add_check_params(self):
        for nom, libelle, _attr, defaut in self.CHECKS:
            self.addParameter(QgsProcessingParameterBoolean(
                nom, libelle, defaultValue=defaut))
        for nom, libelle, _attr, defaut, mini in self.THRESHOLDS:
            self.addParameter(QgsProcessingParameterNumber(
                nom, libelle,
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=defaut, minValue=mini))
        # Zone d'analyse, FACULTATIVE : non renseignée, toute la couche est
        # contrôlée. C'est ce qui rend le contrôle par tuiles automatisable —
        # un modèle ou une boucle qgis_process peut enchaîner les zones d'une
        # grille de livraison sans repasser par l'interface.
        aoi = QgsProcessingParameterExtent(
            self.AOI, "Zone d'analyse (facultative)", optional=True)
        aoi.setHelp(
            "Restreint le contrôle aux entités qui INTERSECTENT cette "
            "étendue ; une entité à cheval sur le bord est analysée en "
            "entier. Vide = toute la couche.")
        self.addParameter(aoi)

    def _options_from(self, parameters, context):
        """``AnalysisOptions`` reconstituées depuis les paramètres."""
        kwargs = {}
        for nom, _libelle, attr, _defaut in self.CHECKS:
            kwargs[attr] = self.parameterAsBool(parameters, nom, context)
        for nom, _libelle, attr, _defaut, _mini in self.THRESHOLDS:
            kwargs[attr] = self.parameterAsDouble(parameters, nom, context)
        # Zone d'analyse : demandée DANS le CRS de la couche, Processing
        # reprojetant lui-même l'étendue fournie. L'authid est donc laissé
        # vide — le moteur prendra le rectangle tel quel (voir
        # resolve_aoi_rect), sans seconde conversion qui le décalerait.
        layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        rect = None
        if layer is not None:
            with contextlib.suppress(Exception):
                extent = self.parameterAsExtent(
                    parameters, self.AOI, context, layer.crs())
                if extent is not None and not extent.isEmpty():
                    rect = (extent.xMinimum(), extent.yMinimum(),
                            extent.xMaximum(), extent.yMaximum())
        # Le profil est laissé vide : les cases ont été fournies une par une,
        # le rapport doit donc annoncer « Réglages manuels » et non usurper le
        # nom d'un préréglage du panneau.
        return AnalysisOptions(profile_name="", aoi_rect=rect,
                               aoi_crs_authid="", **kwargs)

    # -- Cycle de vie -------------------------------------------------------

    def prepareAlgorithm(self, parameters, context, feedback):
        """Capture le contexte de mesure sur le THREAD PRINCIPAL.

        Mesuré : cette méthode tourne sur le thread principal, alors que
        ``processAlgorithm`` tourne sur celui de fond. C'est donc ici, et
        seulement ici, que le contexte peut être lu sans risque — exactement ce
        que fait déjà ``AnalysisTask`` pour le panneau.
        """
        self._mctx = _measurement_context_from(context)
        return True

    def _analyze(self, parameters, context, feedback):
        """Couche validée et résultat d'analyse, ou lève une exception claire."""
        layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        if layer is None:
            raise QgsProcessingException(
                "Couche d'entrée illisible ou absente.")
        erreur = check_layer_ready(layer)
        if erreur:
            raise QgsProcessingException(erreur)

        pont = _FeedbackBridge(feedback)
        options = self._options_from(parameters, context)
        try:
            resultat = GeomAnalyzer(options).analyze(
                layer,
                progress_cb=pont.progress,
                is_canceled=pont.is_canceled,
                measurement_ctx=self._mctx)
        except AnalysisCancelled:
            # Annulation demandée : ce n'est pas une erreur, on ne la présente
            # donc pas comme telle.
            raise QgsProcessingException("Analyse annulée.")
        return (layer, resultat)

    @staticmethod
    def _push_warnings(feedback, resultat):
        """Remonte les avertissements du moteur dans le journal Processing.

        Sans cela, un utilisateur en ligne de commande perdrait tout ce que le
        panneau lui montre — y compris les mentions « contrôle non exécuté »,
        qui sont précisément ce qui empêche de lire un 0 comme un satisfecit.
        """
        if feedback is None:
            return
        for message in getattr(resultat, "errors", []) or []:
            feedback.reportError(message, fatalError=False)
        for message in getattr(resultat, "warnings", []) or []:
            feedback.pushWarning(message) if hasattr(feedback, "pushWarning") \
                else feedback.pushInfo(message)
        couverture = R.coverage_text(resultat)
        if couverture:
            feedback.pushInfo(couverture)


class AnalyzeAlgorithm(_BaseAlgorithm):
    """Analyse complète : score, couverture et compteurs."""

    def name(self):
        return "analyze"

    def displayName(self):
        return "Analyser la qualité géométrique"

    def shortHelpString(self):
        return (
            "Analyse une couche vectorielle et renvoie le score de correctness, "
            "sa couverture et les compteurs d'anomalies.\n\n"
            "Les sorties numériques et textuelles se chaînent dans le modeleur. "
            "Un contrôle décoché n'est pas mesuré : son compteur vaut alors 0 "
            "sans que cela signifie « aucune anomalie » — la sortie COVERAGE_PCT "
            "dit sur quelle part des contrôles le score est assis.")

    def createInstance(self):
        return AnalyzeAlgorithm()

    def initAlgorithm(self, config=None):
        self._add_input()
        self._add_check_params()
        self.addOutput(QgsProcessingOutputNumber("SCORE", "Score de correctness"))
        self.addOutput(QgsProcessingOutputString("GRADE", "Grade"))
        self.addOutput(QgsProcessingOutputNumber(
            "COVERAGE_PCT", "Couverture des contrôles (pour cent)"))
        self.addOutput(QgsProcessingOutputNumber("TOTAL_FEATURES", "Entités analysées"))
        self.addOutput(QgsProcessingOutputNumber("TOTAL_ISSUES", "Anomalies détectées"))
        self.addOutput(QgsProcessingOutputNumber("INVALID", "Géométries invalides"))
        self.addOutput(QgsProcessingOutputNumber("NULL_EMPTY", "Géométries nulles ou vides"))
        self.addOutput(QgsProcessingOutputNumber("OVERLAPS", "Entités en chevauchement"))
        self.addOutput(QgsProcessingOutputNumber("DUPLICATES", "Doublons"))
        self.addOutput(QgsProcessingOutputNumber(
            "UNNODED_CROSSINGS", "Lignes en croisement non nodé"))

    def processAlgorithm(self, parameters, context, feedback):
        _layer, resultat = self._analyze(parameters, context, feedback)
        self._push_warnings(feedback, resultat)
        q = resultat.quality_report
        c = resultat.correctness
        return {
            "SCORE": float(c.score),
            "GRADE": str(c.grade),
            "COVERAGE_PCT": float(c.coverage_pct()),
            "TOTAL_FEATURES": int(resultat.total_features),
            "TOTAL_ISSUES": int(resultat.total_issues()),
            "INVALID": int(q.invalid_count),
            "NULL_EMPTY": int(q.null_empty_count),
            "OVERLAPS": int(q.overlap_count),
            "DUPLICATES": int(q.duplicate_count),
            "UNNODED_CROSSINGS": int(q.unnoded_crossing_count),
        }


class ErrorLayerAlgorithm(_BaseAlgorithm):
    """Couche de points localisant chaque anomalie."""

    OUTPUT = "OUTPUT"

    def name(self):
        return "errorlayer"

    def displayName(self):
        return "Créer la couche des erreurs (points)"

    def shortHelpString(self):
        return (
            "Produit une couche de POINTS localisant chaque anomalie : nœud "
            "d'auto-intersection pour une géométrie invalide, point dans la zone "
            "commune pour un chevauchement, point représentatif sinon.\n\n"
            "Le champ « qc_error » porte la liste des anomalies du point, "
            "« %s » la plus grave d'entre elles — c'est sur ce dernier qu'une "
            "symbologie catégorisée se règle.\n\n"
            "L'analyse est relancée par cet algorithme : il ne dépend d'aucun "
            "résultat calculé en amont, ce qui le rend utilisable seul comme en "
            "traitement par lots." % C.ERROR_CLASS_FIELD)

    def createInstance(self):
        return ErrorLayerAlgorithm()

    def initAlgorithm(self, config=None):
        self._add_input()
        self._add_check_params()
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, "Couche des erreurs",
            type=QgsProcessing.SourceType.TypeVectorPoint))

    def processAlgorithm(self, parameters, context, feedback):
        from qgis.core import QgsFields, QgsWkbTypes
        layer, resultat = self._analyze(parameters, context, feedback)
        self._push_warnings(feedback, resultat)

        feats, tronque = EL.build_error_features(layer, resultat)
        if tronque and feedback is not None:
            # Troncature ANNONCÉE : une couche silencieusement écourtée se
            # lirait comme la liste complète des anomalies.
            feedback.pushWarning(
                "Couche d'erreurs limitée à %d points : au-delà, les anomalies "
                "restantes ne sont pas localisées."
                % C.ERROR_LAYER_CAP)

        fields = QgsFields()
        for f in EL.error_layer_fields():
            fields.append(f)
        sink, dest_id = self.parameterAsSink(
            parameters, self.OUTPUT, context, fields,
            QgsWkbTypes.Type.Point, layer.crs())
        if sink is None:
            raise QgsProcessingException(
                "Impossible de créer la couche des erreurs en sortie.")
        # Annonce du NOMBRE RÉELLEMENT ÉCRIT, et non de len(feats) : une
        # annulation en cours d'écriture sortait de la boucle puis annonçait
        # le total prévu, sur une couche incomplète présentée comme un succès.
        written = 0
        for feat in feats:
            if feedback is not None and feedback.isCanceled():
                raise QgsProcessingException(
                    "Annulé : couche d'erreurs incomplète (%d point(s) sur %d)."
                    % (written, len(feats)))
            if sink.addFeature(feat):
                written += 1
        if written < len(feats):
            raise QgsProcessingException(
                "Couche d'erreurs incomplète : %d point(s) sur %d écrits."
                % (written, len(feats)))
        if feedback is not None:
            feedback.pushInfo("%d point(s) d'erreur écrit(s)." % written)
        # Style deposé à côté du fichier : sans lui, la couche rouverte dans
        # QGIS n'a ni couleurs par type d'anomalie ni étiquettes.
        with contextlib.suppress(Exception):
            destination = self.parameterAsOutputLayer(
                parameters, self.OUTPUT, context)
            # Les classes REELLEMENT ecrites : la legende du fichier decrit
            # alors son contenu exact, sans entree vide.
            classes = [c for c in C.CATEGORY_PRIORITY
                       if any(f[C.ERROR_CLASS_FIELD] == c for f in feats)]
            qml = EL.write_style_sidecar(destination, classes=classes)
            if qml and feedback is not None:
                feedback.pushInfo("Style et étiquettes écrits : %s"
                                  % os.path.basename(qml))
        return {self.OUTPUT: dest_id}


class ReportAlgorithm(_BaseAlgorithm):
    """Rapport exportable (HTML, PDF)."""

    OUTPUT_HTML = "OUTPUT_HTML"
    OUTPUT_PDF = "OUTPUT_PDF"

    def name(self):
        return "report"

    def displayName(self):
        return "Générer le rapport de contrôle qualité"

    def shortHelpString(self):
        return (
            "Écrit le rapport de contrôle dans un ou plusieurs formats. Chaque "
            "sortie est indépendante : ne renseignez que celles qui vous "
            "intéressent.\n\n"
            "Le rapport consigne le profil, l'état de chaque contrôle et les "
            "seuils réellement appliqués, afin de rester interprétable des mois "
            "plus tard.")

    def createInstance(self):
        return ReportAlgorithm()

    def initAlgorithm(self, config=None):
        self._add_input()
        self._add_check_params()
        for nom, libelle, filtre in (
                (self.OUTPUT_HTML, "Rapport HTML", "HTML (*.html)"),
                (self.OUTPUT_PDF, "Rapport PDF", "PDF (*.pdf)")):
            self.addParameter(QgsProcessingParameterFileDestination(
                nom, libelle, fileFilter=filtre, optional=True,
                createByDefault=(nom == self.OUTPUT_HTML)))

    def processAlgorithm(self, parameters, context, feedback):
        _layer, resultat = self._analyze(parameters, context, feedback)
        self._push_warnings(feedback, resultat)

        sorties = {}
        for nom, ecrivain in ((self.OUTPUT_HTML, R.write_html),
                              (self.OUTPUT_PDF, R.write_pdf)):
            chemin = self.parameterAsFileOutput(parameters, nom, context)
            if not chemin:
                continue
            dossier = os.path.dirname(chemin)
            if dossier and not os.path.isdir(dossier):
                raise QgsProcessingException(
                    "Dossier de sortie introuvable : %s" % dossier)
            try:
                ecrivain(resultat, chemin)
            except OSError as exc:
                # Message lisible plutôt qu'une trace Python : le disque plein
                # ou un dossier en lecture seule sont des cas ordinaires.
                raise QgsProcessingException(
                    "Écriture impossible dans %s : %s" % (chemin, exc))
            sorties[nom] = chemin
            if feedback is not None:
                feedback.pushInfo("Rapport écrit : %s" % chemin)
        if not sorties:
            raise QgsProcessingException(
                "Aucun fichier de sortie demandé : renseignez au moins un des "
                "trois formats.")
        return sorties


class StatGeomQCProvider(QgsProcessingProvider):
    """Fournisseur regroupant les algorithmes de STAT GEOM QC."""

    def id(self):
        return PROVIDER_ID

    def name(self):
        return "STAT GEOM QC"

    def longName(self):
        return "STAT GEOM QC — contrôle qualité géométrique"

    def icon(self):
        chemin = os.path.join(os.path.dirname(__file__), "icon.svg")
        return QIcon(chemin) if os.path.exists(chemin) else super().icon()

    def loadAlgorithms(self):
        # Appelé automatiquement par le registre à l'ajout du fournisseur :
        # ne pas l'appeler soi-même, sous peine de charger deux fois.
        self.addAlgorithm(AnalyzeAlgorithm())
        self.addAlgorithm(ErrorLayerAlgorithm())
        self.addAlgorithm(ReportAlgorithm())
