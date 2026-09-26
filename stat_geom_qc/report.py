# -*- coding: utf-8 -*-
"""Génération des rapports STAT GEOM QC. Aucune dépendance externe.

Trois formes, deux rendus HTML distincts — et c'est volontaire :

  * ``build_html`` vise un NAVIGATEUR : anneau de score en SVG, ``display:flex``,
    ``display:grid``. Sert au « Rapport navigateur » et à l'export HTML.
  * ``build_qt_summary`` vise le moteur de texte de QT, qui ne comprend qu'un
    sous-ensemble HTML4/CSS2. Sert à l'onglet Rapport du panneau ET au PDF.

Ne pas rendre le PDF depuis ``build_html`` : mesuré, la jauge SVG disparaît et
les cartes d'indicateurs s'effondrent en « valeur puis libellé » à l'envers.
"""

import html
import math
from typing import List

from . import constants as C
from .analysis_engine import AnalysisResult


# ═══════════════════════════════════════════════════════════════════════════════
# HTML
# ═══════════════════════════════════════════════════════════════════════════════


def _score_ring_svg(score: float, color: str, size: int = 160) -> str:
    """Anneau de progression SVG représentant le score de correctness."""
    r = size / 2 - 14
    cx = cy = size / 2
    circ = 2 * math.pi * r
    frac = max(0.0, min(1.0, score / 100.0))
    dash = circ * frac
    gap = circ - dash
    return f"""
    <svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" role="img" aria-label="Score {score}">
      <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#e5e7eb" stroke-width="14"/>
      <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" stroke-width="14"
              stroke-linecap="round" stroke-dasharray="{dash:.2f} {gap:.2f}"
              transform="rotate(-90 {cx} {cy})"/>
      <text x="50%" y="47%" text-anchor="middle" font-size="34" font-weight="700"
            fill="{color}" font-family="Segoe UI, sans-serif">{score:g}</text>
      <text x="50%" y="63%" text-anchor="middle" font-size="13" fill="#6b7280"
            font-family="Segoe UI, sans-serif">/ 100</text>
    </svg>
    """


def _badge(value, danger: bool, warn: bool = False) -> str:
    cls = "ok"
    if danger and value:
        cls = "err"
    elif warn and value:
        cls = "warn"
    return '<span class="badge %s">%s</span>' % (cls, html.escape(str(value)))


# Libellés des sous-scores : source unique dans ``constants`` (alias conservé
# pour compatibilité avec les imports existants).
_COMPONENT_LABELS = C.COMPONENT_LABELS


def _missing_vertex_suffix(result: AnalysisResult) -> str:
    """Précision accolée au compteur « sommet manquant » : sans elle, un 0
    serait ambigu (aucun cas trouvé, ou option décochée ?)."""
    ratio = getattr(result, "missing_vertex_ratio", 0.0) or 0.0
    return "" if ratio > 0 else " (option inactive)"


def nested_fields_suffix(result: AnalysisResult) -> str:
    """Champs d'altitude réellement comparés, accolés au libellé du contrôle.

    Le contrôle porte sur ceux que la couche possède : sans cette précision, un
    résultat obtenu sur le seul champ AGL se lirait comme un verdict sur AGL
    ET HEIGHT.
    """
    fields = getattr(result, "nested_height_fields", None) or []
    return " (%s)" % ", ".join(fields) if fields else ""


# Texte affiché à la place d'un compteur quand le contrôle correspondant était
# décoché : un 0 s'y serait substitué silencieusement, ce qui se lit comme
# « rien trouvé » alors que le contrôle n'a simplement jamais tourné.
NOT_REQUESTED = "Non demandé"

# Contrôle qui a TOURNÉ sans pouvoir conclure : surface non mesurable, aucune
# paire comparable. À ne pas confondre avec le précédent — l'un n'a pas été
# voulu, l'autre a échoué à répondre — ni avec un résultat conforme : ici on ne
# sait pas, et un 0 se lirait comme une bonne nouvelle.
NOT_CONCLUSIVE = "Non concluant"


def _inconclusive(result: AnalysisResult, key: str) -> bool:
    """Le contrôle ``key`` a-t-il tourné SANS pouvoir conclure ?

    Absence de l'information (résultat produit par une version antérieure) :
    considéré comme concluant, pour ne pas frapper de doute une valeur réelle.
    """
    doute = getattr(result, "checks_inconclusive", None) or {}
    return bool(doute.get(key))


def coverage_text(result: AnalysisResult) -> str:
    """Phrase de couverture accolée au score.

    Le score seul ne dit pas sur quoi il porte : 100 sur quatre dimensions
    mesurées et 100 sur onze ne valent pas la même chose. La couverture rend
    cette assise visible au lieu de la laisser deviner.
    """
    c = getattr(result, "correctness", None)
    if c is None or not getattr(c, "coverage_total", 0):
        return ""
    texte = ("Couverture : %d contrôle(s) sur %d abouti(s) (%g pour cent)."
             % (c.coverage_ran, c.coverage_total, c.coverage_pct()))
    doute = sorted(getattr(c, "inconclusive", {}) or {})
    if doute:
        noms = ", ".join(C.CATEGORY_LABELS.get(k, k) for k in doute)
        texte += " Sans conclusion : %s." % noms
    return texte


