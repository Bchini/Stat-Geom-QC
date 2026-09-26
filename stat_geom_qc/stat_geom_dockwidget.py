# -*- coding: utf-8 -*-
"""Panneau dockable STAT GEOM QC : sélection de couche, options, analyse,
affichage moderne des résultats (score de correctness, indicateurs, rapport)
et actions QGIS (sélection des entités, couche d'erreurs, exports)."""

import contextlib
import dataclasses
import os
import tempfile
import webbrowser

from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtGui import QColor

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsGeometry,
    QgsMarkerSymbol,
    QgsPalLayerSettings,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsRendererCategory,
    QgsStyle,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
    QgsWkbTypes,
)
from qgis.gui import QgsDockWidget, QgsMapLayerComboBox, QgsRubberBand

try:
    from qgis.core import QgsMapLayerProxyModel
    _VECTOR_FILTER = QgsMapLayerProxyModel.Filter.VectorLayer
except Exception:  # pragma: no cover
    _VECTOR_FILTER = None


# Types de champ : detection unique, dans ``error_layer``, partagee avec les
# algorithmes Processing. Deux detections identiques cote a cote finiraient par
# diverger au premier ajustement de l'une des deux.
from .error_layer import FIELD_TYPE_LONGLONG, FIELD_TYPE_STRING  # noqa: E402

from . import constants as C
from . import error_layer as EL
from . import report
from . import settings as sg_settings
from . import theme as sg_theme
from .analysis_engine import (
    AnalysisOptions,
    RepairOptions,
    check_layer_ready,
)
from .aoi_tool import AoiRectTool
from .flow_layout import FlowLayout
from .repair_dialog import RepairOptionsDialog
from .tasks import AnalysisTask, RepairTask
from .widgets import CollapsibleSection, MetricCard, ScoreGauge


# La feuille de style n'est plus statique : elle est générée depuis la palette
# du thème détecté (voir ``theme.build_stylesheet``).

# Constantes partagées (libellés, catégories, filtres) : voir ``constants``.
_SUPPORTED = C.SOURCE_FILE_FILTER
_CATEGORY_LABELS = C.CATEGORY_LABELS
_ALL_CATEGORIES = C.ALL_CATEGORIES


def _point_from_error(err):
    """Géométrie point d'une erreur de validation localisée, sinon ``None``."""
    try:
        if not err.hasWhere():
            return None
        return QgsGeometry.fromPointXY(QgsPointXY(err.where()))
    except Exception:
        return None


def _prep_threshold_spin(spin, unit, prefix="≤ "):
    """Rend un champ de seuil lisible SANS ligne de libellé séparée.

    Le seuil tient désormais sur la même ligne que la case qu'il paramètre.
    L'unité et le sens de la comparaison, qui vivaient dans un « ↳ seuil (m²) »
    sur la ligne suivante, sont portés par le champ lui-même : « ≤ 2,00 m² »
    se lit d'un coup d'œil et supprime un aller-retour visuel. La largeur est
    bornée pour que la case garde toute la place restante.
    """
    spin.setPrefix(prefix)
    spin.setSuffix(" " + unit)
    spin.setAlignment(Qt.AlignmentFlag.AlignRight
                      | Qt.AlignmentFlag.AlignVCenter)
    spin.setMinimumWidth(112)
    spin.setMaximumWidth(150)
    return spin


