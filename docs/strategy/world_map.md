# REVA World — carte du monde : ce qu'on a, ce qui manque, et ce que « prédire » voudrait dire

Agent CARTOGRAPHE, 2026-10-17. C'est un document de travail pour le propriétaire, base d'un brainstorming. Il ne
modifie ni le code ni la base.

**Sources des chiffres.** Sauf mention contraire, les chiffres viennent d'une lecture seule de la base de dev `reva`
(PostgreSQL 17, conteneur `reva-db`, migration `0018_retraction_reasons`, 4,5 Go), faite le 2026-10-17. Les
fréquences et profondeurs temporelles de l'atlas viennent d'un scan DuckDB en lecture seule des 228 tables Parquet
courantes (`var/objects/reva/normalised/…`). Les autres chiffres viennent des rapports cités (`docs/…`).

**En une phrase.** REVA est aujourd'hui un **registre sourcé, annuel et national** du monde social et politique :
93,5 M de valeurs, 2,65 M d'énoncés de graphe, 1,15 M de liens pays↔pays et 50 245 affirmations causales notées.
Il ne contient presque rien de la physique, de la chimie, du vivant, de la météo, des marchés, des entreprises ni des
individus. Aucune de ses affirmations causales n'a de **taille d'effet**. Rien de cela ne suffit aujourd'hui à
prédire l'effet d'une décision. Le chemin pour y arriver existe, mais il est long, et il passe d'abord par la
**mesure honnête** de ce que REVA sait prédire, pas par davantage de données.

---

## 1. Ce qui existe

### 1.1 Vue d'ensemble (base de dev, 2026-10-17)

| Couche | Volume actuel | Où regarder |
|---|---|---|
| **Atlas** (séries par pays) | **228 tables** courantes (244 versions), **93 499 773 lignes**, 83 familles de sources. 108 sources déclarées dans le code (125 modules de connecteurs, dont 103 `atlas_*.py`), 103 fiches source dans `registry/sources/`. Catalogue de **9 479 concepts**, dont 27 « headline » avec ordre de sources justifié | `registry/atlas/catalog/*.yaml` (39 fichiers), `registry/atlas/source_order/`, `docs/dev/v3_atlasops.md`, `v3_a_*.md` |
| **Graphe (KG)** | **169 472 entités** courantes (53 types), **2 649 429 énoncés** courants (37 sources). Base séparée `reva_kg_wdharvest` (Wikidata) : 4 186 entités, 104 875 énoncés. `reva_kg_causwd` : 1 406 entités, 13 868 énoncés | `docs/dev/v3_kgcore.md`, `v3_wdharvest.md` |
| **Liens** (« neurones ») | 1 matérialisation, **1 151 759 liens** répartis en 35 types présents. Le registre déclare 38 types dérivés et 118 arêtes d'énoncés | `registry/links/link_types.yaml`, `docs/dev/v3_links.md` |
| **Événements et causalité** | Taxonomie de 83 types d'événements, 19 rôles et 11 relations causales. **50 245 affirmations causales** courantes, toutes notées (grading v3) | `registry/events/taxonomy.yaml`, `registry/causal/*.yaml`, `docs/roadmap/v3_causality.md` |
| **Croyances** (V1) | 1 release scellée, 482 estimations (WDI population et PIB, Comtrade brut) | `docs/methodology.md` §1 |
| **Simulations** | 96 lignes `sim.result` dans la base de dev. Modèles : brut L0–L2, cohortes (231 pays), retraite (126–128 pays), prix IO (49 pays FIGARO) | `docs/dev/v3_simworld.md`, `v2_*.md` |
| **Prévisions émises** | **0** ligne dans `model.evaluation`. Le registre de prévisions prospectives existe, mais n'a jamais servi | `src/reva/evaluation/ledger.py` |
| **V4 (nouveau)** | 21 familles de relations universelles (aucune causale) et 10 thèmes V4 ajoutés à `themes.yaml`. Seul le thème `prices` a 3 concepts. Une requête KG « composition du pétrole » | `registry/kg/relations_v4.yaml`, `docs/dev/v4_v4core.md` |

### 1.2 Atlas par domaine (thème × nombre de concepts du catalogue)

Le compte vient de `reva.atlas.catalog.load_catalog()`. Le thème est celui de `registry/atlas/themes.yaml`.

| Thème | Concepts | Principales sources (lignes dans la base) |
|---|---:|---|
| éducation | 3 627 | Banque mondiale EdStats/WDI (21,9 M lignes WB au total), UIS (118 k) |
| société / qualité de vie | 1 170 | UNDP (205 k), WHR (26 k), WID (40 pays, 42 k), Gapminder (130 k) |
| économie | 926 | WDI, FMI WEO/BOP/GFS/IMTS (3,97 M), OCDE (1,76 M), Maddison (40 k), Clio-Infra (951 k), JST (117 k) |
| finances publiques et monnaie | 791 | FMI GFS, BIS (43 k), Reinhart-Rogoff (172 k), Laeven-Valencia |
| population | 556 | ONU WPP 2024 (20,8 M), WUP 2025 (4,25 M), DESA migrations (673 k), HCR (243 k), HYDE (138 k) |
| santé | 451 | OMS GHO (606 k), ODD ONU (355 k) |
| politique | 340 | V-Dem (11,7 M), LIED (1,1 M), BMR (230 k), IPU (131 k), ParlGov (18 k) |
| commerce | 315 | BACI (2,24 M), UNCTAD (1,39 M), FMI IMTS |
| droit | 260 | WBL (926 k), CCP/Constitute (138 k), SSA, HCDH (8,7 k) |
| travail | 231 | ILOSTAT (2,99 M) |
| environnement | 201 | OWID (2,72 M), FAO (13,1 M, agriculture comprise), CCKP (12,7 k) |
| science et techno | 158 | OMPI (6,25 M), Nobel (2,9 k). OpenAlex : non chargé (clé requise) |
| histoire | 149 | COW (149 k), ICOW, COLDAT, CShapes, Archigos |
| énergie | 131 | EIA (2,55 M), USGS minéraux (53 k), OWID énergie |
| culture | 124 | UNESCO patrimoine et PCI, Glottolog |
| géographie | 46 | Natural Earth, GeoNames (363 k), UNSD |
| prix (V4) | 3 | — (V4ECON en cours) |
| chimie, matériaux, biologie, écologie, climat, corps et maladies, industrie, entreprises, comportement (V4) | **0** | thèmes déclarés, aucun concept encore |

### 1.3 Espace : à quelle résolution ?

| Niveau | Ce qui existe | Volume |
|---|---|---|
| Monde / agrégats | Agrégats des fournisseurs (WB, FMI, ONU). Ils sont gardés, mais **non mappés** sur des pays (ex. 12 082 lignes WEO « G001… ») | — |
| **Pays** | Le cœur de REVA. Crosswalk 1.1.0 de **251 entités** (ISO 3166-1, Taïwan, XKX, CHI). Les concepts headline couvrent 171 à 238 pays | `registry/entities/country_crosswalk.json`, `docs/dev/v3_gaps.md` |
| États historiques | 3 671 `historical_state` et 1 587 `polity` (Cliopatria, de −2699 à 2024). Les frontières historiques viennent de CShapes (1886–2019) | KG |
| Région (ADM1) | **Seulement des noms** : GeoNames admin1 (11 823 lignes, 228 pays). Quelques NUTS Eurostat via les sources « gap-fill ». **Aucune série socio-économique ADM1 systématique** | `docs/dev/v3_a_cities.md` |
| Ville | ONU WUP 2025 (villes 813 k lignes, superficies 2,34 M, DEGURBA 809 k, capitales). GeoNames cities15000 (346 k lignes, 244 pays) | idem |
| Point | Ports (NGA World Port Index, 548), séismes (6 697), tsunamis (3 138), volcans (279), éruptions (903), sites patrimoniaux (1 273), sites de lancement (GCAT) | KG et atlas |
| Grille / champ physique | **Rien.** L'adaptateur CF/COG/Zarr est un **réservé P8** (`physical.field_adapter`) | `src/reva/models/physical/fields.py` |

