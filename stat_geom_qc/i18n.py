# -*- coding: utf-8 -*-
"""Traduction de l'interface et des rapports.

L'ANGLAIS est la langue source : c'est lui qui figure dans les literals du code,
et c'est lui qui sert de clé de traduction. Le français est donc une traduction
comme les autres. Cette convention est celle de QGIS et des plateformes de
traduction collaborative, qui attendent toutes de l'anglais en entrée — sans
elle, aucun contributeur hispanophone ou sinophone ne pourrait aider.

Pourquoi des catalogues PYTHON et non des ``.qm`` Qt
---------------------------------------------------
Le mécanisme habituel de Qt compile des ``.ts`` en ``.qm`` binaires avec
``lrelease``. Cet outil n'est livré ni par QGIS ni par PyQt : il appartient au
SDK Qt. Un ``.ts`` seul est inerte à l'exécution, et personne ne peut donc
regénérer les traductions sans installer Qt en entier.

Un catalogue Python est un simple dictionnaire ``{source anglaise: traduction}``.
Il ne demande aucune compilation, se relit et se corrige dans n'importe quel
éditeur, se teste dans la suite headless (couverture, clés orphelines), et ne
peut pas se désynchroniser d'un binaire puisqu'il n'y en a pas. Les fichiers
``.ts`` restent générés à côté, comme format d'échange pour qui voudrait
travailler avec les outils Qt (voir ``tools/export_ts.py``).

Chaîne inconnue = chaîne rendue telle quelle
--------------------------------------------
``tr`` ne lève jamais et ne masque jamais un texte : une clé absente du
catalogue renvoie la source anglaise. Une traduction incomplète dégrade donc
l'affichage vers l'anglais, elle ne le vide pas. C'est aussi ce qui permet
d'ajouter une chaîne au code sans bloquer les six autres langues.
"""

import importlib

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QLocale

# Langue source : ses « traductions » sont les chaînes elles-mêmes.
SOURCE_LANGUAGE = "en"

# Langues déclarées, code ISO 639-1 -> nom dans la langue elle-même (c'est ainsi
# qu'un utilisateur reconnaît la sienne dans une liste).
LANGUAGES = {
    "en": "English",
    "fr": "Français",
}

_catalog = None          # {source: traduction} de la langue active
_language = None         # code de la langue active, une fois résolue


def _detect_language() -> str:
    """Langue de l'interface QGIS, réduite à son code de langue.

    QGIS n'écrit ``locale/userLocale`` que si l'utilisateur a explicitement
    surchargé la langue ; sinon les clés sont absentes et c'est la locale
    système qui fait foi. On lit donc les deux, dans cet ordre, avant de retomber
    sur la langue source.

    Le code régional est ignoré : ``pt_BR`` et ``pt_PT`` partagent ``pt``. Une
    variante régionale se traiterait en ajoutant une entrée à ``LANGUAGES``.
    """
    code = ""
    try:
        settings = QgsSettings()
        if settings.value("locale/overrideFlag", False, type=bool):
            code = settings.value("locale/userLocale", "") or ""
        if not code:
            code = settings.value("locale/globalLocale", "") or ""
    except Exception:
        code = ""
    if not code:
        try:
            code = QLocale.system().name() or ""
        except Exception:
            code = ""
    langue = code.replace("-", "_").split("_")[0].lower()
    return langue if langue in LANGUAGES else SOURCE_LANGUAGE


def _load_catalog(langue: str) -> dict:
    """Catalogue d'une langue, ou dictionnaire vide.

    Un catalogue absent ou illisible n'est PAS une erreur fatale : l'interface
    reste en anglais. Mieux vaut un plugin en langue source qu'un plugin qui
    refuse de s'ouvrir parce qu'un fichier de traduction manque.
    """
    if langue == SOURCE_LANGUAGE:
        return {}
    try:
        module = importlib.import_module(
            "%s.translations.%s" % (__package__, langue))
    except Exception:
        return {}
    messages = getattr(module, "MESSAGES", None)
    return messages if isinstance(messages, dict) else {}


def set_language(langue) -> str:
    """Force la langue active. ``None`` rétablit la détection automatique.

    Sert au panneau (sélecteur de langue) et aux tests, qui doivent pouvoir
    vérifier chaque catalogue sans dépendre de la locale de la machine.
    """
    global _catalog, _language
    if langue is None:
        _language = None
        _catalog = None
        return active_language()
    _language = langue if langue in LANGUAGES else SOURCE_LANGUAGE
    _catalog = _load_catalog(_language)
    return _language


def active_language() -> str:
    """Code de la langue active, résolue et mémorisée au premier appel."""
    global _catalog, _language
    if _language is None:
        _language = _detect_language()
        _catalog = _load_catalog(_language)
    return _language


def tr(text: str) -> str:
    """Traduit une chaîne source anglaise dans la langue active.

    Rendue telle quelle si la langue active est l'anglais, ou si la chaîne n'est
    pas au catalogue.
    """
    if _catalog is None:
        active_language()
    if not _catalog:
        return text
    return _catalog.get(text, text)


def translation_coverage(langue: str) -> tuple:
    """``(traduites, total)`` d'un catalogue face aux chaînes du code.

    Destiné aux tests et à un futur écran « à propos » : une couverture
    partielle doit être CONSTATABLE, pas devinée en parcourant l'interface.
    """
    from .translatable import SOURCE_STRINGS
    catalogue = _load_catalog(langue)
    if langue == SOURCE_LANGUAGE:
        return (len(SOURCE_STRINGS), len(SOURCE_STRINGS))
    traduites = sum(1 for s in SOURCE_STRINGS
                    if catalogue.get(s) and catalogue[s] != s)
    return (traduites, len(SOURCE_STRINGS))
