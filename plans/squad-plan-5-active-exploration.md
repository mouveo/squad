# Squad — Plan 5/N : Exploration active du projet (ux + architect)

> Squad injecte déjà un pré-scan statique du projet cible via
> `workspace.get_context` (CLAUDE.md, README, manifests, tree, git log).
> Mais les agents ne peuvent pas **explorer activement** le code : ils
> ont l'outil `Read` mais pas d'ancrage pour résoudre les paths
> relatifs, et aucun moyen de lister, chercher ou filtrer
> l'arborescence à la demande.
>
> Ce plan donne à `ux` (en etat_des_lieux et conception) et `architect`
> (en conception) les outils `Glob`, `LS`, `Grep` en plus de `Read`, et
> pose le `cwd` du sous-processus Claude CLI sur `session.project_path`
> pour que les paths relatifs marchent naturellement. Les autres agents
> de `etat_des_lieux` (customer-success, data, sales) conservent le
> pré-scan seul — s'ils s'avèrent trop fluets en conditions réelles,
> un plan ultérieur élargira le périmètre.
>
> Prérequis : plans 1, 2, 3 et 4 complets (pipeline, CLI, intégration
> Forge et Slack fonctionnels, projet scan implémenté).

---

## LOT 1 — Executor : capabilités Glob/LS/Grep + plomberie cwd

Étend `squad/executor.py` pour parser trois nouvelles capabilités
depuis les agent markdowns (`glob`, `list_files`, `grep_files`) et les
mapper sur les identifiants Claude CLI `Glob`, `LS`, `Grep`. Le dict
`_CAPABILITY_TO_TOOL` est complété en conservant l'ordre existant
(Read, WebSearch, WebFetch d'abord, les nouveaux ensuite) pour garder
la sortie `--allowedTools=...` déterministe.

Ajoute un paramètre `cwd: str | None = None` à `_call_claude_cli`,
`run_agent`, `run_task_text`, `run_agents_parallel`, `run_agents_tolerant`
et `_build_cmd` (le dernier n'en a pas besoin mais on threade le
paramètre par symétrie des call sites). `_call_claude_cli` forward
`cwd` à `subprocess.run`. Quand `cwd` est `None`, le comportement
actuel est strictement préservé (aucun test existant ne doit bouger).

**Success criteria**:
- `parse_agent_capabilities` reconnaît `glob: oui`, `list_files: oui`, `grep_files: oui` dans la section `## Outils autorisés`
- `map_allowed_tools({"glob": True})` retourne `["Glob"]`, idem pour `list_files → LS` et `grep_files → Grep`
- `run_agent(..., cwd="/tmp/foo")` passe `cwd="/tmp/foo"` à `subprocess.run` via `_call_claude_cli` ; `cwd=None` ne passe aucun argument `cwd` (ou `cwd=None`)
- `run_task_text("prompt", cwd="/tmp/foo", allowed_tools=["Glob"])` forward les deux arguments
- Les 65+ tests existants de `tests/test_executor.py` passent sans modification
- Nouveaux tests : `TestCwdForwarding` (4 cas : cwd forwarded pour `run_agent`, `run_task_text`, `run_agents_parallel`, `run_agents_tolerant`) et `TestNewCapabilities` (3 cas : glob, list_files, grep_files)

**Files**: `squad/executor.py`, `tests/test_executor.py`

---

## LOT 2 — Agents ux.md et architect.md : outils d'exploration déclarés

Met à jour `agents/ux.md` et `agents/architect.md` pour déclarer les
trois nouvelles capabilités dans la section `## Outils autorisés` et
ajoute une section `## Exploration du projet` qui rappelle à l'agent :

* Le `cwd` du sous-processus Claude est la racine du projet cible.
* Le pré-scan (CLAUDE.md, README, manifests, tree, git log) est déjà
  injecté dans son prompt — ne pas relire bêtement ce qui est dedans.
* `Glob`, `LS`, `Grep` sont disponibles pour explorer au-delà du
  pré-scan quand c'est nécessaire (ex: `Glob("app/**/*Controller.php")`,
  `Grep("TenantBoundaryScope", glob="*.php")`).
* Les fichiers > 500 lignes doivent être lus par extraits ciblés, pas
  en entier.

Les autres agents de `etat_des_lieux` (`customer-success`, `data`,
`sales`) **ne sont pas touchés** par ce lot — ils restent sur le
pré-scan seul, comme décidé.

**Success criteria**:
- `agents/ux.md` contient `glob: oui`, `list_files: oui`, `grep_files: oui` dans sa section `## Outils autorisés`
- `agents/architect.md` contient les mêmes trois lignes
- Les deux fichiers contiennent une nouvelle section `## Exploration du projet` avec au minimum les 4 points listés ci-dessus
- `agents/customer-success.md`, `agents/data.md`, `agents/sales.md` sont inchangés
- `parse_agent_capabilities(open("agents/ux.md").read())["glob"]` est `True`
- Les tests de parsing existants sur pm/ux passent sans modification

**Files**: `agents/ux.md`, `agents/architect.md`, `tests/test_executor.py`

