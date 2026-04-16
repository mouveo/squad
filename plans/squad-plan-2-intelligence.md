# Squad — Plan 2/3 : Pipeline d'orchestration multi-agents

> Ce plan implémente le cerveau de Squad : le pipeline en 6 phases,
> la logique de recherche, la génération de plans Forge, et le mode autonome.
>
> Prérequis : Plan 1 (Core) terminé et mergé.

---

## LOT 1 — Pipeline orchestrateur : le séquenceur de phases

Implémenter `squad/pipeline.py` — le chef d'orchestre qui enchaîne les 6 phases.

Le pipeline suit cette séquence :

```
Phase 1 — CADRAGE (séquentiel, PM seul)
  → PM pose les questions essentielles (max 3)
  → Si questions → PAUSE (notify Slack, attendre réponses)
  → Si pas de questions → continue

Phase 2 — ÉTAT DES LIEUX (parallèle)
  → UX : audit du parcours actuel (lit le code du projet)
  → Data : métriques existantes, points de friction mesurables
  → Customer Success : top problèmes/tickets sur le sujet
  → Sales (si B2B) : friction commerciale, objections terrain

Phase 3 — BENCHMARK (séquentiel, agent research)
  → Recherche web : concurrents, patterns UX établis, best practices
  → Produit un rapport structuré dans research/

Phase 4 — CONCEPTION (parallèle)
  → UX : parcours cible, wireframes verbaux, états d'interface
  → Architect : faisabilité technique, impacts, choix d'archi
  → Growth : impact funnel, monétisation, limites d'usage
  → AI Lead (si feature IA détectée) : prompt design, évaluation, coût, fallback

Phase 5 — CHALLENGE (parallèle, agents de contrôle)
  → Security : risques RGPD, permissions, abus
  → Delivery : découpage, testabilité, risques de régression
  → FinOps (via Architect) : coût d'exploitation
  → Chaque agent lit les outputs de Phase 4 et écrit ses OBJECTIONS BLOQUANTES
  → Si objections critiques → retour Phase 4 avec contraintes (1 seul retry)

Phase 6 — SYNTHÈSE (séquentiel, PM reprend)
  → PM lit TOUT (phases 1-5)
  → Produit : résumé décisionnel + plans Forge
  → Si des zones de flou subsistent → PAUSE pour questions finales (max 2)
```

La classe `Pipeline` doit :
- Gérer les transitions d'état (phase → phase)
- Décider quels agents lancer par phase (selon le type de sujet détecté)
- Gérer les PAUSEs (questions → Slack → attente)
- Gérer le retry Phase 4 si Challenge bloquant
- Logger chaque transition dans la DB (phase_outputs)
- Être resumable : si le process s'arrête, `squad resume` reprend à la dernière phase incomplète

Implémenter aussi `squad/phase_config.py` qui définit la configuration de chaque phase :
```python
PHASES = {
    "cadrage": {
        "agents": ["pm"],
        "parallel": False,
        "can_pause": True,  # Questions possibles
        "max_questions": 3,
    },
    "etat_des_lieux": {
        "agents": ["ux", "data", "customer-success", "sales"],
        "parallel": True,
        "can_pause": False,
        "requires_context": True,  # Lit le code du projet
    },
    # ... etc
}
```

**Success criteria**:
- Le pipeline exécute les 6 phases dans l'ordre
- Les phases parallèles lancent bien les agents en parallèle
- Les PAUSEs fonctionnent (status → interviewing, notification Slack)
- Le retry Phase 4 après Challenge fonctionne
- Le pipeline est resumable après un crash ou une pause
- Tests couvrent le flow complet (agents mockés)

**Files**: `squad/pipeline.py`, `squad/phase_config.py`, `tests/test_pipeline.py`

---

## LOT 2 — Détection intelligente du sujet et sélection des agents

Tous les agents ne sont pas pertinents pour tous les sujets. Implémenter `squad/subject_detector.py`.

Avant de lancer le pipeline, Squad analyse l'idée + le contexte projet pour déterminer :

1. **Type de sujet** : feature_new | feature_improve | ux_redesign | integration | ai_feature | performance | refactor | pricing | onboarding
2. **Agents pertinents par phase** : quels agents activer/désactiver
3. **Profondeur de recherche** : light (pas de benchmark externe) | normal | deep (benchmark + analyse concurrentielle poussée)

Règles de sélection :
- `sales` activé seulement si le projet est B2B ET le sujet touche au CRM, pricing, onboarding, ou pipeline commercial
- `ai-lead` activé seulement si l'idée mentionne IA, ML, LLM, génération, suggestion automatique, ou si le projet a des dépendances AI (détecté dans composer.json/package.json)
- `data` toujours activé (chaque feature doit être mesurable)
- `security` toujours activé en challenge (garde-fou obligatoire)
- Si sujet = `pricing` ou `onboarding` → activer `growth` en profil principal (pas secondaire)

