# -*- coding: utf-8 -*-
"""Dialogue de réparation géométrique : aperçu adaptatif par type de correction.

Pourquoi ce module existe
--------------------------
Le dialogue historique (une case à cocher, un compteur brut entre parenthèses)
laissait l'utilisateur cocher à l'aveugle : il ne découvrait qu'APRÈS avoir
écrit le fichier réparé combien d'entités avaient été réellement corrigées,
combien laissées intactes par prudence (le retrait aurait dégradé la
géométrie), et combien n'avaient tout simplement AUCUNE correction possible
(la hauteur des polygones inclus, par exemple).

Ici, chaque case cochable s'accompagne d'un message calculé sur LA couche en
cours d'analyse — pas un texte générique — et d'un aperçu (« combien seront
réellement corrigés, combien laissés de côté, et pourquoi ») construit AVANT
que quoi que ce soit ne soit écrit. L'utilisateur ajuste la tolérance de
chaque correction et voit l'effet, plutôt que de le découvrir après coup.

Aucune nouvelle logique de correction n'est écrite ici : l'aperçu réutilise
``GeomAnalyzer.build_repaired_layer(dry_run=True, only_fids=...)`` — exactement
le même calcul de décision que la réparation réelle, simplement rejoué sans
rien écrire. Voir ``tasks.RepairPreviewTask``.
"""

import contextlib
from typing import Callable, List, Optional

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsApplication

from . import constants as C
from . import settings as sg_settings
from .analysis_engine import RepairOptions
from .tasks import RepairPreviewTask
from .widgets import CollapsibleSection

_ESTIMATING = "…"
# Repli historique, conservé pour compatibilité des tests existants ; les
# lignes à compteur nul n'affichent plus aucun message (voir _add_dry_run_row
# / _add_static_row) — la case grisée à « (0) » suffit à le dire.
_NONE_OF_THIS_TYPE = ""


def _prep_threshold_spin(spin, unit, prefix="≤ "):
    """Même rendu compact que le panneau (voir stat_geom_dockwidget.py) —
    dupliqué à dessein plutôt qu'importé : les deux modules ne doivent
    dépendre l'un de l'autre que dans UN sens (le panneau importe ce fichier,
    jamais l'inverse), sans quoi l'import circulaire empêcherait le chargement
    du plugin."""
    spin.setPrefix(prefix)
    spin.setSuffix(" " + unit)
    spin.setAlignment(Qt.AlignmentFlag.AlignRight
                      | Qt.AlignmentFlag.AlignVCenter)
    spin.setMinimumWidth(112)
    spin.setMaximumWidth(150)
    return spin


def _fmt(n) -> str:
    return C.fmt_int(n)


# ═══════════════════════════════════════════════════════════════════════════
# Messages adaptatifs — fonctions PURES (aucune dépendance Qt), donc
# testables isolément. UNE LIGNE COURTE : « combien corrigés · combien
# laissés de côté ». Le mécanisme et les nuances vivent dans l'infobulle de
# la case (voir les appels à ``_add_dry_run_row``/``_add_static_row``), pas
# ici — cette fonction ne répond qu'au résultat chiffré.
# ═══════════════════════════════════════════════════════════════════════════

def msg_invalid(count: int, stats) -> str:
    if count == 0:
        return ""
    if stats is None:
        return _ESTIMATING
    txt = "%s corrigée(s)" % _fmt(stats.fixed_invalid)
    if stats.still_invalid:
        txt += " · %s encore invalide(s)" % _fmt(stats.still_invalid)
    return txt


def msg_self_intersection(count: int, stats) -> str:
    if count == 0:
        return ""
    if stats is None:
        return _ESTIMATING
    txt = "%s corrigée(s)" % _fmt(stats.fixed_invalid)
    if stats.still_invalid:
        txt += " · %s encore invalide(s)" % _fmt(stats.still_invalid)
    return txt


def msg_null_empty(count: int) -> str:
    if count == 0:
        return ""
    return "%s supprimée(s)" % _fmt(count)


def msg_duplicate(count: int) -> str:
    if count == 0:
        return ""
    return "%s supprimé(s)" % _fmt(count)


