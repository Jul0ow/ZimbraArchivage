# Revue Qualité & Maintenabilité

Périmètre : `backend/app`, `frontend/`, `charts/`, Dockerfiles, `docker-compose.yml`, `.github/workflows/release.yml`, README.
Lecture seule : aucun fichier source modifié. Numéros de ligne vérifiés sur l'état courant du dépôt (commit `7fd6486`).

---

## Backend

### [Élevée] Aucune journalisation côté backend
- Emplacement : `backend/app/` (aucun `import logging` dans tout le package) ; en particulier `backend/app/archive.py:207-208`, `backend/app/archive.py:232-235`, `backend/app/archive.py:245-246`, `backend/app/main.py:61-65`
- Problème : les erreurs Zimbra, les messages en échec d'un export, les exceptions d'extraction de pièces jointes (`except Exception`) et les 502 renvoyés par le handler `ZimbraError` ne sont tracés nulle part. Seul le contenu de `erreurs.txt`, remis à l'utilisateur, en garde une trace. Un exploitant ne peut pas diagnostiquer un incident (Zimbra lent, 5xx répétés, échec de login) ni corréler une plainte à un export.
- Recommandation : ajouter `logger = logging.getLogger(__name__)` et tracer au minimum : échecs de login (sans mot de passe), `ZimbraError` dans le handler, messages en erreur dans `stream_zip`, début et fin d'export (dossier, nombre de messages, durée). Ne jamais journaliser le jeton.

### [Moyenne] Gestion d'erreurs incohérente entre les couches (exceptions, valeur de retour, `Exception` large)
- Emplacement : `backend/app/archive.py:214-250` (`_add_message`), `backend/app/archive.py:207`, `backend/app/zimbra.py:258-261`, `backend/app/zimbra.py:274-283`, `backend/app/zimbra.py:249-252`
- Problème :
  1. `_add_message` renvoie un `str | None` (l. 232, 234, 246) alors que le reste du code lève des exceptions. Un `str` vide serait traité comme un succès (`if error:`).
  2. `except Exception` (l. 245) avale tout, y compris les bugs de programmation.
  3. `stream_zip` capture `ZimbraError` par dossier (l. 207) : `SessionExpired`, sous-classe de `ZimbraError`, est donc transformée en ligne de `erreurs.txt`. Si le jeton expire en cours d'export, chaque dossier restant échoue et l'archive est livrée « réussie » avec des erreurs.
  4. Un JSON Zimbra inattendu produit des `KeyError`/`TypeError` bruts (`response["folder"][0]`, `m["id"]`, `int(data["n"])`) qui deviennent des 500 non traités, au lieu de `ZimbraError` (donc 502 avec message).
  5. `login` ne vérifie pas que `authToken` est non vide : un cookie vide serait posé.
