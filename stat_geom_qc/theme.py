# -*- coding: utf-8 -*-
"""Thème visuel du panneau STAT GEOM QC.

Le panneau adopte une identité unique — surfaces sobres, accent turquoise,
angles arrondis — déclinée en DEUX palettes : sombre et claire. La palette est
choisie d'après le thème réellement appliqué par QGIS, détecté sur la luminance
de la couleur de fond de l'application (``UI/UITheme`` n'est pas fiable : il est
vide sur une installation standard).

Toutes les couleurs sont regroupées ici afin que l'interface, la jauge et les
cartes d'indicateurs partagent une source unique.
"""

import os
from typing import Dict

from qgis.PyQt.QtGui import QPalette

# Dossier des pictogrammes livrés avec le plugin (coches des cases à cocher).
# Qt attend des séparateurs « / » dans une url() de feuille de style, y compris
# sous Windows ; le chemin est toujours ENTRE GUILLEMETS à l'usage, car un
# dossier d'installation peut contenir des espaces.
_ASSET_DIR = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")


def _asset(name: str) -> str:
    """Chemin absolu d'un pictogramme, au format attendu par une url() QSS."""
    return "%s/%s" % (_ASSET_DIR, name)

# ── Palettes ────────────────────────────────────────────────────────────────
# Identité commune : accent turquoise, hiérarchie panel > card > field.

DARK: Dict[str, str] = {
    "panel": "#1f2430",
    "card": "#171b24",
    "card_alt": "#232936",
    "field": "#141821",
    "border": "#333a49",
    "border_strong": "#3d4657",
    "text": "#f1efe8",
    "text_muted": "#9fa6b5",
    "text_soft": "#c3c9d4",
    "accent": "#1d9e75",
    "accent_hover": "#25b686",
    "accent_text": "#04342c",
    "accent_soft": "#0f6e56",
    "check": "#5dcaa5",
    "gauge_track": "#2a3340",
    "danger": "#f09595",
    "danger_bg": "#3a2224",
    "danger_border": "#5c3034",
    "warn": "#efb057",
    "warn_bg": "#3a2f1c",
    "warn_border": "#5a4527",
    "ok": "#5dcaa5",
    "ok_bg": "#173029",
    "ok_border": "#245043",
    "muted_fg": "#aab2c0",
    "muted_bg": "#20262f",
    "muted_border": "#333a49",
    "tab_bg": "#141821",
    "tab_text": "#9fa6b5",
}

LIGHT: Dict[str, str] = {
    "panel": "#f6f8fa",
    "card": "#ffffff",
    "card_alt": "#f1f4f8",
    "field": "#ffffff",
    "border": "#e2e8f0",
    "border_strong": "#cbd5e1",
    "text": "#1b2430",
    "text_muted": "#5f6b7a",
    "text_soft": "#425063",
    "accent": "#0f8a67",
    "accent_hover": "#0c7457",
    "accent_text": "#ffffff",
    "accent_soft": "#d5efe6",
    "check": "#0f8a67",
    "gauge_track": "#e2e8f0",
    "danger": "#b3261e",
    "danger_bg": "#fdeceb",
    "danger_border": "#f4c7c3",
    "warn": "#8a5a09",
    "warn_bg": "#fdf3e2",
    "warn_border": "#f0dcb4",
    "ok": "#0f6e56",
    "ok_bg": "#e3f4ed",
    "ok_border": "#bfe3d6",
    "muted_fg": "#5f6b7a",
    "muted_bg": "#f1f4f8",
    "muted_border": "#e2e8f0",
    "tab_bg": "#e8edf3",
    "tab_text": "#5f6b7a",
}

# Couleurs des grades du score, par palette (le moteur fournit un grade, pas une
# couleur d'affichage : on la ré-associe ici pour rester lisible sur fond sombre).
GRADE_COLORS = {
    "dark": {
        "Excellent": "#5dcaa5", "Bon": "#7fd8b8", "Correct": "#efb057",
        "Faible": "#f0a071", "Critique": "#f09595",
    },
    "light": {
        "Excellent": "#0f6e56", "Bon": "#158a68", "Correct": "#8a5a09",
        "Faible": "#a8500f", "Critique": "#b3261e",
    },
}


def is_dark(widget) -> bool:
    """Vrai si l'interface hôte utilise un thème sombre.

    Mesure la luminance perçue du fond de fenêtre. Repli prudent sur « clair »
    si la palette est indisponible.
    """
    try:
        palette = widget.palette() if widget is not None else QPalette()
        color = palette.color(QPalette.ColorRole.Window)
        luminance = (0.2126 * color.redF() + 0.7152 * color.greenF()
                     + 0.0722 * color.blueF())
        return luminance < 0.45
    except Exception:
        return False