class StatGeomDockWidget(QgsDockWidget):
    """Panneau principal du plugin."""

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._result = None
        self._result_layer_id = None      # couche à laquelle se rapporte le résultat
        self._pending_layer_id = None     # couche de l'analyse en cours
        # Vrai dès que la couche analysée est modifiée : les FID du résultat
        # ne désignent alors plus forcément les mêmes entités (un fid
        # supprimé peut être recyclé) et toute action destructive fondée sur
        # eux frapperait à côté. Les actions refusent alors de partir.
        self._result_stale = False
        self._watched_layer = None        # couche dont on écoute les éditions
        self._analysis_task = None
        self._repair_task = None
        self._repair_out_path = None
        self._last_analysis_options = None
        # Vérification automatique post-réparation (idée : idempotence) : tâche
        # indépendante, qui ne touche jamais self._result/_result_layer_id —
        # elle ne remplace pas l'affichage courant, elle le complète.
        self._verify_task = None
        # Palette adaptée au thème de QGIS (clair ou sombre), source unique des
        # couleurs de l'interface, de la jauge et des cartes d'indicateurs.
        self._dark = sg_theme.is_dark(self)
        self._colors = sg_theme.colors_for(self._dark)
        self._severity = sg_theme.severity_colors(self._colors)
        self.setWindowTitle(C.PLUGIN_TITLE)
        self._build_ui()
        self._restore_settings()

    # ── Construction de l'UI ──────────────────────────────────────────────

    def _build_ui(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)

        content = QWidget()
        content.setObjectName("sgContent")
        content.setStyleSheet(sg_theme.build_stylesheet(self._colors))
        root = QVBoxLayout(content)
        root.setContentsMargins(12, 12, 12, 14)
        root.setSpacing(12)

        # -- En-tête : pastille d'icône + titre sur deux niveaux --
        header = QHBoxLayout()
        header.setSpacing(10)
        logo = QLabel("◈")
        logo.setObjectName("sgLogo")
        logo.setFixedSize(34, 34)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        title = QLabel("STAT GEOM QC")
        title.setObjectName("sgHeaderTitle")
        title_row.addWidget(title)
        if C.PLUGIN_VERSION:
            version = QLabel("v%s" % C.PLUGIN_VERSION)
            version.setObjectName("sgVersion")
            version.setToolTip("Version du plugin (metadata.txt).")
            title_row.addWidget(version)
        title_row.addStretch()
        subtitle = QLabel("Contrôle qualité vectoriel")
        subtitle.setObjectName("sgHeaderSub")
        title_box.addLayout(title_row)
        title_box.addWidget(subtitle)
        header.addWidget(logo)
        header.addLayout(title_box)
        header.addStretch()
        root.addLayout(header)

        # -- Source --
        src_group = QGroupBox("◈ Source de données")
        src_lay = QVBoxLayout(src_group)
        src_lay.setContentsMargins(12, 12, 12, 12)
        self.layer_combo = QgsMapLayerComboBox()
        if _VECTOR_FILTER is not None:
            self.layer_combo.setFilters(_VECTOR_FILTER)
        # Texte de substitution plutôt qu'un champ vide tant qu'aucune couche
        # n'est choisie. L'argument ``text`` n'existe que depuis QGIS 3.20 (le
        # plugin cible 3.16+) : repli silencieux sur l'option sans texte.
        try:
            self.layer_combo.setAllowEmptyLayer(True, "Aucune couche sélectionnée")
        except TypeError:
            self.layer_combo.setAllowEmptyLayer(True)
        row = QHBoxLayout()
        row.setSpacing(8)
        btn_file = QPushButton("Charger un fichier…")
        btn_file.setToolTip("Ouvrir une couche vectorielle depuis un fichier "
                            "(Shapefile, GeoPackage, GeoJSON, MapInfo, KML).")
        btn_file.clicked.connect(self._on_load_file)
        row.addWidget(self.layer_combo, 1)
        row.addWidget(btn_file)
        src_lay.addLayout(row)
        # Zone d'analyse : dans le groupe « Source de données », car elle dit
        # QUOI est analysé — pas COMMENT, ce qui est le rôle des options.
        src_lay.addLayout(self._build_aoi_row())
        root.addWidget(src_group)

        # -- Retour aux valeurs par défaut --
        root.addLayout(self._build_reset_row())

        # -- Options (repliées par défaut : voir _build_options_section) --
        root.addWidget(self._build_options_section())

        root.addWidget(self._build_extras_section())

        # -- Bouton d'analyse + légende + progression --
        # Le libellé reste court (le panneau est étroit) ; la légende juste
        # dessous annonce ce que l'analyse débloque, sans surcharger le bouton.
        self.btn_analyze = QPushButton("Lancer l'analyse complète")
        self.btn_analyze.setToolTip(
            "Analyse la couche et produit : les statistiques et le score de "
            "correctness, les contrôles topologiques et de forme, la "
            "localisation des anomalies, puis l'accès aux corrections et aux "
            "exports.\n"
            "Le traitement s'exécute en arrière-plan : QGIS reste utilisable et "
            "l'analyse peut être annulée à tout moment.")
        self.btn_analyze.setObjectName("primary")
        self.btn_analyze.clicked.connect(self._run_analysis)
        root.addWidget(self.btn_analyze)

        self.analyze_caption = QLabel(
            "Statistiques · contrôles topologiques · corrections")
        self.analyze_caption.setObjectName("sgHint")
        self.analyze_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.analyze_caption.setWordWrap(True)
        root.addWidget(self.analyze_caption)

        prog_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.btn_cancel = QPushButton("✕")
        self.btn_cancel.setToolTip("Annuler le traitement en cours.")
        self.btn_cancel.setFixedWidth(38)
        self.btn_cancel.clicked.connect(self._on_cancel)
        prog_row.addWidget(self.progress, 1)
        prog_row.addWidget(self.btn_cancel)
        self.prog_widget = QWidget()
        self.prog_widget.setLayout(prog_row)
        self.prog_widget.setVisible(False)
        root.addWidget(self.prog_widget)

        self.status_label = QLabel(C.MSG_HINT_START)
        self.status_label.setObjectName("sgHint")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        # -- Bloc résultats (masqué au départ) --
        self.results_widget = QWidget()
        res_lay = QVBoxLayout(self.results_widget)
        res_lay.setContentsMargins(0, 0, 0, 0)
        res_lay.setSpacing(12)

        # Bandeau score
        score_group = QGroupBox("◈ Score de correctness")
        score_row = QHBoxLayout(score_group)
        score_row.setContentsMargins(12, 14, 12, 12)
        score_row.setSpacing(14)
        self.gauge = ScoreGauge()
        self.gauge.set_track_color(self._colors["gauge_track"])
        score_col = QVBoxLayout()
        score_col.setSpacing(3)
        self.grade_label = QLabel("—")
        self.grade_label.setStyleSheet(
            "font-size:19px;font-weight:600;color:%s;" % self._colors["text"])
        self.grade_desc = QLabel("")
        self.grade_desc.setObjectName("sgHint")
        self.grade_desc.setWordWrap(True)
        # Couverture : le score seul ne dit pas sur QUOI il porte. 100 sur
        # quatre dimensions mesurées et 100 sur onze ne valent pas la même
        # chose, et rien ne le laissait voir.
        self.grade_coverage = QLabel("")
        self.grade_coverage.setObjectName("sgHint")
        self.grade_coverage.setWordWrap(True)
        self.grade_delta = QLabel("")
        self.grade_delta.setObjectName("sgHint")
        self.grade_delta.setVisible(False)
        score_col.addStretch()
        score_col.addWidget(self.grade_label)
        score_col.addWidget(self.grade_desc)
        score_col.addWidget(self.grade_coverage)
        score_col.addWidget(self.grade_delta)
        score_col.addStretch()
        score_row.addWidget(self.gauge)
        score_row.addLayout(score_col, 1)
        res_lay.addWidget(score_group)

        # Synthèse : les cartes d'indicateurs, et rien d'autre.
        #
        # Les onglets « Qualité », « Anomalies », « Attributs » et « Rapport »
        # ont été retirés en 2.9.49. Ils rejouaient dans un panneau étroit ce
        # que les rapports rendent mieux : un tableau de 25 lignes illisible
        # sur 300 px de large, une liste de 5 000 anomalies qu'on ne parcourt
        # pas à la souris, et un rapport déjà consultable en pleine page. Tout
        # leur contenu se retrouve dans les trois rapports — navigateur, HTML
        # et PDF — y compris ce qui n'y figurait pas encore (détails
        # d'invalidité, aperçu des attributs, liste des anomalies).
        res_lay.addWidget(self._build_summary_section())

        # Barre d'actions — mise en valeur (cadre + titre en badge accent) :
        # c'est la zone la plus utilisée une fois l'analyse terminée, à
        # distinguer des blocs de configuration au style neutre (Source, Score).
        actions = QGroupBox("Actions")
        actions.setObjectName("sgActionsCard")
        act_lay = FlowLayout(actions, margin=10, spacing=8)
        # Libellés sobres (sans pictogramme) : chaque action porte une infobulle
        # explicite, ce qui reste plus lisible qu'un emoji dans un panneau étroit.
        self.btn_select = QPushButton("Sélectionner les entités")
        self.btn_select.setObjectName("sgActionBtn")
        self.btn_select.setToolTip(
            "Sélectionne dans la couche d'origine toutes les entités signalées, "
            "puis zoome sur la sélection."
        )
        self.btn_select.clicked.connect(self._select_features)
        self.btn_errlayer = QPushButton("Couche d'erreurs")
        self.btn_errlayer.setObjectName("sgActionBtn")
        self.btn_errlayer.setToolTip(
            "Crée une couche de POINTS localisés exactement sur chaque anomalie "
            "(nœud d'auto-intersection, point représentatif pour les autres cas)."
        )
        self.btn_errlayer.clicked.connect(self._create_error_layer)
        self.btn_repair = QPushButton("Réparer…")
        self.btn_repair.setObjectName("sgRepair")
        self.btn_repair.setToolTip(
            "Choisir les erreurs à corriger (cases à cocher) et écrire le "
            "résultat dans un nouveau fichier, sans modifier la couche d'origine."
        )
        self.btn_repair.clicked.connect(self._repair_geometries)
        self.btn_export = self._build_export_button()
        self.btn_export.setObjectName("sgActionBtn")
        for b in (self.btn_select, self.btn_errlayer, self.btn_repair, self.btn_export):
            act_lay.addWidget(b)
        res_lay.addWidget(actions)

        self.results_widget.setVisible(False)
        root.addWidget(self.results_widget)
        root.addStretch()

        scroll.setWidget(content)
        self.setWidget(scroll)

    def _build_export_button(self):
        """Bouton unique regroupant les 4 exports (menu déroulant).

        Remplace 4 boutons de même poids visuel par une seule action
        « Rapport » : plus lisible dans un panneau étroit, et cohérent avec le
        fait qu'il s'agit toujours de la même intention (obtenir le rapport).
        Le bouton nomme l'objet obtenu plutôt que le geste : la première entrée
        du menu ouvre le rapport sans rien exporter, et « Rapport » est aussi
        le nom de l'onglet où il s'affiche.
        """
        btn = QToolButton()
        btn.setText("Rapport ▾")
        btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        btn.setToolTip(
            "Obtenir le rapport : dans le navigateur, ou vers un fichier "
            "HTML ou PDF.")
        menu = QMenu(btn)
        menu.setToolTipsVisible(True)

        act_browser = menu.addAction("Rapport navigateur")
        act_browser.setToolTip("Ouvre le rapport complet, mis en forme, dans le navigateur web.")
        act_browser.triggered.connect(self._open_in_browser)

        act_html = menu.addAction("HTML…")
        act_html.setToolTip("Exporter le rapport au format HTML.")
        act_html.triggered.connect(lambda: self._export("html"))

        act_pdf = menu.addAction("PDF…")
        act_pdf.setToolTip(
            "Exporter le rapport au format PDF, prêt à joindre à une "
            "livraison.")
        act_pdf.triggered.connect(lambda: self._export("pdf"))

        btn.setMenu(menu)
        return btn

    def _build_aoi_row(self):
        """Ligne « Restreindre à une zone » (AOI), facultative.

        Décochée par défaut : sans intervention, le plugin analyse toute la
        couche exactement comme avant. Elle n'entre PAS dans les défauts du
        panneau (``C.UI_DEFAULT_CHECKS``) et n'est jamais mémorisée d'une
        session à l'autre — ce n'est pas un contrôle qualité, c'est le
        périmètre sur lequel les contrôles portent.
        """
        outer = QVBoxLayout()
        outer.setSpacing(4)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.chk_aoi = QCheckBox("Restreindre à une zone")
        self.chk_aoi.setToolTip(
            "Ne contrôler que les entités d'une zone tracée sur la carte.\n"
            "Une entité est retenue dès qu'elle INTERSECTE la zone : un "
            "bâtiment à cheval sur le bord est analysé en entier.\n"
            "Les voisins immédiats hors zone sont lus pour ne pas manquer un "
            "chevauchement au bord, mais ne sont jamais signalés.")
        self.chk_aoi.toggled.connect(self._on_aoi_toggled)
        row.addWidget(self.chk_aoi)
        row.addStretch()

        self.btn_aoi_draw = QPushButton("▭ Dessiner…")
        self.btn_aoi_draw.setToolTip(C.MSG_AOI_DRAW_HINT)
        self.btn_aoi_draw.clicked.connect(self._draw_aoi)
        row.addWidget(self.btn_aoi_draw)

        self.btn_aoi_canvas = QPushButton("Étendue visible")
        self.btn_aoi_canvas.setToolTip(
            "Reprendre le cadrage actuel de la carte comme zone d'analyse.")
        self.btn_aoi_canvas.clicked.connect(self._aoi_from_canvas)
        row.addWidget(self.btn_aoi_canvas)

        self.btn_aoi_clear = QPushButton("✕")
        self.btn_aoi_clear.setToolTip("Oublier la zone tracée.")
        self.btn_aoi_clear.setFixedWidth(28)
        self.btn_aoi_clear.clicked.connect(self._clear_aoi)
        row.addWidget(self.btn_aoi_clear)
        outer.addLayout(row)

        self.aoi_label = QLabel(C.MSG_AOI_NONE)
        self.aoi_label.setObjectName("sgHint")
        outer.addWidget(self.aoi_label)

        self._aoi_rect = None          # QgsRectangle, CRS du canevas
        self._aoi_crs_authid = ""
        self._aoi_tool = None
        self._aoi_previous_tool = None
        self._aoi_band = None          # tracé persistant de la zone retenue
        self._sync_aoi_widgets()
        return outer

    def _build_reset_row(self):
        """Retour aux réglages par défaut, en un clic.

        Les trois préréglages « Rapide / Standard / Approfondi » qui occupaient
        cette ligne ont été retirés en 2.9.49 : ils demandaient de choisir un
        mode avant d'avoir vu la donnée, et deux d'entre eux n'existaient que
        pour retirer des contrôles. Le panneau ouvre désormais sur un mode
        unique, sans nom — tout « Options d'analyse » coché, rien d'« Extras » —
        et qui veut moins décoche, ce qui est plus direct que de chercher quel
        préréglage contenait quoi.
        """
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addStretch()
        btn_reset = QPushButton("↺ Réinitialiser")
        btn_reset.setObjectName("sgLinkButton")
        btn_reset.setToolTip(
            "Remet les options d'analyse — y compris Extras — aux valeurs par "
            "défaut du plugin.")
        btn_reset.clicked.connect(self._reset_options)
        row.addWidget(btn_reset)
        return row

    def _build_options_section(self):
        """Section « Options d'analyse », TOUJOURS dépliée.

        Ce bloc dit ce qui sera réellement calculé : le replier revenait à
        lancer une analyse sans avoir sous les yeux les contrôles qu'elle
        couvre — et un contrôle décoché lors d'une session précédente pouvait
        passer inaperçu derrière une simple pastille. La section garde son
        cadre, son titre et son badge, mais ne se referme plus. « Extras »,
        dont les contrôles sont tous facultatifs et décochés par défaut, reste
        repliable.
        """
        section = CollapsibleSection("Options d'analyse", expanded=True,
                                     collapsible=False)
        section.header.setToolTip(
            "Contrôles exécutés pendant l'analyse et leurs seuils.\n"
            "Le badge rappelle combien sont actifs.")
        holder = QWidget()
        opt_lay = QGridLayout(holder)
        opt_lay.setContentsMargins(0, 0, 0, 0)
        opt_lay.setVerticalSpacing(8)
        # Petits polygones : coché par défaut comme tout « Options d'analyse »,
        # avec son seuil en sous-option. DÉTECTER reste sans effet sur la
        # donnée — la suppression est une case du dialogue de réparation.
        self.chk_small = QCheckBox("Détecter les petits polygones")
        self.chk_small.setChecked(C.UI_DEFAULT_CHECKS["small"])
        self.chk_small.setToolTip(
            "Repère les polygones dont la surface est inférieure ou égale au "
            "seuil ci-dessous — scories de numérisation, résidus de découpage.\n"
            "Ce qui est « trop petit » dépend entièrement de vos données : un "
            "abri de 2 m² peut être parfaitement légitime. Ajustez le seuil, ou "
            "décochez si la notion n'a pas de sens sur cette couche.\n"
            "Les signaler ne supprime rien : la suppression est une case à "
            "cocher du dialogue de réparation, jamais implicite.")
        self.spin_threshold = QDoubleSpinBox()
        self.spin_threshold.setRange(C.THRESHOLD_MIN_M2, C.THRESHOLD_MAX_M2)
        self.spin_threshold.setDecimals(2)
        self.spin_threshold.setValue(C.THRESHOLD_DEFAULT_M2)
        _prep_threshold_spin(self.spin_threshold, "m²")
        _tip_threshold = ("Un polygone dont la surface est inférieure ou égale à ce "
                          "seuil (en m²) est signalé comme « petit polygone ».")
        self.spin_threshold.setToolTip(_tip_threshold)
        self.chk_topology = QCheckBox(
            "Contrôles topologiques (invalidité, multi-parties)")
        self.chk_topology.setChecked(C.UI_DEFAULT_CHECKS["topology"])
        self.chk_topology.setToolTip(
            "Analyse les erreurs de validité (auto-intersections, anneaux, etc.) "
            "et en liste le détail. Détecte aussi les entités composées de "
            "plusieurs parties (multi-parties). Décochez pour une analyse plus "
            "légère.")
        self.chk_overlaps = QCheckBox("Détecter les chevauchements/intersections entre polygones")
        self.chk_overlaps.setChecked(C.UI_DEFAULT_CHECKS["overlaps"])
        self.chk_overlaps.setToolTip(
            "Un chevauchement est une intersection de SURFACE entre deux "
            "polygones : le signe d'un accrochage manqué.\n"
            "AUCUNE TOLÉRANCE : le moindre recouvrement d'intérieurs est "
            "signalé, même d'un millimètre carré.\n"
            "Deux polygones correctement accrochés — frontière commune ou "
            "contact en un point — ne sont PAS signalés.\n"
            "C'est le contrôle le plus coûteux : sur de très grosses couches, "
            "il peut allonger sensiblement l'analyse.")

        # Les deux exceptions au contrôle des chevauchements. Elles ne changent
        # PAS la règle de détection : elles retirent chacune une famille de cas
        # de la liste des erreurs, sans plus aucun seuil à régler. Regroupées
        # dans un encart indenté (« sgSubOptions ») pour qu'on lise d'un coup
        # d'œil qu'elles dépendent de la case au-dessus.
        self.chk_ignore_missing_vertex = QCheckBox(
            "Ignorer ceux dus à un sommet manquant")
        self.chk_ignore_missing_vertex.setChecked(
            C.UI_DEFAULT_CHECKS["ignore_missing_vertex"])
        self.chk_ignore_missing_vertex.setToolTip(
            "Décochez pour signaler TOUS les chevauchements.\n"
            "Coché (par défaut) : ceux qui s'expliquent par un sommet absent sur la frontière "
            "commune ne sont plus signalés — le cas où un sommet d'un bâti "
            "empiète sur l'arête du voisin, laquelle n'a aucun sommet à cet "
            "endroit pour l'accrocher.\n"
            "Deux conditions sont exigées : l'empiètement va dans un seul sens "
            "(les sommets d'un seul des deux bâtis entrent chez l'autre) ET la "
            "zone commune longe le mur au lieu de mordre dans le bâti (au moins "
            "quatre fois plus longue que profonde, ou d'épaisseur nulle).\n"
            "Un bâtiment franchement décalé dans son voisin reste donc signalé. "
            "Ces paires écartées sont comptées dans le rapport.")

        self.chk_allow_contained = QCheckBox(
            "Ignorer les polygones entièrement inclus dans un autre")
        self.chk_allow_contained.setChecked(
            C.UI_DEFAULT_CHECKS["allow_contained"])
        self.chk_allow_contained.setToolTip(
            "Coché (par défaut) : l'imbrication est tenue pour LÉGITIME — un "
            "bâtiment inclus dans une parcelle, une superstructure sur un toit — "
            "et ces cas ne sont pas signalés comme chevauchement.\n"
            "Décochez pour les traiter comme une anomalie. Dans les deux cas, "
            "leur nombre est indiqué dans le rapport.")

        self.overlap_sub_options = QFrame()
        self.overlap_sub_options.setObjectName("sgSubOptions")
        sub_lay = QVBoxLayout(self.overlap_sub_options)
        sub_lay.setContentsMargins(12, 2, 0, 2)
        sub_lay.setSpacing(4)
        for chk in (self.chk_ignore_missing_vertex, self.chk_allow_contained):
            chk.setObjectName("sgHint")
            sub_lay.addWidget(chk)

        self.chk_dup = QCheckBox("Détecter les doublons")
        self.chk_dup.setChecked(C.UI_DEFAULT_CHECKS["duplicates"])
        self.chk_dup.setToolTip(
            "Repère les entités dont la géométrie est strictement identique "
            "(la 1ʳᵉ occurrence n'est pas comptée comme doublon).")
        self.chk_dup_vertex = QCheckBox("Détecter les vertex dupliqués")
        self.chk_dup_vertex.setChecked(C.UI_DEFAULT_CHECKS["duplicate_vertex"])
        self.chk_dup_vertex.setToolTip(
            "Repère les entités contenant des sommets STRICTEMENT dupliqués "
            "(points superposés). Ces doublons alourdissent les géométries et "
            "peuvent provoquer des invalidités ; ils sont réparables sans "
            "modifier la forme du tracé.")

        self.chk_holes = QCheckBox("Détecter les trous dans les polygones")
        self.chk_holes.setChecked(C.UI_DEFAULT_CHECKS["holes"])
        self.chk_holes.setToolTip(
            "Repère les polygones comportant un trou (anneau intérieur) dont la "
            "surface est inférieure ou égale au seuil ci-dessous. Les grands "
            "trous, souvent justifiés (cour intérieure, îlot, plan d'eau), sont "
            "ignorés.\n"
            "Les signaler ne comble rien : le comblement est une case à cocher "
            "du dialogue de réparation, jamais implicite.")

        # Seuil de surface au-dessous duquel un trou est jugé fautif.
        self.spin_hole_threshold = QDoubleSpinBox()
        self.spin_hole_threshold.setRange(C.HOLE_THRESHOLD_MIN_M2, C.HOLE_THRESHOLD_MAX_M2)
        self.spin_hole_threshold.setDecimals(2)
        self.spin_hole_threshold.setValue(C.HOLE_THRESHOLD_DEFAULT_M2)
        _prep_threshold_spin(self.spin_hole_threshold, "m²")
        _tip_hole = (
            "Un trou dont la surface est ≤ ce seuil est considéré comme une "
            "erreur de saisie et peut être comblé.\nLes trous plus grands sont "
            "jugés justifiés et ne sont ni signalés ni supprimés.")
        self.spin_hole_threshold.setToolTip(_tip_hole)

        # ── Hauteur des polygones entièrement inclus ──
        # Venue d'« Extras » en 2.9.49 : ce n'est pas un contrôle de forme
        # réservé à un type de saisie, c'est une incohérence d'altitude — une
        # vraie faute de donnée, au même titre qu'un chevauchement. Il ne tourne
        # que si la couche porte AGL ou HEIGHT et le SIGNALE sinon : le cocher
        # par défaut ne coûte donc rien sur une couche qui n'a pas ces champs.
        # Sans seuil : la règle est une comparaison stricte entre deux valeurs
        # existantes, il n'y a rien à régler.
        self.chk_nested_height = QCheckBox(
            "%s Hauteur des polygones inclus (%s)"
            % (C.ICON_NESTED_HEIGHT, " / ".join(C.NESTED_HEIGHT_FIELDS)))
        self.chk_nested_height.setChecked(
            C.UI_DEFAULT_CHECKS["nested_height"])
        self.chk_nested_height.setToolTip(
            "Quand un polygone est ENTIÈREMENT inclus dans un autre, sa valeur "
            "d'altitude doit être STRICTEMENT supérieure à celle du polygone "
            "qui le contient — une superstructure posée sur un toit est plus "
            "haute que le bâtiment qui la porte. Égalité comprise dans "
            "l'anomalie.\n"
            "Les champs %s sont reconnus quelle que soit la casse (AGL, agl, "
            "Agl…), et chacun est comparé à son homologue : AGL avec AGL, "
            "HEIGHT avec HEIGHT, jamais l'un avec l'autre.\n"
            "Seul le polygone INCLUS est signalé : c'est sa valeur qui doit "
            "dépasser celle de son contenant.\n"
            "Une altitude vide ou non numérique d'un côté rend la paire non "
            "vérifiable : elle est comptée à part, jamais tenue pour conforme.\n"
            "Sans champ %s dans la couche, le contrôle ne tourne pas et le "
            "signale."
            % (" et ".join(C.NESTED_HEIGHT_FIELDS),
               " ni ".join(C.NESTED_HEIGHT_FIELDS)))

        # Les réglages fins ne sont actifs que si leur contrôle est coché.
        # Les autres signaux tiennent à jour le badge de la section (voir
        # _refresh_options_badge), pas seulement le grisage des champs.
        self.chk_topology.toggled.connect(self._sync_option_widgets)
        self.chk_dup.toggled.connect(self._sync_option_widgets)
        self.chk_dup_vertex.toggled.connect(self._sync_option_widgets)
        self.chk_holes.toggled.connect(self._sync_option_widgets)
        self.chk_small.toggled.connect(self._sync_option_widgets)
        self.chk_nested_height.toggled.connect(self._sync_option_widgets)
        self.spin_threshold.valueChanged.connect(self._sync_option_widgets)

        # Un contrôle et son seuil tiennent sur UNE seule ligne : la case à
        # cocher occupe la colonne extensible, le réglage se cale à droite.
        # Le seuil porte son unité en suffixe (« ≤ 2,00 m² »), ce qui rend
        # l'ancienne ligne « ↳ seuil (m²) » inutile — deux fois moins de
        # lignes à parcourir, et le réglage reste visuellement rattaché à sa
        # case. Les contrôles sans seuil sont d'abord regroupés.
        opt_lay.setColumnStretch(0, 1)
        opt_lay.addWidget(self.chk_topology, 0, 0, 1, 2)
        opt_lay.addWidget(self.chk_overlaps, 1, 0, 1, 2)
        opt_lay.addWidget(self.overlap_sub_options, 2, 0, 1, 2)
        opt_lay.addWidget(self.chk_dup, 3, 0, 1, 2)
        opt_lay.addWidget(self.chk_dup_vertex, 4, 0, 1, 2)
        opt_lay.addWidget(self.chk_small, 5, 0)
        opt_lay.addWidget(self.spin_threshold, 5, 1)
        opt_lay.addWidget(self.chk_holes, 6, 0)
        opt_lay.addWidget(self.spin_hole_threshold, 6, 1)
        # Sans seuil : la case occupe les deux colonnes.
        opt_lay.addWidget(self.chk_nested_height, 7, 0, 1, 2)

        section.content_layout().addWidget(holder)
        self.options_section = section
        return section

    def _build_extras_section(self):
        """Section « Extras » : validation polygonale avancée, REPLIÉE par défaut.

        Regroupe les contrôles de saisie (angles aigus, sommets rapprochés). Tous sont décochés par défaut et ne sont calculés
        que s'ils sont cochés — replier la section ne change rien au calcul, elle
        ne fait que masquer les réglages.
        """
        section = CollapsibleSection("Extras", expanded=False)
        section.header.setToolTip(
            "Contrôles complémentaires de forme et de cohérence des altitudes, "
            "à activer selon le type de données (bâti, parcellaire, réseaux…).\n"
            "Aucun n'est calculé tant qu'il n'est pas coché.")
        holder = QWidget()
        lay = QGridLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setVerticalSpacing(8)

        intro = QLabel(
            "Contrôles complémentaires de forme et d'altitude — à activer selon "
            "le type de données (bâti, parcellaire, réseaux…)."
        )
        intro.setObjectName("sgHint")
        intro.setWordWrap(True)
        lay.addWidget(intro, 0, 0, 1, 2)

        # ── Angles aigus ──
        self.chk_sharp_angles = QCheckBox(
            "%s Angles aigus (pointes de digitalisation)" % C.ICON_SHARP_ANGLE)
        self.chk_sharp_angles.setChecked(C.UI_DEFAULT_CHECKS["sharp_angles"])
        self.chk_sharp_angles.setToolTip(
            "Repère les sommets formant une pointe très fermée — typiquement un "
            "artefact de saisie. La réparation retire ces sommets sans jamais "
            "rendre la géométrie invalide." + C.TIP_OPT_IN_RESET)
        self.spin_sharp_angle = QDoubleSpinBox()
        self.spin_sharp_angle.setRange(C.SHARP_ANGLE_MIN_DEG, C.SHARP_ANGLE_MAX_DEG)
        self.spin_sharp_angle.setDecimals(1)
        self.spin_sharp_angle.setValue(C.SHARP_ANGLE_DEFAULT_DEG)
        # « < » et non « ≤ » : le seuil est l'angle minimal TOLÉRÉ, un sommet
        # n'est signalé que strictement en dessous.
        _prep_threshold_spin(self.spin_sharp_angle, "°", prefix="< ")
        _tip_sharp = ("Tout sommet dont l'angle intérieur est inférieur à cette "
                      "valeur est signalé comme pointe.")
        self.spin_sharp_angle.setToolTip(_tip_sharp)

        # ── Sommets trop rapprochés ──
        self.chk_close_vertices = QCheckBox(
            "%s Sommets trop rapprochés" % C.ICON_CLOSE_VERTEX)
        self.chk_close_vertices.setChecked(C.UI_DEFAULT_CHECKS["close_vertices"])
        self.chk_close_vertices.setToolTip(
            "Repère les sommets consécutifs séparés par une distance très "
            "faible (tracé sur-densifié).\nÀ ne pas confondre avec les sommets "
            "dupliqués : ici les sommets sont DISTINCTS, et leur fusion modifie "
            "légèrement le tracé." + C.TIP_OPT_IN_RESET)
        self.spin_close_vertex = QDoubleSpinBox()
        self.spin_close_vertex.setRange(C.CLOSE_VERTEX_MIN_M, C.CLOSE_VERTEX_MAX_M)
        self.spin_close_vertex.setDecimals(3)
        self.spin_close_vertex.setSingleStep(0.05)
        self.spin_close_vertex.setValue(C.CLOSE_VERTEX_DEFAULT_M)
        _prep_threshold_spin(self.spin_close_vertex, "m")
        _tip_close = ("Deux sommets consécutifs séparés de moins que cette "
                      "distance sont signalés comme trop rapprochés.")
        self.spin_close_vertex.setToolTip(_tip_close)

        # ── Croisements de lignes non nodés ──
        # Réservé aux couches de LIGNES : sans seuil, la règle est purement
        # topologique (un point d'intersection est, ou n'est pas, un sommet).
        self.chk_line_crossings = QCheckBox(
            "%s Croisements de lignes non nodés" % C.ICON_UNNODED_CROSSING)
        self.chk_line_crossings.setChecked(C.UI_DEFAULT_CHECKS["line_crossings"])
        self.chk_line_crossings.setToolTip(
            "Repère les points où deux lignes DISTINCTES se croisent ou se "
            "touchent sans qu'aucune des deux n'y porte de sommet (croix, T) : "
            "la jonction existe sur la carte, mais aucun outil réseau (routage, "
            "connexité) ne peut s'y accrocher.\n"
            "Un croisement d'une ligne SUR ELLE-MÊME n'est pas vérifié par ce "
            "contrôle.\n"
            "Réservé aux couches de lignes : sans entité de ce type, le "
            "contrôle ne tourne pas et le signale."
            + C.TIP_OPT_IN_RESET)

        for chk in (self.chk_sharp_angles, self.chk_close_vertices,
                    self.chk_line_crossings):
            chk.toggled.connect(self._sync_option_widgets)
        # La sous-option d'imbrication n'a de sens que si les chevauchements
        # sont recherchés.
        self.chk_overlaps.toggled.connect(self._sync_option_widgets)

        # Même règle qu'en « Options d'analyse » : un contrôle et son seuil
        # sur une seule ligne, unité portée par le champ lui-même.
        lay.setColumnStretch(0, 1)
        lay.addWidget(self.chk_sharp_angles, 1, 0)
        lay.addWidget(self.spin_sharp_angle, 1, 1)
        lay.addWidget(self.chk_close_vertices, 2, 0)
        lay.addWidget(self.spin_close_vertex, 2, 1)
        # Sans seuil : la case occupe les deux colonnes.
        lay.addWidget(self.chk_line_crossings, 3, 0, 1, 2)

        section.content_layout().addWidget(holder)
        self.extras_section = section
        return section

    def _refresh_extras_badge(self):
        """Indique sur l'en-tête replié combien de contrôles Extras sont actifs.

        Sans cela, un contrôle coché lors d'une session précédente resterait
        invisible une fois la section refermée.
        """
        if not hasattr(self, "extras_section"):
            return
        extras = (self.chk_sharp_angles, self.chk_close_vertices,
                  self.chk_line_crossings)
        active = sum(1 for chk in extras if chk.isChecked())
        if active:
            self.extras_section.set_badge("%d actif%s  "
                                          % (active, "s" if active > 1 else ""),
                                          active=True)
        else:
            self.extras_section.set_badge("%d contrôles  " % len(extras),
                                          active=False)

    def _refresh_options_badge(self):
        """Rappelle sur l'en-tête le nombre de contrôles actifs.

        La section ne se replie plus, mais le compte reste utile : il se lit
        d'un coup d'œil là où recenser les cases cochées demande de parcourir
        la liste.

        Le seuil des petits polygones n'est rappelé que si le contrôle
        correspondant est coché : l'afficher en permanence laissait croire
        qu'un filtrage par surface était toujours à l'œuvre.
        """
        if not hasattr(self, "options_section"):
            return
        checks = (self.chk_topology, self.chk_overlaps, self.chk_dup,
                  self.chk_small, self.chk_dup_vertex, self.chk_holes,
                  self.chk_nested_height)
        active = sum(1 for chk in checks if chk.isChecked())
        seuil = (" · seuil %g m²" % self.spin_threshold.value()
                 if self.chk_small.isChecked() else "")
        self.options_section.set_badge(
            "%d actif%s%s  " % (active, "s" if active > 1 else "", seuil),
            active=False)

    def _reset_options(self):
        """Remet les options d'analyse — y compris Extras — aux valeurs par
        défaut du PANNEAU (``C.UI_DEFAULT_CHECKS``), pas aux derniers réglages
        mémorisés : un retour à un état de référence connu.

        Ce sont les défauts de l'INTERFACE et non ceux de ``AnalysisOptions()``,
        qui reste plus conservateur pour les usages programmatiques (Processing,
        scripts) où personne ne voit de case à cocher. « Réinitialiser » doit
        rendre ce que l'utilisateur voit à la première ouverture.
        """
        d = AnalysisOptions()
        defauts = C.UI_DEFAULT_CHECKS
        self.chk_topology.setChecked(defauts["topology"])
        self.chk_overlaps.setChecked(defauts["overlaps"])
        self.chk_allow_contained.setChecked(defauts["allow_contained"])
        self.chk_ignore_missing_vertex.setChecked(
            defauts["ignore_missing_vertex"])
        self.chk_dup.setChecked(defauts["duplicates"])
        self.chk_dup_vertex.setChecked(defauts["duplicate_vertex"])
        self.chk_small.setChecked(defauts["small"])
        self.chk_holes.setChecked(defauts["holes"])
        self.chk_nested_height.setChecked(defauts["nested_height"])
        self.chk_sharp_angles.setChecked(defauts["sharp_angles"])
        self.chk_close_vertices.setChecked(defauts["close_vertices"])
        self.chk_line_crossings.setChecked(defauts["line_crossings"])
        # Les SEUILS, eux, restent ceux du moteur : une seule valeur de
        # référence sert au panneau comme aux usages programmatiques.
        self.spin_threshold.setValue(d.small_polygon_threshold_m2)
        self.spin_hole_threshold.setValue(d.hole_max_area_m2)
        self.spin_sharp_angle.setValue(d.sharp_angle_min_deg)
        self.spin_close_vertex.setValue(d.close_vertex_min_dist_m)
        # La zone d'analyse fait partie de l'état à remettre à plat : la
        # laisser active après « Réinitialiser » restreindrait l'analyse
        # suivante alors que l'utilisateur croit être reparti de zéro.
        self.chk_aoi.setChecked(False)
        self._set_aoi(None)
        self._sync_option_widgets()
        self.status_label.setText("Options réinitialisées aux valeurs par défaut.")

    def _build_summary_section(self):
        """Synthèse : les cartes d'indicateurs, et rien d'autre.

        Seul bloc de résultats du panneau depuis la 2.9.49. Le détail — tableau
        qualité, détails d'invalidité, aperçu des attributs, liste des anomalies
        — vit dans les rapports, où il a la largeur d'une page pour se lire.
        """
        group = QGroupBox("Synthèse")
        lay = QVBoxLayout(group)
        lay.setContentsMargins(12, 14, 12, 12)
        lay.setSpacing(12)

        cards_holder = QWidget()
        self.cards_layout = FlowLayout(cards_holder, margin=0, spacing=8)
        lay.addWidget(cards_holder)

        hint = QLabel(
            "Le détail complet — tableau qualité, invalidités, attributs, "
            "anomalies entité par entité — est dans le rapport : bouton "
            "« Exporter » → « Rapport navigateur », « HTML… » ou « PDF… ».")
        hint.setObjectName("sgHint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        return group

    # ── Actions source ────────────────────────────────────────────────────

    def _on_load_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Charger une couche vectorielle", sg_settings.load_last_dir(), _SUPPORTED
        )
        if not path:
            return
        sg_settings.save_last_dir(path)
        name = os.path.splitext(os.path.basename(path))[0]
        layer = QgsVectorLayer(path, name, "ogr")
        if not layer.isValid():
            QMessageBox.critical(self, C.PLUGIN_TITLE, "Couche invalide :\n%s" % path)
            return
        QgsProject.instance().addMapLayer(layer)
        self.layer_combo.setLayer(layer)
        self.status_label.setText("Couche chargée : %s" % name)

    @staticmethod
    def _safe_filename(name):
        """Nettoie un nom de couche pour en faire un nom de fichier valide."""
        base = (name or "rapport").strip()
        for ch in '<>:"/\\|?*':
            base = base.replace(ch, "_")
        base = base.strip(" .")
        return base or "rapport"

    def _default_path(self, filename):
        """Chemin proposé dans les dialogues d'enregistrement (dernier dossier)."""
        last = sg_settings.load_last_dir()
        return os.path.join(last, filename) if last else filename

    @classmethod
    def _automatic_repair_path(cls, source, layer_name, fallback_dir=""):
        """Chemin de réparation sans dialogue : à côté du fichier source.

        Le nom du fichier source est conservé et reçoit le suffixe
        ``_repare``. Son format est conservé quand le plugin sait l'écrire ;
        les autres sources fichier (TAB, KML, GDB…) produisent un GeoPackage.
        Une couche sans fichier local conserve l'ancien repli vers le dernier
        dossier utilisé.
        """
        source_path = (source or "").split("|", 1)[0].strip()
        source_dir, source_filename = os.path.split(source_path)
        source_stem, source_ext = os.path.splitext(source_filename)

        is_local_file = (
            "://" not in source_path
            and (source_ext.lower() in C.DRIVER_BY_EXT
                 or os.path.exists(source_path))
        )
        if source_stem and source_ext and is_local_file:
            output_ext = (source_ext if source_ext.lower() in C.WRITABLE_EXTENSIONS
                          else ".gpkg")
            return os.path.join(
                source_dir,
                "%s_repare%s" % (source_stem, output_ext),
            )

        filename = "%s_repare.gpkg" % cls._safe_filename(layer_name or "couche")
        return os.path.join(fallback_dir, filename) if fallback_dir else filename

    @staticmethod
    def _free_variant(path):
        """Premier chemin LIBRE dérivé de ``path`` : ``…_2``, ``…_3``…

        Sert à proposer une sortie qui n'écrase rien. Plafonné : au-delà, on
        rend le chemin d'origine plutôt que de boucler — c'est alors à la
        confirmation d'écrasement de trancher.
        """
        stem, ext = os.path.splitext(path)
        for index in range(2, 100):
            candidat = "%s_%d%s" % (stem, index, ext)
            if not os.path.exists(candidat):
                return candidat
        return path

    def _confirm_repair_output(self, path):
        """Chemin de sortie confirmé, ou ``None`` si l'utilisateur renonce.

        Depuis que la réparation écrit automatiquement à côté de la source,
        plus aucun dialogue d'enregistrement ne prévient qu'un fichier du même
        nom existe déjà : deux réparations d'affilée remplaçaient la première
        SANS RIEN DIRE — y compris si l'utilisateur avait entre-temps travaillé
        dessus. C'est exactement la perte silencieuse que ce plugin refuse
        partout ailleurs.

        Conforme à la règle « l'application propose, elle ne décide pas » : les
        deux issues mènent quelque part, aucune n'est un cul-de-sac.
        """
        if not os.path.exists(path):
            return path
        autre = self._free_variant(path)
        boite = QMessageBox(self)
        boite.setIcon(QMessageBox.Icon.Question)
        boite.setWindowTitle(C.PLUGIN_TITLE)
        boite.setText("Le fichier suivant existe déjà :\n%s" % path)
        boite.setInformativeText(
            "L'écraser remplacera définitivement son contenu."
            if autre == path else
            "L'écraser remplacera définitivement son contenu.\n\n"
            "Sinon, la réparation peut être écrite à côté, dans :\n%s"
            % os.path.basename(autre))
        ecraser = boite.addButton("Écraser", QMessageBox.ButtonRole.DestructiveRole)
        nouveau = None
        if autre != path:
            nouveau = boite.addButton("Nouveau fichier",
                                      QMessageBox.ButtonRole.AcceptRole)
            boite.setDefaultButton(nouveau)
        annuler = boite.addButton(QMessageBox.StandardButton.Cancel)
        boite.exec()
        clique = boite.clickedButton()
        if clique is annuler or clique is None:
            return None
        if nouveau is not None and clique is nouveau:
            return autre
        if clique is ecraser:
            return path
        return None

    def _current_layer(self):
        layer = self.layer_combo.currentLayer()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            return None
        return layer

    # ── Zone d'analyse (AOI) ──────────────────────────────────────────────

    def _sync_aoi_widgets(self):
        """Aligne les boutons et le libellé sur l'état réel de la zone."""
        actif = self.chk_aoi.isChecked()
        for widget in (self.btn_aoi_draw, self.btn_aoi_canvas):
            widget.setEnabled(actif)
        self.btn_aoi_clear.setEnabled(actif and self._aoi_rect is not None)
        if not actif:
            self.aoi_label.setText(C.MSG_AOI_NONE)
            return
        if self._aoi_rect is None:
            self.aoi_label.setText(
                "Aucune zone tracée — « Dessiner… » ou « Étendue visible ».")
            return
        # Dimensions annoncées dans l'unité de la carte : l'utilisateur doit
        # pouvoir vérifier d'un coup d'œil qu'il n'a pas tracé 3 m de côté.
        self.aoi_label.setText(
            "Zone : %s × %s (%s)"
            % (C.fmt_int(round(self._aoi_rect.width())),
               C.fmt_int(round(self._aoi_rect.height())),
               self._aoi_crs_authid or "unités de la carte"))

    def _on_aoi_toggled(self, _checked=False):
        """Décocher la restriction efface le tracé : laisser une zone
        mémorisée sous une case décochée ferait douter du périmètre réel de la
        prochaine analyse."""
        if not self.chk_aoi.isChecked():
            self._set_aoi(None)
        else:
            self._sync_aoi_widgets()

    def _canvas(self):
        if self.iface is None:
            return None
        try:
            return self.iface.mapCanvas()
        except Exception:
            return None

    def _set_aoi(self, rect, authid=""):
        """Retient (ou oublie) la zone et met à jour son tracé sur la carte."""
        self._aoi_rect = rect
        self._aoi_crs_authid = authid if rect is not None else ""
        self._clear_aoi_band()
        if rect is not None:
            self._draw_aoi_band(rect)
        self._sync_aoi_widgets()

    def _clear_aoi(self):
        self._set_aoi(None)

    def _clear_aoi_band(self):
        band, self._aoi_band = self._aoi_band, None
        if band is None:
            return
        with contextlib.suppress(Exception):
            scene = band.scene()
            if scene is not None:
                scene.removeItem(band)

    def _draw_aoi_band(self, rect):
        """Trace la zone RETENUE en permanence sur le canevas.

        Une zone invisible serait la pire des situations : l'analyse porterait
        sur un périmètre que l'utilisateur ne voit pas et croirait avoir
        oublié. Comme la surbrillance d'inspection, c'est une bande élastique —
        rien n'est ajouté au projet.
        """
        canvas = self._canvas()
        if canvas is None:
            return
        with contextlib.suppress(Exception):
            band = QgsRubberBand(canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
            couleur = QColor(*(int(x) for x in C.AOI_COLOR.split(",")))
            band.setColor(couleur)
            remplissage = QColor(couleur)
            remplissage.setAlpha(C.AOI_FILL_ALPHA)
            band.setFillColor(remplissage)
            band.setWidth(C.AOI_WIDTH)
            band.setToGeometry(QgsGeometry.fromRect(rect), None)
            self._aoi_band = band

    def _aoi_from_canvas(self):
        """Reprend le cadrage actuel de la carte comme zone d'analyse."""
        canvas = self._canvas()
        if canvas is None:
            QMessageBox.information(self, C.PLUGIN_TITLE, C.MSG_AOI_NO_CANVAS)
            return
        try:
            rect = QgsRectangle(canvas.extent())
            authid = canvas.mapSettings().destinationCrs().authid()
        except Exception:
            QMessageBox.information(self, C.PLUGIN_TITLE, C.MSG_AOI_NO_CANVAS)
            return
        if rect.isEmpty():
            QMessageBox.information(self, C.PLUGIN_TITLE, C.MSG_AOI_NO_CANVAS)
            return
        self._set_aoi(rect, authid)
        self.status_label.setText("Zone reprise du cadrage de la carte.")

    def _draw_aoi(self):
        """Active l'outil de tracé, en mémorisant l'outil précédent.

        L'outil est rendu à l'utilisateur dès le rectangle tracé ou annulé :
        le laisser actif enfermerait la session de QGIS dans un mode dont
        rien n'indique la sortie.
        """
        canvas = self._canvas()
        if canvas is None:
            QMessageBox.information(self, C.PLUGIN_TITLE, C.MSG_AOI_NO_CANVAS)
            return
        # Deuxième clic alors que le tracé est déjà actif : ne RIEN faire de
        # plus. Sans cette garde, ``canvas.mapTool()`` renvoyait l'outil de
        # tracé lui-même, qui devenait « l'outil précédent » — et le rétablir
        # en fin de tracé enfermait l'utilisateur dans le mode zone, sans
        # aucune sortie visible.
        if self._aoi_tool is not None:
            self.status_label.setText(C.MSG_AOI_DRAW_HINT)
            return
        self._aoi_previous_tool = canvas.mapTool()
        tool = AoiRectTool(canvas)
        tool.rectDefined.connect(self._on_aoi_drawn)
        tool.canceled.connect(self._on_aoi_draw_canceled)
        self._aoi_tool = tool
        canvas.setMapTool(tool)
        self.status_label.setText(C.MSG_AOI_DRAW_HINT)

    def _restore_map_tool(self):
        canvas = self._canvas()
        tool, self._aoi_tool = self._aoi_tool, None
        previous, self._aoi_previous_tool = self._aoi_previous_tool, None
        if canvas is None:
            return
        with contextlib.suppress(Exception):
            if previous is not None:
                canvas.setMapTool(previous)
            elif tool is not None:
                canvas.unsetMapTool(tool)

    def _on_aoi_drawn(self, rect):
        canvas = self._canvas()
        authid = ""
        if canvas is not None:
            with contextlib.suppress(Exception):
                authid = canvas.mapSettings().destinationCrs().authid()
        self._restore_map_tool()
        self._set_aoi(QgsRectangle(rect), authid)
        self.status_label.setText("Zone d'analyse définie.")

    def _on_aoi_draw_canceled(self):
        self._restore_map_tool()
        self.status_label.setText("Tracé de zone annulé.")
        self._sync_aoi_widgets()

    def _aoi_for_options(self):
        """``(rect_tuple, authid)`` à passer à ``AnalysisOptions``.

        ``(None, "")`` dès que la case est décochée ou qu'aucune zone n'a été
        tracée : la restriction doit être un acte explicite, jamais un reste
        d'un réglage précédent.
        """
        if not self.chk_aoi.isChecked() or self._aoi_rect is None:
            return (None, "")
        r = self._aoi_rect
        return ((r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum()),
                self._aoi_crs_authid)

    # ── Péremption du résultat sur édition de la couche ───────────────────

    #: Signaux dont l'émission rend les FID du résultat douteux. Volontairement
    #: large : la géométrie ET les attributs comptent (le contrôle de hauteur
    #: des polygones inclus se prononce sur des valeurs d'altitude), et le
    #: fournisseur comme la couche en édition peuvent être la source du
    #: changement. Un signal absent d'une version de QGIS est simplement
    #: ignoré (voir _watch_layer_edits).
    _STALE_SIGNALS = (
        # Pendant l'édition (tampon de la couche)
        "featureAdded", "featureDeleted", "featuresDeleted",
        "geometryChanged", "attributeValueChanged", "layerModified",
        # À l'enregistrement ou à l'annulation
        "committedFeaturesAdded", "committedFeaturesRemoved",
        "committedGeometriesChanges", "committedAttributeValuesChanges",
        "afterCommitChanges", "afterRollback",
        # Rechargement / changement de source (les FID sont refaits)
        "dataChanged", "dataSourceChanged",
    )

    @staticmethod
    def _layer_by_id(layer_id):
        """La couche du projet portant cet id, ou ``None``.

        La couche du RÉSULTAT, et non celle affichée dans la liste : la
        sélection a pu changer pendant que l'analyse tournait.
        """
        if not layer_id:
            return None
        try:
            layer = QgsProject.instance().mapLayer(layer_id)
        except Exception:
            return None
        return layer if isinstance(layer, QgsVectorLayer) else None

    def _watch_layer_edits(self, layer):
        """Écoute les éditions de ``layer`` pour périmer le résultat.

        Sans cela, analyser une couche, y supprimer ou déplacer des entités,
        puis cliquer « Sélectionner les entités » ou « Réparer » travaillait
        sur des identifiants d'un état révolu — au mieux sur les mauvaises
        entités, au pire en les supprimant.
        """
        self._unwatch_layer_edits()
        self._result_stale = False
        if layer is None:
            return
        connected = False
        for name in self._STALE_SIGNALS:
            signal = getattr(layer, name, None)
            if signal is None:
                continue
            with contextlib.suppress(Exception):
                signal.connect(self._on_watched_layer_edited)
                connected = True
        self._watched_layer = layer if connected else None

    def _unwatch_layer_edits(self):
        layer = self._watched_layer
        self._watched_layer = None
        if layer is None:
            return
        for name in self._STALE_SIGNALS:
            signal = getattr(layer, name, None)
            if signal is None:
                continue
            with contextlib.suppress(Exception):
                signal.disconnect(self._on_watched_layer_edited)

    def _on_watched_layer_edited(self, *_args):
        """La couche analysée a changé : le résultat reste AFFICHÉ (il dit ce
        qui a été trouvé), mais il n'autorise plus d'action sur la couche."""
        if self._result_stale:
            return
        self._result_stale = True
        with contextlib.suppress(Exception):
            self.status_label.setText(
                "Couche modifiée depuis l'analyse — relancez l'analyse avant "
                "d'agir sur la couche.")

    @staticmethod
    def _same_file(a: str, b: str) -> bool:
        """Deux chemins désignent-ils le même fichier ?

        ``os.path.samefile`` d'abord (le seul verdict exact : il compare les
        inodes / identifiants de fichier), avec repli sur une comparaison
        normalisée quand l'un des deux n'existe pas encore — cas d'un
        enregistrement vers un nouveau fichier.
        """
        if not a or not b:
            return False
        with contextlib.suppress(Exception):
            if os.path.exists(a) and os.path.exists(b):
                return os.path.samefile(a, b)
        try:
            return (os.path.normcase(os.path.realpath(a))
                    == os.path.normcase(os.path.realpath(b)))
        except Exception:
            return False

    # ── Analyse ───────────────────────────────────────────────────────────

    def _is_busy(self) -> bool:
        """Un traitement de fond est-il en cours ?

        ``_verify_task`` (vérification automatique du fichier réparé) EN FAIT
        PARTIE : l'oublier laissait démarrer une analyse ou une réparation
        pendant qu'elle tournait, deux tâches se disputant alors l'affichage
        de progression et le même état d'interface.
        """
        return any(task is not None for task in
                   (self._analysis_task, self._repair_task, self._verify_task))

    def _run_analysis(self):
        if self._is_busy():
            QMessageBox.information(self, C.PLUGIN_TITLE, C.MSG_BUSY)
            return
        layer = self._current_layer()
        error = check_layer_ready(layer)
        if error:
            QMessageBox.warning(self, C.PLUGIN_TITLE, error)
            return

        opts = AnalysisOptions(
            check_small_polygons=self.chk_small.isChecked(),
            small_polygon_threshold_m2=self.spin_threshold.value(),
            topology_checks=self.chk_topology.isChecked(),
            check_duplicates=self.chk_dup.isChecked(),
            check_duplicate_vertex=self.chk_dup_vertex.isChecked(),
            check_holes=self.chk_holes.isChecked(),
            hole_max_area_m2=self.spin_hole_threshold.value(),
            check_overlaps=self.chk_overlaps.isChecked(),
            allow_contained_polygons=self.chk_allow_contained.isChecked(),
            ignore_missing_vertex_overlaps=(
                self.chk_ignore_missing_vertex.isChecked()),
            check_nested_height=self.chk_nested_height.isChecked(),
            # Section « Extras »
            check_sharp_angles=self.chk_sharp_angles.isChecked(),
            sharp_angle_min_deg=self.spin_sharp_angle.value(),
            check_close_vertices=self.chk_close_vertices.isChecked(),
            close_vertex_min_dist_m=self.spin_close_vertex.value(),
            check_line_crossings=self.chk_line_crossings.isChecked(),
            # ``profile_name`` reste vide : les préréglages ont disparu en
            # 2.9.49, le panneau n'a plus qu'un mode sans nom. Le champ survit
            # pour les usages programmatiques (un script peut nommer son profil
            # et le voir consigné dans le rapport).
        )
        # Zone d'analyse : appliquée en dernier, HORS des options persistées
        # (voir save_options). Une zone restaurée en silence d'une session à
        # l'autre restreindrait une analyse sans que rien ne le dise.
        opts.aoi_rect, opts.aoi_crs_authid = self._aoi_for_options()
        sg_settings.save_options(opts)
        # Mémorisé pour la vérification automatique post-réparation (voir
        # _on_repair_completed) : elle doit rejouer EXACTEMENT les mêmes
        # contrôles que l'analyse d'origine pour que la comparaison soit
        # significative.
        self._last_analysis_options = opts

        # Copie privée pour le thread d'arrière-plan (voir tasks.py). Les FID du
        # clone restent alignés sur la couche d'origine.
        worker = layer.clone()
        if worker is None or not worker.isValid():
            QMessageBox.critical(
                self, C.PLUGIN_TITLE,
                "Impossible de préparer la couche pour l'analyse (clone invalide)."
            )
            return
        # Mémorisé seulement, puis confirmé en cas de SUCCÈS : sur échec ou
        # annulation, le résultat précédent doit rester associé à sa couche.
        self._pending_layer_id = layer.id()

        task = AnalysisTask(worker, opts)
        task.progressChanged.connect(self._on_task_progress)
        task.progressMessage.connect(self._on_task_message)
        task.taskCompleted.connect(self._on_analysis_completed)
        task.taskTerminated.connect(self._on_analysis_terminated)
        self._analysis_task = task

        self._enter_running_state("Analyse en cours…")
        QgsApplication.taskManager().addTask(task)

    def _on_analysis_completed(self):
        task = self._analysis_task
        self._analysis_task = None
        self._leave_running_state()
        if task is None or task.result is None:
            return
        result = task.result
        # Score de l'analyse précédente DE CETTE SESSION, quelle que soit la
        # couche : la comparaison la plus utile est justement inter-couches
        # (analyser l'original, réparer, puis analyser le fichier réparé).
        previous_score = self._result.correctness.score if self._result is not None else None
        self._result = result
        self._result_layer_id = self._pending_layer_id
        # Le résultat est neuf : on repart de « non périmé » et on se met à
        # l'écoute des éditions de la couche ANALYSÉE — résolue par son id, la
        # sélection ayant pu changer pendant que l'analyse tournait.
        self._watch_layer_edits(self._layer_by_id(self._result_layer_id))
        self._display(result, previous_score)
        self._set_actions_enabled(True)
        self.status_label.setText(
            "Analyse terminée en %.2f s — score %g/100 (%s)."
            % (result.processing_time, result.correctness.score, result.correctness.grade)
        )

    def _on_analysis_terminated(self):
        task = self._analysis_task
        self._analysis_task = None
        self._leave_running_state()
        # Un résultat précédent reste exploitable : ses actions restent actives.
        if self._result is not None:
            self._set_actions_enabled(True)
        if task is not None and task.error:
            QMessageBox.critical(
                self, C.PLUGIN_TITLE, "Échec de l'analyse :\n%s" % task.error)
            self.status_label.setText("Échec de l'analyse.")
        else:
            self.status_label.setText("Analyse annulée.")

    # -- État de l'interface pendant un traitement --------------------------

    def _enter_running_state(self, message: str):
        self.btn_analyze.setEnabled(False)
        self._set_actions_enabled(False)
        self.prog_widget.setVisible(True)
        self.progress.setValue(0)
        self.status_label.setText(message)

    def _leave_running_state(self):
        self.btn_analyze.setEnabled(True)
        self.prog_widget.setVisible(False)

    def _on_task_progress(self, pct):
        self.progress.setValue(int(pct))

    def _on_task_message(self, msg):
        if msg:
            self.status_label.setText(msg)

    def _on_cancel(self):
        for task in (self._analysis_task, self._repair_task, self._verify_task):
            if task is not None:
                task.cancel()
        self.status_label.setText("Annulation demandée…")

    def cancel_tasks(self):
        """Annule proprement tout traitement en cours (appelé au déchargement).

        Rend TOUT ce que le panneau a pris au reste de QGIS. Le rectangle de
        la zone d'analyse survivant au déchargement resterait dessiné sur la
        carte, sans plus rien dans le panneau des couches pour l'expliquer ni
        personne pour l'effacer.

        L'outil de tracé est rendu de la même façon : décharger le plugin en
        plein tracé laissait l'utilisateur prisonnier d'un mode de carte dont
        plus aucun bouton n'expliquait la sortie. Et les signaux d'édition de
        la couche analysée sont débranchés : ils pointent vers un panneau qui
        n'existera plus.
        """
        with contextlib.suppress(Exception):
            self._restore_map_tool()
        with contextlib.suppress(Exception):
            self._clear_aoi_band()
        with contextlib.suppress(Exception):
            self._unwatch_layer_edits()
        self._on_cancel()

    def _set_actions_enabled(self, enabled: bool):
        for btn in (self.btn_select, self.btn_errlayer, self.btn_repair,
                    self.btn_export):
            btn.setEnabled(enabled)

    def _restore_settings(self):
        """Réapplique les derniers paramètres utiles à l'ouverture du panneau.

        Sur un profil vierge, ``load_options`` rend déjà les défauts du PANNEAU
        (``C.UI_DEFAULT_CHECKS``) : tout « Options d'analyse » coché, rien
        d'« Extras ». Il n'y a donc plus rien à rattraper ici — le repli sur un
        préréglage, qui réécrivait parfois des cases délibérément décochées, a
        disparu avec les préréglages eux-mêmes en 2.9.49.
        """
        opts = sg_settings.load_options()
        self.chk_small.setChecked(opts.check_small_polygons)
        self.spin_threshold.setValue(opts.small_polygon_threshold_m2)
        self.chk_topology.setChecked(opts.topology_checks)
        self.chk_overlaps.setChecked(opts.check_overlaps)
        self.chk_allow_contained.setChecked(opts.allow_contained_polygons)
        self.chk_ignore_missing_vertex.setChecked(
            opts.ignore_missing_vertex_overlaps)
        self.chk_dup.setChecked(opts.check_duplicates)
        self.chk_dup_vertex.setChecked(opts.check_duplicate_vertex)
        self.chk_holes.setChecked(opts.check_holes)
        self.spin_hole_threshold.setValue(opts.hole_max_area_m2)
        self.chk_sharp_angles.setChecked(opts.check_sharp_angles)
        self.spin_sharp_angle.setValue(opts.sharp_angle_min_deg)
        self.chk_close_vertices.setChecked(opts.check_close_vertices)
        self.spin_close_vertex.setValue(opts.close_vertex_min_dist_m)
        self.chk_nested_height.setChecked(opts.check_nested_height)
        self.chk_line_crossings.setChecked(opts.check_line_crossings)
        self._sync_option_widgets()
        self._set_actions_enabled(False)

    def _sync_option_widgets(self):
        """Grise les réglages fins dont le contrôle parent est décoché."""
        # Certains signaux sont connectés avant la fin de la construction de
        # l'interface : on ne synchronise qu'une fois tous les widgets présents.
        if not hasattr(self, "spin_close_vertex"):
            return
        pairs = (
            (self.chk_small, self.spin_threshold),
            (self.chk_holes, self.spin_hole_threshold),
            (self.chk_sharp_angles, self.spin_sharp_angle),
            (self.chk_close_vertices, self.spin_close_vertex),
        )
        for checkbox, spin in pairs:
            spin.setEnabled(checkbox.isChecked())
        # Les deux exceptions n'ont de sens que si les chevauchements sont
        # contrôlés : l'encart entier suit donc la case parente.
        self.overlap_sub_options.setEnabled(self.chk_overlaps.isChecked())
        self._refresh_extras_badge()
        self._refresh_options_badge()

    # ── Affichage des résultats ───────────────────────────────────────────

    def _update_score_delta(self, score, previous_score):
        """Écart avec la dernière analyse de la session (ex. avant/après
        réparation) : rend le bénéfice d'une correction visible sans avoir à
        comparer deux captures mentalement. Masqué s'il n'y a rien à comparer
        ou si l'écart est négligeable (bruit d'arrondi)."""
        # Une note « Non évalué » (zone vide) vaut 0 sans rien mesurer :
        # la comparer afficherait « −100 pts » en rouge, comme un effondrement
        # de la qualité, alors que rien n'a été regardé.
        if self._result is not None and self._result.correctness.grade == C.GRADE_NOT_EVALUATED:
            self.grade_delta.setVisible(False)
            return
        delta = None if previous_score is None else score - previous_score
        if delta is None or abs(delta) < 0.05:
            self.grade_delta.setVisible(False)
            return
        sign = "+" if delta > 0 else "−"
        color = self._colors["ok"] if delta > 0 else self._colors["danger"]
        self.grade_delta.setText(
            "%s%.1f pt%s vs analyse précédente"
            % (sign, abs(delta), "s" if abs(delta) >= 2 else ""))
        self.grade_delta.setStyleSheet("color:%s;font-size:11px;" % color)
        self.grade_delta.setVisible(True)

    def _display(self, result, previous_score=None):
        c = result.correctness
        self.results_widget.setVisible(True)
        # La couleur du moteur est calibrée pour un fond clair : on la ré-associe
        # au thème courant pour rester lisible en palette sombre.
        grade_hex = sg_theme.grade_color(c.grade, self._dark, c.color)
        self.gauge.set_score(c.score, grade_hex, c.grade)
        self.grade_label.setText(c.grade)
        self.grade_label.setStyleSheet(
            "font-size:19px;font-weight:600;color:%s;" % grade_hex)
        self._update_score_delta(c.score, previous_score)
        self.grade_desc.setText(
            "%d alertes sur %s entités analysées.%s"
            % (result.total_issues(), C.fmt_int(result.total_features),
               report.skipped_note(result))
        )
        couverture = report.coverage_text(result)
        self.grade_coverage.setText(couverture)
        self.grade_coverage.setVisible(bool(couverture))

        self._build_metric_cards(result)

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            wdg = item.widget()
            if wdg is not None:
                wdg.setParent(None)
                wdg.deleteLater()

    def _build_metric_cards(self, result):
        self._clear_layout(self.cards_layout)
        q = result.quality_report
        # Contrôle réellement exécuté pendant CETTE analyse (result.checks_enabled),
        # pas l'état actuel des cases : l'utilisateur a pu re-cocher/décocher
        # entre-temps, la carte doit refléter ce qui a produit le résultat affiché.
        checks = result.checks_enabled
        # Un contrôle qui a TOURNÉ sans pouvoir conclure (surface non mesurable,
        # aucune paire imbriquée comparable…) ne doit pas fusionner avec « non
        # demandé » : les deux rendaient la même carte grise « 0 », alors que
        # l'onglet Qualité et le score affichent explicitement « Non concluant »
        # pour la même analyse.
        inconclusive = getattr(result, "checks_inconclusive", None) or {}

        def _muted_label(cle, libelle):
            return "%s (n.c.)" % libelle if inconclusive.get(cle) else libelle

        def _muted_value(cle, valeur):
            return "?" if inconclusive.get(cle) else valeur

        # Toujours calculés : aucune case ne les désactive.
        cards = [
            (C.fmt_int(result.total_features), "Entités", "ok"),
            (C.fmt_int(q.null_empty_count), "Nulles/Vides", "err" if q.null_empty_count else "muted"),
            (C.fmt_int(q.invalid_count), "Invalides", "err" if q.invalid_count else "muted"),
        ]
        # Contrôles décochables : la carte n'apparaît que si le contrôle a
        # tourné (ou qu'une anomalie a été trouvée), même convention que les
        # cartes suivantes. Une carte « 0 » pour un contrôle non exécuté se
        # lisait comme « couche propre » et contredisait l'onglet Qualité, qui
        # affiche « Non demandé » sur la même analyse.
        if q.self_intersection_count or checks.get("self_intersection"):
            cards.append((
                C.fmt_int(q.self_intersection_count), "Auto-inters.",
                "err" if q.self_intersection_count else "muted",
            ))
        if q.overlap_count or checks.get("overlap"):
            cards.append((
                C.fmt_int(q.overlap_count), "Chevauch.",
                "err" if q.overlap_count else "muted",
            ))
        if q.small_area_count or checks.get("small"):
            cards.append((
                _muted_value("small", C.fmt_int(q.small_area_count)),
                _muted_label("small", "≤ %g m²" % result.threshold_m2),
                "warn" if q.small_area_count else "muted",
            ))
        if q.duplicate_count or checks.get("duplicate"):
            cards.append((
                C.fmt_int(q.duplicate_count), "Doublons",
                "warn" if q.duplicate_count else "muted",
            ))
        if q.duplicate_vertex_count or checks.get("duplicate_vertex"):
            cards.append((
                C.fmt_int(q.duplicate_vertex_count), "Vertex dupl.",
                "warn" if q.duplicate_vertex_count else "muted",
            ))
        if q.hole_count or checks.get("hole"):
            cards.append((
                C.fmt_int(q.hole_count), "Trous",
                "warn" if q.hole_count else "muted",
            ))
        if q.multipart_count or checks.get("multipart"):
            cards.append((
                C.fmt_int(q.multipart_count), "Multi-parties",
                "warn" if q.multipart_count else "muted",
            ))
        # Section « Autres » : n'afficher la carte que si le contrôle est actif
        # ou qu'une anomalie a été trouvée.
        for cle, count, active, label in (
            (None, q.sharp_angle_count, checks.get("sharp_angle"), "Angles aigus"),
            (None, q.close_vertex_count, checks.get("close_vertex"), "Somm. rappr."),
            ("nested_height", q.nested_height_count, checks.get("nested_height"), "Haut. incluses"),
            (None, q.unnoded_crossing_count, checks.get("unnoded_crossing"), "Croisements non nodés"),
        ):
            if count or active:
                cards.append((
                    _muted_value(cle, C.fmt_int(count)),
                    _muted_label(cle, label),
                    "warn" if count else "muted",
                ))
        for value, label, sev in cards:
            self.cards_layout.addWidget(MetricCard(
                value, label, sev, palette=self._severity,
                label_color=self._colors["text_muted"]))

    # ── Actions QGIS ──────────────────────────────────────────────────────

    def _all_flagged_fids(self):
        fids = set()
        for cat in _ALL_CATEGORIES:
            fids.update(self._result.flagged.get(cat, []))
        return list(fids)

    def _select_features(self):
        if not self._require_result():
            return
        layer = self._matching_layer()
        if layer is None:
            return
        fids = self._all_flagged_fids()
        if not fids:
            QMessageBox.information(self, "STAT GEOM QC", "Aucune entité problématique à sélectionner.")
            return
        layer.selectByIds(fids)
        with contextlib.suppress(Exception):
            self.iface.mapCanvas().zoomToSelected(layer)
        self.status_label.setText("%d entités problématiques sélectionnées." % len(fids))

    def _create_error_layer(self):
        """Charge dans le projet une couche de points localisant les anomalies.

        La construction vit dans ``error_layer``, partagée avec les algorithmes
        Processing : ici ne restent que les gestes propres au panneau — vérifier
        qu'un résultat existe, prévenir l'utilisateur, styliser, ajouter au
        projet, écrire dans la barre de statut.
        """
        if not self._require_result():
            return
        layer = self._matching_layer()
        if layer is None:
            return

        mem, tronque = EL.build_error_layer(layer, self._result)
        if mem is None:
            QMessageBox.information(
                self, C.PLUGIN_TITLE,
                "Aucun point d'erreur localisable n'a pu être créé "
                "(une géométrie nulle ou vide n'a pas de position).")
            return

        nb = mem.featureCount()
        # Étiquettes TOUJOURS actives. Elles étaient coupées au-delà de 500
        # points, au motif qu'elles se chevaucheraient — mais QGIS écarte déjà
        # de lui-même les étiquettes en collision, si bien que ce garde-fou
        # privait de tout repérage les couches où il sert le plus.
        EL.style_error_layer(mem)
        QgsProject.instance().addMapLayer(mem)
        note = " (limité à %d)" % C.ERROR_LAYER_CAP if tronque else ""
        self.status_label.setText(
            "Couche d'erreurs (points) créée : %d point(s)%s." % (nb, note))

    @staticmethod
    def _style_error_layer(mem, label=True):
        """Délègue à ``error_layer`` : source unique de la symbologie."""
        EL.style_error_layer(mem, label=label)

    def _repair_geometries(self):
        """Réparation sélective (cases à cocher) écrite dans un NOUVEAU fichier.

        La couche d'origine n'est jamais modifiée : l'utilisateur choisit les
        types d'erreurs à corriger, puis un nouveau fichier est créé et chargé.
        """
        if self._is_busy():
            QMessageBox.information(self, C.PLUGIN_TITLE, C.MSG_BUSY)
            return
        if not self._require_result():
            return
        layer = self._matching_layer()
        if layer is None:
            return
        result = self._result

        counts = {
            "invalid": len(result.flagged.get("invalid", [])),
            "self_intersection": len(result.flagged.get("self_intersection", [])),
            "null_empty": len(result.flagged.get("null_empty", [])),
            "duplicate": len(result.flagged.get("duplicate", [])),
            "small": len(result.flagged.get("small", [])),
            "overlap": len(result.flagged.get("overlap", [])),
            C.CAT_DUPLICATE_VERTEX: len(result.flagged.get(C.CAT_DUPLICATE_VERTEX, [])),
            C.CAT_HOLE: len(result.flagged.get(C.CAT_HOLE, [])),
            C.CAT_MULTIPART: len(result.flagged.get(C.CAT_MULTIPART, [])),
            C.CAT_SHARP_ANGLE: len(result.flagged.get(C.CAT_SHARP_ANGLE, [])),
            C.CAT_CLOSE_VERTEX: len(result.flagged.get(C.CAT_CLOSE_VERTEX, [])),
            # Aucune correction n'existe pour ces deux-là — comptées à part
            # pour décider si le dialogue a quelque chose à MONTRER, même
            # quand il n'a rien à PROPOSER (voir la garde ci-dessous).
            C.CAT_NESTED_HEIGHT: len(result.flagged.get(C.CAT_NESTED_HEIGHT, [])),
            C.CAT_UNNODED_CROSSING: len(result.flagged.get(C.CAT_UNNODED_CROSSING, [])),
        }
        fixable = {k: v for k, v in counts.items()
                  if k not in (C.CAT_NESTED_HEIGHT, C.CAT_UNNODED_CROSSING)}
        if not any(counts.values()):
            QMessageBox.information(
                self, C.PLUGIN_TITLE,
                "Aucune erreur détectée (invalides, auto-intersections, "
                "nulles/vides, doublons, petits polygones, chevauchements, "
                "sommets dupliqués, trous, multi-parties, angles aigus, "
                "sommets rapprochés, hauteur des polygones inclus, "
                "croisements de lignes non nodés)."
            )
            return
        # Rien à COCHER (``fixable`` tout à zéro) mais quelque chose à MONTRER
        # (hauteur imbriquée et/ou croisements non nodés) : le dialogue
        # s'ouvre quand même, sur sa seule section informationnelle — plutôt
        # que de cacher cette explication derrière un message qui
        # prétendrait qu'il n'y a « rien » à signaler.

        ropts = self._ask_repair_options(layer, counts, result)
        if ropts is None:
            return  # annulé
        if not ropts.any_selected():
            QMessageBox.information(self, C.PLUGIN_TITLE, "Aucune correction sélectionnée.")
            return

        # Fichier de sortie automatique : même dossier et même nom que la
        # source, avec le suffixe ``_repare``. Le clic sur « Réparer » lance
        # ainsi immédiatement le traitement, sans second dialogue.
        path = self._automatic_repair_path(
            layer.source(), result.layer_name, sg_settings.load_last_dir())
        # Aucun dialogue d'enregistrement ne prévient plus qu'un fichier du
        # même nom existe : la garde est ici, sinon deux réparations d'affilée
        # remplaçaient la première sans rien dire.
        path = self._confirm_repair_output(path)
        if path is None:
            return
        sg_settings.save_last_dir(path)

        # ``normcase`` + ``realpath`` et non ``abspath`` seul : sous Windows,
        # « D:\data\Foo.gpkg » et « d:\data\foo.gpkg » désignent le MÊME
        # fichier mais ne se comparaient pas égaux — la garde tombait et la
        # couche source, ouverte dans QGIS, était écrasée par sa version
        # réparée. ``realpath`` couvre en plus les liens et jonctions.
        if self._same_file(path, layer.source().split("|")[0]):
            QMessageBox.warning(
                self, C.PLUGIN_TITLE,
                "Le fichier de sortie doit être différent du fichier d'origine."
            )
            return

        # Copie privée pour le thread d'arrière-plan (voir tasks.py).
        worker = layer.clone()
        if worker is None or not worker.isValid():
            QMessageBox.critical(
                self, C.PLUGIN_TITLE,
                "Impossible de préparer la couche pour la réparation (clone invalide)."
            )
            return

        ctx = QgsProject.instance().transformContext()
        task = RepairTask(worker, result, ropts, path, ctx)
        task.progressChanged.connect(self._on_task_progress)
        task.progressMessage.connect(self._on_task_message)
        task.taskCompleted.connect(self._on_repair_completed)
        task.taskTerminated.connect(self._on_repair_terminated)
        self._repair_task = task
        self._repair_out_path = path
        # Le bilan final doit savoir ce qui a été DEMANDÉ : un compteur à 0 pour
        # une correction décochée n'est pas le résultat d'une action, c'est une
        # affirmation sur un fichier que personne n'a vérifié.
        self._repair_ropts = ropts

        self._enter_running_state("Réparation en cours…")
        QgsApplication.taskManager().addTask(task)

    def _on_repair_completed(self):
        task = self._repair_task
        self._repair_task = None
        self._leave_running_state()
        self._set_actions_enabled(True)
        if task is None or task.stats is None:
            return
        stats = task.stats
        path = self._repair_out_path

        # Charger la nouvelle couche dans le projet (thread principal).
        # Seul le fichier réparé est produit : pas de couche annexe
        # (rejets/corrections) — un seul livrable en sortie de l'étape.
        name = os.path.splitext(os.path.basename(path))[0]
        new_layer = QgsVectorLayer(path, name, "ogr")
        if new_layer.isValid():
            QgsProject.instance().addMapLayer(new_layer)

        # Bilan construit ligne à ligne, chacune conditionnée à la correction
        # RÉELLEMENT demandée. Le bloc unique précédent affirmait « Encore
        # invalides après make valid : 0 » même quand make valid n'avait pas
        # tourné : ce n'était plus le compte rendu d'une action nulle mais une
        # affirmation fausse sur la qualité du fichier produit. Une correction
        # décochée est donc annoncée comme telle, jamais chiffrée à 0.
        ropts = getattr(self, "_repair_ropts", None)

        def demande(*attrs):
            """Vrai si l'une de ces corrections a été cochée (vrai si inconnu)."""
            if ropts is None:
                return True
            return any(getattr(ropts, a, False) for a in attrs)

        lignes = []

        def ligne(demandee, libelle, valeur):
            lignes.append("• %s : %s"
                          % (libelle, valeur if demandee else "non demandée"))

        # Périmètre EN PREMIER quand une zone était active : le fichier produit
        # est alors un EXTRAIT, pas la couche entière réparée. Le lire comme un
        # remplaçant de la source ferait perdre tout ce qui est hors zone.
        if self._result is not None and self._result.has_aoi():
            lignes.append(
                "• ⚠ Sortie limitée à la ZONE analysée : %s entité(s) écrite(s) "
                "sur %s dans la couche. Ce fichier est un extrait, il ne "
                "remplace pas la source."
                % (C.fmt_int(stats.written),
                   C.fmt_int(self._result.aoi_layer_features)))

        invalides_demandees = demande("fix_invalid", "fix_self_intersection")
        ligne(invalides_demandees, "Géométries réparées (valides)",
              stats.fixed_invalid)
        if invalides_demandees:
            lignes.append("• Encore invalides après make valid : %d"
                          % stats.still_invalid)
        ligne(demande("remove_null_empty"), "Nulles/vides supprimées",
              stats.removed_null_empty)
        ligne(demande("remove_duplicates"), "Doublons supprimés",
              stats.removed_duplicates)
        petits_demandes = demande("remove_small")
        ligne(petits_demandes, "Petits polygones supprimés",
              "%d (%.1f m² au total, seuil ≤ %g m²)"
              % (stats.removed_small, stats.removed_small_area_m2,
                 getattr(ropts, "small_polygon_threshold_m2", 0) if ropts else 0))
        if petits_demandes and stats.small_kept_above_threshold:
            lignes.append(
                "• Repérés à l'analyse mais conservés (seuil resserré) : %d"
                % stats.small_kept_above_threshold)
        # Motif DISTINCT du précédent : la surface n'a pas pu être remesurée.
        # Les fondre annonçait « au-dessus du seuil » pour des entités dont on
        # ne sait justement pas où elles se situent.
        if petits_demandes and stats.small_not_measurable:
            lignes.append(
                "• Conservés faute de surface mesurable (à vérifier à la "
                "main) : %d" % stats.small_not_measurable)
        ligne(demande("fix_overlaps"), "Chevauchements mineurs rognés",
              stats.fixed_overlaps)
        ligne(demande("fix_duplicate_vertex"),
              "Entités nettoyées de vertex dupliqués",
              "%d (%d sommets retirés)" % (stats.cleaned_duplicate_vertex,
                                           stats.removed_vertices))
        ligne(demande("remove_holes"), "Trous comblés",
              "%d (sur %d entités)" % (stats.removed_holes,
                                       stats.features_with_holes_fixed))
        ligne(demande("explode_multipart"), "Entités multi-parties éclatées",
              "%d (+%d entités)" % (stats.exploded_features,
                                    stats.added_features))
        ligne(demande("fix_sharp_angles"), "Sommets en pointe retirés",
              "%d (sur %d entités)" % (stats.removed_spike_vertices,
                                       stats.fixed_sharp_angles))
        ligne(demande("fix_close_vertices"), "Sommets rapprochés fusionnés",
              "%d (sur %d entités)" % (stats.removed_close_vertices,
                                       stats.cleaned_close_vertices))
        lignes.append("• Entités écrites : %d" % stats.written)

        msg = ("Réparation terminée — nouveau fichier créé :\n%s\n\n%s\n"
               % (path, "\n".join(lignes)))

        # Les invalides délibérément laissées passer sont reportées telles
        # quelles dans le nouveau fichier : le dire, sinon « nouveau fichier
        # créé » se lit comme « fichier assaini ».
        if not invalides_demandees and self._result is not None:
            restantes = len(self._result.flagged.get(C.CAT_INVALID, []))
            if restantes:
                msg += ("\n⚠ %d géométrie(s) invalide(s) reportée(s) telles "
                        "quelles : leur correction n'était pas demandée.\n"
                        % restantes)

        # Corrections volontairement abandonnées pour ne pas dégrader la donnée.
        skipped_total = (stats.skipped_sharp_angles + stats.skipped_close_vertices
                         + stats.skipped_duplicate_vertex)
        if skipped_total:
            details = []
            if stats.skipped_sharp_angles:
                details.append("%d angle(s) aigu(s)" % stats.skipped_sharp_angles)
            if stats.skipped_close_vertices:
                details.append("%d sommet(s) rapproché(s)" % stats.skipped_close_vertices)
            if stats.skipped_duplicate_vertex:
                details.append("%d sommet(s) dupliqué(s)" % stats.skipped_duplicate_vertex)
            msg += (
                "\n⚠ %d entité(s) laissée(s) intacte(s) — la correction aurait "
                "dégradé la géométrie (%s). À revoir manuellement.\n"
                % (skipped_total, ", ".join(details))
            )
        # Distinct d'un refus : il n'y avait plus rien à corriger, une étape
        # antérieure ayant déjà fait disparaître l'anomalie. Le dire séparément,
        # sinon ces entités étaient accusées à tort d'être incorrigibles.
        rien_total = (stats.unchanged_sharp_angles
                      + stats.unchanged_close_vertices)
        if rien_total:
            msg += (
                "\nℹ %d entité(s) sans correction nécessaire : l'anomalie avait "
                "déjà disparu à l'étape précédente.\n" % rien_total
            )
        if stats.rejected:
            msg += (
                "\n⚠ %d entité(s) refusée(s) à l'écriture : signalez ce cas, il "
                "ne devrait pas se produire.\n" % stats.rejected
            )

        # Identifiant source renommé : le fichier écrit possède sa propre clé
        # primaire, la valeur d'origine reste consultable sous le nouveau nom.
        if getattr(stats, "renamed_fields", None):
            pairs = ", ".join("« %s » → « %s »" % (old, new)
                              for old, new in stats.renamed_fields)
            msg += (
                "\nℹ Identifiant source renommé (%s) : le nouveau fichier gère "
                "sa propre clé primaire. Les valeurs d'origine sont conservées "
                "et permettent de retrouver l'entité source.\n" % pairs
            )

        # ── Corrections manuelles requises ──────────────────────────────────
        # Section UNIQUE regroupant tout ce qu'aucune case à cocher ne corrige
        # automatiquement : jamais deviné, jamais rogné au hasard. Distinct des
        # entités « laissées intactes » plus haut (celles-là ont une correction
        # qui EXISTE mais aurait dégradé la géométrie) : ici, soit la
        # correction n'existe simplement pas (hauteur imbriquée), soit elle a
        # été jugée trop risquée pour être automatique (chevauchement majeur).
        def _fid_list(fids, limite=30):
            shown = ", ".join(str(f) for f in fids[:limite])
            if len(fids) > limite:
                shown += ", … (+%d)" % (len(fids) - limite)
            return shown

        manuel_sections = []
        if stats.manual_overlaps:
            manuel_sections.append((
                "%d chevauchement(s) trop important(s) NON corrigé(s) (la "
                "forme aurait été trop modifiée)" % stats.manual_overlaps,
                stats.manual_overlap_fids,
                "« 🎯 Sélectionner les entités » puis filtrez la catégorie "
                "« Chevauchement » sur la couche d'origine."
            ))
        if stats.skipped_sharp_angles:
            manuel_sections.append((
                "%d angle(s) aigu(s) NON corrigé(s) (le retrait aurait "
                "dégradé la géométrie)" % stats.skipped_sharp_angles,
                stats.skipped_sharp_angle_fids, ""))
        if stats.skipped_close_vertices:
            manuel_sections.append((
                "%d sommet(s) rapproché(s) NON fusionné(s) (la fusion aurait "
                "dégradé la géométrie)" % stats.skipped_close_vertices,
                stats.skipped_close_vertex_fids, ""))
        if stats.skipped_duplicate_vertex:
            manuel_sections.append((
                "%d sommet(s) dupliqué(s) NON fusionné(s) (la fusion aurait "
                "dégradé la géométrie)" % stats.skipped_duplicate_vertex,
                stats.skipped_duplicate_vertex_fids, ""))
        # Hauteur des polygones inclus : AUCUNE correction automatique n'existe
        # pour ce contrôle, quelle que soit la case cochée — rien ne dit si
        # c'est la hauteur de l'inclus ou celle du contenant qui est fausse.
        nested_fids = (self._result.flagged.get(C.CAT_NESTED_HEIGHT, [])
                       if self._result is not None else [])
        if nested_fids:
            manuel_sections.append((
                "%d polygone(s) inclus pas assez haut(s) — aucune correction "
                "automatique n'existe pour ce contrôle" % len(nested_fids),
                nested_fids,
                "« 🎯 Sélectionner les entités » puis filtrez la catégorie "
                "« Hauteur imbriquée » sur la couche d'origine."
            ))
        # Croisements de lignes non nodés : même absence de correction
        # automatique — insérer un sommet déplacerait le tracé, ce qu'aucune
        # case de ce dialogue ne fait sans confirmation explicite.
        crossing_fids = (self._result.flagged.get(C.CAT_UNNODED_CROSSING, [])
                         if self._result is not None else [])
        if crossing_fids:
            manuel_sections.append((
                "%d ligne(s) en croisement non nodé — aucune correction "
                "automatique n'existe pour ce contrôle" % len(crossing_fids),
                crossing_fids,
                "« 🎯 Sélectionner les entités » puis filtrez la catégorie "
                "« Croisement non nodé » sur la couche d'origine."
            ))

        if manuel_sections:
            lignes_manuel = ["\n⚠ Corrections manuelles requises :"]
            for libelle, fids, astuce in manuel_sections:
                lignes_manuel.append("• %s. FID source : %s"
                                     % (libelle, _fid_list(fids)))
                if astuce:
                    lignes_manuel.append("  Astuce : %s" % astuce)
            msg += "\n" + "\n".join(lignes_manuel) + "\n"

        # La vérification automatique (lancée juste après) porte les mêmes
        # contrôles sur le fichier réparé : dire ici qu'elle va suivre évite de
        # laisser croire qu'il faut la déclencher soi-même.
        verif_possible = (new_layer.isValid()
                          and self._last_analysis_options is not None)
        msg += (
            "\nLa couche d'origine n'a pas été modifiée.%s"
            % (" Vérification automatique en cours sur la couche réparée…"
               if verif_possible else
               " Relancez l'analyse sur la couche réparée pour actualiser le "
               "score.")
        )
        QMessageBox.information(self, C.PLUGIN_TITLE, msg)
        self.status_label.setText("Couche réparée créée : %s" % os.path.basename(path))

        # ── Vérification automatique post-réparation (idée : idempotence) ──
        # Rejoue EXACTEMENT les mêmes contrôles que l'analyse d'origine sur le
        # fichier tout juste écrit, sans attendre que l'utilisateur y pense —
        # et sans toucher à self._result : cette tâche COMPLÈTE l'affichage
        # courant, elle ne le remplace pas.
        if verif_possible:
            verify_worker = new_layer.clone()
            if verify_worker is not None and verify_worker.isValid():
                # La zone d'analyse n'est PAS rejouée : le fichier réparé est
                # déjà l'extrait de cette zone, la réappliquer ne retirerait
                # rien et ferait annoncer « analyse restreinte : N entités sur
                # N » sur un fichier qui n'a jamais rien contenu d'autre.
                verify_opts = self._last_analysis_options
                with contextlib.suppress(Exception):
                    verify_opts = dataclasses.replace(
                        verify_opts, aoi_rect=None, aoi_crs_authid="")
                vtask = AnalysisTask(
                    verify_worker, verify_opts,
                    description="STAT GEOM QC — vérification post-réparation")
                vtask.taskCompleted.connect(self._on_verify_completed)
                vtask.taskTerminated.connect(self._on_verify_terminated)
                self._verify_task = vtask
                self._verify_layer_name = new_layer.name()
                QgsApplication.taskManager().addTask(vtask)

    def _on_verify_completed(self):
        """Bilan de la vérification automatique post-réparation.

        N'affecte JAMAIS self._result / self._result_layer_id : cette tâche ne
        fait que confirmer — ou infirmer — que la réparation a fonctionné, elle
        ne se substitue pas à une analyse demandée par l'utilisateur sur la
        couche réparée.
        """
        task = self._verify_task
        self._verify_task = None
        nom = getattr(self, "_verify_layer_name", "")
        if task is None or task.result is None:
            return
        result = task.result
        c = result.correctness
        total_restant = result.total_issues()
        # Sous zone d'analyse, le fichier vérifié est un EXTRAIT : les voisins
        # hors zone n'y sont pas. Un chevauchement de bord — pourtant bien
        # signalé par l'analyse d'origine, grâce à la passe de voisinage — n'a
        # donc plus de second membre à comparer et ne peut PAS être recontrôlé.
        # L'annoncer « 0 anomalie restante » sans le dire serait un faux
        # « conforme », exactement ce que ce plugin refuse partout ailleurs.
        reserve = ""
        if self._result is not None and self._result.has_aoi():
            reserve = (" Réserve : le fichier vérifié est l'extrait de la zone, "
                       "les entités voisines n'y figurent pas — les erreurs de "
                       "PAIRE au bord de la zone n'ont pas pu être recontrôlées.")
        if total_restant == 0:
            self.status_label.setText(
                "Vérification automatique — %s : 0 anomalie restante "
                "(score %g/100).%s" % (nom, c.score, reserve)
            )
        else:
            self.status_label.setText(
                "Vérification automatique — %s : %s anomalie(s) restante(s) "
                "(score %g/100). Relancez l'analyse sur cette couche pour le "
                "détail.%s" % (nom, C.fmt_int(total_restant), c.score, reserve)
            )

    def _on_verify_terminated(self):
        # Best-effort et silencieux : un échec de la vérification automatique
        # (annulation, erreur) ne doit jamais faire croire à un échec de la
        # réparation elle-même, dont le fichier existe déjà et est intact.
        self._verify_task = None

    def _on_repair_terminated(self):
        task = self._repair_task
        self._repair_task = None
        self._leave_running_state()
        self._set_actions_enabled(True)
        if task is not None and task.error:
            QMessageBox.critical(
                self, C.PLUGIN_TITLE, "Échec de la réparation :\n%s" % task.error)
            self.status_label.setText("Échec de la réparation.")
        else:
            self.status_label.setText("Réparation annulée.")

    def _ask_repair_options(self, layer, counts, result):
        """Dialogue de sélection des corrections, avec aperçu adaptatif par
        catégorie (voir ``repair_dialog.RepairOptionsDialog``) : chaque case
        s'accompagne d'un message calculé sur CETTE couche — combien seront
        réellement corrigés, combien laissés intacts par prudence, et pourquoi
        — plutôt que de le découvrir après coup dans le fichier réparé.

        Renvoie un RepairOptions, ou None si l'utilisateur annule.
        """
        dlg = RepairOptionsDialog(self, layer, counts, result)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return dlg.to_repair_options()

    @staticmethod
    def _ensure_extension(path, selected_filter):
        """Ajoute l'extension adéquate si l'utilisateur ne l'a pas saisie."""
        ext = os.path.splitext(path)[1].lower()
        if ext in C.WRITABLE_EXTENSIONS:
            return path
        if selected_filter and "shp" in selected_filter.lower():
            return path + ".shp"
        if selected_filter and "geojson" in selected_filter.lower():
            return path + ".geojson"
        return path + ".gpkg"

    # ── Exports ───────────────────────────────────────────────────────────

    def _export(self, kind):
        if not self._require_result():
            return
        base = "STAT_GEOM_QC_%s" % self._safe_filename(self._result.layer_name)
        path, _ = QFileDialog.getSaveFileName(
            self, "Exporter le rapport",
            self._default_path(base + "." + kind), C.EXPORT_FILTERS[kind]
        )
        if not path:
            return
        try:
            if kind == "html":
                report.write_html(self._result, path)
            else:
                report.write_pdf(self._result, path)
            sg_settings.save_last_dir(path)
            self.status_label.setText("Exporté : %s" % path)
        except Exception as exc:
            QMessageBox.critical(self, C.PLUGIN_TITLE, "Échec de l'export :\n%s" % exc)

    def _open_in_browser(self):
        if not self._require_result():
            return
        # Fichier temporaire propre à la couche : évite d'écraser un rapport
        # déjà ouvert. Repli sur un fichier temporaire unique si le nom est
        # verrouillé (onglet ouvert sous Windows).
        safe = self._safe_filename(self._result.layer_name)
        tmp = os.path.join(tempfile.gettempdir(), "stat_geom_qc_%s.html" % safe)
        try:
            report.write_html(self._result, tmp)
        except OSError:
            fd, tmp = tempfile.mkstemp(prefix="stat_geom_qc_", suffix=".html")
            os.close(fd)
            report.write_html(self._result, tmp)
        webbrowser.open("file:///" + tmp.replace("\\", "/"))

    # ── Helpers ───────────────────────────────────────────────────────────

    def _require_result(self):
        if self._result is None:
            QMessageBox.information(self, C.PLUGIN_TITLE, C.MSG_NO_RESULT)
            return False
        return True

    def _layer_matches_result(self, layer) -> bool:
        """``layer`` est-elle bien la couche du dernier résultat ?

        L'identifiant de couche est le seul critère fiable. Le repli sur
        ``source()`` sert au cas d'une couche rechargée (nouvel id, même
        fichier) — mais il est réservé aux sources qui DÉSIGNENT un fichier :
        deux couches mémoire distinctes portent la même chaîne de source
        (« Polygon?crs=EPSG:2154 ») et se faisaient passer l'une pour l'autre.
        """
        if layer is None or self._result is None:
            return False
        if self._result_layer_id is not None and layer.id() == self._result_layer_id:
            return True
        source = self._result.source or ""
        if not source or layer.source() != source:
            return False
        # Une source de couche mémoire n'identifie rien : elle décrit un type.
        try:
            if layer.dataProvider().name() == "memory":
                return False
        except Exception:
            return False
        return self._same_file(source.split("|")[0], layer.source().split("|")[0])

    def _result_layer(self):
        """La couche du résultat courant, ou ``None`` — SANS message.

        Pour les usages purement visuels (surbrillance) : ils doivent se taire
        quand la couche sélectionnée n'est pas celle du résultat, pas afficher
        des FID appartenant à une autre couche.
        """
        layer = self._current_layer()
        return layer if self._layer_matches_result(layer) else None

    def _matching_layer(self):
        """La couche sélectionnée doit correspondre au dernier résultat
        (les FID ne sont valables que pour la couche analysée), et n'avoir pas
        été MODIFIÉE depuis : un FID supprimé puis recyclé par le fournisseur
        désigne alors une autre entité, et supprimer ou réparer sur cette base
        détruirait la mauvaise donnée.
        """
        layer = self._current_layer()
        if layer is None:
            QMessageBox.warning(self, C.PLUGIN_TITLE, C.MSG_NO_LAYER)
            return None
        if not self._layer_matches_result(layer):
            QMessageBox.warning(self, C.PLUGIN_TITLE, C.MSG_LAYER_MISMATCH)
            return None
        if self._result_stale:
            QMessageBox.warning(
                self, C.PLUGIN_TITLE,
                "La couche a été modifiée depuis l'analyse : les "
                "identifiants d'entités du résultat ne sont plus fiables.\n\n"
                "Relancez l'analyse avant d'agir sur la couche."
            )
            return None
        return layer
