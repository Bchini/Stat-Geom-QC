# STAT GEOM QC — Plugin QGIS

Contrôle qualité et statistiques géométriques des couches vectorielles, avec
**score de correctness** et rapport exportable. Portage natif PyQGIS de
l'application desktop *STAT GEOM* (aucune dépendance externe : ni geopandas,
ni matplotlib).

## ✨ Fonctionnalités

- **▭ Zone d'analyse (AOI), facultative** : tracer un rectangle sur la carte
  (ou reprendre l'**étendue visible**) pour ne contrôler que les entités de
  cette zone — contrôle par tuiles, quartier par quartier, sur une couche de
  plusieurs centaines de milliers d'entités. Décochée par défaut : sans
  intervention, toute la couche est analysée comme avant.
  - Une entité est retenue dès qu'elle **intersecte** la zone : un bâtiment à
    cheval sur le bord est analysé **en entier**, jamais coupé.
  - Les **voisins immédiats hors zone sont lus** pour les contrôles de paires
    (chevauchement, imbrication, croisement de lignes) : un bâtiment de la
    zone qui chevauche son voisin juste dehors est une erreur réelle et elle
    est signalée. Ces voisins n'apportent en revanche **aucune anomalie**
    propre — l'erreur d'à-côté relève de la tuile qui la porte.
  - Le **score et tous les compteurs** se rapportent à la zone (dénominateur =
    entités analysées). Une zone **vide** ne rend pas 100/100 mais un score
    explicitement **« Non évalué »**.
  - Une **réparation** sous zone produit un fichier couvrant **exactement le
    même périmètre** que l'analyse : c'est un extrait, annoncé comme tel dans
    le bilan, jamais un remplaçant de la couche source.
  - La zone n'est **pas mémorisée** entre deux sessions : une zone restaurée en
    silence restreindrait une analyse sans que rien ne le dise. Elle est rendue
    à QGIS au déchargement du plugin — tracé, outil de carte et signaux.
  - L'**aperçu des attributs** du rapport et l'**étendue** annoncée portent
    eux aussi sur la seule zone : ce qui est montré est ce qui a été
    contrôlé.
  - La **vérification automatique** après une réparation sous zone énonce sa
    propre limite : le fichier vérifié est un extrait, les voisins n'y figurent
    pas, les erreurs de **paire au bord** n'y sont donc pas recontrôlables.
  - Disponible aussi dans **Processing** (paramètre d'étendue facultatif), donc
    en traitement par lots et via `qgis_process` — de quoi enchaîner les tuiles
    d'une grille de livraison sans passer par l'interface.
- **Analyse de qualité géométrique** directement sur les couches QGIS :
  - géométries **nulles ou vides** (type de problème unifié) ;
  - géométries invalides + auto-intersections (contrôles topologiques) ;
  - **chevauchements / intersections entre polygones** (intersection des intérieurs, via index spatial ; les simples contacts de frontière et les doublons exacts sont exclus) ;
  - doublons (WKB normalisé) ;
  - petits polygones sous un seuil m² configurable (surface ellipsoïdale, fonctionne aussi en CRS géographique) ;
  - **sommets dupliqués** (option) : sommets strictement superposés uniquement,
    sans tolérance ;
  - **trous** dans les polygones sous un seuil de surface (option) : les grands
    trous, souvent justifiés, sont conservés ;
  - entités **multi-parties** (contrôles topologiques) ;
  - section **« Extras »** (options, désactivées par défaut) : **angles aigus**
    (pointes de digitalisation), **sommets trop rapprochés** et
    **croisements de lignes non nodés** (réservé aux
    couches de lignes : un point où deux entités distinctes se croisent ou se
    touchent sans qu'aucune des deux n'y porte de sommet — la jonction est
    invisible pour un outil de routage ; un croisement d'une ligne sur
    elle-même n'est pas vérifié).
- **Hauteur des polygones inclus** (option d'analyse, cochée par défaut) : quand un polygone est
  entièrement inclus dans un autre, sa valeur d'altitude doit être
  **strictement supérieure** à celle du polygone qui le contient. Les champs
  `AGL` et `HEIGHT` sont reconnus quelle que soit la casse, et chacun est
  comparé à son homologue. Seul le polygone inclus est signalé ; une altitude
  absente ou non numérique rend la paire *non vérifiable* et elle est comptée à
  part, jamais tenue pour conforme. Sans aucun de ces champs, le contrôle se
  marque **non exécuté** au lieu d'afficher un 0.
- Les polygones **entièrement inclus** dans un autre peuvent être **acceptés ou
  signalés** comme chevauchement, au choix (case dédiée).
- Un contrôle **interne** de cohérence des altitudes (AGL / AMSL) est exécuté
  silencieusement lorsque ces deux champs existent et sont entièrement
  renseignés ; il n'influe pas sur le score et n'apparaît que sous forme d'une
  note en fin de rapport.
- **Score de correctness (0-100)** pondéré, avec grade (Excellent → Critique),
  jauge circulaire et décomposition par dimension.
- **Interface moderne dockable** : sélection de couche (ou chargement de
  fichier), options, progression annulable, et une **synthèse** en cartes
  d'indicateurs. Le panneau s'en tient là : le détail — tableau qualité
  groupé par catégorie, détails d'invalidité — vit dans
  les **rapports** (navigateur, HTML, PDF), où il a la largeur d'une page
  pour se lire.
- **Un seul mode d'analyse**, sans nom : à l'ouverture, tous les contrôles
  d'*Options d'analyse* sont cochés et aucun d'*Extras* ne l'est. Décocher
  reste à un clic, et ce choix est mémorisé — contrairement aux *Extras*,
  qui repartent décochés à chaque session.
- **Actions QGIS intégrées** :
  - 🎯 sélectionner les entités problématiques (avec zoom) ;
  - 🧩 **couche de points des erreurs**, colorée par type d'anomalie et
    **étiquetée** : un chevauchement entre deux bâtiments produit **un seul
    point**, portant les deux identifiants — et non deux points superposés.
    Écrite dans un fichier (algorithme Processing), elle est accompagnée d'un
    `.qml` que QGIS lit à l'ouverture : couleurs et étiquettes se retrouvent
    telles quelles, sans réglage manuel ;
  - 🛠 **réparation géométrique sélective** : un dialogue à **cases à cocher**
    laisse choisir quelles erreurs corriger automatiquement (géométries
    invalides → *make valid*, **auto-intersections** → *make valid*, suppression
    des nulles/vides, des doublons, des petits polygones, et **correction
    prudente des chevauchements mineurs**). Le résultat est écrit dans un
    **nouveau fichier** (GeoPackage / Shapefile / GeoJSON) : **la couche
    d'origine n'est jamais modifiée**. Après validation des cases, la réparation
    démarre directement, sans demander de chemin : la sortie est créée à côté
    de la source, avec le même nom suivi de **`_repare`**. Si ce fichier existe
    déjà, rien n'est écrasé sans confirmation : le choix est proposé entre
    **écraser** et écrire à côté (**`_repare_2`**, `_repare_3`…).
  - 🔎 **aperçu adaptatif** : chaque case du dialogue de réparation s'accompagne
    d'un message calculé sur LA couche en cours d'analyse — combien seront
    réellement corrigés, combien laissés intacts par prudence (le retrait
    aurait dégradé la géométrie), et pourquoi — **avant** que le fichier ne
    soit écrit. Chaque tolérance réglable (seuil des petits polygones,
    pourcentage de rognage, degré des angles aigus, distance des sommets
    rapprochés, seuil des trous) se règle directement là, avec un
    recalcul à la demande ; le bouton **Réparer…** reste désactivé tant qu'une
    case cochée n'a pas d'aperçu à jour. Les seuils de **détection** ne
    peuvent y être que **resserrés**, jamais élargis au-delà de l'analyse :
    la réparation ne travaille que sur les entités déjà signalées, élargir
    appliquerait deux traitements différents à la même donnée. Les deux contrôles sans correction
    automatique (hauteur des polygones inclus, croisements de lignes non
    nodés) y sont listés **sans case**, avec l'explication du pourquoi et le
    chemin de reprise manuelle.
  - 🔗 **chevauchements** : seul le **plus petit** polygone de chaque paire est
    rogné (le plus grand conserve sa forme), et **uniquement si le rognage reste
    mineur** (surface retirée ≤ un seuil % réglable, 5 % par défaut). Les
    chevauchements importants sont **laissés intacts** et leurs **FID** sont
    listés pour une correction manuelle.
  - 📋 **bilan de réparation** : une seule sortie, le fichier réparé — rien
    d'autre n'est écrit. Le bilan regroupe dans une section **« Corrections
    manuelles requises »** tout ce qu'aucune case ne corrige automatiquement,
    avec les **FID source** de chacune. Une **vérification automatique**
    relance ensuite les mêmes contrôles sur le fichier réparé et annonce,
    sans action de l'utilisateur, ce qu'il en reste.
- **Rapport en trois formes** : **navigateur** (rapport complet mis en
  forme, anneau de score), fichier **HTML**, fichier **PDF** prêt à joindre
  à une livraison. Le PDF est rendu depuis la version simplifiée du
  rapport, celle que le moteur de texte de Qt sait afficher — le rendu
  navigateur (SVG, flexbox) y perdrait sa jauge et ses cartes.
- **Algorithmes QGIS Processing** — les mêmes contrôles, utilisables là où le
  panneau ne peut pas aller :
  - `statgeomqc:analyze` — analyse complète. Renvoie le score, sa **couverture**
    (combien de contrôles ont réellement abouti) et les compteurs, sous forme de
    sorties **chaînables dans le modeleur**.
  - `statgeomqc:errorlayer` — couche de points localisant chaque anomalie, avec
    le champ de classement qui permet une symbologie par type.
  - `statgeomqc:report` — rapport HTML et/ou PDF. Chaque format est une
    sortie indépendante : ne renseignez que ceux qui vous intéressent.

  Ils fonctionnent dans le **modeleur graphique**, en **traitement par lots**, et
  via **`qgis_process`** en ligne de commande — sans interface et **sans projet
  ouvert**, ce qui les rend utilisables dans une chaîne de production :

  ```bash
  qgis_process plugins enable stat_geom_qc
  qgis_process run statgeomqc:analyze -- INPUT=bati.gpkg NESTED_HEIGHT=true
  ```

  Chaque algorithme est **autonome** : il relance l'analyse pour son propre
  compte. Enchaîner les trois dans un modèle recalcule donc trois fois — c'est
  le prix de la fiabilité, le résultat d'analyse ne pouvant pas transiter d'un
  algorithme à l'autre sans risque de décalage d'identifiants.

  Le panneau, lui, continue d'appeler le moteur **directement** : le faire passer
  par ces algorithmes figerait l'interface pendant le calcul et rendrait le
  bouton *Annuler* inopérant. Les deux façades partagent le même moteur, aucune
  logique n'est dupliquée.

> ℹ️ **Score de correctness** : un score parfait `100/100` est réservé aux
> couches **sans aucune anomalie**. Dès qu'une erreur subsiste, le score est
> plafonné sous 100. À l'inverse, un contrôle qui n'a **rien pu examiner**
> (couche sans polygone pour les contrôles polygonaux, sans ligne pour les
> croisements, altitudes illisibles pour la hauteur imbriquée) est déclaré
> **non applicable** ou **non concluant** et **sort du score** : il n'y pèse
> ni comme réussite ni comme échec.

> ⚠️ **Fiabilité des identifiants** : les FID d'un résultat ne valent que pour
> la couche analysée, dans l'état où elle a été analysée. Éditer cette couche
> **périme** le résultat : les actions qui agissent sur la donnée
> (sélectionner, réparer) refusent alors de partir jusqu'à une nouvelle
> analyse, plutôt que de frapper des entités qui ne sont plus les mêmes.
> Une réparation refuse également d'écrire une sortie **partielle** : si le
> moindre enregistrement est rejeté, aucun fichier n'est produit.

## 📦 Installation

1. Dans QGIS : **Extensions → Installer/Gérer les extensions → Installer depuis un ZIP**.
2. Sélectionner `stat_geom_qc.zip`.
3. Activer *STAT GEOM QC*.

L'outil apparaît dans le menu **Vecteur → STAT GEOM QC** et dans la barre d'outils.

## 🚀 Utilisation

1. Cliquer sur l'icône pour ouvrir le panneau.
2. Choisir une couche vectorielle (ou *Charger un fichier*).
3. *Facultatif* : cocher **Restreindre à une zone**, puis **▭ Dessiner…**
   (cliquer-glisser sur la carte, *Échap* annule) ou **Étendue visible**. La
   zone retenue reste tracée sur la carte tant qu'elle est active.
4. Régler les options (seuil m², topologie, doublons, trous, section « Autres »).
5. **Analyser la couche**.
6. Consulter le score, les indicateurs et le rapport ; exporter ou agir sur la couche.

## 🧮 Score de correctness

Le score part de 100 et retranche une pénalité proportionnelle à la part
d'entités concernées, pondérée par gravité :

| Dimension | Poids |
|-----------|-------|
| Nulles/Vides · Invalides | 1.00 |
| Croisements de lignes non nodés | 0.60 |
| Chevauchements | 0.70 |
| Doublons | 0.60 |
| Petits polygones · Trous | 0.40 |
| Hauteur des polygones inclus | 0.40 |
| Multi-parties · Angles aigus | 0.30 |
| Sommets rapprochés | 0.20 |
| Sommets dupliqués | 0.25 |

Les contrôles optionnels ne pèsent sur le score que s'ils ont été exécutés.
Le contrôle interne AGL/AMSL n'entre **pas** dans le score.

Grades : Excellent ≥ 95 · Bon ≥ 85 · Correct ≥ 70 · Faible ≥ 50 · Critique < 50.

---
*Adel Bchini — adel.bchini@gmail.com — v2.9.48*