### 1.4 Temps : profondeur et fréquence

Fréquences, classées d'après la durée `period_end − period_start` des 93,5 M lignes :

| Fréquence | Lignes | Part | Contenu |
|---|---:|---:|---|
| **Annuelle** | 89 250 536 | 95,5 % | presque tout |
| Datée au jour (événements ou états ponctuels, **pas des séries quotidiennes**) | 3 034 729 | 3,2 % | votes ONU (1,03 M), panel WBL, contributions aux missions de paix, villes GeoNames, ratifications, élections |
| Trimestrielle | 714 533 | 0,8 % | connectivité maritime UNCTAD (LSCI/LSBCI, 670 k), prix immobiliers et service de la dette BIS |
| **Mensuelle** | 283 743 | 0,3 % | **indices de prix à la consommation FAO (261 810 : la seule vraie série mensuelle d'ampleur)**, routes aériennes 2014, SMIC DOM |
| Pluriannuelle | 211 561 | 0,2 % | moyennes FAO, accord de vote ONU par période, normales climatiques CCKP |
| Hebdomadaire | 561 | ~0 | — |
| **Quotidienne ou intrajournalière** (marchés, météo, flux) | **0** | 0 | — |

**Profondeur.** HYDE remonte à −10 000, les catastrophes NOAA NCEI à −4359, Maddison à l'an 1, les revendications
ICOW à 301, COLDAT à 1462 et Clio-Infra à 1500. Ensuite viennent OWID CO₂ (1750), V-Dem, LIED, CCP (1789),
Gapminder, WID, BMR, Reinhart-Rogoff (1800), COW (1816) et Archigos (1840). Après 1945, la plupart des sources
modernes sont présentes. Les **projections** (WPP jusqu'en 2100, WEO jusqu'en 2031–2032, OMS 2030) sont marquées
`projection_upstream` et ne répondent jamais par défaut.

**Millésimes.** REVA conserve chaque révision avec son `recorded_at`. Mais son horloge ne commence que le
**2026-10-14**. REVA ne possède donc **aucun historique de millésimes** des données passées (voir §5.4).

### 1.5 Graphe : entités par type (top) et énoncés par source

* **Entités** (169 472) : événements 35 759 ; déplacements liés aux catastrophes 22 108 ; catastrophes 21 678 ;
  dialectes 13 706 ; langues 8 635 ; séismes 6 697 ; votes nominaux ONU 6 551 ; familles de langues 4 853 ;
  incidents militarisés 4 482 ; issues de campagnes 3 676 ; États historiques 3 671 ; tsunamis 3 138 ;
  **personnes 2 802** ; accords de paix 2 631 ; MID 2 436 ; épisodes de sanctions 1 738 ; polities 1 587 ;
  pays 495 ; traités 52 ; organisations 2. Il n'y a **aucune** entreprise, molécule, espèce, gène, maladie, usine
  ni station météo.
* **Énoncés** (2 649 429) : votes ONU 949 876 ; Grambank 362 025 ; Mass Mobilization 216 256 ; NOAA NCEI 174 629 ;
  IDMC 157 222 ; Glottolog 152 355 ; **Wikidata 118 059 seulement** ; COW 88 893 ; WALS 76 475 ; MEC 64 090 ;
  NAVCO 56 979 ; ICB 29 904 ; UCDP 27 759 ; Cliopatria 26 079 ; PA-X 24 440 ; puis 22 sources de moins de 25 k.
* **Requêtes Wikidata** : 117 spécifications YAML réparties dans 12 domaines (`registry/kg/queries/`). **8
  seulement ont un état de récolte** (core ×3, history ×4, causal ×1). WDQS a été limité (HTTP 429) pendant toute la
  session V3.

### 1.6 Liens : types et couverture temporelle (matérialisation n° 2)

| Type | Liens | Années |
|---|---:|---|
| commerce bilatéral BACI | 386 708 | 1995–2015 (au-delà, non matérialisé) |
| accord de vote AGNU | 144 043 | 1946–2024 |
| co-parties aux traités (UNTC) | 114 862 | 1953–2025 |
| commerce IMTS import / export | 98 520 / 97 944 | 2000–2025 |
| territoire de polity partagé | 50 449 | −2699–2024 |
| langue officielle / langue utilisée partagée | 37 600 / 37 512 | — |
| co-participants à un conflit / même camp / camps opposés | 28 411 / 21 236 / 5 499 | −1257–2025 |
| co-signataires, co-membres | 28 116 / 27 404 | — |
| stock de migrants | 26 125 | 1990–2024 |
| missions de paix, Glottolog, sanctions, routes aériennes 2014, relations diplomatiques, monnaie, succession, aléas partagés, lancements, câbles sous-marins, frontières (Wikidata, CShapes, NE), colonisation (COW, CShapes, COLDAT, Wikidata), chefs d'État partagés, alliances, programmes de sanctions US | de 9 700 à 20 | divers |

Un lien dit « connecté via ». **Ce n'est jamais une cause.**

### 1.7 Couverture causale (affirmations courantes, grading v3)

| Source | Rang | B | C | D | E | Total |
|---|---|---:|---:|---:|---:|---:|
| IDMC GIDD (catastrophe → déplacement) | T1 | 20 897 | | | 1 | 20 898 |
| Mass Mobilization (manifestation → réaction de l'État) | T1 | 7 221 | | | | 7 221 |
| ICB (déclencheur → crise → issue) | T1 | 4 392 | | | 1 | 4 393 |
| NAVCO | T1 | 4 167 | | | 2 | 4 169 |
| MEC | T1 | 3 414 | | | | 3 414 |
| COW MID | T1 | 3 311 | | | | 3 311 |
| NOAA NCEI (source → tsunami) | T1 | 1 388 | | 783 | 15 | 2 186 |
| UCDP terminaisons / accords | T1 | 1 416 | | | 13 | 1 429 |
| TIES, EUSANCT, ESRB | T1 | 1 551 | | | 1 | 1 552 |
| Wikidata (P828/P1542/…) | T2–T4 | 8 | 158 | 1 479 | 27 | 1 672 |
| **Total** | | **47 765** | **158** | **2 262** | **60** | **50 245** |

* **Aucune note A**, car aucune corroboration indépendante. **Aucune revue humaine** (`kg.causal_review` est vide).
* Relations : `has_effect` 21 374, `reaction_to` 12 438, `coded_outcome` 11 212 (ce sont des issues, pas des
  causes), `coded_trigger` 3 823, `immediate_cause` 502, `contributing_factor` 453, `has_cause` 443.
* Vérification temporelle : cohérent 11 436, chevauchant 28 519, indéterminé 10 247, incohérent 43.
* Ce qui accompagne les affirmations : polarité 21 100, portée 38 917, **texte de mécanisme 69**, **taille d'effet
  0** (le schéma `kg.causal_claim` n'a pas de champ de magnitude), contrefactuel 0.
* Couverture thématique : conflits, crises, contestation, sanctions, catastrophes et déplacements. **Aucune** causalité
  économique quantifiée (élasticités, multiplicateurs), sanitaire (facteurs de risque), écologique ni climatique.
* Les candidats Wikipedia (T5) sont en quarantaine dans `reva_causcand` : 21 extraits, 86 % corrects à la main.

### 1.8 Capacités de calcul (`src/reva/**`, catalogue `src/reva/capabilities.py`)

| Capacité | Statut | Fichiers |
|---|---|---|
| Prévision de base : `last_value`, `log_drift`, `local_level_log` (Kalman) | implémenté. Les intervalles de `local_level` ne sont **pas calibrés** (0,80 nominal → 0,66–0,70 réels) | `src/reva/models/forecasting/` |
| Tournoi de prévision : origines glissantes, MASE, CRPS log, score d'intervalle, Diebold-Mariano-HLN + Holm, holdout verrouillé ouvert une seule fois | implémenté, uniquement en mode `revised_data_retrospective` | `src/reva/models/tournament.py`, `src/reva/evaluation/tournament_service.py` |
| Scores : MAE, MASE, couverture, interval score, CRPS gaussien et Student-t, log-score gaussien | implémenté. **Pas de Brier** (aucune prévision binaire) | `src/reva/models/metrics.py`, `src/reva/evaluation/scoring.py` |
| Rejeu temporel à 3 modes (`operational_as_known`, `available_then_reconstruction`, `revised_data_retrospective`), dépôt « cutoff-scoped », garde anti-fuite | implémenté | `src/reva/evaluation/{replay,cutoff,leakage}.py` |
| Registre prospectif : prévisions gelées, hachées, notées plus tard | implémenté, **0 prévision émise** | `src/reva/evaluation/ledger.py` |
| Back-tests de modèles (API `/v1/models/{key}/backtests`) | implémenté. Cohortes 2013→2023 sur 232 pays : APE médian 2,96 % contre 3,33 % pour la croissance constante, **pire que la croissance constante dans 99 pays** | `src/reva/evaluation/backtests.py`, `docs/dev/v3_simworld.md` |
| Valeur de l'information (EVSI exact sur problème fini) | implémenté (démonstration) | `src/reva/evaluation/voi.py` |
| Scénarios : brut L0/L1/L2, cohortes, prix IO, retraite, marges des ménages | implémenté, **expérimental, non revu** | `src/reva/models/**`, `src/reva/simulation/**` |
| Brouillons de scénarios en langage naturel, questions `/v1/ask` | implémenté. **LLM jamais vérifié en direct pour `/v1/ask`** (crédits épuisés), le planificateur par mots-clés tourne | `src/reva/nl/`, `src/reva/nlqa/` |
| Autonomie T0–T3 et porte de promotion séparée | implémenté (bac à sable externe **bloqué**) | `src/reva/autonomy/`, `src/reva/gate/` |
| Monte-Carlo L3, L4, couplage rétroactif, horizon multi-période, sorties macro (PIB, chômage, inflation), modèle de politique causal, recherche de décision et Pareto, réactions multi-acteurs, champs physiques, ménages synthétiques, plugins écologie et santé | **réservé** (interfaces typées qui refusent) | `src/reva/extensions/{decision,feedback,io_sfc,population}.py`, `docs/implementation_status.md` §4 |

---

## 2. Ce qui a été fait (V1 → V4, sur une page)

361 commits sur la branche `claude/kind-darwin-kyy8re` : 56 le 14/10, 128 le 15/10, 104 le 16/10, 73 le 17/10.

| Date | Version | Livré | Bilan |
|---|---|---|---|
| 14–15 oct. | **V1** (P0–P7) | Noyau typé (valeurs manquantes typées, hachage canonique), registre de preuves append-only avec octets bruts, releases de croyances scellées et rejouables, scénario « exposition au brut » L0–L2, tournoi de prévision, autonomie avec porte séparée, API et UI | 2 439 tests ; 13/13 étapes d'acceptation en direct ; non déployé (identifiants manquants) — `docs/final_report.md` |
| 15 oct. | **V2** | Brut pour tous les pays, brouillons en langage naturel (gpt-4.1), cohortes UE+AELE, prix IO FIGARO, âge de la retraite FRA/DEU/NLD, API de back-tests, UI V2 | Tous expérimentaux ; revue scientifique `review_science_v2.md` ; un benchmark naïf bat souvent les modèles |
| 16 oct. | **V3** « la planète connectée » | Atlas (108 sources, ~9 250 concepts, 251 entités), KG (Wikidata et ~20 jeux spécialisés), 38 types de liens et moteur de chemins (18/20 chaînes d'ancrage), événements et causalité notée A–E, simulations pour tous les pays, UI « Planète » (8 écrans), `/v1/ask` | 52 rapports d'agents, 2 revues indépendantes — `docs/final_report_v3.md` |
| 17 oct. (nuit) | V3 ops et audits | Chargement de la base de dev (93,5 M lignes), 2 audits adversariaux de la causalité (`review_v3_causal_audit{,2}.md`), FIXA/FIXB (grading v3, raisons de rétraction, migration 0018), SWEEP2 | pytest **4 256 réussis / 0 échec**, vitest 248, e2e V3 13/13. Instantané des données `reva-data-20261017c` |
| 17 oct. | **V4** lancé | Feuille de route `docs/roadmap/v4_world_model.md`. V4CORE (commités `b40bff8`, `a4a484e`, `8dff518`) : 21 familles de relations, `src/reva/kg/families.py`, 10 thèmes, aides espace-temps (périodes du jour au siècle, identifiants de lieux) et chemins contraints par famille | en cours (§3) |

---

## 3. Ce qui est en cours

### 3.1 Agents V4 (état au moment de la rédaction)

| Agent | Périmètre | Ce qui est visible |
|---|---|---|
| V4CORE | familles de relations, résolveur, page entité, espace-temps | **commité** : `relations_v4.yaml` (21 familles), `src/reva/kg/families.py`, `family_query.py`, `spacetime.py`, 10 thèmes ; demandes de propriétés P186, P927, P183, P9714, P1582, P1034, P2293, P780, P2283, P128 ; rapport `docs/dev/v4_v4core.md` **partiel** |
| V4CHEM | éléments, isotopes, composés, matériaux | spécification `petroleum_composition.yaml` ; non commités : `atlas_chem_common.py`, `registry/kg/predicates_chem.yaml` — **en cours** |
| V4ENERGY | réserves, production, centrales, prix | non commités : `atlas_pinksheet.py` (prix Banque mondiale), `atlas_eia_steo.py` (perspectives EIA) — **en cours** |
| V4INDUSTRY | entreprises, usines, chaînes d'approvisionnement | WIP commité : `atlas_industry_common.py`, `registry/kg/predicates_industry.yaml` ; non commité : `registry/sources/gleif_api.yaml` — **en cours** |
| V4ECON | trimestriel et mensuel, infranational, prix | non commités : `atlas_imf_hf.py` (haute fréquence FMI), `atlas_eurostat_macro.py`, `econ_regions_kg.py` — **en cours** |
| V4BIO | taxons, gènes, protéines | rien de visible — **en cours** |
| V4ECO | occurrences, interactions, menaces | rien de visible — **en cours** |
| V4CLIMATE | séries météo et climat, projections | non commités : `atlas_cckp_series.py` + fixture + test — **en cours** |
| V4HEALTH | anatomie, maladies, charge de morbidité | non commités : `health_obo_common.py`, `registry/kg/predicates_health.yaml` — **en cours** |
| V4MIND | bien-être, valeurs, emploi du temps | WIP commité : `atlas_mind_{common,oecd,who}.py` ; non commités : `atlas_mind_{hofstede,occupations,openalex,timeuse}.py` — **en cours** |
| V4LINK | liens inter-domaines par identifiants | non commités : `v4link_common.py`, `atlas_unsd_classif.py` (classifications ONU), `registry/kg/identity_rules_v4.yaml` — **en cours** |

Les fichiers non commités sont rattachés aux agents d'après leur nom (déduction, pas une déclaration des agents).
Aucun rapport `docs/dev/v4_*.md` autre que `v4_v4core.md` n'existe au moment de la rédaction ; aucune donnée V4 n'est
encore chargée dans la base de dev (0 entité de type chimie, taxon, entreprise, maladie, station).

Contrainte de toute la V4 : environ **6 Go de disque libre** (5,6 Go mesurés), au plus 300 Mo de brut par agent, et
uniquement des agrégats côté fournisseur. Les gros jeux (GBIF, ERA5, PubChem, GLEIF, EXIOBASE, OpenAlex) **attendent
une machine plus grosse**.

### 3.2 Opérations en attente (`docs/dev/HANDOFF_V3.md`)

1. **Fichier de requête de l'atlas absent.** Sa construction (~7,1 Go + ~1,5 Go de débordement) a été interrompue
   faute de disque. Il faut libérer environ 9 Go, puis lancer `atlas snapshot --warm`, `links materialise`,
   `links prune` et `causal refresh`. Le type de lien `submarine_cable` reste à 0 dans le service tant que ce n'est
   pas fait.
2. **Récolte Wikidata** : environ 109 spécifications sur 117 ne sont pas récoltées (WDQS limité ; budgets de
   1 500 requêtes WDQS et 4 000 wbapi par jour). Demandes de propriétés à fusionner : `events.yaml` (P1923, P8031,
   P8032, P726, P991, P1590, P1339), plus les demandes V4.
3. **Sources en échec** : `atlas.ourairports` (503 sur la page de licence) et `atlas.rcs_dem` (OSF en
   maintenance). OpenAlex : clé requise. Plusieurs hôtes refusent les clients non-navigateurs : Powell & Thyne,
   Cline Center, ILO NORMLEX, UNODA, CITES/Ramsar, OCDE ICIO, ILO WSPD.
4. **Décisions du propriétaire** :
   * crédits LLM (OpenAI et Gemini épuisés) ;
   * clés et comptes : Comtrade, OpenAlex, Seshat, SIPRI ;
   * revues de licences : non commerciales (IPU, CShapes, JST, CCP), pas de redistribution (ICOW, UNTC, HCDH),
     licences floues ;
   * revues humaines : 163 tables de règles WBL, équivalences d'ordre de sources, crosswalk, QIDs ATA et PSE,
     toutes les affirmations causales ;
   * déploiement : Render, bucket S3 et 3 paires de clés ;
   * confidentialité : la variable d'environnement qui expose l'adresse du propriétaire aux agents.

---

## 4. Ce qui manque réellement : matrice des écarts par domaine de la réalité

Légende de priorité, du point de vue « modèle sourcé utile aux décisions » : **P1** = indispensable au prochain
palier, **P2** = utile, **P3** = encyclopédique, faible effet sur la prédiction. Les tailles sont des ordres de
grandeur publics à vérifier avant tout chargement.

| Domaine | Ce qui existe dans REVA | Ce qui manque | Meilleures sources ouvertes | Fermé, payant, personnel ou inobservable | Taille (complet → agrégé) | Prio. |
|---|---|---|---|---|---|---|
| **Physique et constantes** | rien | constantes, unités physiques, propriétés des matériaux | NIST CODATA, Wikidata | — | Mo | P3 |
| **Chimie et matériaux** | 1 spécification KG (pétrole) | éléments, isotopes, composés, compositions (ex. ce dont le brut est fait), toxicité | PubChem (PUG-REST), ChEBI, IAEA LiveChart, essais de bruts publiés (certaines majors, Equinor) | bases d'essais de bruts complètes (payantes), formules industrielles (secret) | PubChem ~10² Go → Mo par sous-ensemble | P3 (P2 pour l'énergie) |
| **Terre et géologie** | séismes, volcans, tsunamis (NOAA) ; minéraux USGS (annuel) | catalogue sismique complet, sols, relief, gisements localisés | USGS ComCat, Smithsonian GVP, ISRIC SoilGrids, Copernicus DEM, USGS MRDS | gisements privés (données d'exploration), réserves « prouvées » auto-déclarées | DEM et sols : To → agrégats en Mo | P2 |
| **Océans** | ports, câbles, connectivité maritime UNCTAD | température et salinité, niveau de la mer, pêche, **trafic des navires (AIS)** | NOAA OISST, Argo, CSIRO/NOAA niveau de la mer, FAO FishStat, Global Fishing Watch (en partie NC), AIS côtier US (NOAA) et danois | AIS mondial temps réel (Spire, MarineTraffic : payant) | AIS mondial : To par an | P2 |
| **Atmosphère, météo, climat** | normales CCKP, CO₂ par habitant OWID | **toute série météo**, réanalyses, projections CMIP6 par pays, CO₂ Mauna Loa, prévisions opérationnelles | GHCN-Daily et Monthly, Berkeley Earth, ERA5 (compte Copernicus gratuit), NASA POWER, CCKP CMIP6, ECMWF open data, GFS | — (largement ouvert) | ERA5 complet : de l'ordre du **pétaoctet** ; mensuel par pays : Mo | **P1** |
| **Biosphère : espèces et gènes** | rien (hors parcs via V3 nature, non chargés) | taxonomie, occurrences, abondance, réseaux trophiques, gènes, protéines | GBIF (API à facettes), Catalogue of Life, NCBI, GloBI, Living Planet Index, HGNC, UniProt, Ensembl | Liste rouge UICN (jeton, non commercial), génomes individuels (personnels) | GBIF : plus de 3 milliards d'occurrences (~10² Go+) → facettes en Mo | P2 |
| **Corps humain et santé** | indicateurs OMS GHO (606 k lignes), espérance de vie | anatomie, ontologie des maladies, causes de décès détaillées, facteurs de risque **quantifiés**, épidémies hebdomadaires | UBERON, MONDO/DO, OMS mortalité, IHME GBD (inscription, NC), OWID, FluNet, CDC WONDER | **dossiers médicaux individuels** (RGPD art. 9), essais non publiés | GBD : Go | P1 (pour les décisions de santé publique) |
| **Individus et comportement** | indicateurs agrégés (bonheur WHR, V-Dem) ; 2 802 personnes « historiques » | valeurs, confiance, emploi du temps, préférences, mobilité, attention | WVS et EVS (inscription), ESS, Global Preference Survey, enquêtes emploi du temps HETUS, pages vues Wikipedia | **intentions, pensées, comportements privés : inobservables** ; données de traces individuelles (téléphones, cartes) : personnelles, légalement hors limites | — | P2 |
| **Ménages** | marges des ménages (V2), Gini, WID (40 pays) | distributions complètes revenu, consommation, patrimoine ; microdonnées | LIS (accès restreint), IPUMS International (inscription, pas de redistribution), DHS, LSMS, EU-SILC (accès sur demande) | microdonnées fiscales individuelles (accès sécurisé type CASD), patrimoine des plus riches (opacité) | Go par pays | P2 |
| **Entreprises** | **rien** | identité, propriété, bilans, emploi, implantation, appartenance à des groupes | GLEIF LEI et relations parent (CC0), SEC EDGAR XBRL, SIRENE et INPI (France), Companies House (UK), OpenCorporates (licence), Wikidata | Orbis et Compustat (payants), comptes des non-cotées dans la plupart des pays, **transactions interentreprises** (confidentielles) | GLEIF ~3 M LEI (Go) ; EDGAR ~10² Go | **P1** |
| **Marchés, prix et finance** | IPC mensuel FAO, prix immobiliers BIS (trimestriel), crises financières | prix des matières premières (Pink Sheet), changes et taux quotidiens, indices boursiers, courbes de taux, **données haute fréquence** | Banque mondiale Pink Sheet, BCE (changes, taux), FRED et ALFRED, Trésor US, EDGAR, Eurostat HICP mensuel, échanges crypto (ticks publics) | **ticks et carnets d'ordres actions et futures (payants : bourses, LSEG, Bloomberg, LOBSTER)**, positions des fonds, flux d'ordres (privés), information privilégiée (illégale) | quotidien : Mo–Go ; ticks : **To par an et par marché** | P1 (quotidien) / hors périmètre (ticks) |
| **Infrastructure, transport, logistique** | routes aériennes 2014, ports, câbles, lancements, LSCI | réseaux routier et ferroviaire, flux réels (vols, navires, camions), centrales électriques localisées | OpenStreetMap, GEM, WRI Global Power Plant DB, OpenSky (accès recherche), OurAirports, GRIP routes | horaires OAG et Cirium (payants), flux de fret des transporteurs (privés) | OSM planète ~80 Go pbf | P2 |
| **Énergie (flux)** | EIA international annuel, OWID énergie | production et consommation **horaires**, prix de gros, capacités par centrale, échanges transfrontaliers | ENTSO-E Transparency (jeton gratuit), EIA API (clé gratuite), Ember (CC BY), Energy Institute Statistical Review, GEM | prix OTC et contrats bilatéraux (privés), données de Platts et Argus (payantes) | ENTSO-E horaire : Go | **P1** |
| **Gouvernements, droit, politiques** | constitutions (CCP), WBL, âges légaux, sanctions, régimes (V-Dem), traités | **textes de décisions et lois datées, budgets détaillés, mesures de politique** (le « traitement » des décisions) | EUR-Lex, Légifrance, Judilibre, BOOST (Banque mondiale), OxCGRT, IMF AREAER, Global Trade Alert (partiellement ouvert), ParlGov | délibérations internes, notes non publiées, intentions des décideurs (inobservables) | Go | **P1** (cœur de la prédiction conditionnelle aux décisions) |
| **Conflits et sécurité** | UCDP, COW, ICB, MID, Archigos, missions de paix, sanctions | événements géolocalisés quotidiens, dépenses militaires et transferts d'armes | UCDP GED et Candidate (mensuel), ACLED (compte ; licence restrictive), GDELT, SIPRI (autorisation) | renseignement (classifié), ordres de bataille | GED : 10² Mo ; GDELT : To | P1 |
| **Culture, médias, information** | UNESCO, langues, Nobel | flux d'information (couverture médiatique, attention), sondages d'opinion | GDELT, Media Cloud, pages vues Wikipedia, archives de sondages (Roper : payant) | contenus des plateformes (API fermées), audiences | To | P2 |
| **Science, technologie, brevets** | OMPI (6,25 M lignes), Nobel | publications par domaine et pays, citations, brevets au niveau document, courbes de coût des technologies | OpenAlex (CC0, clé), Crossref, PubMed, USPTO et EPO (bulk), Google Patents (BigQuery), données de courbes d'apprentissage (ex. Our World in Data, Way et al.) | PATSTAT (payant), R&D des entreprises (confidentielle) | OpenAlex snapshot ~10² Go | P2 |
| **Espace** | lancements GCAT | objets en orbite, éphémérides, catalogue stellaire | CelesTrak, Space-Track (compte), JPL Horizons, Minor Planet Center, Gaia DR3 | satellites militaires (non publiés) | Gaia : To | P3 |
| **Démographie** (pour mémoire) | **fort** : WPP, WUP, DESA, HCR, ILOSTAT | registres de décès hebdomadaires, migrations bilatérales annuelles | HMD et STMF (mortalité hebdomadaire), Eurostat, OCDE migrations | registres individuels (personnels) | Mo | P2 |

### 4.1 Ce qu'**aucune** base de données ne contient

* **Les intentions, croyances et plans des individus et des décideurs** (« Que fera Poutine ? », « Que fera la BCE
  jeudi ? »). On n'en a que des traces indirectes : discours, votes, sondages, marchés de prédiction.
* **Les transactions privées** : la plupart des échanges interentreprises, les dons, l'économie informelle et
  illégale (estimée, jamais mesurée), les patrimoines offshore (estimations type Zucman, pas de mesure).
* **Les événements non enregistrés** : petites violences non rapportées, morts non certifiées (dans de nombreux pays,
  une grande partie des décès n'a pas de cause certifiée), espèces non décrites (la majorité des espèces vivantes),
  passé préhistorique au-delà des reconstructions.
* **L'état complet d'un système chaotique** : l'atmosphère n'est mesurée qu'à une résolution finie. Les erreurs de
  mesure sous la maille croissent jusqu'à détruire la prévision (§5.2).
* **Le contrefactuel** : ce qui se serait passé sans la décision n'est jamais observé. Il ne peut qu'être **estimé**
  sous des hypothèses.
* **Le futur**, y compris sa part qui dépend de prévisions publiées (réflexivité).

### 4.2 Ce qui est **légalement ou éthiquement hors limites**

* **Données personnelles** (RGPD) : identifier, profiler ou prédire le comportement d'**individus** ; catégories
  particulières (santé, opinions politiques, religion, orientation : art. 9) ; recoupements qui réidentifient des
  microdonnées. L'invariant n° 10 de la constitution (`docs/data_constitution.md`) interdit déjà tout lien d'identité
  de personnes privées. Il faut le garder.
* **Information privilégiée et manipulation de marché** (règlement UE 596/2014 « MAR ») : utiliser une information non
  publique précise, ou publier des « prévisions » destinées à faire bouger un cours, est interdit.
* **Conseil en investissement** : recommander des opérations financières personnalisées relève d'un statut réglementé
  (CIF/AMF en France, MiFID II). REVA ne doit produire **aucun conseil financier**.
* **Conditions d'utilisation et licences** : sources non commerciales (IPU, CShapes, JST, CCP, UICN, GBD, ACLED selon
  la licence), interdiction de contourner les hôtes qui bloquent les robots (règle déjà appliquée en V3).
* **Double usage** : la prédiction fine de conflits ou de vulnérabilités peut servir à nuire. Il faut des règles
  d'agrégation et de publication, et pas de ciblage.

---

## 5. Prédire : analyse honnête

### 5.1 Ce qui est prévisible, et à quel horizon

| Type de grandeur | Horizon utile (ordre de grandeur) | Pourquoi | Ce que REVA en sait aujourd'hui |
|---|---|---|---|
| **Démographie** (effectifs, structure par âge) | années à décennies pour les 65+ et les totaux ; moins bien pour les naissances et les migrations | forte inertie : les adultes de 2050 sont presque tous déjà nés | Cohortes 2013→2023 : APE médian 2,96 %, mais **pire que la croissance constante dans 99 pays sur 232**. La valeur ajoutée est la structure par âge, pas le total |
| **Tendances climatiques globales** (température moyenne selon un scénario d'émissions) | décennies, **conditionnellement** au scénario | contrainte physique (bilan radiatif) : prévisibilité « de deuxième espèce », par conditions aux limites | rien chargé (V4CLIMATE en cours) |
| Climat régional (pluies, extrêmes locaux) | probabiliste, saisonnier si ENSO ; incertain à long terme | variabilité interne forte | rien |
| **Météo** | prévision déterministe utile jusqu'à ~7–10 jours ; limite théorique ~2 semaines | chaos (Lorenz) : croissance exponentielle des erreurs de l'état initial | rien |
| **Économie structurelle lente** (niveau de PIB, structure sectorielle, dette, démographie économique) | 1–2 ans avec un faible gain sur la tendance ; au-delà, la tendance elle-même | chocs et révisions ; les récessions sont historiquement mal anticipées par les prévisionnistes officiels | tournoi « revised data » uniquement, **aucun résultat temps réel** |
| Inflation et chômage | nowcast (mois en cours) à quelques trimestres | inertie et données mensuelles | pas de séries mensuelles chargées (hors IPC FAO) |
| **Marchés financiers** (rendements) | quasi imprévisibles au-delà du bruit ; la **volatilité** est plus prévisible (GARCH) que le sens | efficience : un signal public rentable est arbitré ; les anomalies publiées s'affaiblissent après publication | aucune donnée de marché |
| Élections | semaines à mois, avec sondages ; erreurs de quelques points | erreurs de sondage corrélées | résultats ParlGov et IPU, pas de sondages |
| **Conflits et crises** (survenue) | persistance prévisible (un conflit en cours continue) ; **survenue difficile**, taux de base faibles | événements rares, déclencheurs contingents, décisions stratégiques | 50 k affirmations codées, **aucun modèle** |
| Catastrophes rares (séismes, pandémies) | **taux** (aléa probabiliste) oui ; **date** non | processus sans précurseur fiable (séismes) | catalogues NOAA et IDMC |
| Technologies (coûts) | décennies, statistiquement (loi de Wright, courbes en S) | régularités empiriques robustes, mais avec des cassures | rien |
| Comportement **individuel** | non (et interdit, §4.2) | variables cachées, libre arbitre pratique | — |

### 5.2 Pourquoi « tout savoir depuis la création » n'implique pas « prédire »

1. **Chaos et sensibilité aux conditions initiales.** En météo, une erreur de mesure infime double en quelques jours.
   Même une connaissance parfaite du passé ne donne pas l'état présent à précision infinie, et c'est cet état qui
   compte.
2. **Limites de mesure.** On mesure toujours à une résolution finie, avec des révisions (PIB révisé de plusieurs
   dixièmes, voire de points), des définitions changeantes (`source_break` dans l'atlas) et des trous (530 cellules
   entité × concept sans aucune valeur dans la matrice `v3_gaps.md`).
3. **Réflexivité.** Une prévision publiée change le comportement : prophéties autoréalisatrices (panique bancaire)
   ou autodestructrices (alerte → prévention). La **critique de Lucas** ajoute qu'une nouvelle politique change les
   comportements, donc les paramètres estimés sous l'ancien régime ne valent plus.
4. **Efficience des marchés.** Tout signal public et rentable est exploité jusqu'à disparaître. Prédire les prix avec
   des données publiques, c'est concourir contre tous les autres qui ont les mêmes données, plus rapidement.
5. **Explosion combinatoire.** 8 milliards d'agents, des millions d'entreprises, des interactions non linéaires. Un
   modèle complet n'est ni calculable ni calibrable, d'où des modèles **réduits** dont l'erreur doit être mesurée.
6. **Variables non observées.** Intentions, informations privées, transactions cachées (§4.1). Le modèle ne voit
   qu'une projection du monde.
7. **Corrélation ≠ causalité.** Le monde observé mélange causes, confusions et sélection. Prédire l'effet d'une
   **décision**, c'est P(Y | do(X)), pas P(Y | X observé). Il faut une **identification** : expériences, expériences
   naturelles, hypothèses explicites. Plus de données ne remplace pas une identification.
8. **Un seul historique, des régimes qui changent.** On n'observe qu'une réalisation de l'histoire. Les événements
   rares (guerres mondiales, pandémies) ont des échantillons minuscules. Les relations se cassent (ruptures
   structurelles).
9. **Loi de Goodhart.** Un indicateur devenu cible cesse de mesurer ce qu'il mesurait.

**Conséquence.** La bonne ambition n'est pas « prédire le monde », mais **produire des prévisions probabilistes,
calibrées et conditionnelles sur des questions bien posées**, dont on mesure publiquement la qualité contre des
références fortes.

### 5.3 Ce que REVA a déjà pour prédire (et ce qui lui manque)

* **A déjà** :
  * une discipline anti-fuite rare : dépôt « cutoff-scoped », garde de fuite typée, trois modes de rejeu
    explicitement étiquetés (`src/reva/evaluation/`) ;
  * des releases scellées et rejouables `release_as_of` ;
  * un registre prospectif haché (`ledger.py`) ;
  * des scores propres (CRPS, interval score, log-score) et un tournoi statistiquement prudent (DM-HLN + Holm,
    holdout verrouillé) ;
  * l'EVSI ;
  * des interfaces réservées, déjà spécifiées, pour la décision (frontière de Pareto, multi-acteurs) et pour le
    couplage rétroactif.
* **Lui manque** :
  1. des **millésimes historiques** : son horloge commence le 2026-10-14, donc aucun back-test « tel que connu à
     l'époque » n'est possible sans importer des vintages ;
  2. des **séries infra-annuelles** ;
  3. des **tailles d'effet** ;
  4. des **modèles structurels** au-delà de trois modèles comptables ou démographiques ;
  5. des **prévisions binaires** (pas de score de Brier) ;
  6. une **calibration** des intervalles (`local_level` sous-couvre) ;
  7. **une seule prévision réellement émise et notée** (aujourd'hui : 0).

### 5.4 Feuille de route de recherche vers la prédiction conditionnelle aux décisions

**Étape A — Mesurer avant de construire (3–6 mois)**

* **Importer des millésimes.** Archives du FMI WEO (publiées deux fois par an depuis des décennies), ALFRED (Fed de
  Saint-Louis), base temps réel de l'OCDE, SPF de la Fed de Philadelphie et de la BCE. Cela permet des back-tests
  `available_then` honnêtes et fournit des **références fortes** (les prévisions du FMI eux-mêmes).
* **Pré-enregistrer** chaque trimestre un panier de questions (ex. 100–200) dans le `ledger` :
  * croissance et inflation à 1 et 4 trimestres ;
  * population ;
  * CO₂ ;
  * morts de conflits UCDP Candidate (mensuel) ;
  * questions binaires (« coup d'État dans le pays X d'ici 12 mois ? »).

  Les références sont la persistance ou la marche aléatoire, la climatologie (taux de base), le FMI WEO, le SPF et
  ViEWS (conflits).
* **Scores** :
  * Brier et log-score (binaire), CRPS (continu), score d'intervalle ;
  * **scores de compétence** relatifs à chaque référence ;
  * diagrammes de fiabilité et histogrammes PIT (calibration) ;
  * finesse ;
  * test de Diebold-Mariano et SPA de Hansen contre le data-snooping.

**Étape B — Quantifier la causalité (6–12 mois)**

* **Les affirmations ne sont pas des effets.** « Selon ICB, X a déclenché Y » ne dit ni de combien, ni avec quelle
  probabilité, ni dans quel contexte. Il faut une nouvelle couche d'**estimations d'effet sourcées**, qui demande une
  migration et une décision du propriétaire. Chaque estimation porte :
  * l'effet et son erreur-type ;
  * l'unité et l'horizon ;
  * la population et la période ;
  * la stratégie d'identification (RCT, DiD, IV, RDD, contrôle synthétique, VAR structurel) ;
  * le DOI et le passage cité ;
  * des notes de transportabilité.

  Les sources sont les méta-analyses et revues (multiplicateurs budgétaires, élasticités prix et revenu, effet des
  sanctions sur le commerce, pass-through énergie-prix déjà amorcé dans `registry/assumptions/`, facteurs de risque
  GBD).
* **Estimations propres, sous étiquette « estimation REVA »** : études d'événements sur les panels de l'atlas, en
  utilisant **les événements codés comme dates de traitement**. Exemples :
  * sanctions TIES/EUSANCT → commerce BACI/IMTS ;
  * crises bancaires Laeven-Valencia → PIB ;
  * catastrophes → PIB et déplacements ;
  * coups d'État Archigos → croissance.

  Méthodes : DiD à adoption échelonnée (Callaway-Sant'Anna), projections locales (Jordà), contrôle synthétique.
  **Validation par réplication** : on reproduit d'abord des résultats publiés connus, et un écart est un signal
  d'alerte.
* Toute estimation affiche son hypothèse d'identification et reste distincte des affirmations de sources. **Cela
  modifie le principe « REVA n'affirme aucune causalité »** (voir question 15).

**Étape C — Modèles structurels et simulation (12–24 mois)**

* **Modèles causaux structurels par domaine**, construits sur les estimations de l'étape B :
  * graphes causaux explicites (DAG) avec leurs hypothèses ;
  * énergie → prix (IO déjà là, à étendre en multi-période) ;
  * démographie → travail → retraites ;
  * climat → agriculture.
* **Modèles ouverts reconnus plutôt que réinventés**, via le port de modèle externe réservé P8/P9 :
  * émulateurs climatiques (FaIR) ;
  * systèmes énergétiques (PyPSA, OSeMOSYS) ;
  * épidémiologie (SEIR) ;
  * modèles macro multi-agents (des ABM nationaux récents rivalisent avec VAR et DSGE en prévision).
* **Ensembles** : combiner plusieurs modèles et références (les ensembles battent souvent chaque modèle, comme dans
  les hubs de prévision épidémique) et mesurer l'**ambiguïté de modèle** au lieu de la cacher.
* **Décision** : activer `decision.search` avec des objectifs et contraintes explicites, fournis par l'humain et
  jamais inférés. Chaque point de la frontière de Pareto reste conditionnel au modèle.

**Étape D — Validation prospective des décisions (24 mois et plus)**

* Pré-enregistrer des **prévisions conditionnelles** (« si la mesure M est adoptée, Y à 2 ans ∈ […] ») et les noter
  seulement si la condition se réalise, ou sur des expériences naturelles.
* Étapes V3 (intervention), V4 (décision) et V5 (durabilité) du programme de validation
  (`docs/scientific_validation.md`) : toutes **non commencées** aujourd'hui.

### 5.5 Comparaison avec les logiciels de trading et les autres systèmes de prévision

| Système | Ce qu'il prédit | Données | Comment on le juge | REVA en face |
|---|---|---|---|---|
| Trading algorithmique et haute fréquence | micro-mouvements de prix (millisecondes à jours) | ticks, carnets d'ordres, colocation (payants) | Sharpe après coûts ; gains minuscules × volume ; avantages qui s'érodent | **Hors périmètre** : pas de données, pas d'exécution, cadre légal. Ne pas concourir |
| Quant « macro » et factoriel | rendements sur semaines et mois | quotidien, fondamentaux, données alternatives (payantes) | P&L hors échantillon | REVA pourrait au mieux fournir du contexte structurel, **jamais** de signal de trading |
| Météo (ECMWF IFS, modèles IA type GraphCast/GenCast) | atmosphère, 0–15 jours | observations mondiales, réanalyses | scores de compétence contre climatologie, CRPS | REVA n'a pas à refaire la PNT : **consommer** ces prévisions comme entrées |
| Modèles climatiques (CMIP6) | climat selon scénarios | physique | rétro-prévisions, ensembles | consommer CCKP et CMIP6 |
| Banques centrales et FMI (DSGE, semi-structurels, nowcasts GDPNow) | PIB, inflation à 0–2 ans | millésimes temps réel | erreurs publiées | **référence à battre** sur les questions macro |
| Hubs épidémiques (FluSight, COVID Forecast Hub) | cas et hospitalisations, semaines | surveillance | WIS (score d'intervalle pondéré), l'ensemble gagne | modèle de protocole à copier |
| Superprévisionnistes, Metaculus, marchés de prédiction | questions binaires géopolitiques et techniques | jugement humain agrégé | Brier, log-score | **référence à battre** sur les questions événementielles |
| ViEWS (conflits) | morts de conflits mensuelles, 1–36 mois | UCDP et covariables | CRPS, MSE contre persistance | **référence à battre** sur les conflits |
| Prévision par LLM (bancs type ForecastBench) | questions ouvertes | texte | Brier sur questions **résolues après la date de coupure du modèle** | piège : un LLM « connaît » le passé, donc tout back-test LLM sur des questions anciennes est contaminé |

**Benchmark qui montrerait que REVA « bat » les autres.** Il faut :

* un panier **pré-enregistré** (hachés dans le `ledger` avant résolution), large (au moins plusieurs centaines de
  questions par an), **fixé à l'avance** et multi-domaines ;
* des scores propres comparés **question par question** aux références (WEO, SPF, Metaculus, ViEWS, persistance,
  climatologie) ;
* une différence significative (DM ou bootstrap par blocs, correction de Holm) sur **au moins 2 ans** de résolution ;
* une calibration vérifiée ;
* des résultats publiés **même mauvais** (règle déjà inscrite dans `docs/scientific_validation.md`).

Pour la partie « décision » : réussir des **réplications** d'effets publiés et des prévisions conditionnelles
pré-enregistrées.

Pronostic honnête : REVA a une chance réaliste d'**égaler ou d'ajouter de la valeur** sur des questions lentes,
structurelles et multi-domaines, grâce à sa provenance et à son honnêteté sur les manques. Il n'a **aucune chance**
contre le trading sur les prix ni contre la PNT sur la météo.

### 5.6 Risques méthodologiques

* **Surapprentissage** : trop de modèles ou de paramètres pour peu d'observations annuelles. Parade : modèles
  parcimonieux, régularisation, holdout verrouillé (déjà en place).
* **Biais d'anticipation (look-ahead)** : utiliser des données révisées ou publiées plus tard. Parade : millésimes et
  mode `available_then`. Le mode `revised_data_retrospective` est déjà étiqueté comme non temps réel.
* **Biais du survivant** : pays disparus, entreprises radiées, jeux de données qui ont « survécu » parce qu'ils
  étaient intéressants. Parade : garder les États historiques (déjà là) et les entités radiées.
* **Data snooping** et chemins qui bifurquent : tester 1 000 spécifications et garder la meilleure. Parade :
  pré-enregistrement, Holm, SPA ou Reality Check, journal de **toutes** les tentatives.
* **Sélection des questions** : ne garder que les faciles. Parade : panier fixé à l'avance, par tirage.
* **Non-stationnarité** et critique de Lucas : il faut évaluer en continu et retirer les modèles qui dérivent
  (P11 `models.shadow_promotion`, réservé).
* **Contamination par les LLM** : connaissance du futur dans les poids. Il ne faut jamais back-tester un LLM sur des
  périodes antérieures à sa date de coupure.

### 5.7 Notes juridiques et éthiques

* **Aucun conseil financier**, aucune recommandation d'achat ou de vente. Toute sortie touchant des prix doit être
  étiquetée « recherche, pas un conseil ».
* **Pas d'information privilégiée** (MAR). Pas de publication de prévisions de cours destinées à influencer le
  marché. Pas d'usage de données obtenues en violation de conditions d'utilisation.
* **Pas de données personnelles** ni de prédiction individuelle (RGPD, invariant 10). Microdonnées uniquement sous
  convention d'accès, avec agrégation et contrôle de divulgation.
* **Double usage** : publier les prévisions de conflits uniquement au niveau agrégé, sans ciblage.
* Les licences NC bloquent tout usage commercial futur : il faut les recenser avant toute décision de
  commercialisation.

---

## 6. Brainstorming : 20 questions pour le propriétaire, puis les 10 prochaines actions

### 6.1 Vingt questions qui changeraient la feuille de route

1. **But premier** : un atlas explicatif sourcé (« comprendre ») ou un système de prévision noté (« prédire ») ? Les
   deux ne demandent pas les mêmes données.
2. **Quelles décisions précises** voulez-vous éclairer ? Les vôtres (personnelles, d'entreprise), des politiques
   publiques, des investissements ? Donnez 5 exemples concrets.
3. **Horizon** : jours, mois, années ou décennies ? (cf. §5.1 : chaque horizon change tout)
4. **Trois domaines prioritaires** pour les 6 prochains mois, plutôt que les 20 de la V4 en même temps ?
5. **Profondeur ou étendue** : faut-il geler l'extension V4 tant qu'un domaine n'est pas prédictif et validé ?
6. **Usage commercial un jour ?** Si oui, il faut écarter ou isoler les sources NC (IPU, CShapes, JST, CCP, UICN,
   GBD, ACLED).
7. **Budget de données payantes** (Comtrade premium, ACLED commercial, Orbis, données de marché) : oui, non, combien ?
8. **Machine** : accepter un serveur avec 1–2 To de disque et 64 Go de RAM pour lever la contrainte des 6 Go ? C'est
   la condition de GBIF, OpenAlex, OSM, ERA5 agrégé, EDGAR et Wikidata complet.
9. **Comptes à créer** (gratuits mais nominatifs) : Copernicus CDS, ENTSO-E, ACLED, UICN, IHME, OpenAlex, IPUMS,
   Space-Track. Lesquels acceptez-vous ?
10. **Marchés financiers** : à exclure, ou à inclure en données quotidiennes publiques, strictement comme contexte de
    recherche (jamais de signal, jamais de conseil) ?
11. **Granularité spatiale cible** : pays (aujourd'hui), ADM1, ville, ou grille ?
12. **Microdonnées sous convention** (LIS, IPUMS, DHS) : en accepter les contraintes (pas de redistribution,
    contrôle de divulgation) ?
13. **Rôle des LLM** : planificateur et rédacteur seulement, ou prévisionnistes notés comme les autres (avec le
    risque de contamination) ? Faut-il remettre des crédits ?
14. **Qui révise ?** Combien d'heures par semaine pouvez-vous consacrer aux revues (affirmations causales, licences,
    tables WBL) ? Faut-il des experts externes par domaine ?
15. **REVA peut-il estimer des effets lui-même** (économétrie étiquetée « estimation REVA »), ou seulement citer la
    littérature ? C'est un changement de constitution.
16. **Couche « tailles d'effet »** : approuvez-vous le schéma et sa migration (V4CORE est le seul à pouvoir en
    ajouter une) ?
17. **Références à battre** : FMI WEO, SPF, Metaculus, ViEWS, persistance ? Lesquelles comptent pour vous ?
18. **Publier les prévisions** (pré-enregistrement public = crédibilité) ou rester privé ?
19. **Déploiement** maintenant (Render, S3) ou poursuite en local ?
20. **Critère de succès à 6 mois** : par exemple « 300 prévisions pré-enregistrées, dont 100 résolues, score de
    compétence de Brier/CRPS > 0 contre la persistance et ≥ 0 contre le WEO, calibration dans ±5 points » ?

### 6.2 Les 10 prochaines actions, par ordre de priorité

Effort en jours d'agent. « Attente » = temps calendaire dû aux quotas des fournisseurs.

| # | Action | Pourquoi | Effort |
|---|---|---|---|
| 1 | **Remettre la base de dev d'aplomb** : libérer ~9 Go, reconstruire le fichier de requête de l'atlas, matérialiser les liens (câbles), élaguer, `causal refresh` | rien n'est interrogeable proprement sans ce fichier | 0,5 j |
| 2 | **Décision machine et stockage** (question 8), puis migration de l'instantané des données | lève le verrou de la V4 et de Wikidata | décision + 1 j |
| 3 | **Activer le registre prospectif** : panier de 100 questions trimestrielles (macro, population, CO₂, conflits UCDP Candidate, binaires), références persistance et climatologie, job de notation, ajout du **score de Brier** | sans prévisions émises, aucune affirmation de prédiction n'est possible ; l'horloge des résultats démarre | 3–5 j |
| 4 | **Importer des millésimes** : archives FMI WEO, ALFRED, base temps réel OCDE, SPF Fed et BCE | back-tests `available_then` honnêtes et référence forte (WEO) | 3–5 j |
| 5 | **Séries infra-annuelles clés** : IPC, production industrielle et chômage mensuels (FMI, OCDE, Eurostat), changes et taux quotidiens (BCE, FRED), Pink Sheet. Recouvre en partie V4ECON et V4ENERGY | 95,5 % de l'atlas est annuel, ce qui est inutilisable pour un horizon de quelques mois | 4–6 j |
| 6 | **Schéma « estimations d'effet »** (migration) et import de 3 à 5 méta-analyses (multiplicateurs budgétaires, élasticités pétrole, sanctions→commerce, pass-through énergie) | passer des affirmations aux effets quantifiés | 5 j + décision 15/16 |
| 7 | **Moteur d'études d'événements** sur les panels de l'atlas (DiD échelonné, projections locales, contrôle synthétique) avec les événements codés comme traitements ; réplication de 3 résultats publiés | premier effet causal estimé et validé par réplication | 10–15 j |
| 8 | **Récolte Wikidata** des ~109 spécifications restantes et fusion des demandes de propriétés | complète le graphe (personnes, culture, science, économie) | 2 j de travail + 5–10 j d'attente |
| 9 | **Climat et énergie horaire** : Berkeley Earth et ERA5 agrégés par pays et par mois, CCKP CMIP6, ENTSO-E et Ember ; premier adaptateur P8 réel (champ → région) | les deux domaines physiques où la prévision est la mieux fondée et les décisions les plus fréquentes | 8–12 j |
| 10 | **Campagne de revue humaine** : échantillon de 200 affirmations causales, équivalences headline, licences NC | aujourd'hui 0 revue ; la crédibilité dépend de ces revues | 2 h par semaine (propriétaire) + 2 j d'outillage |

**À ne pas faire maintenant** :

* des données de marché haute fréquence ou du trading ;
* la prévision météo maison ;
* toute donnée individuelle ;
* l'extension « encyclopédique » (espace, constantes, gènes) avant d'avoir un premier domaine prédictif noté.