def msg_small(count: int, stats, threshold_m2: float) -> str:
    if count == 0:
        return ""
    if stats is None:
        return _ESTIMATING
    txt = "%s supprimé(s) (%.1f m²)" % (_fmt(stats.removed_small),
                                        stats.removed_small_area_m2)
    if stats.small_kept_above_threshold:
        txt += " · %s conservé(s)" % _fmt(stats.small_kept_above_threshold)
    # Motif DISTINCT de « au-dessus du seuil » : la surface n'a pas pu être
    # remesurée. Les fondre annoncerait une raison qui n'est pas la bonne.
    if getattr(stats, "small_not_measurable", 0):
        txt += " · %s non mesurable(s)" % _fmt(stats.small_not_measurable)
    return txt


def msg_overlap(count: int, stats, max_fraction_pct: float) -> str:
    if count == 0:
        return ""
    if stats is None:
        return _ESTIMATING
    txt = "%s rogné(s)" % _fmt(stats.fixed_overlaps)
    if stats.manual_overlaps:
        txt += " · %s manuel(s)" % _fmt(stats.manual_overlaps)
    return txt


def msg_duplicate_vertex(count: int, stats) -> str:
    if count == 0:
        return ""
    if stats is None:
        return _ESTIMATING
    txt = "%s nettoyée(s)" % _fmt(stats.cleaned_duplicate_vertex)
    if stats.skipped_duplicate_vertex:
        txt += " · %s intacte(s)" % _fmt(stats.skipped_duplicate_vertex)
    return txt


def msg_hole(count: int, stats, threshold_m2: float) -> str:
    if count == 0:
        return ""
    if stats is None:
        return _ESTIMATING
    return "%s trou(s) comblé(s)" % _fmt(stats.removed_holes)


def msg_multipart(count: int, stats) -> str:
    if count == 0:
        return ""
    if stats is None:
        return _ESTIMATING
    return "%s éclatée(s) (+%s)" % (_fmt(count), _fmt(stats.added_features))


def msg_sharp_angle(count: int, stats, min_deg: float) -> str:
    if count == 0:
        return ""
    if stats is None:
        return _ESTIMATING
    txt = "%s corrigée(s)" % _fmt(stats.fixed_sharp_angles)
    if stats.skipped_sharp_angles:
        txt += " · %s intacte(s)" % _fmt(stats.skipped_sharp_angles)
    return txt


def msg_close_vertex(count: int, stats, min_dist_m: float) -> str:
    if count == 0:
        return ""
    if stats is None:
        return _ESTIMATING
    txt = "%s fusionnée(s)" % _fmt(stats.cleaned_close_vertices)
    if stats.skipped_close_vertices:
        txt += " · %s intacte(s)" % _fmt(stats.skipped_close_vertices)
    return txt


def msg_nested_height(count: int) -> str:
    if count == 0:
        return "Aucun détecté."
    return "%s détecté(s) — aucune correction automatique." % _fmt(count)


def msg_unnoded_crossing(count: int) -> str:
    if count == 0:
        return "Aucun détecté."
    return "%s détecté(s) — aucune correction automatique." % _fmt(count)