def colors_for(dark: bool) -> Dict[str, str]:
    """Palette complète correspondant au thème détecté.

    Contient aussi les chemins des pictogrammes de coche, déclinés par thème :
    la feuille de style les consomme comme les couleurs, sans avoir à savoir
    quelle palette est active.
    """
    colors = dict(DARK if dark else LIGHT)
    suffix = "dark" if dark else "light"
    colors["check_icon"] = _asset("check_%s.svg" % suffix)
    colors["check_ghost"] = _asset("check_ghost_%s.svg" % suffix)
    return colors


def grade_color(grade: str, dark: bool, fallback: str) -> str:
    """Couleur d'affichage d'un grade, adaptée au thème."""
    table = GRADE_COLORS["dark" if dark else "light"]
    return table.get(grade, fallback)


def severity_colors(colors: Dict[str, str]) -> Dict[str, tuple]:
    """Palette des cartes d'indicateurs : (texte, fond, bordure) par gravité."""
    return {
        "ok": (colors["accent"], colors["ok_bg"], colors["ok_border"]),
        "err": (colors["danger"], colors["danger_bg"], colors["danger_border"]),
        "warn": (colors["warn"], colors["warn_bg"], colors["warn_border"]),
        "muted": (colors["muted_fg"], colors["muted_bg"], colors["muted_border"]),
    }


def checkbox_assets_available(c: Dict[str, str]) -> bool:
    """Vrai si les deux pictogrammes de coche du thème sont bien présents."""
    return all(os.path.isfile(c.get(key, ""))
               for key in ("check_icon", "check_ghost"))


def _checkbox_rules(c: Dict[str, str]) -> str:
    """Règles QSS des cases à cocher, avec REPLI si les pictogrammes manquent.

    L'état coché repose sur un fichier livré avec le plugin. Une installation
    incomplète le rendrait invisible : toutes les cases paraîtraient vides, sans
    aucun message d'erreur — impossible de savoir ce qui est activé. Dans ce cas
    on revient donc au carré entièrement rempli des versions antérieures : moins
    parlant, mais l'information reste lisible.
    """
    if not checkbox_assets_available(c):
        return """
QCheckBox {{ color:{text_soft}; spacing:9px; }}
QCheckBox:disabled {{ color:{text_muted}; }}
QCheckBox::indicator {{ width:14px; height:14px; border-radius:5px;
    border:2px solid {border_strong}; background:{field}; }}
QCheckBox::indicator:hover {{ border-color:{accent}; }}
QCheckBox::indicator:checked {{ background:{check}; border-color:{check}; }}
QCheckBox::indicator:disabled {{ border-color:{border}; background:{muted_bg}; }}
""".format(**c)
    return """
QCheckBox {{ color:{text_soft}; spacing:9px; }}
QCheckBox:disabled {{ color:{text_muted}; }}
QCheckBox::indicator {{ width:14px; height:14px; border-radius:5px;
    border:2px solid {border_strong}; background:{field}; }}
QCheckBox::indicator:hover {{ border-color:{accent}; background:{card_alt}; }}
/* Appui sur une case NON cochée : fond neutre, pas teinté — un fond vert
   franc (accent_soft) donnait l'illusion d'un état déjà validé. Seule la coche
   en transparence annonce ce que le relâchement va faire. */
QCheckBox::indicator:pressed {{ border-color:{accent}; background:{card_alt};
    image:url("{check_ghost}"); }}
QCheckBox::indicator:checked {{ border-color:{accent}; background:{ok_bg};
    image:url("{check_icon}"); }}
/* Survol d'une case cochée : un cran de teinte au-dessus, jamais l'accent plein
   — sur une colonne de cases, un aplat vif attirerait l'œil à tort. */
QCheckBox::indicator:checked:hover {{ border-color:{accent_hover};
    background:{ok_border}; }}
QCheckBox::indicator:checked:pressed {{ background:{ok_bg};
    image:url("{check_ghost}"); }}
QCheckBox::indicator:disabled {{ border-color:{border}; background:{muted_bg}; }}
QCheckBox::indicator:checked:disabled {{ border-color:{border_strong};
    background:{muted_bg}; image:url("{check_ghost}"); }}
""".format(**c)


