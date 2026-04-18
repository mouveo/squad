# Squad — Plan 7/N : Hardening pipeline + migration Opus 4.7 1M

> Une session end-to-end sur Sitavista (47 000 tokens de contexte cumulé,
> deepsearch jointe, stack Laravel/React détectée) a échoué en 4 endroits
> distincts du pipeline :
>
> 1. `research_depth` n'a jamais été calculé → benchmark silencieusement
>    skippé (`ValueError: Session … has no research_depth`).
> 2. L'agent `ideation` a produit un markdown dont les angles n'étaient
>    pas parseables → fallback trivial utilisé sans retry.
> 3. L'agent `synthese` n'a pas produit le bloc JSON de contrat → plan
>    generation a crashé avec `No JSON object found in text`, sessions
>    marquée `failed` après ~50 minutes de travail.
> 4. Le contexte cumulatif a dépassé 210 000 caractères à la phase
>    `challenge` (3,5× le budget de 60k) → les agents ont reçu un prompt
>    monstrueux qui les a fait dévier du format attendu (cause racine
>    probable des points 2 et 3).
>
> En parallèle, Squad tourne encore sur `claude-opus-4-6` alors que
> Forge exploite déjà `claude-opus-4-7[1m]` (1M context window). Migrer
> Squad sur le même modèle règle mécaniquement le problème de contexte
> et remonte la qualité d'exécution des agents.
>
> Ce plan traite les 4 bugs d'exécution et la migration modèle en lots
> atomiques. Aucun nouveau fichier fonctionnel côté produit — on
> durcit l'existant.
>
> Prérequis : plans 1 à 6 mergés.

---

## LOT 1 — Migration vers claude-opus-4-7[1m]

Remplace **uniquement** la constante `_MODEL = "claude-opus-4-6"`
dans `squad/executor.py` par `_MODEL = "claude-opus-4-7[1m]"`. La
constante `_MODEL_LIGHT = "claude-sonnet-4-6"` reste inchangée — le
modèle `claude-sonnet-4-7` n'existe pas dans la Claude CLI, la version
la plus récente de sonnet reste 4-6. Le flag `[1m]` est accepté par la
Claude CLI pour activer la fenêtre 1M tokens du modèle Opus 4.7 (testé
manuellement le 2026-04-18 : `claude --model "claude-opus-4-7[1m]"
--print --max-turns 2 "say ok"` retourne `ok`).

Met à jour `DEFAULT_CONFIG_YAML` dans `squad/config.py` pour que la
ligne commentée `# model: claude-opus-4-6` devienne `# model:
claude-opus-4-7[1m]`. Aucune clé obligatoire n'est ajoutée : la config
reste entièrement backward-compatible et la constante reste le défaut.

Met à jour les références dans les tests (`tests/test_executor.py`)
qui assertent `"claude-opus-4-6" in cmd` — elles doivent maintenant
pointer sur `"claude-opus-4-7[1m]"`. Les tests `test_subject_detector.py`
qui référencent `MODEL_LIGHT` ne changent pas.

Aucun changement de comportement produit : même API, seul le modèle
Opus change. Le coût par phase augmente légèrement mais le gain en
fenêtre (8k → 1M) règle tout problème de contexte trop long en aval.

**Success criteria**:
- `squad.executor._MODEL == "claude-opus-4-7[1m]"`
- `squad.executor._MODEL_LIGHT == "claude-sonnet-4-6"` (inchangé)
- Tous les tests existants de `test_executor.py` passent après mise à jour
- `squad serve` démarre sans erreur avec le nouveau modèle Opus
- `grep -r '"claude-opus-4-6"' squad/ tests/` retourne vide
- `DEFAULT_CONFIG_YAML` mentionne `claude-opus-4-7[1m]` dans son commentaire

**Files**: `squad/executor.py`, `squad/config.py`, `tests/test_executor.py`

---

## LOT 2 — Force subject_detector en tête de run_pipeline

Le bug observé : `run_research` lève `ValueError: Session has no
research_depth` parce que `detect_and_persist` n'a jamais été appelé
avant la phase benchmark. Cause probable : lors d'une session démarrée
via Slack (`create_session_from_slack`), la classification n'est pas
déclenchée — seule la CLI `squad start` l'appelle.

Fix : `run_pipeline` vérifie au démarrage que la session a bien un
`research_depth` et un `subject_type` persistés. Si absent, appelle
`detect_and_persist(session_id, use_llm=True, db_path=db_path)`
directement. Si la détection échoue (Claude unreachable, idée vide),
retombe sur le fallback déterministe `default_depth_for_signals(set())`
qui retourne `normal` (fix 4cbe314) — mais **jamais `None`**.

