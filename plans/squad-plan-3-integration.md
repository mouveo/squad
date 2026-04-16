# Squad — Plan 3/3 : Intégration, configuration et test réel

> Ce plan finalise Squad : configuration multi-projets, skill de recherche avancée,
> end-to-end test sur un vrai sujet, et documentation.
>
> Prérequis : Plans 1 et 2 terminés et mergés.

---

## LOT 1 — Configuration YAML multi-projets

Implémenter le système de configuration dans `squad/config.py`.

Fichier de config global : `~/.squad/config.yaml`

```yaml
# Configuration globale Squad
defaults:
  mode: approval              # approval | autonomous
  model: claude-opus-4-6      # modèle pour les agents
  model_light: claude-sonnet-4-6  # modèle pour classification, résumés
  max_questions_per_pause: 3
  max_research_searches: 15
  agent_timeout_seconds: 900  # 15 min
  parallel_agents_max: 4      # max agents en parallèle

notifications:
  slack_webhook: ${FORGE_SLACK_WEBHOOK}  # réutilise Forge par défaut
  notify_on: [questions, plans_ready, forge_queued, error]

forge:
  auto_apply_review: true     # laisser Codex review les plans
  auto_merge: true
  stop_on_failure: true

agents:
  # Permettre de désactiver globalement certains agents
  disabled: []
  # Ou de forcer certains agents sur tous les projets
  always_on: [pm, ux, architect, security, delivery, data]
```

Fichier de config projet (optionnel) : `{project_path}/.squad/config.yaml`

```yaml
# Surcharges spécifiques au projet
project:
  name: Sitavista
  type: saas_b2b              # saas_b2b | saas_b2c | marketplace | tool | api
  stack: laravel_filament     # pour aider la détection de fichiers
  
agents:
  always_on: [sales, ai-lead]  # activer sales et ai-lead pour ce projet B2B avec IA
  
defaults:
  mode: autonomous             # ce projet est en début de vie, on itère vite
```

La config projet surcharge la config globale (merge profond).

Implémenter :
- `load_config(project_path=None)` : charge global + projet, merge, résout les variables d'env
- `get_config_value(key, project_path=None)` : accès simple avec dot notation ("forge.auto_merge")
- Créer `squad init` comme commande CLI qui génère le `~/.squad/config.yaml` avec les valeurs par défaut

**Success criteria**:
- Config globale chargée correctement
- Config projet surcharge la globale
- Variables d'environnement résolues
- `squad init` génère un fichier de config commenté
- Tests couvrent merge et surcharges

**Files**: `squad/config.py`, `tests/test_config.py`

---

## LOT 2 — Skill de deep research intégré

Créer un skill de recherche structurée que l'agent Research utilise. Ce n'est PAS un agent Squad, c'est un SKILL Claude Code que l'agent Research invoque.

Créer `skills/deep-research/SKILL.md` :

```markdown
# Deep Research Skill for Squad

## Protocole
1. Recevoir les axes de recherche (3-5 mots-clés ou questions)
2. Pour chaque axe :
   a. Recherche web (2-3 requêtes courtes, 1-6 mots)
   b. Lire les 2-3 pages les plus pertinentes (web_fetch)
   c. Extraire : produit, positionnement, prix, forces, faiblesses
3. Croiser les sources (ne pas se fier à une seule)
4. Produire le rapport structuré

## Contraintes
- Max 15 recherches web au total
- Max 10 pages lues au total
- Toujours citer les URLs sources
- Distinguer FAIT (trouvé dans une source) et HYPOTHÈSE (extrapolé)
- Format de sortie : markdown structuré avec tableaux

## Format de sortie
{le template du benchmark défini dans Plan 2 LOT 3}
```

Installer le skill dans `~/.claude/skills/squad-research/` pour qu'il soit accessible par Claude Code CLI quand l'agent Research tourne.

**Success criteria**:
- Le skill est installable via un script `install-skills.sh`
- L'agent Research produit un benchmark plus structuré et fiable avec le skill
- Les limites de recherche sont respectées
- Test : lancer une recherche sur un sujet connu et vérifier la qualité

**Files**: `skills/deep-research/SKILL.md`, `scripts/install-skills.sh`

**Depends on**: Plan 2 LOT 3

---

## LOT 3 — Template de plan Forge avec instructions embarquées

Créer le template que le plan_generator utilise pour produire des plans conformes.

Fichier `templates/forge-plan.md` :

```markdown
# {title}

> Généré par Squad — Session {session_id}
> Projet : {project_name} ({project_path})
> Date : {date}
> Sujet : {idea}
>
> Contexte : {résumé_1_ligne_du_cadrage}

---

## LOT 1 — {titre}

{description}

**Success criteria**:
{critères}

**Files**: {fichiers}

---

{lots suivants}
```