def build_stylesheet(c: Dict[str, str]) -> str:
    """Feuille de style Qt du panneau, construite depuis la palette.

    Volontairement limitée aux propriétés réellement supportées par Qt :
    couleurs plates, bordures, rayons, marges internes. Aucune ombre ici — les
    reliefs éventuels passent par QGraphicsDropShadowEffect côté widgets.
    """
    # Les règles des cases à cocher sont déjà mises en forme : elles sont donc
    # injectées comme une valeur, sans repasser par le formatage du gabarit.
    c = dict(c, checkbox_rules=_checkbox_rules(c))
    return """
#sgContent {{ background:{panel}; color:{text}; }}
#sgContent QLabel {{ background:transparent; color:{text}; }}
#sgHeaderTitle {{ font-size:15px; font-weight:600; color:{text}; }}
#sgHeaderSub {{ font-size:11px; color:{text_muted}; }}
/* Numéro de version : petit, en retrait, jamais en concurrence avec le titre.
   Fond léger + coins arrondis pour lire un « badge », pas une simple étiquette
   posée à côté du texte. */
#sgVersion {{ font-size:10px; color:{text_muted}; background:{card_alt};
    border-radius:6px; padding:1px 6px; }}
#sgLogo {{ background:{accent}; color:{accent_text}; border-radius:9px;
    font-size:17px; font-weight:600; }}
#sgSectionLabel {{ font-size:11px; font-weight:600; color:{text_muted};
    text-transform:uppercase; letter-spacing:.04em; }}
#sgHint {{ font-size:11px; color:{text_muted}; }}

/* Encart des exceptions d'un contrôle : un filet vertical à l'accent, décalé,
   dit visuellement « ces cases dépendent de celle du dessus ». Remplace les
   flèches « ↳ » insérées dans les libellés, qui se lisaient mal une fois
   plusieurs niveaux empilés. */
#sgSubOptions {{ border:0; border-left:2px solid {border_strong};
    background:transparent; margin-left:14px; }}

QGroupBox {{ border:1px solid {border}; border-radius:11px; margin-top:14px;
    background:{card}; font-weight:600; color:{text}; padding-top:6px; }}
QGroupBox::title {{ subcontrol-origin:margin; left:12px; padding:0 6px;
    color:{text_muted}; font-size:11px; text-transform:uppercase;
    letter-spacing:.04em; }}

#sgCard {{ background:{card}; border:1px solid {border}; border-radius:11px; }}
#sgCardAlt {{ background:{card_alt}; border:1px solid {border};
    border-radius:11px; }}

/* Bloc « Actions » : c'est la zone la plus utilisée une fois l'analyse faite
   (sélection, couche d'erreurs, réparation, export) — un cadre accent et un
   titre en badge la distinguent des blocs de configuration (Source, Score),
   qui partagent tous le même style neutre par défaut. */
QGroupBox#sgActionsCard {{ border:1.5px solid {accent}; border-radius:12px;
    margin-top:16px; background:{card}; padding-top:10px; }}
QGroupBox#sgActionsCard::title {{ subcontrol-origin:margin; left:10px;
    padding:3px 10px; background:{accent}; color:{accent_text};
    border-radius:7px; font-size:11px; font-weight:700;
    text-transform:uppercase; letter-spacing:.04em; }}
QPushButton#sgActionBtn, QToolButton#sgActionBtn {{ padding:9px 14px;
    font-size:12px; }}

QPushButton, QToolButton {{ border:1px solid {border_strong}; border-radius:8px;
    padding:7px 12px; background:{card_alt}; color:{text}; }}
QPushButton:hover, QToolButton:hover {{ background:{card}; border-color:{accent}; }}
QPushButton:pressed, QToolButton:pressed {{ background:{field}; }}
QPushButton:disabled, QToolButton:disabled {{ color:{text_muted};
    border-color:{border}; background:{muted_bg}; }}
QToolButton::menu-indicator {{ image:none; }}

/* Menu déroulant (ex. « Exporter ▾ ») : sans cette règle, la liste s'ouvre
   dans le style natif du système, en rupture avec le thème sombre ou clair. */
QMenu {{ background:{card}; color:{text}; border:1px solid {border_strong};
    border-radius:8px; padding:4px; }}
QMenu::item {{ padding:6px 20px 6px 12px; border-radius:6px; }}
QMenu::item:selected {{ background:{accent}; color:{accent_text}; }}
QMenu::separator {{ height:1px; background:{border}; margin:4px 8px; }}
QPushButton#primary {{ background:{accent}; color:{accent_text}; border:none;
    font-weight:600; padding:10px 14px; font-size:13px; }}
QPushButton#primary:hover {{ background:{accent_hover}; }}
QPushButton#primary:disabled {{ background:{muted_bg}; color:{text_muted}; }}
/* « Réparer » écrit un NOUVEAU fichier — une action utile, pas destructrice :
   un accent (pas le rouge d'alerte) suffit à la distinguer des exports. */
QPushButton#sgRepair {{ color:{accent}; border-color:{accent};
    background:{card_alt}; font-weight:600; padding:9px 14px; font-size:12px; }}
QPushButton#sgRepair:hover {{ background:{accent_soft}; }}

/* Préréglages d'analyse (Rapide / Standard / Approfondi). */
QPushButton#sgChip {{ border:1px solid {border_strong}; border-radius:8px;
    padding:6px 4px; background:{card_alt}; color:{text_soft}; font-size:11px; }}
QPushButton#sgChip:hover {{ border-color:{accent}; color:{text}; }}
QPushButton#sgChipActive {{ border:1px solid {accent}; border-radius:8px;
    padding:6px 4px; background:{accent}; color:{accent_text}; font-size:11px;
    font-weight:600; }}

/* Lien discret (ex. « Réinitialiser les options »). */
QPushButton#sgLinkButton {{ background:transparent; border:none;
    color:{text_muted}; font-size:11px; padding:2px 4px; }}
QPushButton#sgLinkButton:hover {{ color:{accent}; text-decoration:underline; }}

QComboBox, QDoubleSpinBox {{ border:1px solid {border_strong}; border-radius:8px;
    padding:5px 8px; background:{field}; color:{text}; selection-background-color:{accent}; }}
QComboBox:hover, QDoubleSpinBox:hover {{ border-color:{accent}; }}
QComboBox:disabled, QDoubleSpinBox:disabled {{ color:{text_muted};
    background:{muted_bg}; border-color:{border}; }}
QComboBox QAbstractItemView {{ background:{card}; color:{text};
    border:1px solid {border_strong}; selection-background-color:{accent};
    selection-color:{accent_text}; }}

/* Cases à cocher : un carré VIDE, et une coche verte quand c'est coché — plutôt
   qu'un carré rempli, qui ne disait pas « validé » et se confondait avec un
   simple aplat de couleur. Règles produites par _checkbox_rules (elles ont un
   repli si les pictogrammes manquent), d'où l'insertion ici. */
{checkbox_rules}

/* Section repliable : la barre porte le cadre, le bouton y est transparent. */
#sgFoldRow {{ border:1px solid {border}; border-radius:10px;
    background:{card_alt}; }}
#sgFoldRowOpen {{ border:1px solid {accent}; border-bottom:none;
    border-top-left-radius:10px; border-top-right-radius:10px;
    border-bottom-left-radius:0; border-bottom-right-radius:0;
    background:{card}; }}
#sgFold {{ background:transparent; border:none; color:{text};
    text-align:left; padding:9px 11px; font-weight:600; font-size:12px; }}
#sgFold:hover {{ color:{accent}; }}
#sgFoldBody {{ background:{card}; border:1px solid {accent}; border-top:none;
    border-bottom-left-radius:10px; border-bottom-right-radius:10px; }}
#sgFoldBadge {{ color:{text_muted}; font-size:11px; font-weight:500;
    background:transparent; }}
#sgFoldBadgeActive {{ color:{accent}; font-size:11px; font-weight:600;
    background:transparent; }}

QTabWidget::pane {{ border:1px solid {border}; border-radius:11px;
    background:{card}; top:-1px; }}
/* Panneau étroit : marges internes serrées pour que les quatre onglets
   tiennent sans être tronqués. */
QTabBar::tab {{ padding:6px 8px; margin-right:2px; border-top-left-radius:7px;
    border-top-right-radius:7px; background:{tab_bg}; color:{tab_text};
    font-size:11px; min-width:60px; }}
QTabBar::tab:selected {{ background:{card}; color:{text}; font-weight:600; }}
QTabBar::tab:hover {{ color:{text}; }}

QTableWidget {{ border:none; background:{card}; gridline-color:{border};
    color:{text}; }}
QTableWidget::item:selected {{ background:{accent}; color:{accent_text}; }}
QHeaderView::section {{ background:{card_alt}; color:{text_muted}; padding:6px;
    border:none; border-bottom:1px solid {border}; font-weight:600;
    font-size:11px; }}
QTextBrowser {{ border:none; background:{card}; color:{text}; }}

QProgressBar {{ border:1px solid {border}; border-radius:8px;
    background:{muted_bg}; text-align:center; color:{text}; height:18px;
    font-size:11px; }}
QProgressBar::chunk {{ border-radius:7px; background:{accent}; }}

QScrollArea {{ border:none; background:{panel}; }}
QScrollBar:vertical {{ background:{panel}; width:11px; margin:0; }}
QScrollBar::handle:vertical {{ background:{border_strong}; border-radius:5px;
    min-height:26px; }}
QScrollBar::handle:vertical:hover {{ background:{accent}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:none; }}
QToolTip {{ background:{card}; color:{text}; border:1px solid {border_strong};
    padding:5px; }}
""".format(**c)
