# Squad — Plan 6/N : Phase ideation divergente + gate de richesse d'input

> Squad produit aujourd'hui un plan unique à partir d'une idée. Les 6
> phases actuelles convergent immédiatement : pas d'étape d'exploration
> divergente où le pipeline teste plusieurs angles contrastés (segments
> différents, architectures différentes, propositions de valeur
> différentes) avant de choisir. Résultat : les plans sont compétents
> mais rarement ambitieux.
>
> Ce plan ajoute une **phase ideation** entre `etat_des_lieux` et
> `benchmark` qui génère 3 à 5 angles distincts informés par
> l'exploration active du projet (plan 5). Un **gate de décision**
> stratégie choisit automatiquement :
>
> * **`auto_pick`** quand l'input utilisateur est déjà riche (prompt
>   détaillé + deepsearch jointe) OU quand les angles sont peu
>   divergents → Squad sélectionne le meilleur angle sans demander.
> * **`ask_user`** quand les angles divergent fortement et que l'input
>   est pauvre → Squad pause le pipeline et poste les angles dans le
>   thread Slack avec des boutons "Garder A / B / C / D / E" + "Tout
>   benchmarker en parallèle".
>
> Un **détecteur de richesse d'input** précède l'ideation et influence
> le gate : pas question de refaire la roue si le PO a déjà fourni un
> prompt détaillé avec une deepsearch en pièce jointe.
>
> Parallel benchmarking multi-angles n'est **pas couvert par ce plan**
> — il sera traité dans un plan ultérieur. Le bouton "Tout
> benchmarker" est présent dans l'UI pour la forme mais, dans cette
> livraison, il sélectionne simplement tous les angles et le benchmark
> couvre chaque axe séquentiellement dans un seul output (comme le
> deep-research skill le fait déjà sur plusieurs axes).
>
> Prérequis : plans 1 à 5 complets. Le plan 5 est un vrai prérequis —
> l'ideation exploite l'exploration active via ux en `etat_des_lieux`.

---

## LOT 1 — Constantes, modèles, migration DB

Ajoute la phase `ideation` dans `squad/constants.py` :
`PHASE_IDEATION = "ideation"` et insère-la dans `PHASES` entre
`PHASE_ETAT_DES_LIEUX` et `PHASE_BENCHMARK`. `PHASE_LABELS` et
`PHASE_DIRS` sont complétés (`ideation` → `03-ideation` par exemple,
décale les suivants si nécessaire). Les tests qui itèrent sur `PHASES`
verront maintenant 7 phases ; mets à jour les asserts concernés pour
qu'ils s'appuient sur `PHASES` (longueur dynamique), pas sur des
constantes hardcodées.

Dans `squad/models.py` : ajoute deux champs optionnels sur `Session` :
`input_richness: Literal["sparse", "rich"] | None = None` et
`selected_angle_idx: int | None = None`. Ajoute un nouveau dataclass
`IdeationAngle(session_id, idx, title, segment, value_prop, approach,
divergence_note, created_at)` et l'expose dans `__all__`.

Migration DB additive dans `ensure_schema` (`squad/db.py`, pattern
`add_column` existant) : colonnes `input_richness TEXT` et
`selected_angle_idx INTEGER` sur `sessions` ; nouvelle table
`ideation_angles` (clé composite session_id+idx, colonnes title,
segment, value_prop, approach, divergence_note, created_at). Helpers
CRUD : `persist_ideation_angle`, `list_ideation_angles(session_id)`,
`set_selected_angle(session_id, idx)`, `update_input_richness(session_id, value)`.

