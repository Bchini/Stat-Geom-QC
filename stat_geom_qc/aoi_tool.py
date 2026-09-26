# -*- coding: utf-8 -*-
"""Outil de canevas : tracer un rectangle de zone d'analyse (AOI).

Un cliquer-glisser sur la carte définit l'étendue à laquelle l'analyse sera
restreinte. L'outil ne modifie RIEN : il ne touche ni la couche, ni le projet,
ni la sélection — il émet une étendue et rend la main.

Il se comporte comme les outils de mesure de QGIS : la touche *Échap* annule
sans rien produire, et l'outil précédent est rétabli par l'appelant à la fin
(voir ``StatGeomDockWidget._draw_aoi``), pour ne pas laisser l'utilisateur
prisonnier d'un mode dont il ne connaît pas la sortie.
"""

import contextlib

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsGeometry, QgsPointXY, QgsRectangle, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand

from . import constants as C


def rect_is_usable(rect) -> bool:
    """Ce rectangle définit-il une zone exploitable ?

    Faux pour ``None`` et pour tout rectangle plat — celui qu'on obtient en
    cliquant sans glisser. Une zone plate ne sélectionnerait aucune entité, et
    rien ne l'expliquerait à l'utilisateur : mieux vaut annuler franchement.

    Fonction séparée de l'outil à dessein : elle est vérifiable sans canevas,
    là où instancier un ``QgsMapTool`` exige une application QGIS graphique.
    """
    if rect is None:
        return False
    try:
        return (not rect.isEmpty()) and rect.width() > 0 and rect.height() > 0
    except Exception:
        return False


class AoiRectTool(QgsMapTool):
    """Trace un rectangle au cliquer-glisser et émet son étendue.

    ``rectDefined`` porte un ``QgsRectangle`` exprimé dans le CRS DU CANEVAS,
    jamais dans celui de la couche : la conversion est faite plus tard, une
    fois la couche connue (voir ``GeomAnalyzer._resolve_aoi``). Émettre déjà
    converti obligerait cet outil à connaître la couche analysée, qui peut
    changer entre le tracé et le lancement.

    ``canceled`` est émis sur *Échap* ou sur un rectangle dégénéré (simple
    clic sans glisser) : mieux vaut ne rien produire qu'une zone vide qui
    n'aurait sélectionné aucune entité sans que rien ne l'explique.
    """

    rectDefined = pyqtSignal(object)   # QgsRectangle, CRS du canevas
    canceled = pyqtSignal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self._canvas = canvas
        self._start = None
        self._band = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    # -- Cycle de vie -------------------------------------------------------

    def deactivate(self):
        """Toujours nettoyer le tracé : un rectangle fantôme resterait
        dessiné par-dessus la carte après un changement d'outil."""
        self._clear_band()
        self._start = None
        super().deactivate()

    def _clear_band(self):
        if self._band is None:
            return
        band, self._band = self._band, None
        with contextlib.suppress(Exception):
            scene = band.scene()
            if scene is not None:
                scene.removeItem(band)

    def _ensure_band(self):
        if self._band is not None:
            return self._band
        band = QgsRubberBand(self._canvas,
                             QgsWkbTypes.GeometryType.PolygonGeometry)
        couleur = QColor(*(int(x) for x in C.AOI_COLOR.split(",")))
        band.setColor(couleur)
        remplissage = QColor(couleur)
        remplissage.setAlpha(C.AOI_FILL_ALPHA)
        band.setFillColor(remplissage)
        band.setWidth(C.AOI_WIDTH)
        self._band = band
        return band

    # -- Interactions -------------------------------------------------------

    def canvasPressEvent(self, event):  # noqa: N802 (API QGIS)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._start = self.toMapCoordinates(event.pos())
        self._ensure_band().reset(QgsWkbTypes.GeometryType.PolygonGeometry)

    def canvasMoveEvent(self, event):  # noqa: N802 (API QGIS)
        if self._start is None:
            return
        self._draw(self._rect_to(event.pos()))

    def canvasReleaseEvent(self, event):  # noqa: N802 (API QGIS)
        if self._start is None or event.button() != Qt.MouseButton.LeftButton:
            return
        rect = self._rect_to(event.pos())
        self._start = None
        self._clear_band()
        # Un simple clic donne un rectangle plat : ce n'est pas une zone.
        if not rect_is_usable(rect):
            self.canceled.emit()
            return
        self.rectDefined.emit(rect)

    def keyPressEvent(self, event):  # noqa: N802 (API QGIS)
        if event.key() == Qt.Key.Key_Escape:
            self._start = None
            self._clear_band()
            self.canceled.emit()
            return
        super().keyPressEvent(event)

    # -- Tracé --------------------------------------------------------------

    def _rect_to(self, pos):
        if self._start is None:
            return None
        end = self.toMapCoordinates(pos)
        return QgsRectangle(QgsPointXY(self._start), QgsPointXY(end))

    def _draw(self, rect):
        if rect is None:
            return
        band = self._ensure_band()
        with contextlib.suppress(Exception):
            band.setToGeometry(QgsGeometry.fromRect(rect), None)