**Depends on**: LOT 1

---

## LOT 3 — Pipeline : cwd = session.project_path pour ux et architect

Dans `squad/pipeline.py`, le call site qui invoque `run_agent` (et
celui qui invoque `run_agents_tolerant` pour les phases parallèles)
doit désormais passer `cwd=session.project_path` **uniquement** pour
les agents listés dans une constante dédiée
`_AGENTS_WITH_CWD = {"ux", "architect"}`. Pour les autres agents,
`cwd` reste `None` (comportement inchangé).

Le routage est fait dans `_run_agents` (séquentiel) et dans le call
vers `run_agents_tolerant` (parallèle). En parallèle, il faut passer
un `cwd_by_agent: dict[str, str | None]` que `run_agents_tolerant`
forward individuellement par agent — ajoute cet argument optionnel si
nécessaire dans l'executor (LOT 1 ne le couvre pas, ce lot peut
compléter).

**Success criteria**:
- `_run_agents` passe `cwd=session.project_path` quand l'agent est dans `_AGENTS_WITH_CWD`, sinon `cwd=None`
- Pour les phases parallèles (`run_agents_tolerant`), chaque agent reçoit son `cwd` correctement (ux reçoit project_path, data/customer-success/sales reçoivent `None`)
- Nouveau test `TestCwdRoutingByAgent` dans `test_pipeline.py` : vérifie que `run_agent` reçoit le bon `cwd` selon l'agent et la phase, avec `_call_claude_cli` mocké
- Les 25 tests existants de `test_pipeline.py` passent sans modification
- `session.project_path` n'existant pas (cas théorique) → le pipeline ne crash pas ; on passe `cwd=None` silencieusement avec un warning

**Files**: `squad/pipeline.py`, `squad/executor.py`, `tests/test_pipeline.py`, `tests/test_executor.py`

**Depends on**: LOT 1, LOT 2

---

## LOT 4 — Context builder : rappel d'exploration + doc

Dans `squad/context_builder.py`, quand l'agent courant est dans
`_AGENTS_WITH_CWD` (ux ou architect), ajoute une section
`## Exploration disponible` juste après `## Contexte projet` qui
rappelle dans le prompt final : les outils `Glob/LS/Grep/Read` sont
dispos, le `cwd` est la racine, ne pas relire le pré-scan. Pour les
autres agents, rien n'est ajouté (ils n'ont pas les outils).

Met à jour `docs/ARCHITECTURE.md` (section "Agents & phases" si elle
existe, sinon ajoute une section "Exploration active") pour
documenter :
* Quels agents ont quels outils (tableau ciblé : ux + architect = Read, Glob, LS, Grep, WebSearch, WebFetch ; les autres = Read, WebSearch, WebFetch seulement)
* Pourquoi le pré-scan reste la source principale (déterminisme, coût)
* Comment ajouter l'exploration à un autre agent à l'avenir (les deux points de changement : `agents/<name>.md` + `_AGENTS_WITH_CWD`)

**Success criteria**:
- `build_cumulative_context` inclut la section `## Exploration disponible` pour les agents ux et architect, et ne l'inclut pas pour les autres
- `docs/ARCHITECTURE.md` contient un tableau agent → outils et une section "Exploration active" avec les 3 points ci-dessus
- Nouveau test `test_context_builder.py::test_exploration_section_only_for_ux_architect` (2 cas : présent pour ux, absent pour customer-success)

**Files**: `squad/context_builder.py`, `docs/ARCHITECTURE.md`, `tests/test_context_builder.py`

**Depends on**: LOT 3

---

## LOT 5 — Test d'intégration end-to-end + mise à jour CLAUDE.md Squad

Ajoute un test d'intégration dans `tests/test_pipeline.py` qui exécute
un pipeline complet (avec `_call_claude_cli` mocké) et vérifie que :

* Quand ux tourne en etat_des_lieux, le cmd passé à la CLI contient
  `--allowedTools=Read,WebSearch,WebFetch,Glob,LS,Grep` (ordre
  déterministe selon `_CAPABILITY_TO_TOOL`), et `cwd=session.project_path`
* Quand customer-success tourne, `--allowedTools` ne contient PAS
  Glob/LS/Grep et `cwd` est None
* Quand architect tourne en conception, même pattern que ux

Met à jour `CLAUDE.md` (racine Squad, pas le projet cible) avec une
note "Exploration active des projets cibles" dans la section
`## Intégration avec Claude Code CLI` pour expliquer le `cwd=` et
les outils Glob/LS/Grep.

**Success criteria**:
- Le test `test_ux_gets_exploration_tools_and_cwd_end_to_end` passe
- Le test `test_customer_success_stays_without_exploration` passe
- Le test `test_architect_gets_exploration_tools_in_conception` passe
- `CLAUDE.md` racine contient une note explicite sur `cwd` + Glob/LS/Grep pour ux/architect
- `pytest -m "not integration"` retourne 100 % pass (aucune régression)

**Files**: `tests/test_pipeline.py`, `CLAUDE.md`

**Depends on**: LOT 4