- Recommandation : définir un petit type de résultat (`AddedMessage | MessageFailure`) ou lever une exception dédiée ; dans `stream_zip`, faire `except SessionExpired: raise` avant `except ZimbraError` (ou arrêter l'export) ; restreindre `except Exception` à `(ValueError, LookupError, UnicodeError, ...)` et loguer le reste ; encapsuler les parsers (`_parse_folder`, `search_messages`) dans un `try/except (KeyError, TypeError, ValueError) -> ZimbraError`.

### [Moyenne] Backoff de réessai de plusieurs minutes dans des requêtes interactives
- Emplacement : `backend/app/zimbra.py:21-22`, `backend/app/zimbra.py:188-200` (`self._sleep(3**attempt)`)
- Problème : en cas de 502/503/504, `_send` dort 3 + 9 + 27 + 81 + 243 s, soit environ 6 minutes, y compris pour `login`, `folders`, `addresses` ou `summary`. Le README parle de « jusqu'à 5 fois » sans mentionner ce délai, ni que la requête HTTP du navigateur ou l'ingress aura abandonné bien avant. Le thread du pool FastAPI reste occupé pendant tout ce temps. Le délai (3**n) et le nombre d'essais sont des constantes codées en dur, non configurables, et `MAX_TRY` est ambigu (`range(MAX_TRY + 1)` donne en réalité 6 tentatives).
- Recommandation : renommer en `MAX_RETRIES`, extraire le backoff (`RETRY_BASE_DELAY`), le plafonner pour les appels SOAP interactifs (ou le rendre configurable), et documenter le comportement dans le README.

### [Moyenne] `main.py` mélange schémas, dépendances, handlers et logique métier
- Emplacement : `backend/app/main.py:1-332`, notamment `main.py:271-276` (`archive_file_name`), `main.py:178-240` (`ExportParams`, `build_export_request`), `main.py:77-111`
- Problème : un seul module de 332 lignes porte les modèles Pydantic, les dépendances, les exception handlers, tous les endpoints et des helpers qui n'ont rien à y faire (`archive_file_name` relève de l'archive ; `build_export_request` fait de la résolution de dossier). Les modèles de réponse recopient champ à champ des dataclasses existantes : `to_folder_response` (l. 103-111) et `SummaryResponse` construit à la main (l. 254-258) dupliquent `Folder` et `ExportSummary`. Chaque nouveau champ doit être ajouté à trois endroits.
- Recommandation : découper en `schemas.py`, `deps.py` et `routers/{auth,folders,export}.py`, déplacer `archive_file_name` dans `archive.py`. Utiliser `SummaryResponse.model_validate(summary, from_attributes=True)` ou des dataclasses Pydantic pour supprimer la recopie.

### [Moyenne] Cycle de vie manuel du client Zimbra dans `export()`
- Emplacement : `backend/app/main.py:289-297`, `main.py:306-320`
- Problème : le client est créé hors `with`, fermé via `except BaseException: client.close(); raise` (attrape aussi `KeyboardInterrupt`/`SystemExit`), puis rouvert dans le générateur. Si `progress_registry.start()` ou `archive_file_name()` (l. 300 et 322) levait une exception, le client ne serait jamais fermé et le `finally` de progression ne s'exécuterait pas. Le client n'est fermé que si le générateur est effectivement consommé ou fermé par Starlette.
- Recommandation : utiliser `contextlib.ExitStack` / dépendance FastAPI à `yield`, ou fermer dans un `try/finally` couvrant tout jusqu'au `return`. Remplacer `BaseException` par `Exception`.

### [Faible] Le fuseau est passé deux fois et `stream_zip` a une signature trop large
- Emplacement : `backend/app/archive.py:179-211` (`stream_zip`, params `zone`, `max_file_name_length`, `on_message`), `archive.py:48` (`DateRange.zone`), `archive.py:214-250` (`_add_message`, 7 paramètres), `backend/app/main.py:309-316`
- Problème : le fuseau vit à la fois dans `DateRange.zone` et dans l'argument `zone` de `stream_zip` ; on peut les désynchroniser. `_add_message` reçoit 7 paramètres (`client, token, archive, message, arcname, zone, extract_attachments`) et mélange téléchargement, écriture ZIP et extraction.
- Recommandation : regrouper `zone`, `max_file_name_length`, `extract_attachments` dans `ExportRequest` ou un `ExportOptions`, et scinder `_add_message` en `_download_eml()`, `_write_entry()`, `_write_attachments()`.

### [Faible] `Settings` : validation incohérente et fuseau invalide détecté trop tard
- Emplacement : `backend/app/config.py:19`, `config.py:21`, `config.py:23`, `config.py:37-40`, `config.py:42-44`
- Problème : `search_batch_size` est validé via `Field(ge, le)` (erreur au démarrage), mais `max_file_name_length` est silencieusement « clampé » entre 50 et 180 (une valeur de 10 est corrigée sans avertissement). `zone_id` n'est pas validé : une faute de frappe (`Europe/Pars`) ne lève `ZoneInfoNotFoundError` qu'à la première requête d'export (500). `http_timeout` n'a aucune borne.
- Recommandation : `max_file_name_length: int = Field(default=50, ge=50, le=180)`, `http_timeout: float = Field(120.0, gt=0)`, et un `field_validator("zone_id")` qui instancie `ZoneInfo` au chargement.

