# -*- coding: utf-8 -*-
"""Classe principale du plugin STAT GEOM QC : enregistrement du menu / de la
barre d'outils et gestion du panneau dockable."""

import contextlib
import os

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .stat_geom_dockwidget import StatGeomDockWidget

PLUGIN_DIR = os.path.dirname(__file__)
MENU_NAME = "STAT GEOM QC"


class StatGeomQCPlugin:
    """Point d'entrée du plugin (interface iface QGIS)."""

    def __init__(self, iface):
        self.iface = iface
        self.dock = None
        self.action = None
        # Fournisseur Processing : enregistré par ``initProcessing``, qui est
        # appelé aussi bien depuis l'interface que sans elle.
        self.provider = None

    def initProcessing(self):  # noqa: N802 (API QGIS)
        """Enregistre les algorithmes auprès du registre Processing.

        Méthode SÉPARÉE de ``initGui`` à dessein : hors interface graphique —
        sous ``qgis_process`` en ligne de commande — QGIS n'appelle que
        celle-ci, jamais ``initGui``, et ``self.iface`` vaut alors ``None``.
        Rien ici ne doit donc toucher à l'interface, sous peine de faire échouer
        le chargement du plugin en ligne de commande.

        Suppose ``hasProcessingProvider=yes`` dans ``metadata.txt`` : sans ce
        drapeau, QGIS ne cherche même pas cette méthode et les algorithmes
        restent invisibles, sans le moindre message.
        """
        if self.provider is not None:
            return
        from .processing_algs import StatGeomQCProvider
        self.provider = StatGeomQCProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def unloadProcessing(self):  # noqa: N802 (API QGIS)
        """Retire le fournisseur du registre, s'il y était."""
        if self.provider is None:
            return
        with contextlib.suppress(Exception):
            QgsApplication.processingRegistry().removeProvider(self.provider)
        self.provider = None

    def initGui(self):  # noqa: N802 (API QGIS)
        # Un seul chemin d'enregistrement du fournisseur, comme le fait le
        # greffon GRASS livré avec QGIS : en mode graphique, ``initGui`` appelle
        # ``initProcessing`` ; sans interface, QGIS appelle directement ce
        # dernier. Les algorithmes sont ainsi disponibles dans les deux cas.
        self.initProcessing()

        icon_path = os.path.join(PLUGIN_DIR, "icon.svg")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        self.action = QAction(icon, "STAT GEOM QC — Contrôle qualité", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.triggered.connect(self.toggle_panel)

        self.iface.addPluginToVectorMenu(MENU_NAME, self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        self.unloadProcessing()
        if self.action is not None:
            self.iface.removePluginVectorMenu(MENU_NAME, self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None
        if self.dock is not None:
            # Annule tout traitement d'arrière-plan avant de retirer le panneau.
            with contextlib.suppress(Exception):
                self.dock.cancel_tasks()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None

    def toggle_panel(self, checked=None):
        just_created = self.dock is None
        if just_created:
            self.dock = StatGeomDockWidget(self.iface, self.iface.mainWindow())
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
            self.dock.visibilityChanged.connect(self._on_visibility_changed)
        # Un panneau fraîchement ajouté est déjà visible : l'inverser ici le
        # masquerait aussitôt, rendant le premier clic inopérant.
        visible = True if just_created else not self.dock.isVisible()
        self.dock.setVisible(visible)
        if self.action is not None:
            self.action.setChecked(visible)

    def _on_visibility_changed(self, visible):
        if self.action is not None:
            self.action.setChecked(visible)