def _profil_et_zone(result: AnalysisResult):
    """``[(libellé, valeur)]`` pour le profil et le périmètre analysé — les deux
    premières informations attendues d'un rapport, avant même les contrôles.

    Périmètre AVANT les contrôles : c'est la première chose à savoir en
    relisant un rapport. « 0 chevauchement » ne veut pas dire la même chose sur
    une couche entière et sur un quartier de 800 bâtiments.

    La ligne « Profil » n'apparaît QUE si un nom a été donné. Les préréglages du
    panneau ont disparu en 2.9.49 : il n'y a plus qu'un mode, sans nom, et une
    ligne « Réglages manuels » systématique n'opposerait plus rien à rien. Un
    script qui nomme son profil, lui, le voit toujours consigné. Ce qui a
    vraiment tourné se lit de toute façon contrôle par contrôle, plus bas.
    """
    lignes = []
    profil = getattr(result, "profile_name", "") or ""
    if profil:
        lignes.append(("Profil", profil))
    if getattr(result, "aoi_wkt", ""):
        lignes.append((
            "Zone d'analyse",
            "Restreinte — %s entité(s) sur %s"
            % (C.fmt_int(getattr(result, "aoi_features", 0)),
               C.fmt_int(getattr(result, "aoi_layer_features", 0)))))
    else:
        lignes.append(("Zone d'analyse", "Couche entière"))
    return lignes


def configuration_rows(result: AnalysisResult):
    """``[(libellé, valeur)]`` décrivant le profil, les contrôles et les seuils.

    Un rapport qui ne consigne pas ses réglages n'est pas relisible : « 0 petit
    polygone » ne veut rien dire si l'on ne sait plus si le seuil valait 2 ou
    25 m², et « 0 chevauchement » ne veut rien dire si l'on ne sait plus que les
    imbrications étaient écartées.
    """
    lignes = list(_profil_et_zone(result))
    checks = getattr(result, "checks_enabled", {}) or {}
    doute = getattr(result, "checks_inconclusive", {}) or {}
    for cle in sorted(checks):
        if doute.get(cle):
            etat = NOT_CONCLUSIVE
        else:
            etat = "Exécuté" if checks[cle] else NOT_REQUESTED
        lignes.append((C.CATEGORY_LABELS.get(cle, cle), etat))
    snap = getattr(result, "options_snapshot", {}) or {}
    if snap:
        lignes.append(("Seuil petits polygones",
                       "%g m²" % snap.get("small_polygon_threshold_m2", 0)))
        lignes.append(("Seuil trou « fautif »",
                       "%g m²" % snap.get("hole_max_area_m2", 0)))
        lignes.append(("Angle minimal toléré",
                       "%g °" % snap.get("sharp_angle_min_deg", 0)))
        lignes.append(("Distance minimale entre sommets",
                       "%g m" % snap.get("close_vertex_min_dist_m", 0)))
        lignes.append(("Chevauchements par sommet manquant",
                       "Ignorés" if snap.get("ignore_missing_vertex_overlaps")
                       else "Signalés"))
        lignes.append(("Polygones entièrement inclus",
                       "Ignorés" if snap.get("allow_contained_polygons")
                       else "Signalés"))
    return lignes


def skipped_note(result: AnalysisResult) -> str:
    """Mention accolée au score quand un contrôle ATTENDU n'a pas été exécuté.

    Sans elle, une note « 100/100 Excellent » calculée sur les seules dimensions
    mesurées se lit comme un verdict sur l'ensemble de la donnée.

    Ne compte QUE les contrôles actifs par défaut (``C.CHECKS_ON_BY_DEFAULT``) :
    les contrôles opt-in sont décochés dans le cas courant, les signaler à
    chaque analyse produirait une mention permanente que plus personne ne lit.
    Le décompte couvre aussi bien un contrôle décoché qu'un contrôle demandé
    mais abandonné en cours (limite de volume, index spatial indisponible),
    d'où le libellé « non exécuté » plutôt que « non demandé ».
    """
    checks = getattr(result, "checks_enabled", {})
    manquants = sum(1 for key in C.CHECKS_ON_BY_DEFAULT
                    if not checks.get(key, True))
    if not manquants:
        return ""
    return (" 1 contrôle non exécuté." if manquants == 1
            else " %d contrôles non exécutés." % manquants)


def _checked(result: AnalysisResult, key: str) -> bool:
    """Vrai si le contrôle ``key`` (voir AnalysisResult.checks_enabled) a
    réellement été exécuté pendant cette analyse.

    Absence de la clé (résultat produit par une version antérieure, par
    exemple relu depuis un JSON) : considéré comme exécuté, pour ne jamais
    masquer à tort une valeur réelle.
    """
    return getattr(result, "checks_enabled", {}).get(key, True)


def _count_or_skip(result: AnalysisResult, key: str, value) -> str:
    """Valeur affichée pour un compteur dépendant d'un contrôle facultatif.

    Trois issues et non deux : non demandé, non concluant, ou le chiffre. Les
    confondre revenait à présenter une ignorance comme un résultat.
    """
    if not _checked(result, key):
        return NOT_REQUESTED
    if _inconclusive(result, key):
        return NOT_CONCLUSIVE
    return C.fmt_int(value)


def _badge_or_skip(result: AnalysisResult, key: str, value,
                   danger: bool, warn: bool = False) -> str:
    """Comme ``_badge``, mais neutre (ni erreur ni avertissement) si le
    contrôle ``key`` était décoché : un 0 non calculé n'est pas une bonne
    nouvelle à célébrer en vert."""
    if not _checked(result, key):
        return '<span class="badge muted">%s</span>' % NOT_REQUESTED
    if _inconclusive(result, key):
        return '<span class="badge muted">%s</span>' % NOT_CONCLUSIVE
    return _badge(value, danger, warn)


def _metric_or_skip(result: AnalysisResult, key: str, value, label: str,
                    css_class: str = "warn") -> str:
    """Carte de la section « Indicateurs » : neutre et étiquetée si le contrôle
    ``key`` n'a pas tourné, sinon coloré selon ``css_class`` quand ``value``
    est non nul."""
    if not _checked(result, key):
        return ('<div class="metric muted"><div class="v">%s</div>'
                '<div class="l">%s</div></div>' % (NOT_REQUESTED, html.escape(label)))
    if _inconclusive(result, key):
        return ('<div class="metric muted"><div class="v">%s</div>'
                '<div class="l">%s</div></div>' % (NOT_CONCLUSIVE, html.escape(label)))
    cls = css_class if value else "ok"
    return ('<div class="metric %s"><div class="v">%s</div><div class="l">%s</div></div>'
            % (cls, C.fmt_int(value), html.escape(label)))