### [Faible] Duplication de la logique de parcours d'arbre et de quotation de valeur
- Emplacement : `backend/app/archive.py:106-109` (`_walk`) vs `backend/app/zimbra.py:98-101` (`Folder.walk`) ; `backend/app/suggestions.py:42` vs `backend/app/filters.py:26-30` (`quote_value`) ; `backend/app/zimbra.py:263-302`
- Problème :
  1. `_walk(folder, excluded)` réimplémente `Folder.walk()` avec un filtre d'exclusion.
  2. `suggestions._from_messages` répète la règle « valeur simple, sans `*`, pas un mot réservé » déjà codée dans `quote_value`, en important les constantes internes `SIMPLE_VALUE` et `RESERVED_WORDS` : les deux modules sont couplés par des détails d'implémentation.
  3. `search_messages` et `search_addresses` dupliquent la construction du corps `SearchRequest` (`types`, `limit`, `query`, `locale` « fr_FR » codé en dur, l. 266-272 et 288-295).
- Recommandation : ajouter `Folder.walk(exclude: set[str] = frozenset())`, exposer `filters.is_simple_value(text)` / `is_prefix_searchable(text)`, et un helper privé `_search(token, query, **extra)`.

### [Faible] Identifiants Zimbra « magiques » en dur et dupliqués
- Emplacement : `backend/app/zimbra.py:44-55` (`HUMAN_NAMES`), `backend/app/archive.py:34`, `archive.py:37` (`ROOT_FOLDER_ID`, `EXCLUDED_FROM_MAILBOX`) ; `frontend/app/dossiers/page.tsx:26`, `frontend/lib/api.ts:16`
- Problème : les identifiants de dossiers système (`"1"`, `"3"`, `"4"`) sont définis dans `archive.py` côté backend et redéfinis dans `page.tsx` et `api.ts` côté frontend, sans lien entre eux. `ROOT_FOLDER_ID` n'est pas dans `zimbra.py`, qui possède pourtant `HUMAN_NAMES`.
- Recommandation : regrouper ces constantes dans `zimbra.py` (`ROOT_FOLDER_ID`, `TRASH_ID`, `SPAM_ID`) et exposer côté API (par exemple un drapeau `excluded_from_mailbox` dans `FolderResponse`) pour que le frontend n'ait plus à les connaître.