class _PreviewRow:
    """État d'une ligne du dialogue : widgets + tout ce qu'il faut pour
    (re)calculer son aperçu à la demande.

    ``static`` : vrai pour les catégories sans logique de refus (nulle/vide,
    doublon) — leur message est connu d'emblée, aucun aperçu à attendre.
    ``fids`` : ``None`` pour une ligne informationnelle (aucune case, jamais
    de tâche lancée) ; liste vide ou pleine sinon.
    """

    def __init__(self, category: str, count: int, checkbox=None, spin=None,
                fids: Optional[List[int]] = None,
                make_ropts: Optional[Callable[[], RepairOptions]] = None,
                format_message: Optional[Callable] = None,
                static: bool = False):
        self.category = category
        self.count = count
        self.checkbox = checkbox
        self.spin = spin
        self.fids = fids
        self.make_ropts = make_ropts
        self.format_message = format_message
        self.static = static
        self.message_label: Optional[QLabel] = None
        self.refresh_btn: Optional[QPushButton] = None
        self.stats = None
        self.stale = False
        self._task = None   # tâche de rafraîchissement EN COURS pour cette ligne
        # Incrémenté à chaque rafraîchissement MANUEL déclenché : un résultat
        # de l'aperçu initial (lancé une fois, sur l'état de CE moment-là)
        # peut arriver APRÈS qu'un rafraîchissement plus récent ait déjà
        # affiché un chiffre plus à jour — le comparer à ce compteur permet de
        # jeter silencieusement un résultat périmé au lieu d'écraser un
        # résultat plus récent avec un plus ancien.
        self.gen = 0

    def needs_preview(self) -> bool:
        return (not self.static) and self.fids is not None and self.count > 0

    def is_ready_for_repair(self) -> bool:
        """Cette ligne bloque-t-elle « Réparer… » si elle est cochée ?"""
        if self.static or not self.needs_preview():
            return True
        return self.stats is not None and not self.stale

    def refresh_text(self):
        if self.message_label is None:
            return
        if self.static:
            text = self.format_message(self.count) if self.format_message else ""
        else:
            # Convention à 2 arguments (count, stats) pour TOUTE ligne à
            # aperçu, y compris à compteur nul : chaque message gère déjà
            # count == 0 comme premier cas, sans jamais toucher stats.
            text = self.format_message(self.count, self.stats)
        self.message_label.setText(text)
        # Une ligne sans rien à dire ne doit pas laisser un blanc dans la
        # grille : masquée plutôt qu'affichée vide.
        self.message_label.setVisible(bool(text))


