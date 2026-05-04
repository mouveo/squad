# Squad — guide pour IA collaboratrices

> Tu es une IA (Claude, autre) invitée dans un projet de l'utilisateur.
> L'utilisateur te demande *"écris-moi un prompt"*, *"écris-moi un
> document"*, *"brief-moi un truc pour Squad"* — ou toute variante.
> Ce fichier te dit **exactement ce que Squad attend** et **ce qu'il
> NE faut pas lui dire**.

## 1. Ce qu'est Squad (lecture à froid)

Squad est un orchestrateur multi-agents Python qui vit dans
`~/Developer/squad`. Il prend une idée produit + un contexte de projet
et sort des plans Forge exécutables (markdown formaté).

Entrée principale : **Slack** (le workspace `mouveo`, channel
`#squad-chat`) via le slash command `/squad new <idée>`. Entrée
alternative : **CLI** (`squad run <projet> "<idée>"`) pour les
workflows scriptés.

Le pipeline enchaîne 6 phases : cadrage → état des lieux → benchmark
→ conception → challenge → synthèse. Trois agents runtime portent ces
phases (PM, UX, Architect), plus le service Research pour le benchmark.

Dashboard de suivi : `http://localhost:8501` (lancé par
`squad dashboard`).

## 2. Ce que Squad accepte en entrée (à ne jamais nier)

### Idée texte

La commande `/squad new` prend un argument texte libre. Pas de limite
pratique côté Slack (2000 chars environ). La commande CLI prend l'idée
en argument positionnel.

### Pièces jointes

**Squad accepte des fichiers**, contrairement à ce qu'une IA mal
informée pourrait répondre.

#### Workflow préféré — écrire le path dans l'idée

Quand tu prépares un brief à froid (deepsearch, audit, notes), **pose
les `.md/.txt/.csv` dans `{projet}/plans/<sujet>/`** ET **mentionne
explicitement le path `plans/<sujet>/` dans ton idée**. Squad extrait
chaque path `plans/...` qu'il trouve dans le texte et importe les
fichiers éligibles avant la première phase. Zéro guesswork, zéro
collision possible avec un mot de l'idée qui matcherait par hasard un
nom de dossier.

Exemple concret :

```
~/Developer/ressort/
└── plans/
    └── whaou/
        ├── audit-actuel.md
        ├── benchmark.md
        └── reco-prioritisee.md
```

Puis :
- Slack : `/squad new Ajouter le module whaou à Ressort — voir plans/whaou.`
- CLI : `squad run ressort "Ajouter le module whaou — voir plans/whaou"`

Les trois `.md` sont auto-attachés avant le cadrage ; Slack affiche
`:open_file_folder: 3 fichier(s) auto-attaché(s) depuis plans/whaou
— 0 rejeté, 0 ignoré` dans le thread, et le CLI imprime
`Auto-scan : 3 importé(s), 0 rejeté(s), 0 ignoré(s) depuis …`.

Patterns reconnus dans l'idée :

* `plans/<nom>` ou `plans/<nom>/` — importe tous les fichiers éligibles
  du dossier (non-récursif).
* `plans/<nom>/<fichier>.md` — importe juste ce fichier.
* Plusieurs paths dans la même idée → tous importés.
* La ponctuation de fin de phrase (`.,;:!?)"'`) est retirée automatiquement.
* Un path qui ne pointe sur rien d'existant est **silencieusement
  ignoré** (typo).

Scope retenu : fichiers directs `.md/.txt/.csv` (pas de récursion),
tri alphabétique, capé à 10 fichiers par dossier. Les autres
extensions et le surplus sont comptés comme *ignorés*.

Clé de config projet pour opt-out : `pipeline.project_plans_autoscan:
false`. Flag CLI ponctuel : `--no-plans-autoscan`.

#### Fallback — drag-drop Slack

Quand le fichier n'existe pas encore côté filesystem (deepsearch
fraîchement exportée depuis un outil externe, capture d'écran, etc.),
les deux modes Slack restent disponibles :

1. **Drop dans le même message Slack que `/squad new`** — Slack émet
   d'abord le slash command puis le file_shared event. Squad
   auto-attache à la session récente (fenêtre de 120 s) grâce à
   `find_recent_session_by_channel`.
2. **Drop dans le thread créé par le bot après la commande** —
   matching direct par `slack_thread_ts` via
   `find_session_by_thread`.

Formats acceptés par défaut (configurable via `slack.attachments.allowed_extensions`) :
`md, txt, csv, pdf, png, jpg, jpeg`.

Limites par défaut : 10 Mo par fichier, 50 Mo cumulés par session.

Stockage : `{projet_cible}/.squad/sessions/<uuid>/attachments/`.

Injection dans le contexte : le fichier est lu (texte) ou listé
(binaire) dans la section `## Attachments` du prompt cumulatif de
chaque phase aval.

### Mécanique "input riche"

Squad calcule un score `input_richness` en début de pipeline. Quand
le score est `rich` (idée > 500 chars ET/OU attachment texte > 3000
chars), Squad ajuste le prompt benchmark :

> *"L'utilisateur a fourni du contexte, comble les angles morts plutôt
> que de refaire la recherche."*

C'est exactement ce qu'on veut quand le PO a déjà fourni une
deepsearch détaillée.

## 3. Anatomie d'un bon prompt Squad

Quand l'utilisateur te demande *"un prompt pour Squad"*, tu produis
un paragraphe qui respecte la structure suivante :

1. **Nom du projet cible dans la première phrase.** Squad fait
   l'auto-discovery sur `~/Developer/<nom>`, donc le mot doit être
   présent en clair. Exemples : `sitavista`, `ressort`, `twig`.
2. **Objectif business en une phrase.** Pas plus. Réserve les
   détails pour la trajectoire (point 6).
