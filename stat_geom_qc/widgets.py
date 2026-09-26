# -*- coding: utf-8 -*-
"""Widgets personnalisés : jauge de score, carte d'indicateur et section
repliable. Tous acceptent les couleurs du thème courant (voir ``theme``) afin
de rester lisibles en palette claire comme en palette sombre."""

from qgis.PyQt.QtCore import Qt, QRectF, QSize, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QPainter, QPen
from qgis.PyQt.QtWidgets import (QFrame, QHBoxLayout, QLabel, QSizePolicy,
                                 QToolButton, QVBoxLayout, QWidget)


class ScoreGauge(QWidget):
    """Jauge circulaire (anneau) affichant un score 0..100."""

    def __init__(self, parent=None, diameter=132):
        super().__init__(parent)
        self._score = 0.0
        self._color = QColor("#94a3b8")
        self._grade = ""
        self._track = QColor("#e2e8f0")
        self._diameter = diameter
        self.setMinimumSize(QSize(diameter, diameter))
        self.setMaximumSize(QSize(diameter, diameter))

    def set_track_color(self, color_hex):
        """Couleur de l'anneau de fond (dépend du thème)."""
        self._track = QColor(color_hex)
        self.update()

    def set_score(self, score, color_hex, grade=""):
        self._score = max(0.0, min(100.0, float(score)))
        self._color = QColor(color_hex)
        self._grade = grade or ""
        self.update()

    def paintEvent(self, event):  # noqa: N802 (API Qt)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        side = min(self.width(), self.height())
        pen_w = max(10, int(side * 0.11))
        margin = pen_w / 2 + 2
        rect = QRectF(margin, margin, side - 2 * margin, side - 2 * margin)

        # Anneau de fond
        p.setPen(QPen(self._track, pen_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(rect, 0, 360 * 16)

        # Arc de score (départ en haut, sens horaire)
        span = int(-self._score / 100.0 * 360 * 16)
        p.setPen(QPen(self._color, pen_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(rect, 90 * 16, span)

        # Valeur au centre
        p.setPen(self._color)
        f = QFont()
        f.setPointSizeF(side * 0.24)
        f.setBold(True)
        p.setFont(f)
        txt = ("%g" % self._score) if self._score == int(self._score) else ("%.1f" % self._score)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, txt)

        p.end()


class MetricCard(QFrame):
    """Petite carte : valeur en gros + libellé, colorée selon la gravité."""

    _PALETTE = {
        "ok": ("#0ea5e9", "#e0f2fe", "#bae6fd"),
        "err": ("#dc2626", "#fef2f2", "#fecaca"),
        "warn": ("#d97706", "#fffbeb", "#fde68a"),
        "muted": ("#475569", "#f1f5f9", "#e2e8f0"),
    }

    def __init__(self, value, label, severity="muted", parent=None,
                 palette=None, label_color="#64748b"):
        """``palette`` : {gravité: (texte, fond, bordure)} issu du thème courant.

        À défaut, la palette claire historique est utilisée : la carte reste
        utilisable hors du panneau (tests, réemploi).
        """
        super().__init__(parent)
        table = palette or self._PALETTE
        fg, bg, border = table.get(severity, table.get("muted"))
        self.setObjectName("metricCard")
        self.setStyleSheet(
            "#metricCard{background:%s;border:1px solid %s;border-radius:12px;}" % (bg, border)
        )
        self.setMinimumWidth(104)
        self.setFixedHeight(64)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(0)

        self.v = QLabel(str(value))
        vf = QFont()
        vf.setPointSize(16)
        vf.setBold(True)
        self.v.setFont(vf)
        self.v.setStyleSheet("color:%s;background:transparent;" % fg)

        self.lbl = QLabel(label)
        self.lbl.setStyleSheet(
            "color:%s;background:transparent;font-size:11px;" % label_color)

        lay.addWidget(self.v)
        lay.addWidget(self.lbl)


class CollapsibleSection(QWidget):
    """Section repliable : un en-tête cliquable et un contenu masquable.

    L'en-tête affiche un chevron (▸ replié / ▾ déplié), un titre et une pastille
    d'information à droite (par exemple le nombre de contrôles activés). Le
    contenu est **replié par défaut** : les widgets qu'il porte existent bel et
    bien, ils sont simplement masqués — leur état (cases cochées) reste donc
    parfaitement fonctionnel.

    ``collapsible=False`` verrouille la section ouverte : elle garde son cadre,
    son titre et sa pastille, mais ne peut plus être refermée. Pour un bloc que
    l'utilisateur doit avoir en permanence sous les yeux, un repli n'est pas une
    commodité — c'est un moyen de perdre de vue ce qui sera réellement calculé.
    Le chevron disparaît alors, puisqu'il n'y a plus rien à actionner.
    """

    toggled = pyqtSignal(bool)

    def __init__(self, title, parent=None, expanded=False, collapsible=True):
        super().__init__(parent)
        self._title = title
        self._collapsible = bool(collapsible)
        if not self._collapsible:
            expanded = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.header = QToolButton()
        self.header.setObjectName("sgFold")
        self.header.setCheckable(True)
        self.header.setChecked(bool(expanded))
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        if self._collapsible:
            self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            # Verrouillée ouverte : la barre reste un titre, plus un bouton. On
            # laisse passer les clics au lieu de désactiver le bouton, qui en
            # grisant le texte donnerait l'impression d'une section inactive.
            self.header.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self.header.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # L'en-tête occupe toute la largeur disponible : la pastille se retrouve
        # ainsi plaquée à droite, comme dans une barre de section.
        self.header.setSizePolicy(QSizePolicy.Policy.Expanding,
                                  QSizePolicy.Policy.Fixed)
        self.header.toggled.connect(self._on_toggled)

        # La BARRE est le cadre visuel : le bouton y est transparent et la
        # pastille se place à l'intérieur, alignée à droite.
        self.header_row = QWidget()
        self.header_row.setObjectName("sgFoldRow")
        header_lay = QHBoxLayout(self.header_row)
        header_lay.setContentsMargins(0, 0, 11, 0)
        header_lay.setSpacing(0)
        header_lay.addWidget(self.header, 1)
        self.badge = QLabel("")
        self.badge.setObjectName("sgFoldBadge")
        self.badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        header_lay.addWidget(self.badge)
        outer.addWidget(self.header_row)

        self.body = QWidget()
        self.body.setObjectName("sgFoldBody")
        self._body_layout = QVBoxLayout(self.body)
        self._body_layout.setContentsMargins(11, 10, 11, 11)
        self._body_layout.setSpacing(8)
        self.body.setVisible(bool(expanded))
        outer.addWidget(self.body)

        self._refresh_header()

    # -- API ----------------------------------------------------------------

    def content_layout(self):
        """Disposition dans laquelle placer les widgets de la section."""
        return self._body_layout

    def set_expanded(self, expanded):
        """Ignoré sur une section verrouillée ouverte (``collapsible=False``)."""
        if not self._collapsible:
            return
        self.header.setChecked(bool(expanded))

    def is_expanded(self):
        return self.header.isChecked()

    def set_badge(self, text, active=False):
        """Texte de la pastille ; ``active`` la met en avant (accent)."""
        self.badge.setText(text or "")
        self.badge.setObjectName("sgFoldBadgeActive" if active else "sgFoldBadge")
        # Forcer la réapplication de la feuille de style après changement de nom.
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)

    # -- Interne ------------------------------------------------------------

    def _on_toggled(self, checked):
        self.body.setVisible(checked)
        self._refresh_header()
        self.toggled.emit(checked)

    def _refresh_header(self):
        expanded = self.header.isChecked()
        if self._collapsible:
            self.header.setText("%s  %s" % ("▾" if expanded else "▸", self._title))
        else:
            # Pas de chevron : il annoncerait une action qui n'existe plus.
            self.header.setText(self._title)
        # Le cadre de la barre change d'aspect à l'ouverture (bordure d'accent et
        # coins bas droits, pour se souder au contenu).
        self.header_row.setObjectName("sgFoldRowOpen" if expanded else "sgFoldRow")
        self.header_row.style().unpolish(self.header_row)
        self.header_row.style().polish(self.header_row)