class RepairOptionsDialog(QDialog):
    """Dialogue de réparation géométrique, avec aperçu par catégorie.

    Construit à partir de ``layer`` (la couche ANALYSÉE, pour l'aperçu),
    ``counts`` (compteurs bruts par catégorie — voir
    ``StatGeomDockWidget._repair_geometries``) et ``result``
    (``AnalysisResult`` complet : seuils, ``flagged``, options actives).
    """

    def __init__(self, parent, layer, counts, result):
        super().__init__(parent)
        self._layer = layer
        self._counts = counts
        self._result = result
        self._rows: List[_PreviewRow] = []
        self._initial_task = None
        self._closing = False

        self.setWindowTitle("Réparation géométrique — choix des corrections")
        self.setMinimumWidth(560)
        self.setMinimumHeight(420)
        lay = QVBoxLayout(self)

        intro = QLabel(
            "Cochez les corrections à appliquer — écrites dans un NOUVEAU "
            "fichier, l'original n'est jamais modifié. Survolez une case "
            "pour le détail."
        )
        intro.setWordWrap(True)
        intro.setObjectName("sgHint")
        intro.setToolTip(
            "Chaque résultat est calculé sur CETTE couche, en supposant que "
            "SEULE cette case est cochée : en combiner plusieurs peut "
            "légèrement changer le résultat réel (rogner un chevauchement "
            "peut par exemple réduire un polygone sous le seuil « petit »)."
        )
        lay.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        holder = QWidget()
        holder_lay = QVBoxLayout(holder)
        holder_lay.setContentsMargins(0, 0, 0, 0)

        holder_lay.addWidget(self._build_standard_section())
        holder_lay.addWidget(self._build_advanced_section())
        holder_lay.addWidget(self._build_not_fixable_section())
        holder_lay.addStretch()
        scroll.setWidget(holder)
        lay.addWidget(scroll, 1)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Réparer…")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

        self.finished.connect(self._on_finished)
        self._refresh_ok_button()

        # Le premier aperçu se lance une fois le dialogue affiché — pas
        # pendant sa construction, pour qu'il apparaisse immédiatement.
        QTimer.singleShot(0, self._launch_initial_preview)

    # ── Construction des sections ────────────────────────────────────────

    def _build_standard_section(self):
        section = CollapsibleSection("Corrections standard", expanded=True,
                                     collapsible=False)
        grid = QGridLayout()
        grid.setVerticalSpacing(2)
        grid.setHorizontalSpacing(8)
        r = 0
        r = self._add_dry_run_row(
            grid, r, "invalid", "Géométries invalides",
            self._counts["invalid"], msg_invalid,
            tooltip="« make valid » sur les géométries invalides.")
        r = self._add_dry_run_row(
            grid, r, "self_intersection", "Auto-intersections",
            self._counts["self_intersection"], msg_self_intersection,
            tooltip=("« make valid », uniquement sur les auto-intersections "
                    "(sous-ensemble des invalides) — sans toucher aux autres."))
        r = self._add_static_row(
            grid, r, "null_empty", "Nulles / vides",
            self._counts["null_empty"], msg_null_empty,
            tooltip="Suppression inconditionnelle : tout le lot y passe.")
        r = self._add_static_row(
            grid, r, "duplicate", "Doublons",
            self._counts["duplicate"], msg_duplicate,
            tooltip="Conserve la 1ʳᵉ occurrence de chaque groupe identique.")
        r = self._add_dry_run_row(
            grid, r, "small", "Petits polygones",
            self._counts["small"], msg_small,
            tol_attr="small_polygon_threshold_m2", tol_unit="m²",
            tol_range=(C.THRESHOLD_MIN_M2, C.THRESHOLD_MAX_M2),
            tol_default=self._result.threshold_m2, tol_decimals=2, tol_step=0.5,
            tol_detection=True,
            tooltip=("Suppression définitive sous ce seuil. Seules les "
                    "entités déjà repérées à l'analyse sont concernées : le "
                    "seuil ne peut donc être que RESSERRÉ (%s m² à "
                    "l'analyse). Pour l'élargir, relancez l'analyse."
                    % ("%g" % self._result.threshold_m2)))
        r = self._add_dry_run_row(
            grid, r, "overlap", "Chevauchements (mineurs)",
            self._counts["overlap"], msg_overlap,
            tol_attr="overlap_max_fraction_pct", tol_unit="%",
            tol_range=(C.OVERLAP_TOLERANCE_MIN_PCT, C.OVERLAP_TOLERANCE_MAX_PCT),
            tol_default=sg_settings.load_overlap_tolerance_pct(),
            tol_decimals=1, tol_step=0.5, tol_prefix="",
            tooltip=("Seul le plus PETIT polygone de chaque paire est rogné "
                    "(le plus grand garde sa forme), et seulement si la part "
                    "retirée reste sous ce seuil. Au-delà : laissé intact, "
                    "à corriger à la main."))
        r = self._add_dry_run_row(
            grid, r, C.CAT_DUPLICATE_VERTEX, "Vertex dupliqués",
            self._counts[C.CAT_DUPLICATE_VERTEX], msg_duplicate_vertex,
            tooltip=("Retire les sommets STRICTEMENT superposés — forme "
                    "inchangée, aucune tolérance."))
        r = self._add_dry_run_row(
            grid, r, C.CAT_HOLE, "Trous",
            self._counts[C.CAT_HOLE], msg_hole,
            tol_attr="hole_max_area_m2", tol_unit="m²",
            tol_range=(C.HOLE_THRESHOLD_MIN_M2, C.HOLE_THRESHOLD_MAX_M2),
            tol_default=self._result.hole_threshold_m2, tol_decimals=2, tol_step=0.5,
            tol_detection=True,
            tooltip=("Comble les trous sous ce seuil. Les plus grands, "
                    "souvent justifiés (cour, îlot, plan d'eau), restent "
                    "intacts. Seuil resserrable seulement (%s m² à "
                    "l'analyse) : l'élargir demande une nouvelle analyse."
                    % ("%g" % self._result.hole_threshold_m2)))
        r = self._add_dry_run_row(
            grid, r, C.CAT_MULTIPART, "Multi-parties",
            self._counts[C.CAT_MULTIPART], msg_multipart,
            tooltip=("Éclate chaque partie en une entité distincte (mêmes "
                    "attributs) : le nombre total d'entités augmente."))
        section.content_layout().addLayout(grid)
        return section

    def _build_advanced_section(self):
        section = CollapsibleSection("Corrections avancées", expanded=True,
                                     collapsible=False)
        grid = QGridLayout()
        grid.setVerticalSpacing(2)
        grid.setHorizontalSpacing(8)
        r = 0
        r = self._add_dry_run_row(
            grid, r, C.CAT_SHARP_ANGLE,
            "%s Angles aigus" % C.ICON_SHARP_ANGLE,
            self._counts[C.CAT_SHARP_ANGLE], msg_sharp_angle,
            tol_attr="sharp_angle_min_deg", tol_unit="°",
            tol_range=(C.SHARP_ANGLE_MIN_DEG, C.SHARP_ANGLE_MAX_DEG),
            tol_default=self._result.sharp_angle_min_deg, tol_decimals=1,
            tol_step=1.0, tol_prefix="< ", tol_detection=True,
            tooltip=("Retire les sommets en pointe trop fermée. Abandonné "
                    "par entité si le retrait dégraderait sa validité — "
                    "laissée intacte plutôt qu'abîmée. Seuil resserrable "
                    "seulement (%s° à l'analyse)."
                    % ("%g" % self._result.sharp_angle_min_deg)))
        r = self._add_dry_run_row(
            grid, r, C.CAT_CLOSE_VERTEX,
            "%s Sommets rapprochés" % C.ICON_CLOSE_VERTEX,
            self._counts[C.CAT_CLOSE_VERTEX], msg_close_vertex,
            tol_attr="close_vertex_min_dist_m", tol_unit="m",
            tol_range=(C.CLOSE_VERTEX_MIN_M, C.CLOSE_VERTEX_MAX_M),
            tol_default=self._result.close_vertex_min_dist_m, tol_decimals=3,
            tol_step=0.05, tol_detection=True,
            tooltip=("Fusionne les sommets consécutifs trop proches (tracé "
                    "très légèrement modifié). Abandonné si la fusion "
                    "dégraderait la géométrie. Seuil resserrable seulement "
                    "(%s m à l'analyse)."
                    % ("%g" % self._result.close_vertex_min_dist_m)))
        section.content_layout().addLayout(grid)
        return section

    def _build_not_fixable_section(self):
        """Hauteur imbriquée / croisements non nodés : SANS case à cocher —
        l'app ne propose pas une correction qui n'existe pas, elle dit
        pourquoi (en infobulle, pas en pavé de texte). Toujours dépliée : la
        reléguer masquerait justement ce que ce dialogue veut rendre visible.
        """
        section = CollapsibleSection("Non corrigible automatiquement",
                                     expanded=True, collapsible=False)
        grid = QGridLayout()
        grid.setVerticalSpacing(2)
        grid.setHorizontalSpacing(8)
        for r, (icon, title, count, fmt, tip) in enumerate((
            (C.ICON_NESTED_HEIGHT, C.CATEGORY_LABELS.get(C.CAT_NESTED_HEIGHT, ""),
             self._counts.get(C.CAT_NESTED_HEIGHT, 0), msg_nested_height,
             "Rien n'indique laquelle des deux altitudes est fausse — la "
             "reprise est attributaire et manuelle.\n"
             "🎯 Sélectionner les entités → catégorie « Hauteur imbriquée »."),
            (C.ICON_UNNODED_CROSSING, C.CATEGORY_LABELS.get(C.CAT_UNNODED_CROSSING, ""),
             self._counts.get(C.CAT_UNNODED_CROSSING, 0), msg_unnoded_crossing,
             "Insérer un sommet déplacerait le tracé — jamais sans "
             "confirmation explicite.\n"
             "🎯 Sélectionner les entités → catégorie « Croisement non nodé »."),
        )):
            heading = QLabel("%s %s" % (icon, title))
            heading.setStyleSheet("font-weight:600;")
            heading.setToolTip(tip)
            grid.addWidget(heading, r, 0)
            label = QLabel(fmt(count))
            label.setObjectName("sgHint")
            grid.addWidget(label, r, 1)
        section.content_layout().addLayout(grid)
        return section

    # ── Une ligne « aperçu » générique ───────────────────────────────────

    def _add_dry_run_row(self, grid, r, category, label, count, message_fn,
                         tol_attr=None, tol_unit=None, tol_range=None,
                         tol_default=None, tol_decimals=1, tol_step=0.5,
                         tol_prefix="≤ ", tooltip="", tol_detection=False):
        """Ajoute une ligne dont l'aperçu tourne via un dry-run.

        ``category`` désigne à la fois la clé de ``result.flagged`` (le lot
        de FID à prévisualiser) et, via ``_FLAG_ATTR``, le SEUL champ booléen
        activé dans le ``RepairOptions`` isolé de cette ligne (voir
        ``_isolated_ropts``). Si ``tol_attr`` est fourni, la case porte un
        réglage de tolérance inline, dont la valeur COURANTE est relue à
        chaque (re)calcul de l'aperçu.

        ``tol_detection`` : ce seuil est celui qui a servi à DÉTECTER les
        anomalies, pas celui qui règle l'agressivité du remède. La réparation
        ne travaille que sur les entités déjà signalées par l'analyse :
        resserrer un tel seuil est exact (il ne fait qu'écarter des candidats
        du lot), l'ÉLARGIR ne l'est pas — il corrigerait davantage dans les
        entités signalées tout en laissant identiques celles qui n'avaient pas
        franchi le seuil d'analyse, donnant deux traitements différents à la
        même donnée. Le maximum est donc verrouillé sur la valeur de
        l'analyse ; pour élargir, il faut relancer l'analyse.
        """
        chk = QCheckBox(("%s  (%d)" % (label, count)) if count else label)
        chk.setEnabled(count > 0)
        chk.toggled.connect(self._refresh_ok_button)
        grid.addWidget(chk, r, 0)

        spin = None
        if tol_attr is not None:
            spin = QDoubleSpinBox()
            if tol_detection and tol_default is not None:
                spin.setRange(min(tol_range[0], float(tol_default)),
                              float(tol_default))
            else:
                spin.setRange(*tol_range)
            spin.setDecimals(tol_decimals)
            spin.setSingleStep(tol_step)
            spin.setValue(tol_default)
            _prep_threshold_spin(spin, tol_unit, prefix=tol_prefix)
            spin.setEnabled(count > 0)
            grid.addWidget(spin, r, 1)

        refresh_btn = QPushButton("↻")
        refresh_btn.setToolTip("Réglage changé : cliquez pour recalculer l'aperçu.")
        refresh_btn.setFixedWidth(24)
        refresh_btn.setVisible(False)
        grid.addWidget(refresh_btn, r, 2)
        r += 1

        msg_label = QLabel("")
        msg_label.setObjectName("sgHint")
        grid.addWidget(msg_label, r, 0, 1, 3)
        r += 1

        if tooltip:
            chk.setToolTip(tooltip)

        fids = list(self._result.flagged.get(category, []) or [])

        row = _PreviewRow(category, count, checkbox=chk, spin=spin, fids=fids,
                          make_ropts=lambda: self._isolated_ropts(
                              category, tol_attr,
                              spin.value() if spin is not None else None),
                          format_message=(
                              (lambda c, s, _fn=message_fn, _sp=spin: _fn(c, s, _sp.value()))
                              if spin is not None else message_fn))
        row.message_label = msg_label
        row.refresh_btn = refresh_btn
        row.refresh_text()

        if spin is not None:
            spin.valueChanged.connect(lambda _v, cat=category: self._on_tolerance_changed(cat))
        refresh_btn.clicked.connect(lambda _=False, cat=category: self._refresh_row(cat))

        self._rows.append(row)
        return r

    def _add_static_row(self, grid, r, category, label, count, message_fn,
                        tooltip=""):
        chk = QCheckBox(("%s  (%d)" % (label, count)) if count else label)
        chk.setEnabled(count > 0)
        chk.toggled.connect(self._refresh_ok_button)
        if tooltip:
            chk.setToolTip(tooltip)
        grid.addWidget(chk, r, 0, 1, 3)
        r += 1
        msg_label = QLabel("")
        msg_label.setObjectName("sgHint")
        grid.addWidget(msg_label, r, 0, 1, 3)
        r += 1

        row = _PreviewRow(category, count, checkbox=chk, static=True,
                          format_message=message_fn)
        row.message_label = msg_label
        row.refresh_text()
        self._rows.append(row)
        return r

    # ── Construction des RepairOptions isolés (un seul indicateur actif) ──

    _FLAG_ATTR = {
        "invalid": "fix_invalid",
        "self_intersection": "fix_self_intersection",
        "small": "remove_small",
        "overlap": "fix_overlaps",
        C.CAT_DUPLICATE_VERTEX: "fix_duplicate_vertex",
        C.CAT_HOLE: "remove_holes",
        C.CAT_MULTIPART: "explode_multipart",
        C.CAT_SHARP_ANGLE: "fix_sharp_angles",
        C.CAT_CLOSE_VERTEX: "fix_close_vertices",
    }

    def _isolated_ropts(self, category, tol_attr, tol_value) -> RepairOptions:
        """RepairOptions n'activant QUE la case ``category``, avec la
        tolérance courante — les deux règles de chevauchement de l'analyse
        (sommet manquant, polygones inclus) sont toujours rejouées à
        l'identique, quelle que soit la ligne, pour que l'aperçu des
        chevauchements reste fidèle à l'analyse.

        ``fix_invalid`` est explicitement REMIS À FAUX : il vaut ``True`` par
        défaut dans ``RepairOptions``, si bien que chaque aperçu « isolé »
        appliquait aussi un ``make valid`` — la ligne « angles aigus »
        montrait donc le résultat obtenu sur des géométries rendues valides,
        que l'utilisateur ait coché « invalides » ou non. Toutes les cases
        partent d'ici à faux, et seule celle de la ligne est levée.
        """
        kwargs = {
            "fix_invalid": False,
            self._FLAG_ATTR[category]: True,
            "missing_vertex_ratio": self._result.missing_vertex_ratio,
            "allow_contained_polygons": self._result.allow_contained_polygons,
            "equal_is_overlap": not self._result.checks_enabled.get("duplicate", True),
        }
        if tol_attr == "overlap_max_fraction_pct":
            kwargs["overlap_max_fraction"] = tol_value / 100.0
        elif tol_attr is not None:
            kwargs[tol_attr] = tol_value
        return RepairOptions(**kwargs)

    # ── Aperçu : lancement, réception, rafraîchissement ──────────────────

    def _row(self, category) -> Optional[_PreviewRow]:
        for row in self._rows:
            if row.category == category:
                return row
        return None

    def _launch_initial_preview(self):
        jobs = []
        gens = {}
        for row in self._rows:
            if row.needs_preview():
                jobs.append((row.category, row.make_ropts(), row.fids))
                # Instantané du compteur à L'INSTANT du lancement : si un
                # rafraîchissement manuel a lieu avant que cette tâche
                # n'atteigne cette catégorie, son résultat sera comparé à ce
                # nombre FIGÉ et rejeté comme périmé (voir _on_row_ready).
                gens[row.category] = row.gen
        if not jobs:
            return
        worker = self._layer.clone()
        if worker is None or not worker.isValid():
            for row in self._rows:
                if row.needs_preview():
                    row.message_label.setText(
                        "Aperçu indisponible (copie de couche impossible).")
            return
        task = RepairPreviewTask(worker, self._result, jobs)
        task.rowReady.connect(
            lambda cat, stats, _gens=gens: self._on_row_ready(
                cat, stats, _gens.get(cat, 0)))
        task.taskTerminated.connect(self._on_initial_preview_terminated)
        self._initial_task = task
        QgsApplication.taskManager().addTask(task)

    def _on_row_ready(self, category, stats, expected_gen=None):
        if self._closing:
            return
        row = self._row(category)
        if row is None:
            return
        if expected_gen is not None and expected_gen != row.gen:
            return   # rafraîchissement plus récent entre-temps : on jette
        row.stats = stats
        row.stale = False
        if row.refresh_btn is not None:
            row.refresh_btn.setVisible(False)
        row.refresh_text()
        self._refresh_ok_button()

    def _on_initial_preview_terminated(self):
        self._initial_task = None

    def _on_tolerance_changed(self, category):
        row = self._row(category)
        if row is None:
            return
        row.stale = True
        if row.refresh_btn is not None:
            row.refresh_btn.setVisible(True)
        if row.message_label is not None:
            row.message_label.setText("Réglage changé — ↻ pour recalculer")
            row.message_label.setVisible(True)
        self._refresh_ok_button()

    def _refresh_row(self, category):
        row = self._row(category)
        if row is None or not row.needs_preview():
            return
        # Un rafraîchissement encore en vol pour CETTE ligne est annulé : le
        # nouveau, avec le réglage courant, prime. ``cancel()`` ne fait
        # qu'une DEMANDE — le résultat de l'ancienne tâche peut donc encore
        # arriver plus tard ; c'est le compteur de génération qui le jettera.
        if row._task is not None:
            with contextlib.suppress(Exception):
                row._task.cancel()
        row.gen += 1
        my_gen = row.gen
        worker = self._layer.clone()
        if worker is None or not worker.isValid():
            return
        task = RepairPreviewTask(worker, self._result,
                                [(row.category, row.make_ropts(), row.fids)])

        def _done(cat, stats, _cat=category, _gen=my_gen):
            self._on_row_ready(_cat, stats, _gen)

        def _clear(_row=row, _task=task):
            # Ne se désigne « terminée » que si AUCUN rafraîchissement plus
            # récent n'a déjà remplacé cette tâche — sinon on effacerait la
            # référence de la tâche ACTIVE avec le signal tardif de l'ancienne.
            if _row._task is _task:
                _row._task = None
                # Dialogue déjà fermé : ne plus toucher aux widgets, qui
                # peuvent être détruits côté C++ (RuntimeError sinon).
                if not self._closing and _row.refresh_btn is not None:
                    _row.refresh_btn.setEnabled(True)

        task.rowReady.connect(_done)
        task.taskCompleted.connect(_clear)
        task.taskTerminated.connect(_clear)
        row._task = task
        if row.refresh_btn is not None:
            row.refresh_btn.setEnabled(False)
        QgsApplication.taskManager().addTask(task)

    # ── État du bouton « Réparer… » ───────────────────────────────────────

    def _refresh_ok_button(self):
        any_checked = any(row.checkbox is not None and row.checkbox.isChecked()
                          for row in self._rows)
        all_ready = all(
            row.is_ready_for_repair()
            for row in self._rows
            if row.checkbox is not None and row.checkbox.isChecked()
        )
        ok_btn = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setEnabled(any_checked and all_ready)

    # ── Nettoyage ─────────────────────────────────────────────────────────

    def _on_finished(self, _result_code=0):
        self._closing = True
        if self._initial_task is not None:
            with contextlib.suppress(Exception):
                self._initial_task.cancel()
        for row in self._rows:
            if row._task is not None:
                with contextlib.suppress(Exception):
                    row._task.cancel()

    # ── Résultat ──────────────────────────────────────────────────────────

    def to_repair_options(self) -> RepairOptions:
        threshold = self._result.threshold_m2
        overlap_pct = sg_settings.load_overlap_tolerance_pct()
        hole_m2 = self._result.hole_threshold_m2
        sharp_deg = self._result.sharp_angle_min_deg
        close_m = self._result.close_vertex_min_dist_m

        def checked(cat):
            row = self._row(cat)
            return bool(row and row.checkbox is not None and row.checkbox.isChecked())

        def spin_value(cat, default):
            row = self._row(cat)
            if row is not None and row.spin is not None:
                return row.spin.value()
            return default

        overlap_pct_value = spin_value("overlap", overlap_pct)
        sg_settings.save_overlap_tolerance_pct(overlap_pct_value)

        return RepairOptions(
            fix_invalid=checked("invalid"),
            fix_self_intersection=checked("self_intersection"),
            remove_null_empty=checked("null_empty"),
            remove_duplicates=checked("duplicate"),
            remove_small=checked("small"),
            small_polygon_threshold_m2=spin_value("small", threshold),
            fix_overlaps=checked("overlap"),
            overlap_max_fraction=overlap_pct_value / 100.0,
            fix_duplicate_vertex=checked(C.CAT_DUPLICATE_VERTEX),
            remove_holes=checked(C.CAT_HOLE),
            hole_max_area_m2=spin_value(C.CAT_HOLE, hole_m2),
            explode_multipart=checked(C.CAT_MULTIPART),
            fix_sharp_angles=checked(C.CAT_SHARP_ANGLE),
            sharp_angle_min_deg=spin_value(C.CAT_SHARP_ANGLE, sharp_deg),
            fix_close_vertices=checked(C.CAT_CLOSE_VERTEX),
            close_vertex_min_dist_m=spin_value(C.CAT_CLOSE_VERTEX, close_m),
            missing_vertex_ratio=self._result.missing_vertex_ratio,
            allow_contained_polygons=self._result.allow_contained_polygons,
            equal_is_overlap=not self._result.checks_enabled.get("duplicate", True),
        )