### [Faible] Concurrence incohérente dans `ProgressRegistry` et nettoyage partiel
- Emplacement : `backend/app/progress.py:35-55`, `progress.py:58-62`
- Problème : `start` et `get` prennent le verrou, mais `advance` et `finish` modifient `done`/`finished`/`updated_at` sans verrou (acceptable avec le GIL, mais le contrat n'est pas explicite). L'expiration (l. 39-41) n'est balayée qu'au `start` d'un nouvel export : sans nouvel export, les entrées terminées restent indéfiniment. Le singleton `registry` global (l. 58) rend les tests dépendants de l'état partagé.
- Recommandation : documenter le choix (« mises à jour atomiques via le GIL ») ou verrouiller partout ; balayer aussi dans `get` ; instancier le registre dans `lifespan`/`app.state`.

### [Faible] Imports non triés et absence d'outillage statique Python
- Emplacement : `backend/app/archive.py:17-18` (`from typing import BinaryIO` avant `from datetime import ...`), `backend/requirements-dev.txt`
- Problème : aucun `ruff`/`mypy`/`black` ni `pyproject.toml` ; seul `pytest` est dans les dépendances de développement. Les imports ne sont pas triés (l. 17-18), `raise HTTPException` dans un `except` sans `from None` (`main.py:129-132`, `main.py:214-215`).
- Recommandation : ajouter un `pyproject.toml` avec `ruff` (règles `E,F,I,B,UP`) et `mypy --strict` sur `app/`, et les lancer en CI.

---

## Frontend

### [Élevée] Le téléchargement par `window.location.assign` quitte la page en cas d'erreur HTTP
- Emplacement : `frontend/app/dossiers/page.tsx:128-152` (en particulier l. 134 et 137), `frontend/lib/api.ts:133-134`
- Problème : l'export est lancé par navigation directe vers `/api/export?...`. Seule la réussite renvoie `Content-Disposition: attachment`. Si le backend répond 404 (dossier supprimé), 422 (filtre invalide), 502 ou une réponse du proxy, le navigateur affiche la réponse JSON brute à la place de l'application, et l'écran d'attente (`setDownload`, l. 138) n'a plus de sens. Le contrôle préalable `getMe()` (l. 134) ne couvre qu'une partie des cas (session) et ajoute un aller-retour.
- Recommandation : soit passer par un `<a download>`/`<iframe>` caché et surveiller l'état via `/api/export/progress`, soit utiliser `fetch` + `ReadableStream`/`showSaveFilePicker`. À défaut, valider les paramètres via `getSummary` avant de naviguer (c'est déjà le même contrôle de paramètres côté backend) et afficher l'erreur dans l'UI.

