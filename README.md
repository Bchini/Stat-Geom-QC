# STAT GEOM QC — plugin QGIS

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-ffdd00?style=flat&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/bchini)

Contrôle qualité et statistiques géométriques des couches vectorielles QGIS :
score de *correctness*, détection d'anomalies (géométries nulles/vides,
invalides, auto-intersections, chevauchements, doublons, petits polygones,
incohérences bâtiments AGL/AMSL), réparation géométrique sélective écrite dans
un nouveau fichier, et rapports HTML/CSV/JSON. Portage natif PyQGIS, sans
dépendance externe.

Le code du plugin se trouve dans le dossier [`stat_geom_qc/`](stat_geom_qc/).
Documentation détaillée : [stat_geom_qc/README.md](stat_geom_qc/README.md).

## Installation

Dans QGIS : **Extensions → Installer/Gérer les extensions → Installer depuis un
ZIP**, puis sélectionner l'archive du plugin — ou l'installer depuis le dépôt
officiel QGIS une fois publié.

## Licence

GNU General Public License v2.0 ou ultérieure (voir le fichier `LICENSE`).

## Auteur

Adel Bchini — <adel.bchini@gmail.com>