def quality_groups(result: AnalysisResult):
    """Réglages et résultats groupés catégorie par catégorie.

    Auparavant, un seuil vivait dans le tableau « Configuration de l'analyse »
    et le résultat qu'il conditionnait dans le tableau « Détail qualité »
    séparé : relier « seuil = 2 m² » à « 8 polygones ≤ 2 m² » demandait un
    aller-retour. Ici chaque catégorie porte son propre petit groupe — état du
    contrôle, seuil éventuel, puis résultat(s) mesuré(s) — l'un sous l'autre.

    Renvoie ``[(titre_groupe, [ligne, ...])]``. Chaque ligne est un tuple dont
    le premier élément dit comment la rendre :
      * ``("text", libellé, valeur)`` — affichée telle quelle, sans gating ;
      * ``("plain_badge", libellé, valeur, danger, warn)`` — badge/couleur non
        gaté (compteur toujours calculé, pas de contrôle à décocher) ;
      * ``("badge", libellé, clé, valeur, danger, warn)`` — badge/couleur gaté
        par ``checks_enabled[clé]`` / ``checks_inconclusive[clé]`` ;
      * ``("count", libellé, clé, valeur)`` — nombre gaté, sans couleur.
    """
    q = result.quality_report
    checks = getattr(result, "checks_enabled", {}) or {}
    doute = getattr(result, "checks_inconclusive", {}) or {}
    snap = getattr(result, "options_snapshot", {}) or {}
    _mv_txt = _missing_vertex_suffix(result)

    def etat(cle):
        if doute.get(cle):
            return NOT_CONCLUSIVE
        return "Exécuté" if checks.get(cle, True) else NOT_REQUESTED

    groupes = [("Réglages généraux",
               [("text", lib, val) for lib, val in _profil_et_zone(result)])]

    groupes.append(("Validité géométrique", [
        ("plain_badge", "Géométries valides", q.valid_count, False, False),
        ("plain_badge", "Géométries invalides", q.invalid_count, True, False),
        ("plain_badge", "Géométries nulles/vides", q.null_empty_count, True, False),
    ]))

    groupes.append(("Auto-intersections", [
        ("text", "Contrôle", etat("self_intersection")),
        ("badge", "Auto-intersections", "self_intersection",
         q.self_intersection_count, True, False),
    ]))

    chevauchements = [("text", "Contrôle", etat("overlap"))]
    if snap:
        chevauchements.append(("text", "Chevauchements par sommet manquant",
                               "Ignorés" if snap.get("ignore_missing_vertex_overlaps")
                               else "Signalés"))
        chevauchements.append(("text", "Polygones entièrement inclus",
                               "Ignorés" if snap.get("allow_contained_polygons")
                               else "Signalés"))
    chevauchements += [
        ("badge", "Chevauchements (entités)", "overlap", q.overlap_count, True, False),
        ("badge", "Paires en intersection", "overlap", q.overlap_pairs, True, False),
        ("badge", "Paires imbriquées (inclusion totale)", "overlap",
         q.contained_pairs, False, True),
        ("count", "Paires écartées — sommet manquant%s" % _mv_txt, "overlap",
         q.overlap_missing_vertex_pairs),
    ]
    groupes.append(("Chevauchements", chevauchements))

    groupes.append(("Doublons", [
        ("text", "Contrôle", etat("duplicate")),
        ("badge", "Doublons", "duplicate", q.duplicate_count, False, True),
    ]))

    petits = [("text", "Contrôle", etat("small"))]
    if snap:
        petits.append(("text", "Seuil",
                       "%g m²" % snap.get("small_polygon_threshold_m2", 0)))
    petits.append(("badge", "Polygones ≤ %g m²" % result.threshold_m2, "small",
                   q.small_area_count, False, True))
    groupes.append(("Petits polygones", petits))

    groupes.append(("Sommets dupliqués", [
        ("text", "Contrôle", etat("duplicate_vertex")),
        ("badge", "Entités à vertex dupliqués", "duplicate_vertex",
         q.duplicate_vertex_count, False, True),
        ("badge", "Sommets en doublon (total)", "duplicate_vertex",
         q.duplicate_vertex_total, False, True),
    ]))

    trous = [("text", "Contrôle", etat("hole"))]
    if snap:
        trous.append(("text", "Seuil trou « fautif »",
                      "%g m²" % snap.get("hole_max_area_m2", 0)))
    trous += [
        ("badge", "Entités avec trous ≤ %g m²" % result.hole_threshold_m2, "hole",
         q.hole_count, False, True),
        ("badge", "Trous détectés (total)", "hole", q.hole_total, False, True),
    ]
    groupes.append(("Trous", trous))

    groupes.append(("Multi-parties", [
        ("text", "Contrôle", etat("multipart")),
        ("badge", "Entités multi-parties", "multipart",
         q.multipart_count, False, True),
    ]))

    angles = [("text", "Contrôle", etat("sharp_angle"))]
    if snap:
        angles.append(("text", "Angle minimal toléré",
                       "%g °" % snap.get("sharp_angle_min_deg", 0)))
    angles += [
        ("badge", "Entités à angles aigus (< %g°)" % result.sharp_angle_min_deg,
         "sharp_angle", q.sharp_angle_count, False, True),
        ("badge", "Sommets en pointe (total)", "sharp_angle",
         q.sharp_angle_total, False, True),
    ]
    groupes.append(("Angles aigus", angles))

    proches = [("text", "Contrôle", etat("close_vertex"))]
    if snap:
        proches.append(("text", "Distance minimale entre sommets",
                        "%g m" % snap.get("close_vertex_min_dist_m", 0)))
    proches += [
        ("badge", "Entités à sommets rapprochés (≤ %g m)"
         % result.close_vertex_min_dist_m, "close_vertex",
         q.close_vertex_count, False, True),
        ("badge", "Paires de sommets rapprochés (total)", "close_vertex",
         q.close_vertex_total, False, True),
    ]
    groupes.append(("Sommets rapprochés", proches))

    groupes.append(("Hauteur des polygones inclus", [
        ("text", "Contrôle", etat("nested_height")),
        ("badge", "Polygones inclus pas assez hauts%s"
         % nested_fields_suffix(result), "nested_height",
         q.nested_height_count, True, False),
        ("count", "Paires imbriquées comparées", "nested_height",
         q.nested_pairs_checked),
        ("badge", "Paires imbriquées non vérifiables", "nested_height",
         q.nested_pairs_unknown, False, True),
    ]))

    groupes.append(("Croisements non nodés", [
        ("text", "Contrôle", etat("unnoded_crossing")),
        ("badge", "Lignes en croisement non nodé", "unnoded_crossing",
         q.unnoded_crossing_count, True, False),
        ("badge", "Paires de lignes concernées", "unnoded_crossing",
         q.unnoded_crossing_pairs, True, False),
    ]))

    return groupes