### [Moyenne] `FoldersPage` : composant « dieu » de 280 lignes et états dérivés par `useEffect`
- Emplacement : `frontend/app/dossiers/page.tsx:45-324`, états l. 47-62, `page.tsx:90-93`, `page.tsx:95-99`, `page.tsx:167-184`
- Problème : 12 `useState` indépendants, 3 `useEffect`, 4 handlers asynchrones et tout le JSX dans un composant. L'invalidation de l'estimation se fait par un `useEffect` qui appelle `setSummary(null)`/`setNotice(null)` sur 7 dépendances (anti-pattern « état dérivé » : rendu supplémentaire et risque d'oubli quand une option est ajoutée ; `extractAttachments` y figure alors qu'il ne change pas l'estimation, voir ci-dessous). La branche « chargement » duplique l'en-tête et le conteneur de la branche principale. L'effet de chargement (l. 75-82) n'a pas de nettoyage et peut appeler `setState` après démontage.
- Recommandation : extraire `useExportOptions()` (état + `options` + `datesInvalid`), `useDownload()` (id, total, notice), un composant `<ExportForm>` et un `<AttachmentOptions>` ; invalider l'estimation en stockant dans `summary` la clé des options pour lesquelles elle a été calculée (`summary.key === optionsKey`) ; ajouter un drapeau `cancelled` ou un `AbortController` dans l'effet de chargement.

### [Moyenne] Contrat d'API recopié à la main, sans génération ni validation à l'exécution
- Emplacement : `frontend/lib/api.ts:1-64`, `api.ts:75-99` (casts l. 96 `undefined as T` et l. 98 `response.json() as Promise<T>`), `api.ts:126`, `backend/app/main.py:68-100`, `backend/app/filters.py:33-56`
- Problème : les types TypeScript (`FolderNode`, `Summary`, `MessageFilter`, `FilterCondition`...) recopient les modèles Pydantic ; aucun mécanisme (génération OpenAPI, test de contrat) ne détecte une dérive. Le typage repose sur des assertions : `json()` renvoie `any`, `body.detail` (l. 85-88) n'est pas typé, et un tableau `detail` de FastAPI (erreurs 422) est réduit à « Paramètres invalides » sans indiquer le champ. Les littéraux `"subject" | "from" | "to"` (l. 21) et `"from" | "to"` (l. 126) sont répétés aussi dans `ConditionValueInput.tsx:27` et le backend (`filters.py:14`, `suggestions.py:14`). Mélange de conventions : `message_count`/`folder_count` en snake_case (l. 9, 55-57) mais `folderId`/`includeSubfolders` en camelCase pour `ExportOptions`.
- Recommandation : générer les types depuis `/api/openapi.json` (`openapi-typescript`) dans un script `npm run gen:api` vérifié en CI ; typer `body` en `{ detail?: unknown }` ; réexploiter le premier message du 422 (`detail[0].msg`) ; dériver `AddressField` de `FilterCondition["field"]` avec `Exclude<..., "subject">`.

### [Moyenne] Le frontend fabrique un faux dossier racine et recalcule des totaux côté client
- Emplacement : `frontend/app/dossiers/page.tsx:26-43`, `page.tsx:86`, `frontend/components/FolderTree.tsx:52` (test `folder.id === ROOT_FOLDER_ID`)
- Problème : `/api/folders` renvoie les enfants de la racine, et le frontend ajoute un nœud synthétique `id: "1"`, `path: "/"`, avec un `message_count` recalculé par `countMessages` en excluant `EXCLUDED_FROM_MAILBOX` (règle métier backend recopiée). `FolderTree` doit ensuite traiter ce cas particulier (icône). Si le backend change la règle d'exclusion, le compteur affiché diverge silencieusement de l'export réel.
- Recommandation : faire renvoyer le nœud « Toute la boîte » (avec son compteur) par le backend, ou au moins un champ `excluded_from_mailbox` ; le frontend n'afficherait plus que ce qu'il reçoit.

### [Faible] Casts `as` répétés dans `FilterEditor`, composant de 170 lignes de JSX imbriqué
- Emplacement : `frontend/components/FilterEditor.tsx:113`, `:136`, `:166`, `:181` (casts `e.target.value as LogicalOperator` / `as FilterCondition["field"]` / `["operator"]`), `FilterEditor.tsx:79-252`, `FilterEditor.tsx:34-35` (compteur global `lastKey`)
- Problème : les valeurs de `<select>` sont forcées en types union par assertion, sans vérification ; `Object.entries(FIELD_LABELS)` perd le typage des clés. Le composant mélange logique d'état (`updateGroup`, `updateCondition`) et 170 lignes de JSX sur 7 niveaux d'imbrication, avec des `onClick` inline qui reconstruisent l'état. `lastKey` est un compteur mutable de module, partagé entre instances et non réinitialisé (acceptable, mais fragile en test/SSR).
- Recommandation : extraire `<GroupCard>` et `<ConditionRow>` ; créer un helper générique `SelectField<T extends string>({ options: Record<T,string>, value, onChange })` qui élimine les casts ; générer les clés avec `useRef`/`crypto.randomUUID` ou `useId`.

### [Faible] Hook et composant mélangés, logique d'état dispersée dans `DownloadOverlay.tsx`
- Emplacement : `frontend/components/DownloadOverlay.tsx:18-65` (hook `useExportProgress`), `DownloadOverlay.tsx:76-131` (composant), `DownloadOverlay.tsx:44-49`
- Problème : le fichier exporte un hook de polling (avec `setTimeout` récursif, statut `unknown`, délai de 15 s codé en dur) et un composant de présentation. Le hook est importé par la page, ce qui rend `DownloadOverlay` à la fois « vue » et « service ». Le `setState` dans le `catch` fabrique un état à partir de `s` précédent de façon peu lisible.
- Recommandation : déplacer le hook dans `frontend/lib/useExportProgress.ts`, garder le composant purement présentationnel.

### [Faible] Erreurs avalées ou expressions à effet de bord obscures
- Emplacement : `frontend/components/ConditionValueInput.tsx:43` (`.catch(() => !cancelled && setSuggestions([]))`), `frontend/app/dossiers/page.tsx:145` (`.catch(() => undefined)`), `page.tsx:163` (`logout().catch(() => undefined)`), `frontend/app/page.tsx:18-20`
- Problème : l'expression `!cancelled && setSuggestions([])` utilise le court-circuit comme instruction et retourne un booléen que personne n'utilise ; plusieurs `catch` ignorent totalement l'erreur sans commentaire (seul le premier est justifié). Au niveau de `page.tsx:142-145`, un échec de l'estimation du total laisse la barre indéterminée sans trace.
- Recommandation : `if (!cancelled) setSuggestions([])` dans un bloc ; documenter chaque `catch` vide ; loguer en `console.debug` en développement.

### [Faible] Constantes de validation dupliquées entre frontend et backend
- Emplacement : `frontend/components/ConditionValueInput.tsx:8` (`MIN_QUERY_LENGTH = 2`) vs `backend/app/main.py:168` (`min_length=2`) ; `ConditionValueInput.tsx:79` (`maxLength={200}`) vs `backend/app/filters.py:23` ; `frontend/app/dossiers/page.tsx:101` vs `backend/app/main.py:205-209` (règle `start > end`) ; `frontend/lib/api.ts:140-143` vs `main.py:51` (format `export_id`)
- Problème : les limites (2 caractères, 200 caractères, 20 conditions/groupe, 10 groupes) existent des deux côtés. Les maxima 20/10 de `filters.py:45` et `:53` ne sont pas appliqués dans l'UI : un filtre trop gros produit un 422 « Filtre invalide » sans détail (`main.py:215`).
- Recommandation : faire exposer ces limites par le backend (OpenAPI les contient déjà : `minLength`, `maxItems`) et les lire côté frontend, ou au moins plafonner l'UI (désactiver « + Ajouter ») ; ajouter le détail du champ fautif dans le message d'erreur du filtre.

### [Faible] Estimation invalidée à tort par `extractAttachments`, paramètre inutile envoyé à l'estimation
- Emplacement : `frontend/app/dossiers/page.tsx:93`, `frontend/lib/api.ts:101-112` (`exportQuery` utilisé par `getSummary` ET `exportUrl`), `backend/app/main.py:243-258`
- Problème : `exportQuery` ajoute `extract_attachments` à la requête d'estimation, que le backend ignore silencieusement (paramètre inconnu non rejeté). Cocher « extraire les pièces jointes » efface pourtant l'estimation (l. 93) alors que le nombre de messages et la taille ne changent pas.
- Recommandation : séparer `exportQuery` (communs) de `extract_attachments` (export seulement) et retirer `extractAttachments` des dépendances de l'effet.

### [Faible] Accessibilité de l'arbre et de la boîte de dialogue incomplète
- Emplacement : `frontend/components/FolderTree.tsx:15-71`, `frontend/components/DownloadOverlay.tsx:81-89`
- Problème : `role="tree"`/`treeitem` annoncent un widget clavier complet (flèches, Home/End) qui n'est pas implémenté ; la boîte `role="dialog" aria-modal` n'a ni piège de focus ni fermeture à `Escape`. Un lecteur d'écran promet donc des interactions inexistantes.
- Recommandation : soit retirer les rôles ARIA complexes (liste imbriquée de boutons), soit implémenter la navigation au clavier ; ajouter `onKeyDown` pour `Escape` sur l'overlay.

### [Info] Pas d'ESLint : `npm run lint` n'est qu'un `tsc --noEmit`, et `tsconfig` peu strict
- Emplacement : `frontend/package.json:10` (`"lint": "tsc --noEmit"`), `frontend/tsconfig.json`, `README.md` section Développement
- Problème : le script « lint » ne contrôle ni les imports inutilisés, ni les règles React (`react-hooks/exhaustive-deps`), ni l'accessibilité (`jsx-a11y`). `noUnusedLocals`, `noUnusedParameters`, `noUncheckedIndexedAccess` ne sont pas activés (par exemple `suggestions[active]` dans `ConditionValueInput.tsx:68` n'est pas vérifié).
- Recommandation : ajouter ESLint (`eslint-config-next`) et Prettier, activer ces trois options `tsconfig`, renommer l'actuel script `typecheck`.