Après cette garantie, `run_research` ne peut plus croiser une session
sans `research_depth` — si ça arrive malgré tout, le guard existant
raise. Ajoute un test dédié qui vérifie l'invariant.

**Success criteria**:
- `run_pipeline` appelle `detect_and_persist` si `session.research_depth` est `None` au démarrage, puis re-lit la session
- Après la détection, `session.research_depth` est garanti non-`None` (au pire `"normal"` via fallback déterministe)
- `run_pipeline` ne lève plus jamais l'exception "has no research_depth" — elle devient logiquement impossible
- Une session Slack (`create_session_from_slack`) suit le même chemin qu'une session CLI
- Nouveau test `test_pipeline.py::test_ensures_research_depth_is_classified_before_phases` avec `detect_and_persist` mocké

**Files**: `squad/pipeline.py`, `tests/test_pipeline.py`

**Depends on**: LOT 1

---

## LOT 3 — Durcir l'agent ideation + parser tolérant + retry

Le bug observé : `parse_angles` n'a trouvé aucun angle parseable dans
la sortie de l'agent → `log.info "no parseable angles — using
fallback"` → pipeline continue avec un angle trivial construit à
partir de l'idée.

Trois leviers de fix :

1. Renforcer le prompt de `agents/ideation.md` avec un exemple concret
   d'output attendu (titre `### Angle N — titre`, bullets
   `- Segment: …`, `- Proposition de valeur: …`, etc.) et un rappel
   explicite que le markdown ASCII-only est requis (pas d'emoji
   décoratif, pas de tableau custom).

2. Rendre `parse_angles` plus tolérant : tolérer titres `## Angle N`
   (h2) en plus de `### Angle N` (h3), tolérer `-` ou `*` en bullet,
   tolérer `Value prop:` ou `Proposition de valeur:` (l'agent peut
   angliciser). Ajoute 3 cas de test.

3. Si la première exécution ne produit toujours aucun angle, `run_ideation`
   fait un **retry unique** avec `phase_instruction=` explicite
   demandant de reformater selon l'exemple en pied de prompt. Le
   `squad.executor.run_agent` supporte déjà `phase_instruction` depuis
   plan 5. Si le retry échoue aussi, on tombe sur le fallback actuel
   mais en **loggant WARNING**, pas INFO — pour que l'opérateur sache
   qu'un angle a été fabriqué.

**Success criteria**:
- `agents/ideation.md` contient une section `## Exemple d'output` avec un angle complet + bloc JSON strategy canonique
- `parse_angles` accepte `## Angle 1 — …` et `### Angle 1 — …` ; accepte `Segment:` en FR comme en EN ; accepte puces `-` et `*`
- Nouveaux tests parse_angles : titre h2, bullets mixés, champs anglicisés
- `run_ideation` retry automatiquement une fois si 0 angle parseable, avec un `phase_instruction` adéquat
- Le fallback trivial log en WARNING (pas INFO) avec la session_id et le nombre de caractères de l'output reçu
- Tests : `test_ideation.py::test_parser_h2_titles`, `::test_parser_mixed_bullets`, `::test_retries_once_on_no_angles`, `::test_fallback_logs_warning`

**Files**: `agents/ideation.md`, `squad/ideation.py`, `tests/test_ideation.py`

**Depends on**: LOT 1

---

## LOT 4 — Durcir l'agent synthese + parser + retry + fail explicite

Le bug observé : la phase synthese a produit un markdown mais sans le
bloc JSON de contrat attendu par `plan_generator.generate_plans_from_session`
→ `ContractError: Could not parse a synthesis contract from the
synthese outputs: No JSON object found in text` → pipeline failed
après 50 min de travail.

Fix en cascade :

1. Renforcer le prompt de `agents/pm.md` section "Phase synthese" (ou
   `agents/synthese.md` si c'est une identité séparée — vérifier à la
   lecture) avec un exemple canonique du bloc JSON de contrat :
   `{"plans": [{"title": "…", "scope": "…", "dependencies": []}, …]}`
   et un rappel que sans ce bloc, la session échoue.