def invalid_details_state(result: AnalysisResult):
    """``(état, lignes)`` pour la section « Détails d'invalidité ».

    Trois états et non deux, exactement comme les compteurs (voir
    ``_count_or_skip``) : le DÉTAIL n'est produit que sous « Contrôles
    topologiques », alors que le COMPTE d'invalides l'est toujours. Une liste
    vide ne veut donc rien dire quand le contrôle était décoché, et l'annoncer
    comme « aucun détail » célébrait une absence jamais mesurée.

    ``état`` vaut ``"details"`` (``lignes`` non vide), ``"not_requested"`` ou
    ``"none"``.
    """
    details = list(getattr(result.quality_report, "invalid_details", None) or [])
    if details:
        return "details", details
    if not _checked(result, "self_intersection"):
        return "not_requested", []
    return "none", []


def _quality_table_html(result: AnalysisResult) -> str:
    """Rend ``quality_groups`` en lignes ``<tr>`` pour ``build_html``, un
    en-tête de groupe par catégorie. Remplace les anciens tableaux séparés
    « Configuration de l'analyse » et « Détail qualité »."""
    esc = html.escape
    out = []
    for titre, lignes in quality_groups(result):
        out.append('<tr class="grp"><th colspan="2">%s</th></tr>' % esc(titre))
        for spec in lignes:
            kind = spec[0]
            if kind == "text":
                _, label, value = spec
                out.append("<tr><th>%s</th><td>%s</td></tr>"
                           % (esc(label), esc(str(value))))
            elif kind == "plain_badge":
                _, label, value, danger, warn = spec
                out.append("<tr><th>%s</th><td>%s</td></tr>"
                           % (esc(label), _badge(value, danger, warn)))
            elif kind == "badge":
                _, label, key, value, danger, warn = spec
                out.append("<tr><th>%s</th><td>%s</td></tr>"
                           % (esc(label), _badge_or_skip(result, key, value, danger, warn)))
            else:  # "count"
                _, label, key, value = spec
                out.append("<tr><th>%s</th><td>%s</td></tr>"
                           % (esc(label), esc(_count_or_skip(result, key, value))))
    return "".join(out)


# Rendu Qt (QTextDocument), pas de feuille de style externe : sans largeur ni
# taille de police explicites sur CHAQUE cellule, un tableau de deux colonnes
# se réduit à son contenu (Qt ne connaît que HTML4/CSS2, pas de flexbox/grid
# pour étirer un conteneur). Résultat mesuré sans ce correctif : un document
# n'occupant que 17 % de la largeur imprimable d'une page A4, tout le texte
# plaqué contre la marge gauche — exactement le défaut signalé. Les deux
# proportions ci-dessous (38 % / 62 %) donnent au libellé assez de place sans
# jamais écraser la valeur.
_QT_COL_LABEL = "width:38%;font-size:11pt;padding:7px 10px;color:#64748b"
_QT_COL_VALUE = "width:62%;font-size:11pt;padding:7px 10px"
# Attribut ET style : l'attribut HTML4 ``width`` est celui que les vieux
# moteurs de rendu (dont Qt) lisent le plus fiablement pour dimensionner un
# tableau ; le style le double pour les versions de Qt qui préfèrent le CSS.
_QT_TABLE = 'width="100%" style="width:100%;border-collapse:collapse"'


def _qt_row(label, value, color=None) -> str:
    esc = html.escape
    style = ";color:%s;font-weight:bold" % color if color else ""
    return ("<tr><td style='%s'>%s</td>"
            "<td style='%s%s'>%s</td></tr>"
            % (_QT_COL_LABEL, esc(label), _QT_COL_VALUE, style, esc(str(value))))


def _qt_row_or_skip(result: AnalysisResult, label, key, value, color=None) -> str:
    """Comme ``_qt_row``, mais affiche ``NOT_REQUESTED``/``NOT_CONCLUSIVE`` en
    italique neutre si le contrôle ``key`` n'a pas produit de valeur exploitable."""
    if not _checked(result, key) or _inconclusive(result, key):
        mention = (NOT_CONCLUSIVE if _checked(result, key) else NOT_REQUESTED)
        return ("<tr><td style='%s'>%s</td>"
                "<td style='%s;color:#94a3b8;font-style:italic'>%s</td></tr>"
                % (_QT_COL_LABEL, html.escape(label), _QT_COL_VALUE, mention))
    return _qt_row(label, value, color)