L'implémentation utilise Claude Code CLI lui-même pour classifier le sujet : un appel rapide (Haiku/Sonnet) avec l'idée + le contexte, qui retourne un JSON structuré avec le type, les agents, et la profondeur.

**Success criteria**:
- Classification correcte sur 5 cas de test variés
- Le JSON de sortie est parsé et utilisé par le pipeline
- Tests unitaires avec des idées de différents types

**Files**: `squad/subject_detector.py`, `tests/test_subject_detector.py`

**Depends on**: LOT 1

---

## LOT 3 — Agent Research : benchmark concurrentiel et veille

Implémenter l'agent de recherche spécialisé dans `squad/research.py`.

Cet agent est différent des autres : il ne suit pas un profil fixe mais un PROTOCOLE DE RECHERCHE :

1. **Cadrage recherche** : à partir de l'idée + output PM, identifier 3-5 axes de recherche (ex: "CRM pour SaaS B2B petites équipes", "alternatives à Pipedrive pour PME", "UX patterns gestion de leads")
2. **Recherche itérative** : pour chaque axe, faire 2-3 recherches web (web_search), lire les pages pertinentes (web_fetch), extraire les insights
3. **Synthèse** : produire un rapport structuré :

```markdown
# Benchmark — {sujet}

## Concurrents identifiés
| Produit | Positionnement | Ce qu'ils font bien | Ce qu'ils font mal | Prix |
|---------|---------------|---------------------|--------------------| -----|

## Patterns UX observés
- Pattern 1 : description + qui l'utilise
- Pattern 2 : ...

## Tendances du marché
- ...

## Opportunités de différenciation
- ...

## Sources consultées
- [url] — résumé
```

L'agent utilise `--allowedTools "WebSearch,WebFetch,Read"` et a un budget de 10-15 recherches web max (pour contrôler le coût).

Le rapport est stocké dans `research/benchmark-{sujet_slug}.md` du workspace.

**Success criteria**:
- L'agent produit un rapport structuré et substantiel
- Les sources sont citées
- Le budget de recherches est respecté
- Le rapport est lisible et actionnable par les agents suivants
- Test d'intégration sur un vrai sujet (ex: "améliorer le CRM")

**Files**: `squad/research.py`, `tests/test_research.py`

**Depends on**: LOT 1

---

## LOT 4 — Construction des prompts avec contexte cumulatif

Améliorer `squad/executor.py` pour que chaque agent reçoive le bon contexte cumulatif.

Le prompt d'un agent en Phase N doit contenir :
1. Son system prompt (depuis `agents/{agent}.md`)
2. Le contexte projet (CLAUDE.md du projet cible)
3. L'idée originale
4. Les réponses aux questions (si la phase cadrage a eu lieu)
5. **Les outputs de TOUTES les phases précédentes** (pas juste la phase N-1)
6. La consigne spécifique de ce qu'on attend de lui dans cette phase

Le contexte cumulatif doit être construit intelligemment pour ne pas exploser la fenêtre de contexte :
- Phase 1 outputs : inclure en entier (c'est le cadrage, c'est court)
- Phase 2 outputs : inclure en entier (état des lieux)
- Phase 3 (benchmark) : inclure le résumé exécutif + tableau concurrents (pas le rapport complet)
- Phase 4 outputs : inclure en entier pour Phase 5 (le challenge doit tout lire)
- Phase 5 outputs : inclure en entier pour Phase 6 (la synthèse doit tout lire)

Implémenter `squad/context_builder.py` :
- `build_cumulative_context(session_id, current_phase)` : construit le contexte complet
- `summarize_research(research_content, max_tokens=2000)` : résume le benchmark si trop long
- `format_qa(questions_and_answers)` : formate les Q&A proprement

**Success criteria**:
- Le contexte cumulatif est correct à chaque phase
- Le benchmark est résumé si trop long
- Le prompt final ne dépasse pas ~15000 tokens de contexte (hors system prompt)
- Tests vérifient le contenu du prompt à chaque phase

**Files**: `squad/context_builder.py`, `tests/test_context_builder.py`

**Depends on**: LOT 1

---

## LOT 5 — Génération des plans Forge

Implémenter `squad/plan_generator.py` — transforme la synthèse PM (Phase 6) en plans Forge.

Le PM en Phase 6 produit un résumé décisionnel. Le plan_generator le transforme en 1 ou plusieurs plans Forge valides.

Règles :
1. Un plan Forge = 5-15 lots, chaque lot = une responsabilité atomique
2. Si le scope dépasse 15 lots → découper en 2-3 plans (avec dépendances séquentielles pour la queue Forge)
3. Chaque lot suit le format Forge exact :
   ```markdown
   ## LOT N — Titre court impératif

   Description détaillée de ce qu'il faut faire. Contexte métier issu
   de l'analyse. Pattern ou approche à suivre si identifié.

   **Success criteria**:
   - Critère 1
   - Critère 2

   **Files**: `app/...`, `tests/...`

   **Depends on**: LOT X (si applicable)
   ```