2. `parse_synthesis_contract` dans `squad/phase_contracts.py` tolère
   plusieurs fences (` ```json `, ` ```JSON `, ` ``` `), tolère un bloc
   sans fence si l'output commence par `{` et se termine par `}`.

3. Si la première exécution ne produit pas de JSON parseable,
   `_generate_and_copy_plans` (dans `pipeline.py`) lance un retry
   unique avec un `phase_instruction` explicite : "Ton précédent
   output ne contenait pas le bloc JSON de contrat requis. Reformule
   en terminant strictement par un bloc ```json { \"plans\": [...] }
   ```. Ne change pas le fond, seulement la forme." Si le retry
   échoue aussi, pipeline failed mais avec un output partiel conservé
   en workspace pour inspection.

4. Le message d'erreur posté dans Slack inclut désormais un lien
   cliquable vers le fichier synthese brut dans le workspace pour que
   l'humain puisse récupérer manuellement les recommandations.

**Success criteria**:
- Le prompt de l'agent synthese contient un exemple canonique du bloc JSON
- `parse_synthesis_contract` parse un bloc JSON dans ` ```json `, ` ```JSON `, et sans fence si l'output commence/termine par `{}`
- `_generate_and_copy_plans` retry automatiquement une fois sur `ContractError` avec un `phase_instruction` strict
- Si retry échoue, le pipeline fail proprement avec le chemin du fichier synthese brut dans le message d'erreur
- Nouveaux tests : `test_phase_contracts.py::test_parse_synthesis_no_fence`, `::test_parse_synthesis_uppercase_json_fence` ; `test_pipeline.py::test_plan_generation_retries_once_on_contract_error`

**Files**: `agents/pm.md`, `squad/phase_contracts.py`, `squad/pipeline.py`, `squad/plan_generator.py`, `tests/test_phase_contracts.py`, `tests/test_pipeline.py`

**Depends on**: LOT 1

---

## LOT 5 — Ceiling + résumé adaptatif du contexte cumulatif

Le bug observé : le warning
`Cumulative context for session X / phase Y exceeds target (N > 60000 chars)`
apparaît à partir de la phase `ideation` (80k chars) et atteint 210k
chars en phase `synthese`. Avec l'Opus 4.6 (8k context window), cela
force la Claude CLI à tronquer violemment le prompt en queue — l'agent
voit un prompt incomplet et sort du format. Avec Opus 4.7 1M (LOT 1),
le problème de fenêtre est réglé mais le coût en tokens reste excessif
et la qualité de raisonnement dégrade au-delà de ~100k.

Fix : quand `build_cumulative_context` détecte que la taille va
dépasser le budget (60k par défaut, désormais configurable via
`pipeline.context_budget_chars` dans `config.yaml`), il **résume les
phase outputs les plus anciens** au lieu de les inliner intégralement.

Algorithme proposé (déterministe, sans Claude) :

1. Calcule la taille cumulée par section (idée + contexte projet +
   Q&A + attachments + phase outputs par phase).
2. Si total > budget, identifie les phases les plus anciennes (cadrage,
   etat_des_lieux) et les remplace par un résumé auto-généré :
   premier paragraphe de chaque phase output + liste à puces des
   titres de section `##` trouvés. Log INFO le gain en chars.
3. Si toujours > budget, applique le même traitement aux phases
   suivantes, en gardant toujours intactes les 2 phases les plus
   récentes et les attachments (la deepsearch de l'utilisateur doit
   rester intacte — c'est la source de vérité).
4. Si toujours > budget après compression (cas extrême), log WARNING
   et tronque brutalement en queue avec un marqueur `[… contexte
   tronqué au-delà du budget]`.

Un nouveau test `test_context_builder.py::TestContextCeiling` couvre
les 3 scénarios : sous budget (pas de compression), léger dépassement
(compression des 2 premières phases), dépassement massif (compression
agressive + marqueur de troncature).

**Success criteria**:
- Nouvelle config `pipeline.context_budget_chars` (défaut 60000) lue via `get_config_value`
- `build_cumulative_context` retourne un contexte ≤ `context_budget_chars` pour tous les cas non pathologiques
- La section `Attachments` (deepsearch utilisateur) n'est jamais compressée
- Les 2 phases les plus récentes ne sont jamais compressées
- Log INFO émis pour chaque phase compressée avec le gain en chars
- Log WARNING émis si troncature brutale nécessaire après compression
- Nouveaux tests dans `test_context_builder.py::TestContextCeiling` couvrant les 3 cas
- Les 907+ tests existants passent sans modification

**Files**: `squad/context_builder.py`, `squad/config.py`, `tests/test_context_builder.py`

**Depends on**: LOT 1
