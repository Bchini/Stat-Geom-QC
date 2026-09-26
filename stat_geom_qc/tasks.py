# -*- coding: utf-8 -*-
"""Traitements longs exécutés en arrière-plan via QgsTask.

Objectif : ne jamais bloquer l'interface QGIS pendant une analyse ou une
réparation, et permettre une annulation propre.

Sûreté des threads
-------------------
Un QgsTask s'exécute dans un thread secondaire. Les objets QGIS ne sont pas
sûrs à partager entre threads. On confie donc à chaque tâche une COPIE privée
de la couche (``layer.clone()`` réalisé sur le thread principal), que le thread
principal ne touche plus ensuite. Les identifiants d'entités (FID) d'un clone
restent identiques à ceux de la couche d'origine (vérifié pour les fournisseurs
OGR et mémoire), de sorte que la sélection et la couche d'erreurs restent
valides côté couche source.
"""

import os
import traceback
from typing import Optional

from qgis.PyQt.QtCore import pyqtSignal
from qgis.core import (
    QgsCoordinateTransformContext,
    QgsTask,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from . import constants as C
from .analysis_engine import (
    AnalysisCancelled,
    AnalysisOptions,
    AnalysisResult,
    GeomAnalyzer,
    MeasurementContext,
    RepairOptions,
    RepairStats,
)


class AnalysisTask(QgsTask):
    """Analyse une couche (clone privé) dans un thread d'arrière-plan.

    À l'issue de ``run()`` :
      * succès  -> ``taskCompleted`` émis, ``result`` renseigné ;
      * annulé  -> ``taskTerminated`` émis, ``result`` vaut ``None`` ;
      * erreur  -> ``taskTerminated`` émis, ``error`` contient le message.
    """

    # Message de progression détaillé (le pourcentage passe par progressChanged).
    progressMessage = pyqtSignal(str)

    def __init__(self, worker_layer: QgsVectorLayer, options: AnalysisOptions,
                 description: str = "STAT GEOM QC — analyse"):
        super().__init__(description, QgsTask.Flag.CanCancel)
        self._layer = worker_layer            # clone privé, propriété de la tâche
        self._options = options
        # Capturé ICI, donc sur le thread principal : le moteur ne doit jamais
        # interroger le singleton QgsProject depuis le thread secondaire.
        self._mctx = MeasurementContext.from_project()
        self.result: Optional[AnalysisResult] = None
        self.error: Optional[str] = None

    def run(self) -> bool:  # exécuté dans le thread secondaire
        try:
            analyzer = GeomAnalyzer(self._options)
            self.result = analyzer.analyze(
                self._layer,
                progress_cb=self._on_progress,
                is_canceled=self.isCanceled,
                measurement_ctx=self._mctx,
            )
            return True
        except AnalysisCancelled:
            self.result = None
            return False
        except Exception:  # noqa: BLE001 - toute erreur est remontée proprement
            self.error = traceback.format_exc()
            self.result = None
            return False

    def _on_progress(self, pct: float, msg: str = "") -> None:
        self.setProgress(max(0.0, min(100.0, float(pct))))
        if msg:
            self.progressMessage.emit(msg)


class RepairTask(QgsTask):
    """Construit une couche réparée (clone privé) puis l'écrit dans un fichier.

    L'écriture (QgsVectorFileWriter) a lieu dans le thread secondaire ; le
    chargement du fichier résultant dans le projet est laissé au thread
    principal (appelant), une fois la tâche terminée.
    """

    progressMessage = pyqtSignal(str)

    def __init__(self, worker_layer: QgsVectorLayer, result: AnalysisResult,
                 ropts: RepairOptions, out_path: str,
                 transform_context: QgsCoordinateTransformContext,
                 description: str = "STAT GEOM QC — réparation"):
        super().__init__(description, QgsTask.Flag.CanCancel)
        self._layer = worker_layer
        self._result = result
        self._ropts = ropts
        self._out_path = out_path
        self._ctx = transform_context
        # Même précaution que pour l'analyse : instantané pris sur le thread
        # principal, la mesure des trous et les conversions d'unités s'y fient.
        self._mctx = MeasurementContext.from_project()
        self.stats: Optional[RepairStats] = None
        self.error: Optional[str] = None

    def run(self) -> bool:  # exécuté dans le thread secondaire
        try:
            self.progressMessage.emit("Construction de la couche réparée…")
            mem, stats = GeomAnalyzer.build_repaired_layer(
                self._layer, self._result, self._ropts,
                progress_cb=lambda p: self.setProgress(max(0.0, min(95.0, p * 0.95))),
                is_canceled=self.isCanceled,
                measurement_ctx=self._mctx,
            )
            if self.isCanceled():
                return False
            self.progressMessage.emit("Écriture du fichier…")
            self._write_layer(mem, self._out_path)
            self.setProgress(100.0)
            self.stats = stats
            return True
        except AnalysisCancelled:
            return False
        except Exception:  # noqa: BLE001
            self.error = traceback.format_exc()
            return False

    def _write_layer(self, mem: QgsVectorLayer, out_path: str) -> None:
        ext = os.path.splitext(out_path)[1].lower()
        driver = C.DRIVER_BY_EXT.get(ext, C.DEFAULT_DRIVER)
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = driver
        opts.layerName = os.path.splitext(os.path.basename(out_path))[0]

        # API récente (QGIS ≥ 3.20) avec repli sur l'ancienne signature.
        try:
            res = QgsVectorFileWriter.writeAsVectorFormatV3(
                mem, out_path, self._ctx, opts)
        except AttributeError:
            res = QgsVectorFileWriter.writeAsVectorFormatV2(
                mem, out_path, self._ctx, opts)

        err_code = res[0]
        err_msg = res[1] if len(res) > 1 else ""
        if err_code != QgsVectorFileWriter.WriterError.NoError:
            raise RuntimeError(err_msg or ("code d'erreur %s" % err_code))


class RepairPreviewTask(QgsTask):
    """Calcule un aperçu — RIEN n'est écrit — pour une ou plusieurs catégories
    de correction du dialogue de réparation.

    Chaque ``job`` = ``(clé_catégorie, RepairOptions, [fid])`` : un
    ``RepairOptions`` ISOLÉ à la seule case de cette catégorie (répond à « si
    je ne coche QUE celle-ci »), restreint aux FID déjà signalés par
    l'analyse pour cette catégorie — jamais toute la couche, jamais les autres
    catégories. Voir ``GeomAnalyzer.build_repaired_layer(dry_run=True,
    only_fids=...)``, qui exécute le même calcul de décision que la
    réparation réelle, sans rien construire.

    ``rowReady`` est émis catégorie par catégorie, AU FIL du calcul (même
    principe que ``progressMessage`` sur les deux tâches ci-dessus) : les
    catégories bon marché (petits polygones) remplissent leur ligne pendant
    que les plus coûteuses (chevauchements) tournent encore.
    """

    rowReady = pyqtSignal(str, object)   # (clé_catégorie, RepairStats)

    def __init__(self, worker_layer: QgsVectorLayer, result: AnalysisResult,
                 jobs, description: str = "STAT GEOM QC — aperçu réparation"):
        super().__init__(description, QgsTask.Flag.CanCancel)
        self._layer = worker_layer
        self._result = result
        self._jobs = list(jobs)
        self._mctx = MeasurementContext.from_project()
        self.stats_by_category = {}
        self.error: Optional[str] = None

    def run(self) -> bool:  # exécuté dans le thread secondaire
        try:
            for key, ropts, fids in self._jobs:
                if self.isCanceled():
                    raise AnalysisCancelled()
                _mem, stats = GeomAnalyzer.build_repaired_layer(
                    self._layer, self._result, ropts,
                    is_canceled=self.isCanceled, measurement_ctx=self._mctx,
                    dry_run=True, only_fids=fids)
                self.stats_by_category[key] = stats
                self.rowReady.emit(key, stats)
            return True
        except AnalysisCancelled:
            return False
        except Exception:  # noqa: BLE001
            self.error = traceback.format_exc()
            return False