### [Info] Deux chemins pour `/api` : relais Next.js et ingress direct
- Emplacement : `frontend/app/api/[...path]/route.ts:7-61`, `charts/zimbra-archivage/templates/frontend.yaml` (`BACKEND_URL`), `README.md` (« En Kubernetes, l'ingress envoie directement `/api` au backend »)
- Problème : selon l'environnement (Compose vs Helm), les appels passent par le proxy Next.js (qui fabrique sa propre erreur 502 `Service d'archivage injoignable`, l. 50) ou non. Les comportements d'erreur, de tampon et de timeout diffèrent et ne sont testés que d'un côté.
- Recommandation : documenter la différence dans le README (déjà partiellement), tester explicitement le relais (annulation de flux, 502) et aligner le format d'erreur.

---

## CI, Docker, Helm, configuration

### [Moyenne] Pas de CI sur pull request : tests et vérifications seulement au moment d'une release
- Emplacement : `.github/workflows/release.yml:5-8` (déclencheurs `tags` et `workflow_dispatch` uniquement)
- Problème : `pytest` et `tsc` ne tournent qu'à la publication d'un tag. Une régression est découverte au moment de publier, pas à la revue. Le chart n'est jamais validé (`helm lint` / `helm template`) avant le packaging (l. 91-94). Aucun linter Python/TS n'est exécuté.
- Recommandation : ajouter `.github/workflows/ci.yml` (sur `push` et `pull_request`) avec backend (pytest + ruff + mypy), frontend (typecheck + `next build`), `helm lint` et `docker build` sans push ; faire de `release.yml` un workflow qui réutilise ce job (`workflow_call`).

### [Faible] Workflow de release : version non validée, `latest` et `--prerelease` codés en dur
- Emplacement : `.github/workflows/release.yml:11-12`, `:19`, `:75-76`, `:137`
- Problème : l'entrée `version` n'est pas validée (format `X.Y.Z`) avant d'être utilisée comme tag d'image et version de chart ; le tag `:latest` est poussé à chaque release, y compris une pré-version ; toutes les releases sont marquées `--prerelease` sans possibilité de changer. Les images sont publiées avant la création du tag Git dans le cas `workflow_dispatch`.
- Recommandation : valider par regex (`^[0-9]+\.[0-9]+\.[0-9]+(-.+)?$`) dans un premier step ; rendre `prerelease` conditionnel (présence d'un suffixe `-rc`) ; ne pousser `latest` que pour une version stable.

### [Faible] Dérive de configuration entre `.env.example`, Compose, Helm et README
- Emplacement : `docker-compose.yml:11-17`, `.env.example`, `backend/.env.example`, `charts/zimbra-archivage/values.yaml:9-16`, `README.md` (tableau « Configuration du backend »)
- Problème : Compose ne transmet que `ZIMBRA_*`, `VERIFY_TLS`, `ZONE_ID` et force `COOKIE_SECURE=false` ; `SEARCH_BATCH_SIZE`, `MAX_FILE_NAME_LENGTH` et `HTTP_TIMEOUT` ne peuvent donc pas être réglés via `.env` alors que le README les présente comme des variables de l'application et que le chart Helm les expose. Le README ne dit pas que Compose force `COOKIE_SECURE=false`. Les valeurs par défaut sont répétées à quatre endroits (`config.py`, `.env.example`, `values.yaml`, README).
- Recommandation : utiliser `env_file: .env` dans Compose (ou lister toutes les variables avec valeurs par défaut) et indiquer le comportement `COOKIE_SECURE` dans le README.

### [Faible] Version « 0.1.0 » dispersée et `docker build` du README figé
- Emplacement : `charts/zimbra-archivage/Chart.yaml:5-6`, `frontend/package.json:3`, `README.md` (commandes `docker build ... :0.1.0`, `git tag v0.1.0`), `backend/app/main.py:21` (aucune version dans `FastAPI(...)`)
- Problème : la version est écrite à plusieurs endroits sans source unique ; le backend n'expose aucune version (`/api/health` ne la renvoie pas), ce qui complique le diagnostic d'un déploiement.
- Recommandation : injecter la version au build (`ARG VERSION` → variable d'environnement lue par FastAPI `version=`), et la faire renvoyer par `/api/health`.

### [Info] Dockerfile backend sans `HEALTHCHECK`, sécurité du build non verrouillée
- Emplacement : `backend/Dockerfile:1-20`, `backend/requirements.txt`
- Problème : le contrôle de santé n'existe que dans Compose et Helm ; les dépendances sont épinglées (bien) mais sans hachages, et l'image `python:3.13-slim` n'est pas épinglée par digest.
- Recommandation : ajouter un `HEALTHCHECK` (ou le laisser à l'orchestrateur en le documentant), envisager `pip-compile --generate-hashes`.

---

## README vs code

### [Faible] Écarts mineurs entre le README et le comportement réel
- Emplacement : `README.md` (liste des endpoints et Développement), `backend/app/main.py:243-258`, `backend/app/zimbra.py:188-200`, `frontend/package.json:10`
- Problème :
  1. `GET /api/export/summary` est décrit sans ses paramètres alors qu'il partage `ExportParams` avec l'export (et ignore `extract_attachments`).
  2. Le délai de réessai (3, 9, 27, 81, 243 s) n'est pas mentionné.
  3. `npm run lint` est décrit comme « vérification TypeScript » (exact), mais le nom laisse croire à un linter.
  4. Les codes d'erreur de l'API (401, 404, 422, 502) et le message générique « Filtre invalide » ne sont pas documentés, alors que le frontend en dépend (`api.ts:84-92`, `page.tsx:66`).
  5. La logique de session expirée (suppression du cookie sur 401, `main.py:54-58`) n'apparaît pas dans la section Sécurité.
- Recommandation : compléter la liste des endpoints (paramètres + codes d'erreur), mentionner le backoff, renommer le script en `typecheck`.

### [Info] Points positifs à préserver
- Emplacement : `backend/app/filters.py`, `backend/app/progress.py`, `backend/app/suggestions.py`, `backend/app/zimbra.py:130-161`
- Problème : sans objet (constat positif). Modules courts, bien documentés (docstrings en français), constantes nommées avec commentaires de pourquoi, injection de dépendances (`transport`, `sleep`, `clock`) qui facilite les tests, `dataclass(frozen=True)` systématiques, Helm avec `securityContext` strict. Le style est homogène (identifiants anglais, messages utilisateur et commentaires en français).
- Recommandation : conserver ces conventions et les formaliser dans un `CONTRIBUTING.md` / config `ruff`.

---

## Résumé

- Qualité globale bonne : code court, commenté et testable, mais sans journalisation, sans linter Python/TS ni CI hors release, et avec une gestion d'erreurs hétérogène (valeur de retour vs exceptions, `SessionExpired` avalée dans l'export).
- Principaux risques de maintenabilité : `FoldersPage` (12 états, effets de dérivation) et `main.py` trop gros, contrat d'API dupliqué à la main entre Pydantic et TypeScript, règles métier (dossiers 1/3/4, limites de saisie) recopiées côté frontend.
- Priorités : ajouter logs et propagation de `SessionExpired`, corriger le téléchargement par navigation directe qui expose l'utilisateur à une page JSON, mettre en place une CI sur PR avec lint/typage, et générer les types TS depuis OpenAPI.