4. Le premier lot doit être le plus simple (valider le setup)
5. Les tests sont dans le même lot que la feature (ou le lot suivant)
6. Pas de HOW (Claude décide de l'implémentation), mais le QUOI est très précis
7. Les fichiers cibles sont listés quand on peut les anticiper

L'implémentation utilise Claude (Opus) pour transformer la synthèse en plans. Le prompt inclut :
- Le QUICK-REFERENCE.md de Forge (embarqué dans le skill, pas lu dynamiquement)
- Le résumé décisionnel de Phase 6
- Les contraintes techniques de Phase 5 (challenge)
- Le contexte projet (stack, conventions)

Chaque plan est écrit dans `workspace/plans/plan-N-{titre_slug}.md` ET copié dans `{project_path}/plans/` (où Forge les attend).

**Success criteria**:
- Les plans générés sont valides pour Forge (header format, lots numérotés)
- 5-15 lots par plan
- Les lots sont atomiques et buildables
- Le contenu est spécifique au projet (pas générique)
- Les fichiers cibles sont cohérents avec la stack du projet
- Test de validation du format Forge

**Files**: `squad/plan_generator.py`, `squad/forge_format.py`, `tests/test_plan_generator.py`

**Depends on**: LOT 4

---

## LOT 6 — Mode autonome et intégration Forge queue

Implémenter `squad/forge_bridge.py` — le pont entre Squad et Forge.

Deux modes de sortie :

**Mode approval (défaut)** :
1. Plans générés → notification Slack "📋 Plans prêts, `squad review` pour valider"
2. L'utilisateur lance `squad review {session_id}`
3. Les plans sont affichés un par un en CLI
4. Pour chaque plan : approve / reject / edit (ouvre $EDITOR)
5. Les plans approuvés sont envoyés à Forge : `forge queue add <project> <plan.md>`
6. Notification Slack "🚀 Envoyé à Forge"

**Mode autonomous** :
1. Plans générés → envoyés directement à Forge sans attendre
2. `forge queue add <project> <plan.md> --no-auto-apply` (on laisse Codex review)
3. Puis `forge queue run <project> --stop-on-failure`
4. Notification Slack "🚀 Mode autonome : {n} plans envoyés et queue lancée"

Implémenter :
- `send_to_forge(project_path, plan_paths, auto_run=False)` : envoie les plans dans la queue
- `check_forge_available(project_path)` : vérifie que Forge est installé et accessible
- `get_forge_queue_status(project_path)` : vérifie si la queue est libre ou occupée

Gestion d'erreur : si Forge n'est pas installé ou si la queue est occupée, notifier Slack et passer en mode approval automatiquement.

**Success criteria**:
- Mode approval fonctionne bout en bout (review CLI + envoi Forge)
- Mode autonomous fonctionne bout en bout
- Fallback si Forge indisponible
- Les plans sont bien copiés dans `{project_path}/plans/`
- Tests couvrent les deux modes (Forge CLI mocké)

**Files**: `squad/forge_bridge.py`, `tests/test_forge_bridge.py`

**Depends on**: LOT 5

---

## LOT 7 — Gestion des interruptions et reprise de session

Le flow Squad est asynchrone : l'utilisateur peut répondre aux questions des heures après. Implémenter la logique de reprise robuste.

Cas à gérer :

1. **Pause pour questions (Phase 1 ou 6)** :
   - Status → `interviewing`
   - Questions stockées en DB et dans `questions/pending.json`
   - `squad answer` → enregistre réponses → `squad resume` → reprend à la phase suivante

2. **Crash mid-pipeline** :
   - `squad resume` détecte la dernière phase complétée (via phase_outputs en DB)
   - Reprend à la phase suivante
   - Les agents de la phase incomplète sont relancés

3. **Retry après challenge bloquant (Phase 5 → retour Phase 4)** :
   - Le pipeline détecte les objections marquées "BLOQUANT" dans les outputs Phase 5
   - Il relance Phase 4 avec les objections comme contraintes additionnelles
   - Maximum 1 retry (si Phase 5 bloque encore → inclure les objections dans le plan avec un warning)

4. **Timeout** :
   - Si un agent ne répond pas en 15 min → marquer comme failed
   - Si un agent non-critique fail → continuer sans lui (log warning)
   - Si le PM fail → session failed (impossible de continuer sans cadrage)

Implémenter `squad/recovery.py` :
- `get_resume_point(session_id)` : détermine où reprendre
- `handle_challenge_blockers(session_id)` : détecte et gère les objections bloquantes
- `is_critical_agent(agent_name, phase)` : détermine si un agent est critique pour cette phase

**Success criteria**:
- Reprise après pause fonctionne
- Reprise après crash fonctionne
- Le retry Phase 4 après challenge fonctionne
- Les agents non-critiques ne bloquent pas le pipeline
- Tests couvrent les 4 cas

**Files**: `squad/recovery.py`, `tests/test_recovery.py`

**Depends on**: LOT 1