def _quality_table_qt(result: AnalysisResult) -> str:
    """Même contenu que ``_quality_table_html``, rendu pour le moteur de texte
    de Qt (couleur inline plutôt que badges, comme le reste de
    ``build_qt_summary``)."""
    danger = "#dc2626"
    warn = "#d97706"
    out = []
    for titre, lignes in quality_groups(result):
        out.append("<tr><td colspan='2' style='padding:14px 10px 4px 10px;"
                   "font-size:12pt;color:#0f172a;font-weight:700'>%s</td></tr>"
                   % html.escape(titre))
        for spec in lignes:
            kind = spec[0]
            if kind == "text":
                _, label, value = spec
                out.append(_qt_row(label, value))
            elif kind == "plain_badge":
                _, label, value, dgr, wrn = spec
                col = danger if (dgr and value) else warn if (wrn and value) else None
                out.append(_qt_row(label, value, col))
            elif kind == "badge":
                _, label, key, value, dgr, wrn = spec
                col = danger if (dgr and value) else warn if (wrn and value) else None
                out.append(_qt_row_or_skip(result, label, key, value, col))
            else:  # "count"
                _, label, key, value = spec
                out.append(_qt_row_or_skip(result, label, key, value))
    return "".join(out)


def build_html(result: AnalysisResult) -> str:
    """Construit un rapport HTML complet et autonome."""
    c = result.correctness
    q = result.quality_report
    esc = html.escape

    geom_types = ", ".join("%s : %s" % (k, v) for k, v in result.geometry_types.items()) or "—"
    cols = result.attribute_info.get("columns", [])
    cols_preview = ", ".join(cols[:12]) + ("…" if len(cols) > 12 else "")

    # Barres de sous-scores
    comp_rows = []
    for key, label in _COMPONENT_LABELS.items():
        if key not in c.components:
            continue
        val = c.components[key]
        bar_color = "#22c55e" if val >= 95 else "#eab308" if val >= 80 else "#f97316" if val >= 60 else "#ef4444"
        comp_rows.append(f"""
          <div class="comp">
            <div class="comp-head"><span>{esc(label)}</span><span class="comp-val">{val:g}%</span></div>
            <div class="bar"><div class="bar-fill" style="width:{val:g}%;background:{bar_color}"></div></div>
          </div>""")
    comp_html = "\n".join(comp_rows)

    # Les avertissements sont placés JUSTE APRÈS les indicateurs, et non en fin
    # de page : ils disent en toutes lettres ce que les compteurs se contentent
    # de chiffrer (« 3 polygones inclus pas assez hauts », « CRS géographique :
    # seuils approximatifs »). Reléguée sous les tableaux, cette lecture arrivait
    # après coup, une fois le rapport déjà parcouru.
    warnings_html = ""
    if result.warnings:
        items = "".join("<li>%s</li>" % esc(w) for w in result.warnings)
        warnings_html = f'<div class="card"><h2>⚠️ Avertissements</h2><ul class="msg warn-list">{items}</ul></div>'

    errors_html = ""
    if result.errors:
        items = "".join("<li>%s</li>" % esc(e) for e in result.errors)
        errors_html = f'<div class="card"><h2>⛔ Erreurs</h2><ul class="msg err-list">{items}</ul></div>'

    # Détails d'invalidité : la mention « non demandé » compte autant que la
    # liste elle-même (voir invalid_details_state).
    etat_invalid, details = invalid_details_state(result)
    if etat_invalid == "details":
        corps = '<ul class="msg">%s</ul>' % "".join(
            "<li>%s</li>" % esc(d) for d in details)
    elif etat_invalid == "not_requested":
        corps = ('<p class="note">%s — les détails d\'invalidité ne sont '
                 'produits que si « Contrôles topologiques » est coché.</p>'
                 % NOT_REQUESTED)
    else:
        corps = '<p class="msg">Aucun détail d\'invalidité.</p>'
    invalid_html = ('<div class="card"><h2>🔎 Détails d\'invalidité</h2>%s</div>'
                    % corps)

    # Configuration ET détail qualité, groupés catégorie par catégorie : sans
    # eux, un rapport n'est pas relisible plus tard, et un seuil séparé de son
    # résultat obligeait à l'aller-retour entre deux tableaux.
    quality_html = _quality_table_html(result)

    # Note discrète du contrôle interne AGL/AMSL (vide = rien à signaler).
    agl_note_html = ""
    if result.agl_note:
        agl_note_html = (
            '<p class="note">%s</p>' % esc(result.agl_note)
        )

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>STAT GEOM QC — Rapport : {esc(result.layer_name)}</title>
<style>
  :root {{
    --bg:#f1f5f9; --card:#ffffff; --ink:#0f172a; --muted:#64748b; --line:#e2e8f0;
    --accent:#0ea5e9;
  }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif; background:var(--bg);
         color:var(--ink); margin:0; padding:28px; line-height:1.5; }}
  .wrap {{ max-width:960px; margin:0 auto; }}
  header {{ display:flex; align-items:center; gap:14px; margin-bottom:22px; }}
  header .logo {{ width:44px; height:44px; border-radius:12px;
    background:linear-gradient(135deg,#0ea5e9,#6366f1); display:flex; align-items:center;
    justify-content:center; color:#fff; font-size:22px; font-weight:700; }}
  header h1 {{ font-size:20px; margin:0; }}
  header .sub {{ color:var(--muted); font-size:13px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:16px;
    padding:22px; margin-bottom:18px; box-shadow:0 1px 2px rgba(15,23,42,.04); }}
  h2 {{ font-size:15px; margin:0 0 14px; letter-spacing:.02em; text-transform:uppercase;
    color:var(--muted); }}
  .hero {{ display:flex; gap:26px; align-items:center; flex-wrap:wrap; }}
  .hero .ring {{ flex:0 0 auto; }}
  .hero .grade {{ flex:1 1 240px; }}
  .grade .g-label {{ font-size:26px; font-weight:700; color:{c.color}; }}
  .grade .g-desc {{ color:var(--muted); font-size:14px; margin-top:4px; }}
  .comps {{ flex:1 1 340px; min-width:280px; }}
  .comp {{ margin-bottom:11px; }}
  .comp-head {{ display:flex; justify-content:space-between; font-size:13px; margin-bottom:4px; }}
  .comp-val {{ color:var(--muted); font-variant-numeric:tabular-nums; }}
  .bar {{ height:8px; background:#eef2f7; border-radius:99px; overflow:hidden; }}
  .bar-fill {{ height:100%; border-radius:99px; }}
  .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:12px; }}
  .metric {{ border:1px solid var(--line); border-radius:12px; padding:14px; }}
  .metric .v {{ font-size:22px; font-weight:700; font-variant-numeric:tabular-nums; }}
  .metric .l {{ font-size:12px; color:var(--muted); margin-top:2px; }}
  .metric.err {{ border-color:#fecaca; background:#fef2f2; }} .metric.err .v {{ color:#dc2626; }}
  .metric.warn {{ border-color:#fde68a; background:#fffbeb; }} .metric.warn .v {{ color:#d97706; }}
  .metric.ok .v {{ color:#0ea5e9; }}
  .metric.muted {{ opacity:.55; }} .metric.muted .v {{ color:var(--muted); font-style:italic;
    font-size:14px; }}
  table {{ width:100%; border-collapse:collapse; font-size:14px; }}
  th,td {{ text-align:left; padding:9px 6px; border-bottom:1px solid var(--line); }}
  th {{ color:var(--muted); font-weight:600; width:42%; }}
  tr.grp th {{ color:var(--ink); font-weight:700; background:#f8fafc; width:auto;
    padding-top:16px; }}
  tr.grp:first-child th {{ padding-top:9px; }}
  .badge {{ display:inline-block; padding:2px 10px; border-radius:99px; font-size:12px;
    font-weight:600; }}
  .badge.ok {{ background:#dcfce7; color:#166534; }}
  .badge.warn {{ background:#fef9c3; color:#854d0e; }}
  .badge.err {{ background:#fee2e2; color:#991b1b; }}
  .badge.muted {{ background:#e2e8f0; color:#64748b; font-style:italic; }}
  .msg {{ margin:0; padding-left:20px; font-size:14px; }}
  .msg li {{ margin:3px 0; }}
  .warn-list li {{ color:#854d0e; }} .err-list li {{ color:#991b1b; }}
  footer {{ text-align:center; color:var(--muted); font-size:12px; margin-top:8px; }}
  .note {{ color:var(--muted); font-size:12px; font-style:italic; margin:0 0 14px;
    padding-left:10px; border-left:3px solid var(--line); }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">◈</div>
    <div>
      <h1>STAT GEOM QC — Rapport de contrôle qualité</h1>
      <div class="sub">{esc(result.layer_name)} · {esc(result.timestamp)} · {result.processing_time:.2f}s</div>
    </div>
  </header>

  <div class="card">
    <h2>Score de correctness</h2>
    <div class="hero">
      <div class="ring">{_score_ring_svg(c.score, c.color)}</div>
      <div class="grade">
        <div class="g-label">{esc(c.grade)}</div>
        <div class="g-desc">Qualité globale des données évaluée sur {C.fmt_int(result.total_features)} entités.{esc(skipped_note(result))}</div>
        <div class="g-desc">{esc(coverage_text(result))}</div>
      </div>
      <div class="comps">{comp_html}</div>
    </div>
  </div>

  <div class="card">
    <h2>Indicateurs</h2>
    <div class="metrics">
      <div class="metric ok"><div class="v">{C.fmt_int(result.total_features)}</div><div class="l">Entités</div></div>
      <div class="metric {'err' if q.null_empty_count else 'ok'}"><div class="v">{C.fmt_int(q.null_empty_count)}</div><div class="l">Nulles/Vides</div></div>
      <div class="metric {'err' if q.invalid_count else 'ok'}"><div class="v">{C.fmt_int(q.invalid_count)}</div><div class="l">Invalides</div></div>
      {_metric_or_skip(result, "overlap", q.overlap_count, "Chevauchements", "err")}
      {_metric_or_skip(result, "duplicate", q.duplicate_count, "Doublons")}
      {_metric_or_skip(result, "small", q.small_area_count, "≤ %g m²" % result.threshold_m2)}
      {_metric_or_skip(result, "hole", q.hole_count, "Trous")}
      {_metric_or_skip(result, "multipart", q.multipart_count, "Multi-parties")}
      {_metric_or_skip(result, "sharp_angle", q.sharp_angle_count, "Angles aigus")}
      {_metric_or_skip(result, "close_vertex", q.close_vertex_count, "Somm. rapprochés")}
      {_metric_or_skip(result, "nested_height", q.nested_height_count, "Haut. incluses", "err")}
      {_metric_or_skip(result, "unnoded_crossing", q.unnoded_crossing_count, "Croisements non nodés", "err")}
    </div>
  </div>

  {warnings_html}

  <div class="card">
    <h2>Informations couche</h2>
    <table>
      <tr><th>Couche</th><td>{esc(result.layer_name)}</td></tr>
      <tr><th>Source</th><td>{esc(result.source)}</td></tr>
      <tr><th>Fournisseur</th><td>{esc(result.provider)}</td></tr>
      <tr><th>CRS</th><td>{esc(result.crs_authid)} — {esc(result.crs_description)}</td></tr>
      <tr><th>Types de géométrie</th><td>{esc(geom_types)}</td></tr>
      <tr><th>Colonnes ({len(cols)})</th><td>{esc(cols_preview)}</td></tr>
      <tr><th>Étendue</th><td>X [{result.bounds.get('minx', 0):.4f}, {result.bounds.get('maxx', 0):.4f}] · Y [{result.bounds.get('miny', 0):.4f}, {result.bounds.get('maxy', 0):.4f}]</td></tr>
    </table>
  </div>

  <div class="card">
    <h2>Configuration de l'analyse et détail qualité</h2>
    <table>
      {quality_html}
    </table>
  </div>

  {invalid_html}
  {errors_html}
  {agl_note_html}

  <footer>Généré par STAT GEOM QC · Adel Bchini</footer>
</div>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# CSV / JSON
# ═══════════════════════════════════════════════════════════════════════════════


def build_qt_summary(result: AnalysisResult) -> str:
    """Rapport simplifié rendu par QTextBrowser (HTML4/CSS2 limité de Qt)."""
    c = result.correctness
    q = result.quality_report
    esc = html.escape

    geom_types = ", ".join("%s: %s" % (k, v) for k, v in result.geometry_types.items()) or "—"

    comp_rows = ""
    for key, label in _COMPONENT_LABELS.items():
        if key in c.components:
            v = c.components[key]
            col = "#16a34a" if v >= 95 else "#ca8a04" if v >= 80 else "#ea580c" if v >= 60 else "#dc2626"
            comp_rows += _qt_row(label, "%g%%" % v, col)

    warn_html = ""
    if result.warnings:
        warn_html = ("<h3 style='color:#854d0e'>Avertissements</h3>"
                    "<ul style='font-size:11pt'>" + "".join(
                        "<li>%s</li>" % esc(w) for w in result.warnings) + "</ul>")
    err_html = ""
    if result.errors:
        err_html = ("<h3 style='color:#991b1b'>Erreurs</h3>"
                    "<ul style='font-size:11pt'>" + "".join(
                        "<li>%s</li>" % esc(e) for e in result.errors) + "</ul>")

    # Configuration ET détail qualité, groupés catégorie par catégorie : voir
    # ``_quality_table_qt`` — le seuil d'un contrôle précède immédiatement son
    # résultat au lieu de vivre dans un tableau séparé.
    quality_qt = _quality_table_qt(result)

    # Détails d'invalidité : venus de l'onglet « Qualité », retiré en 2.9.49.
    # Doivent donc figurer AUSSI dans le PDF, qui est rendu d'ici.
    etat_invalid, details = invalid_details_state(result)
    if etat_invalid == "details":
        invalid_qt = ("<h3>Détails d'invalidité</h3>"
                      "<ul style='font-size:11pt'>%s</ul>"
                      % "".join("<li>%s</li>" % esc(d) for d in details))
    elif etat_invalid == "not_requested":
        invalid_qt = ("<h3>Détails d'invalidité</h3>"
                      "<p style='font-size:10pt;color:#94a3b8;font-style:italic'>"
                      "%s — les détails d'invalidité ne sont produits que si "
                      "« Contrôles topologiques » est coché.</p>"
                      % NOT_REQUESTED)
    else:
        invalid_qt = ("<h3>Détails d'invalidité</h3>"
                      "<p style='font-size:11pt;color:#64748b'>"
                      "Aucun détail d'invalidité.</p>")

    # Note discrète du contrôle interne AGL/AMSL (vide = rien à signaler).
    agl_note_qt = ""
    if result.agl_note:
        agl_note_qt = (
            "<p style='color:#64748b;font-size:10pt;font-style:italic'>%s</p>"
            % esc(result.agl_note)
        )

    # Tous les tableaux prennent la LARGEUR PLEINE (voir _QT_TABLE) : sans
    # elle, Qt réduit chaque tableau à son contenu et tout le rapport se
    # retrouve plaqué contre la marge gauche, le reste de la page vide — le
    # défaut corrigé en 2.9.50. Les tailles de police sont fixées explicitement
    # partout : rien ici n'hérite d'un défaut de document qui peut varier
    # d'un poste à l'autre.
    return f"""
    <div style="font-family:'Segoe UI',sans-serif;font-size:11pt">
      <h2 style="font-size:20pt;color:{c.color};margin-bottom:4px">Score : {c.score:g}/100 — {esc(c.grade)}</h2>
      <p style="font-size:11pt;color:#64748b;margin-top:0">{esc(result.layer_name)} · {C.fmt_int(result.total_features)} entités · {esc(result.timestamp)}{esc(skipped_note(result))}</p>

      <p style="font-size:10pt;color:#64748b;margin-top:0">{esc(coverage_text(result))}</p>

      <h3 style="font-size:14pt">Sous-scores</h3>
      <table {_QT_TABLE}>{comp_rows}</table>

      <h3 style="font-size:14pt">Configuration de l'analyse et détail qualité</h3>
      <table {_QT_TABLE}>{quality_qt}</table>

      <h3 style="font-size:14pt">Couche</h3>
      <table {_QT_TABLE}>
        {_qt_row("CRS", "%s — %s" % (result.crs_authid, result.crs_description))}
        {_qt_row("Types", geom_types)}
        {_qt_row("Colonnes", len(result.attribute_info.get("columns", [])))}
        {_qt_row("Durée", "%.2f s" % result.processing_time)}
      </table>

      {invalid_qt}
      {warn_html}
      {err_html}
      {agl_note_qt}
    </div>
    """


def write_pdf(result: AnalysisResult, path: str) -> None:
    """Écrit le rapport en PDF, une page par débordement.

    Le PDF est rendu depuis ``build_qt_summary`` et NON depuis ``build_html``.
    Ce n'est pas un raccourci : ``build_html`` vise un navigateur — anneau de
    score en SVG, ``display:flex``, ``display:grid`` — et le moteur de texte de
    Qt n'en rend rien. Mesuré : la jauge disparaît purement et simplement, et
    les cartes d'indicateurs s'effondrent en « valeur puis libellé » à l'envers,
    illisibles. ``build_qt_summary`` est justement écrit pour ce moteur
    (HTML4/CSS2 restreint), et son rendu est net.

    Un en-tête est ajouté ici : le résumé Qt commence directement par le score,
    ce qui convient à un onglet du panneau mais pas à un document autonome qui
    doit se relire des mois plus tard sans savoir d'où il sort.

    L'APPAREIL DE MISE EN PAGE est attaché explicitement au document, et c'est
    indispensable : ``QTextDocument.print()`` ne l'attache lui-même que si le
    document n'a PAS de taille de page — dès qu'on lui en donne une, il le
    considère déjà paginé et le met en page tel quel. Sans cet appel, Qt
    calculait donc les métriques à ~96 ppp pendant que la page était exprimée
    en pixels à 300 ppp : mesuré, une ligne de 11 pt sortait à 1,5 mm sur le
    papier, soit 4,3 pt — un rapport illisible, tassé en haut à gauche de la
    feuille. C'est le défaut corrigé en 2.9.51.

    Corollaire mesuré sur les unités CSS, à ne pas inverser : les tailles de
    POLICE doivent être en ``pt`` (Qt les convertit avec les métriques de
    l'appareil), tandis que les ``padding``/``margin`` doivent être en ``px``
    (Qt met les px à l'échelle dpi/96, mais IGNORE purement et simplement un
    padding exprimé en pt).
    """
    from qgis.PyQt.QtGui import QPdfWriter

    entete = (
        "<h2 style='font-size:20pt;margin:0'>STAT GEOM QC — Rapport de "
        "contrôle qualité</h2>"
        "<p style='font-size:11pt;margin:2px 0 12px 0;color:#64748b'>%s</p><hr/>"
        % html.escape(result.layer_name or "")
    )

    writer = QPdfWriter(path)
    configure_pdf_page(writer)
    # 300 ppp : le texte reste net à l'impression, sans gonfler le fichier
    # (aucune image bitmap dans ce rapport).
    writer.setResolution(300)
    writer.setTitle("STAT GEOM QC — %s" % (result.layer_name or ""))

    doc = pdf_document(result, writer, entete)
    doc.print(writer)


def configure_pdf_page(writer) -> str:
    """Page A4 portrait, marges de 12 mm — quelle que soit la version de PyQt.

    Défaut de la 2.9.51, signalé sous QGIS : « setPageSize(self,
    QPagedPaintDevice.PageSize): argument 1 has unexpected type 'QPageSize' ».
    Dans certaines compilations de PyQt5, ``QPdfWriter.setPageSize`` redéfinit
    l'ancienne surcharge à ÉNUMÉRATION et MASQUE celle de la classe mère qui
    prend un ``QPageSize`` — sans qu'on puisse le savoir avant l'appel : la
    même ligne passe sous PyQt 5.15.11 et échoue ailleurs.

    ``setPageLayout`` est tenté d'abord : QPdfWriter ne le redéfinit pas, il
    existe de Qt 5.3 à Qt 6, et il fixe format, orientation et marges en un
    seul appel. Deux replis suivent pour les liaisons qui ne l'exposeraient
    pas. Renvoie le nom de la voie retenue (utile aux tests et au diagnostic).
    """
    import contextlib

    from qgis.PyQt.QtCore import QMarginsF
    from qgis.PyQt.QtGui import QPageLayout, QPageSize

    marges = QMarginsF(12, 12, 12, 12)
    # contextlib.suppress et non « except: pass » : Bandit (B110) refuse le
    # second, et plugins.qgis.org rejette alors le paquet.
    with contextlib.suppress(TypeError, AttributeError):
        mise_en_page = QPageLayout(
            QPageSize(QPageSize.PageSizeId.A4),
            QPageLayout.Orientation.Portrait, marges,
            QPageLayout.Unit.Millimeter)
        if writer.setPageLayout(mise_en_page) is not False:
            return "setPageLayout"
    try:
        writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        voie = "setPageSize(QPageSize)"
    except TypeError:
        # Surcharge historique à énumération, seule exposée par ces liaisons.
        from qgis.PyQt.QtGui import QPagedPaintDevice
        writer.setPageSize(QPagedPaintDevice.PageSize.A4)
        voie = "setPageSize(enum)"
    writer.setPageMargins(marges, QPageLayout.Unit.Millimeter)
    return voie


def pdf_document(result: AnalysisResult, writer, entete: str = ""):
    """Document mis en page POUR ``writer`` — la moitié fragile de ``write_pdf``.

    Extrait pour être vérifiable : le défaut de la 2.9.50 ne se voyait ni dans
    le HTML produit ni dans un rendu sur image, seulement dans les métriques du
    document réellement mis en page pour le périphérique de sortie. Un test peut
    donc appeler cette fonction et mesurer ce que Qt a calculé (voir
    ``test_pdf_report_is_laid_out_at_the_output_resolution``).
    """
    from qgis.PyQt.QtCore import QSizeF
    from qgis.PyQt.QtGui import QTextDocument

    doc = QTextDocument()
    # AVANT toute mise en page (voir la docstring de write_pdf) : c'est cet
    # appel qui donne au document la résolution réelle de la sortie.
    doc.documentLayout().setPaintDevice(writer)
    # Police de base explicite : celle du document vaut ce que l'application
    # hôte a configuré (mesuré : 8,2 pt sous QGIS), ce qui s'appliquerait à
    # tout élément sans taille propre — les puces des listes, par exemple.
    police = doc.defaultFont()
    police.setFamily("Segoe UI")
    police.setPointSizeF(11.0)
    doc.setDefaultFont(police)
    doc.setHtml(entete + build_qt_summary(result))
    doc.setPageSize(QSizeF(writer.width(), writer.height()))
    return doc


def write_html(result: AnalysisResult, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_html(result))