Implémenter aussi `squad/forge_format.py` (référencé dans Plan 2 LOT 5) avec :
- `validate_plan(plan_content)` : vérifie que le plan est valide Forge (regex sur headers, lots numérotés séquentiellement, critères de succès présents)
- `split_plan_if_needed(plan_content, max_lots=15)` : découpe un plan trop long en plusieurs plans avec indication de dépendance
- `inject_project_context(plan_content, project_path)` : ajoute dans chaque lot une référence au CLAUDE.md si pertinent

**Success criteria**:
- Le template produit des plans valides pour Forge
- La validation détecte les plans mal formés
- Le split fonctionne correctement (dépendances entre plans)
- Tests avec des plans valides et invalides

**Files**: `templates/forge-plan.md`, `squad/forge_format.py`, `tests/test_forge_format.py`

---

## LOT 4 — Commande `squad run` : one-shot complet

Ajouter une commande `squad run` qui combine `start` + attente + `resume` + `approve` en mode interactif continu.

```bash
squad run ~/Developer/sitavista "améliorer le CRM : gestion des leads, pipeline, scoring"
```

Comportement :
1. Crée la session et lance le pipeline
2. Si pause pour questions → les affiche directement dans le terminal (pas besoin de `squad answer` séparé)
3. L'utilisateur répond inline
4. Le pipeline reprend automatiquement
5. À la fin, affiche les plans et demande validation inline
6. Si approuvé → envoie à Forge
7. Si mode autonomous dans la config → skip les questions (le PM les formule comme hypothèses) et skip la validation

C'est le raccourci pour le cas où l'utilisateur est devant son terminal et veut tout faire en une fois. Les commandes séparées (`start`, `answer`, `resume`, `approve`) restent pour le mode asynchrone.

**Success criteria**:
- `squad run` fonctionne de bout en bout en mode interactif
- Les questions s'affichent inline et les réponses sont capturées
- La validation des plans fonctionne inline
- Le mode autonomous skip correctement les interactions
- Tests d'intégration (agents mockés)

**Files**: `squad/cli.py` (ajout commande run), `tests/test_cli_run.py`

**Depends on**: LOT 1, LOT 3

---

## LOT 5 — Test end-to-end réel : "Améliorer le CRM Sitavista"

Ce lot n'est PAS du code à écrire. C'est un TEST RÉEL du système complet.

Exécuter :
```bash
squad run ~/Developer/sitavista "améliorer le CRM : gestion des leads, pipeline de vente, scoring automatique, relances"
```

Vérifier :
1. Phase 1 (Cadrage) : le PM produit un cadrage cohérent et pose des questions pertinentes
2. Phase 2 (État des lieux) : les agents lisent le code de Sitavista et identifient l'existant
3. Phase 3 (Benchmark) : le research produit un benchmark avec de vrais concurrents (Pipedrive, HubSpot, etc.)
4. Phase 4 (Conception) : UX + Architect + Growth produisent des propositions concrètes
5. Phase 5 (Challenge) : Security et Delivery identifient des risques réels
6. Phase 6 (Synthèse) : le PM produit un résumé décisionnel clair
7. Plans Forge : les plans générés sont valides, atomiques, et exécutables

Si des problèmes sont détectés :
- Ajuster les prompts des agents
- Ajuster les paramètres du pipeline
- Ajuster le template de plan
- Documenter les ajustements dans un fichier `docs/TUNING.md`

**Success criteria**:
- Le pipeline complet s'exécute sans crash
- Les plans Forge générés sont valides (passent la validation)
- La qualité des outputs est jugée suffisante pour être envoyée à Forge
- Le temps total est < 30 minutes (hors attente utilisateur)
- Les coûts API sont documentés

**Files**: `docs/TUNING.md`, `examples/crm-sitavista/` (outputs de la session archivés comme exemple)

**Depends on**: LOT 4

---

## LOT 6 — Documentation et README

Écrire la documentation complète :

**README.md** :
- What is Squad (1 paragraphe)
- Quick start (3 commandes)
- Architecture (schéma ASCII)
- Les 6 phases expliquées simplement
- Les 10 agents listés avec leur rôle (1 ligne chacun)
- Configuration (globale et projet)
- Intégration Forge
- Modes (approval vs autonomous)
- Coût typique d'une session

**docs/AGENTS.md** :
- Référence complète des agents
- Comment ajouter un agent custom

**docs/ARCHITECTURE.md** :
- Schéma du pipeline
- Schéma de la DB
- Flow des données entre phases
- Gestion des erreurs et reprises

**CLAUDE.md** (à la racine du projet Squad) :
- Instructions pour Claude Code quand il travaille SUR le code de Squad lui-même
- Stack, conventions, structure

**Success criteria**:
- README clair et actionnable
- Un nouveau utilisateur peut lancer Squad en 5 minutes
- L'architecture est compréhensible
- CLAUDE.md permet à Forge de travailler sur Squad

**Files**: `README.md`, `docs/AGENTS.md`, `docs/ARCHITECTURE.md`, `CLAUDE.md`

**Depends on**: LOT 5