3. **Les avantages différenciants à préserver** — ce que Squad ne
   doit pas casser en proposant la refonte.
4. **Référence explicite à la deepsearch** si un doc est joint :
   > *"Une deepsearch est jointe dans le thread : <description
   > 1-ligne>. Exploite-la comme source de vérité, ne refais pas la
   > recherche web sur les mêmes sujets."*

   Cette formulation déclenche l'attente "rich input" côté Squad et
   économise ~5-10 € de tokens sur le benchmark redondant.
5. **Stack technique** en une ligne (framework principal + base de
   données + auth éventuelle). Squad scanne déjà
   `{projet}/CLAUDE.md` + `package.json` / `composer.json` /
   `pyproject.toml` — mais rappeler ne fait pas de mal.
6. **Trajectoire ordonnée** — liste numérotée des axes à traiter.
   Chaque item en une ligne. C'est ce qui structure les plans Forge
   qui seront générés.
7. **Anti-scope explicite** — ce que tu ne veux PAS voir dans les
   plans. Typique : *"À éviter : cloner HubSpot en largeur,
   omnichannel inbox, CPQ complet."*
8. **Instruction finale** : *"Produis des plans Forge exécutables par
   lots atomiques."*

### Exemple canonique

```
/squad new Refonte progressive du CRM Sitavista pour combler le gap
fonctionnel et UX avec Pipedrive, HubSpot et Monday, en conservant les
3 avantages différenciants : modèle natif Network→Agency multi-tenant,
module RDV riche, copilote IA contextuel. Une deepsearch est jointe
dans le thread : audit actuel, benchmark feature-par-feature, reco
priorisée impact/effort. Exploite-la comme source de vérité — ne refais
pas la recherche web. Stack : Laravel 13 + Inertia v2 + React 18 +
TypeScript + Tailwind v4 + shadcn/ui, RBAC 9 rôles. Trajectoire :
1) fiche record HubSpot-like (3 colonnes, tabs) ; 2) pipeline cockpit
avec preview drawer ; 3) activities workspace global ; 4) email v1 ;
5) devis v1 ; 6) automation v1 (8-12 recettes) ; 7) command palette
Cmd+K. À éviter : cloner HubSpot en largeur, omnichannel inbox, CPQ
complet, workflow builder open-ended. Produis des plans Forge
exécutables par lots atomiques.
```

Environ 1300 chars — passe bien dans un slash command Slack.

## 4. Anatomie d'un bon document joint

Quand l'utilisateur te demande *"un document pour Squad"*, tu produis
un markdown qui :

**Respecte ces contraintes** :
- Format `.md` (Squad l'inline direct dans le contexte).
- Taille visée : **10 à 50 KB**. Au-delà, la compression kick in et
  l'utilité baisse.
- Sections claires en `##` — Squad scanne les titres pour résumer
  quand il doit compresser.

**Structure type** (calquée sur la deepsearch Sitavista qui est la
référence) :

```markdown
# <Titre du dossier>

## Lecture stratégique
Ton diagnostic honnête en 1-2 paragraphes : ce qui est déjà solide,
ce qui coince, les nuances importantes.

## Analyse <dimension 1>
Tableau comparatif OU sections par sujet.
Chaque affirmation quantifiée doit avoir une source (URL, citation).

## Analyse <dimension 2>
...

## Recommandations priorisées
Liste numérotée avec :
- Impact /10
- Effort /10
- Pourquoi MAINTENANT

## Pièges à éviter
Liste explicite de ce qui SEMBLE évident mais serait une mauvaise
décision.

## Synthèse de décision
Un paragraphe qui tranche : voilà la trajectoire recommandée dans
cet ordre, voilà ce qu'on laisse pour plus tard.
```

**Bon exemple concret à lire** :
`~/Developer/sitavista/plans/deep-research/deep-research-report-crm-comparison-sitavista-vs-leaders.md`

## 5. Erreurs à ne pas faire

- ❌ Dire *"Squad n'accepte pas de pièces jointes"* — c'est faux.
- ❌ Dire *"il faut inliner le doc dans le prompt Slack"* — pas la
  peine, il y a les attachments.
- ❌ Écrire un prompt qui oublie le nom du projet (l'auto-discovery
  ne matchera pas).
- ❌ Produire un document sans structure `##` — Squad ne saura pas
  le compresser proprement.
- ❌ Lancer `/squad new` sans d'abord vérifier que `squad serve`
  tourne. Si non, la commande ne sera jamais reçue.
- ❌ Demander à Squad de produire autre chose que des plans Forge
  (il a été conçu pour ça, et il valide la sortie via
  `forge_format.validate_plan`).

## 6. Commandes utiles à suggérer

```bash
# Vérifier que tout est en place (depuis ~/Developer/squad)
bash scripts/preflight.sh

# Lancer les services si non démarrés
squad serve           # Slack (terminal dédié)
squad dashboard       # UI locale (autre terminal)

# Voir une session en cours
squad status
squad status <session_id>
tail -f ~/.squad/serve.log

# Après review/approve
squad review <session_id>
squad approve <session_id>     # soumet à Forge
```

## 7. Où trouver la suite

- `~/Developer/squad/README.md` — vue d'ensemble CLI + philosophie
- `~/Developer/squad/docs/SITAVISTA_TEST.md` — procédure end-to-end
  reproductible
- `~/Developer/squad/docs/TROUBLESHOOTING.md` — bugs réels observés
  et contre-mesures
- `~/Developer/squad/agents/*.md` — identité et capacités de chaque
  agent (utile si tu veux comprendre ce que tel ou tel agent va
  lire de ton brief)
- `~/Developer/squad/CLAUDE.md` — règles si tu travailles SUR Squad
  lui-même (pas juste avec)