**Success criteria**:
- `PHASES` contient `ideation` entre `etat_des_lieux` et `benchmark` ; sa position est 3 (0-indexed)
- `tests/test_constants.py` (ou le fichier équivalent) valide l'ordre complet des 7 phases
- La table `ideation_angles` existe après `ensure_schema` ; insert + list round-trip fonctionnent
- `Session.input_richness` et `Session.selected_angle_idx` sont persistés et relus correctement
- La migration est rétrocompatible : une DB pré-existante (sans les colonnes/table) passe `ensure_schema` sans erreur et les colonnes sont ajoutées
- Tous les tests existants passent sans modification (sauf ceux qui asserent sur `len(PHASES)` ou l'ordre explicite — à mettre à jour chirurgicalement)

**Files**: `squad/constants.py`, `squad/models.py`, `squad/db.py`, `tests/test_db.py`, `tests/test_models.py`, `tests/test_constants.py` (crée si absent)

---

## LOT 2 — Phase config + insertion dans le pipeline

Ajoute `PHASE_IDEATION` dans `squad/phase_config.py::PHASE_CONFIGS` :
`order=3`, `default_agents=("ideation",)`, `critical_agents=()`
(l'ideation est non-critique : un échec ne doit pas bloquer le
pipeline — on retombe sur un angle trivial par défaut), `parallel=False`,
`can_pause=True` (c'est le point de pause pour `ask_user`),
`max_questions=0`, `retry_policy=RetryPolicy(max_attempts=1)`,
`skip_policy=SkipPolicy(skippable=True, skip_when_depth=("light",))`.

Dans `squad/pipeline.py`, le dispatcher de phase doit router le cas
`PHASE_IDEATION + agent == "ideation"` vers une nouvelle fonction
`squad.ideation.run_ideation(session_id, db_path=...)` (comme ce qui
est fait pour `benchmark → run_research`). Cette fonction n'existe pas
encore — le LOT 3 la crée. Ce LOT pose seulement le câblage : un stub
est acceptable si le LOT 3 le remplit.

**Success criteria**:
- `PHASE_CONFIGS[PHASE_IDEATION]` existe avec les valeurs ci-dessus
- L'ordre numérique `order` est cohérent : etat_des_lieux=2, ideation=3, benchmark=4, conception=5, etc.
- Le pipeline, quand il atteint `PHASE_IDEATION`, appelle `run_ideation` (vérifiable par patching dans un test)
- Un échec de `run_ideation` n'interrompt pas le pipeline (research non-critique, fallback sur un angle "trivial" = idée telle quelle)
- Les tests de phase orchestration existants passent sans modification

**Files**: `squad/phase_config.py`, `squad/pipeline.py`, `tests/test_phase_config.py`, `tests/test_pipeline.py`

**Depends on**: LOT 1

---

## LOT 3 — Agent ideation + service `run_ideation`

Crée `agents/ideation.md` (nouveau fichier). Structure identique aux
autres agents mais capabilities : `read_files: oui`, `web_search: oui`
(pour challenger les angles avec un quick check marché),
`glob: oui, list_files: oui, grep_files: oui` (hérite du plan 5 pour
fouiller le projet cible avant de proposer des angles).

Prompt de l'agent : produire 3 à 5 angles **distincts** (divergence
explicite sur au moins un axe : segment cible, proposition de valeur,
ou architecture/approche). Pour chaque angle : titre, segment, value
prop, approche technique, note sur ce qui le distingue. Puis un bloc
JSON strategy contract :

```json
{
  "strategy": "auto_pick" | "ask_user",
  "best_angle_idx": <int>,
  "rationale": "<2-3 phrases expliquant le choix de stratégie>",
  "divergence_score": "low" | "medium" | "high"
}
```

Crée `squad/ideation.py` (nouveau module) avec :
* `run_ideation(session_id, db_path=None, extra_context=None) -> IdeationResult` : invoque l'agent via `run_agent` (avec le `cwd` de LOT 3 du plan 5), parse le markdown en angles + strategy dict, persiste les angles en DB, retourne un dataclass `IdeationResult(content, angles, strategy_dict)`.
* `parse_angles(markdown: str) -> list[IdeationAngle]` : parseur regex robuste des blocs `### Angle N — <titre>` avec leurs champs. Tolérant aux variations de formatting.
* `parse_strategy(markdown: str) -> dict` : extrait le bloc JSON de strategy, valide les champs, fallback `{"strategy": "ask_user", "best_angle_idx": 0, "divergence_score": "medium"}` sur échec.

**Success criteria**:
- `agents/ideation.md` existe, contient les 6 sections attendues (Identité, Mission, Réflexes, Questions clés, Livrable attendu, Outils autorisés) et déclare les capabilities listées
- `parse_angles` retourne 3 à 5 `IdeationAngle` pour un markdown bien formé ; retourne liste vide pour un markdown vide ; tolère les titres avec caractères spéciaux
- `parse_strategy` retourne un dict avec les 4 clés attendues ; applique le fallback sur JSON invalide
- `run_ideation(session_id)` : invoque `run_agent("ideation", ...)` avec `cwd=session.project_path`, persiste les angles, retourne le résultat
- Tests : `test_ideation.py` couvre parse_angles (3 cas : nominal, vide, titres spéciaux), parse_strategy (3 cas : nominal, JSON invalide, clés manquantes), run_ideation (1 cas avec `run_agent` mocké)

**Files**: `agents/ideation.md`, `squad/ideation.py`, `tests/test_ideation.py`

**Depends on**: LOT 1, LOT 2, plan 5 mergé

---

## LOT 4 — Détecteur de richesse d'input

Nouveau module `squad/input_richness.py` avec la fonction
`score_input_richness(session_id, db_path) -> Literal["sparse", "rich"]`.

Inputs considérés :
* Longueur de `session.idea` (seuil : > 300 caractères = point positif)
* Taille de `CLAUDE.md` du projet cible (seuil : > 1000 chars = point positif)
* Attachments Slack (via `attachment_service.list_attachments`) :
  présence d'au moins un fichier `.md` ou `.pdf` > 3000 chars = point
  positif fort (la deepsearch typique fait > 10k chars)

Barème : l'input est `rich` si au moins **2 points** sont positifs,
dont au moins 1 attachment ou une idée > 500 chars. Sinon `sparse`.
Seuils déclarés comme constantes en tête de module pour ajustement
facile.

Appelé au démarrage du pipeline (dans `run_pipeline` juste après la
classification de sujet) pour persister `input_richness` sur la
session. Le gate du LOT 5 le relit.

**Success criteria**:
- `score_input_richness` retourne `"rich"` pour (idée 500 chars + 1 attachment de 10k chars)
- Retourne `"sparse"` pour (idée 3 mots, 0 attachment, pas de CLAUDE.md)
- Retourne `"rich"` pour (idée 200 chars + attachment 15k chars) — l'attachment seul suffit
- Retourne `"sparse"` pour (idée 800 chars sans attachment et sans CLAUDE.md côté projet)
- `run_pipeline` persiste la valeur via `update_input_richness` avant le premier phase run
- 6 cas testés dans `tests/test_input_richness.py` (2×sparse, 4×rich couvrant chaque combinaison)

**Files**: `squad/input_richness.py`, `squad/pipeline.py`, `tests/test_input_richness.py`

**Depends on**: LOT 1

---

## LOT 5 — Gate de décision stratégie dans le pipeline

Dans `squad/pipeline.py`, après l'exécution de `run_ideation`, un nouveau
helper `_resolve_ideation_strategy(session, strategy_dict)` détermine
la suite :

1. Si `session.input_richness == "rich"` → override à `"auto_pick"`
   quelle que soit la reco de Claude (économie de tokens, priorité au
   PO qui a déjà bossé).
2. Sinon, on suit la reco Claude :
   * `"auto_pick"` → persiste `best_angle_idx` comme `selected_angle_idx`, continue le pipeline vers benchmark.
   * `"ask_user"` → met le status en `interviewing` avec une nouvelle
     sous-status `awaiting_angle_choice`, poste les angles dans le
     thread Slack (LOT 6 câble la partie Slack), pause le pipeline.

Quand le pipeline reprend après le choix user (persisté via LOT 6), le
helper est re-appelé et continue en mode `auto_pick` sur l'angle
choisi.

**Success criteria**:
- `_resolve_ideation_strategy(richness="rich", dict={"strategy": "ask_user", ...})` force `"auto_pick"` et persiste `best_angle_idx`
- `_resolve_ideation_strategy(richness="sparse", dict={"strategy": "ask_user", ...})` met le status en `interviewing` avec `awaiting_angle_choice`
- `_resolve_ideation_strategy(richness="sparse", dict={"strategy": "auto_pick", ...})` persiste `best_angle_idx` et continue
- Si `session.selected_angle_idx` est déjà set (reprise après user choice), le helper saute la décision et continue
- Fallback si `strategy_dict` est malformé ou `best_angle_idx` hors bornes : log warning + `auto_pick` sur l'angle 0
- Tests : 5 cas dans `test_pipeline.py::TestIdeationStrategyGate`

**Files**: `squad/pipeline.py`, `squad/db.py`, `squad/models.py`, `tests/test_pipeline.py`

**Depends on**: LOT 3, LOT 4

---

## LOT 6 — Intégration Slack : pause + boutons de choix d'angle

Quand le pipeline entre en `awaiting_angle_choice`, poste dans le
thread Slack un message structuré via Block Kit contenant :

* Un header `:sparkles: Choisir un angle pour la suite`
* Pour chaque angle (3 à 5) un bloc `section` avec le titre +
  segment + value prop (value prop tronquée à 200 chars)
* Un bloc `actions` avec N boutons "Garder cet angle"
  (`action_id=pick_angle:{session_id}:{idx}`) + un bouton secondaire
  "Tout benchmarker en parallèle"
  (`action_id=pick_all_angles:{session_id}`).

Nouveaux handlers Bolt dans `squad/slack_handlers.py` :

* `handle_pick_angle(ack, body, client)` : persiste
  `selected_angle_idx`, met à jour le message (boutons grisés, badge
  "Angle <idx> sélectionné"), appelle `resume_pipeline(session_id)`.
* `handle_pick_all_angles` : dans cette livraison, sélectionne
  simplement l'angle 0 et persiste un flag `benchmark_all_angles=True`
  (colonne ajoutée dans LOT 1) pour que le benchmark prompt couvre
  tous les axes. Pas de vrai parallélisme multi-conceptions.

Idempotence : second clic sur un bouton → le handler détecte que
`selected_angle_idx` est déjà set et no-op avec ack.

**Success criteria**:
- `post_angles_for_review` construit le Block Kit avec N angles + N+1 boutons corrects
- Un clic sur "pick_angle:<session>:2" persiste `selected_angle_idx=2` et appelle `resume_pipeline`
- Le message Slack est mis à jour après clic (boutons désactivés + badge)
- Un 2e clic sur n'importe quel bouton du même message est ignoré (pas de double-resume)
- Clic sur "pick_all_angles" persiste `selected_angle_idx=0` et `benchmark_all_angles=True`
- Tests `test_slack_handlers.py::TestAngleChoice` : 4 cas (pick_angle, pick_all, idempotence, action_id malformé)

**Files**: `squad/slack_service.py`, `squad/slack_handlers.py`, `squad/db.py`, `squad/models.py`, `tests/test_slack_handlers.py`, `tests/test_slack_service.py`

**Depends on**: LOT 5

---

## LOT 7 — Context forwarding vers benchmark et ajustement du prompt benchmark

Deux raccordements aval de l'ideation :

1. `squad/context_builder.py` : quand `session.selected_angle_idx` est
   set, injecte une section `## Angle choisi` dans le contexte
   cumulatif pour toutes les phases aval (benchmark, conception,
   challenge, synthese). Contenu : titre + segment + value prop +
   approach + divergence_note de l'angle sélectionné. Si
   `benchmark_all_angles=True`, la section contient les 3-5 angles
   concaténés sous le titre `## Angles à benchmarker`.

2. `squad/research.py` : le prompt benchmark est amendé pour ajouter,
   quand `session.input_richness == "rich"`, une directive explicite :
   > "L'utilisateur a déjà fourni un contexte riche (idée détaillée
   > et/ou documents joints). Ton job n'est pas de refaire une
   > recherche généraliste sur ce sujet mais de **combler les angles
   > morts** : citations sourcées sur les points non couverts,
   > vérification des chiffres avancés, identification d'alternatives
   > non mentionnées. Ne ré-exécute pas de WebSearch sur les thèmes
   > déjà traités dans les pièces jointes."

Quand `benchmark_all_angles=True`, le prompt mentionne explicitement
que les N angles doivent tous être couverts dans le rapport (sans
doublonner la recherche sur les axes communs).

**Success criteria**:
- `build_cumulative_context(session_id, phase="benchmark")` contient la section `## Angle choisi` quand `selected_angle_idx` est set
- La même section est absente quand `selected_angle_idx` est `None`
- Quand `benchmark_all_angles=True`, la section s'appelle `## Angles à benchmarker` et liste tous les angles
- Le prompt benchmark contient la directive "combler les angles morts" quand `input_richness=rich` ; absente quand `sparse`
- Intégration end-to-end : pipeline complet avec input riche → ideation `auto_pick` → benchmark reçoit `cwd`, `--allowedTools` complet, contexte angle choisi, directive "angles morts"
- 4 nouveaux tests (`test_context_builder.py` + `test_research.py`)
- `pytest -m "not integration"` : 100 % pass

**Files**: `squad/context_builder.py`, `squad/research.py`, `tests/test_context_builder.py`, `tests/test_research.py`

**Depends on**: LOT 5, LOT 6
