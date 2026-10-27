# Revue qualité & maintenabilité

Périmètre : `backend/app/*.py`, `frontend/` (app, components, lib), `charts/zimbra-archivage/`,
`docker-compose.yml`, `.github/workflows/release.yml`, `README.md`.

Vérifications outillées :
- `ruff check --select E,F,W,B,UP,I,SIM,RET,C90 --line-length 100 backend/app` : 10 constats mineurs
  (2 × B904, 1 × I001, 1 × SIM103, 6 × E501), aucune variable/import inutilisé, aucune complexité
  cyclomatique excessive.
- `npm run lint` (tsc --noEmit) : **non exécuté**, `frontend/node_modules` absent.
- `helm lint` : non exécuté (binaire `helm` absent).

Synthèse : 0 Critique, 0 Élevée, 7 Moyenne, 12 Faible, 4 Info.

---

## Moyenne

### [Moyenne] Session expirée en cours d'export : l'export continue et remplit `erreurs.txt`
- Emplacement : `backend/app/archive.py:207-208`, `backend/app/archive.py:231-232`, `backend/app/zimbra.py:69-70`
- Problème : `SessionExpired` hérite de `ZimbraError`. Dans `stream_zip`, l'exception est avalée par
  dossier (`except ZimbraError`), et dans `_add_message` chaque téléchargement ReST en 401 devient une
  simple ligne d'erreur. Si le jeton expire pendant un long export (durée de vie du jeton Zimbra), le
  backend continue à interroger Zimbra pour chaque message/dossier restant, et l'utilisateur obtient
  une archive « réussie » avec des centaines de lignes d'erreur, sans signal clair. Le traitement
  diffère du reste de l'API, où `SessionExpired` a un handler dédié (`main.py:54-58`).
- Vérifié par le teammate tests (voir `review/tests.md`) : avec un jeton expiré, `/api/export` renvoie
  un HTTP 200 et un ZIP qui ne contient que `erreurs.txt` (« Dossier /Inbox : no valid authtoken present »).
- Recommandation : traiter `SessionExpired` comme fatal dans la boucle (le laisser remonter avant le
  `except ZimbraError`), et écrire une ligne explicite dans `erreurs.txt` avant de fermer l'archive.
  Côté ReST, mapper 401/403 vers `SessionExpired` dans `download_eml`.
  ```python
  except SessionExpired as e:
      errors.append(f"Export interrompu : session Zimbra expirée ({e.message})")
      break
  except ZimbraError as e:
      ...
  ```

### [Moyenne] Règles métier « toute la boîte » dupliquées entre backend et frontend
- Emplacement : `backend/app/archive.py:34-37`, `frontend/lib/api.ts:16`, `frontend/app/dossiers/page.tsx:26`, `frontend/app/dossiers/page.tsx:33-43`
- Problème : l'identifiant racine `"1"` et la liste des dossiers exclus `{"3", "4"}` (Corbeille, Spam)
  sont codés en dur des deux côtés ; le frontend recalcule lui-même le nombre de messages de la boîte
  et fabrique un faux nœud `FolderNode`. Toute évolution (ex. exclure aussi les Brouillons) doit être
  faite à deux endroits, sinon le compteur affiché diverge de l'export réel. Côté backend, les ids
  système sont eux-mêmes éparpillés : `HUMAN_NAMES` (`zimbra.py:44-55`) et `ROOT_FOLDER_ID` /
  `EXCLUDED_FROM_MAILBOX` (`archive.py`).
- Recommandation : centraliser les ids système dans `zimbra.py` (`ROOT_ID`, `TRASH_ID`, `JUNK_ID`) et
  faire renvoyer par `/api/folders` l'entrée « Toute la boîte aux lettres » (ou un champ
  `mailbox_message_count`) calculée par le backend ; supprimer `EXCLUDED_FROM_MAILBOX` et
  `countMessages` du frontend.

### [Moyenne] Contrat du filtre dupliqué à la main entre Python et TypeScript, hors OpenAPI
- Emplacement : `backend/app/filters.py:22-56`, `frontend/lib/api.ts:18-35`, `frontend/components/ConditionValueInput.tsx:8,79`, `backend/app/main.py:195-215`
- Problème : `MessageFilter` / `FilterCondition` sont redéfinis manuellement en TS (champs, opérateurs).
  Les contraintes ne sont qu'en partie recopiées : `max_length=200` (repris en dur `maxLength={200}`),
  `min_length=2` des suggestions (repris en `MIN_QUERY_LENGTH = 2`), mais pas les limites 10 groupes /
  20 conditions ; un dépassement renvoie un 422 générique « Filtre invalide » sans détail. Comme le
  filtre transite en chaîne JSON dans un query param, son schéma n'apparaît même pas dans
  `/api/openapi.json`, ce qui empêche toute génération de types.
- Recommandation : soit passer `MessageFilter` en corps `POST` (`/api/export/summary`) pour qu'il soit
  décrit par OpenAPI et générer les types TS (`openapi-typescript`), soit au minimum exposer les
  limites (constantes partagées documentées) et renvoyer le détail Pydantic (`e.errors()`) dans le 422.
  Appliquer les limites dans `FilterEditor` (désactiver « + Ajouter » au-delà).

### [Moyenne] `dossiers/page.tsx` concentre chargement, formulaire, estimation et orchestration du téléchargement
- Emplacement : `frontend/app/dossiers/page.tsx:45-324` (composant de ~280 lignes, 13 `useState`)
- Problème : le composant mélange l'auth (redirection 401), le chargement de l'arbre, l'état du
  formulaire d'export (7 champs), l'estimation, le lancement du téléchargement et l'overlay. La
  remise à zéro de l'estimation repose sur un `useEffect` qui écoute 7 dépendances
  (`page.tsx:90-93`) : c'est de l'état dérivé synchronisé par effet (anti‑pattern React, un rendu
  supplémentaire, facile à casser en ajoutant une option sans l'ajouter aux dépendances).
- Recommandation : extraire un composant `ExportForm` et un hook `useExportOptions` (un `useReducer`
  sur un objet `ExportOptions`) ; mémoriser l'estimation avec la clé des options
  (`summary: { key: string; value: Summary }`, affichée seulement si `key === optionsKey`) plutôt
  que la réinitialiser par effet. Déplacer `handleError` (401 → `/`) dans un hook partagé avec
  `app/page.tsx`.

### [Moyenne] Aucune journalisation côté backend
- Emplacement : `backend/app/` (aucun `import logging`), notamment `suggestions.py:28-35`, `archive.py:207-208`, `archive.py:231-234`, `archive.py:245-246`
- Problème : les erreurs Zimbra sont soit renvoyées au client (502), soit avalées (`_safe`), soit
  écrites uniquement dans `erreurs.txt` à l'intérieur du ZIP de l'utilisateur. L'exploitant n'a
  aucune trace côté serveur pour diagnostiquer un incident (Zimbra indisponible, extraction de PJ en
  échec, retries).
- Recommandation : un `logger = logging.getLogger(__name__)` par module, `logger.warning(...)` dans
  `_safe`, dans les `except` de `stream_zip`/`_add_message` et à chaque retry de `_send`
  (`logger.exception` pour le `except Exception` des pièces jointes). Sans jeton ni contenu de mail
  dans les messages.

### [Moyenne] Pas d'outillage qualité automatisé ni de CI hors release
- Emplacement : `.github/workflows/release.yml:5-12`, `frontend/package.json:9`, absence de `pyproject.toml`/config ruff/mypy, absence d'ESLint
- Problème : tests et `tsc` ne tournent que sur tag `v*` ou déclenchement manuel : une régression
  n'est détectée qu'au moment de publier. Le « lint » frontend n'est que `tsc --noEmit` : pas de règle
  `react-hooks/exhaustive-deps` alors que le code repose sur plusieurs `useEffect` à dépendances
  multiples. Côté Python, aucun linter/formatter configuré (le passage de ruff ci‑dessus trouve
  B904, I001, SIM103). Pas de `helm lint` / `helm template` dans la CI.
- Recommandation : ajouter un workflow `ci.yml` sur `push`/`pull_request` (pytest, `ruff check`,
  `ruff format --check`, `mypy app` ou `pyright`, `npm run lint` + `eslint` avec
  `eslint-config-next`, `helm lint charts/zimbra-archivage` + `helm template | kubeconform`), et faire
  dépendre `release.yml` de ce workflow réutilisable (`workflow_call`) au lieu de dupliquer le job.

### [Moyenne] Chart Helm sans `values.schema.json` : erreurs de configuration détectées au runtime seulement
- Emplacement : `charts/zimbra-archivage/values.yaml:9-19`, `backend/app/config.py:17,37-40`
- Problème : `searchBatchSize` est borné à 1..1000 par Pydantic (`config.py:17`) : une valeur hors
  bornes passe `helm install` puis fait crasher le pod en boucle (CrashLoopBackOff) au démarrage.
  À l'inverse, `maxFileNameLength` est silencieusement ramené entre 50 et 180
  (`clamp_file_name_length`), ce qui masque une erreur de saisie. Deux politiques différentes pour
  le même type de contrainte.
- Recommandation : ajouter `values.schema.json` (types, `minimum`/`maximum`, `format: uri` pour
  `zimbra.soapUrl`/`restUrl`, `enum` pour `service.type`) et uniformiser côté Python : valider par
  `Field(ge=50, le=180)` plutôt que de clamper en silence.

---

## Faible

### [Faible] Logique de retry : nommage trompeur et message faux
- Emplacement : `backend/app/zimbra.py:22`, `backend/app/zimbra.py:188-200`, `README.md:20`
- Problème : `MAX_TRY = 5` mais la boucle `range(MAX_TRY + 1)` fait 6 tentatives ; le message final
  « Zimbra indisponible malgré 5 essais » est donc faux. Le délai `3**attempt` (3 → 243 s, 363 s
  cumulés) n'est ni documenté ni configurable (impact perf transmis au teammate perf).
- Recommandation : renommer en `MAX_RETRIES`, corriger le message (`{MAX_RETRIES + 1} essais`),
  extraire le délai dans `Settings` (ex. `retry_base_delay`) avec un plafond, et journaliser chaque retry.

### [Faible] Gestion manuelle du cycle de vie du client dans `/api/export`
- Emplacement : `backend/app/main.py:289-320`
- Problème : `client = client_factory()` puis `except BaseException: client.close(); raise`, puis
  fermeture via `with client:` dans le générateur. Ce schéma est fragile : si la réponse n'est jamais
  itérée (déconnexion avant le premier octet), ni le client httpx ni `progress_registry.finish` ne
  sont appelés (l'export reste « en cours » jusqu'à l'expiration d'1 h). Les autres endpoints
  utilisent uniformément `with client_factory() as client`.
- Recommandation : passer par `StreamingResponse(..., background=BackgroundTask(cleanup))` pour la
  fermeture/`finish`, ou un `contextlib.ExitStack` transféré au générateur ; documenter l'invariant.

### [Faible] `include_subfolders=false` sur la racine produit silencieusement une archive vide
- Emplacement : `backend/app/archive.py:88-90`, `frontend/app/dossiers/page.tsx:105-106`
- Problème : la règle « toute la boîte implique les sous-dossiers » n'est appliquée que par le
  frontend. Un appel API `folder_id=1&include_subfolders=false` cherche `inid:1` (la racine ne
  contient aucun message) et renvoie un ZIP vide sans erreur.
- Recommandation : appliquer la règle dans `ExportRequest.folders()` (ignorer `include_subfolders`
  pour la racine) ou renvoyer un 422 explicite ; supprimer alors la logique côté frontend.

### [Faible] Réponses Zimbra mal formées → 500 non maîtrisé
- Emplacement : `backend/app/zimbra.py:154`, `backend/app/zimbra.py:225`, `backend/app/zimbra.py:261`, `backend/app/zimbra.py:276`, `backend/app/zimbra.py:250-252`
- Problème : `data["id"]`, `response["folder"][0]`, `m["id"]` lèvent `KeyError`/`IndexError`, et
  `data.get("Body", {})` lève `AttributeError` si le JSON n'est pas un objet. Ces cas sortent en 500
  avec trace, alors que tout le reste du client convertit les anomalies en `ZimbraError` (502). Par
  ailleurs `login` accepte un `authToken` vide et pose un cookie vide (`zimbra.py:249-252`).
- Recommandation : valider la forme des réponses (petits modèles Pydantic ou garde
  `isinstance(data, dict)`) et lever `ZimbraError("Réponse inattendue de Zimbra")` ; refuser un jeton vide.

### [Faible] Duplication dans `ZimbraClient` (cookie d'auth, corps `SearchRequest`)
- Emplacement : `backend/app/zimbra.py:213`, `backend/app/zimbra.py:335`, `backend/app/zimbra.py:266-273`, `backend/app/zimbra.py:288-296`
- Problème : l'en-tête `Cookie: ZM_AUTH_TOKEN=...` est construit à deux endroits ; les corps de
  `search_messages` et `search_addresses` répètent `types`, `limit`, `query`, `locale fr_FR` (la
  locale en dur n'est d'ailleurs pas liée à `ZONE_ID`).
- Recommandation : `_auth_headers(token)` et `_search(token, query, limit, **extra)` privés ; constante
  `SEARCH_LOCALE`.

### [Faible] `_walk` (archive) réimplémente `Folder.walk` (zimbra)
- Emplacement : `backend/app/archive.py:106-110`, `backend/app/zimbra.py:98-101`
- Problème : deux parcours d'arbre quasi identiques dans deux modules ; la notion « dossiers exclus »
  vit hors de la classe `Folder`.
- Recommandation : `Folder.walk(self, excluded: frozenset[str] = frozenset())` et suppression de `_walk`.

### [Faible] Duplication de modèles de réponse et de constantes de cookie dans `main.py`
- Emplacement : `backend/app/main.py:97-100` et `254-258` ; `main.py:57`, `main.py:137`, `main.py:147`
- Problème : `SummaryResponse` recopie champ par champ `ExportSummary` puis est rempli à la main.
  Le chemin `"/api"` du cookie est répété trois fois (set + 2 delete) : un oubli lors d'un changement
  empêcherait la suppression du cookie.
- Recommandation : renvoyer directement le dataclass (`-> ExportSummary`, FastAPI le sérialise) ou
  `SummaryResponse.model_validate(summary, from_attributes=True)` ; constante `COOKIE_PATH = "/api"`
  et helper `clear_session_cookie(response)`.

### [Faible] Chaînage d'exceptions manquant et imports non triés
- Emplacement : `backend/app/main.py:130`, `backend/app/main.py:215`, `backend/app/archive.py:8-19`, `backend/app/archive.py:72-76`
- Problème : `raise HTTPException(...)` dans un `except` sans `from` (B904) masque la cause dans les
  traces ; `from typing import BinaryIO` est inséré au milieu des imports `email`/`datetime` (I001) ;
  `contains` peut être simplifié (SIM103).
- Recommandation : `raise HTTPException(...) from None` (cause volontairement cachée) ; `ruff --fix`
  avec une config `[tool.ruff]` versionnée.

### [Faible] Hook `useExportProgress` logé dans le fichier d'un composant d'affichage
- Emplacement : `frontend/components/DownloadOverlay.tsx:18-65`
- Problème : la logique de polling (timeouts, statuts, 404/401) est exportée depuis
  `DownloadOverlay.tsx` et importée par la page ; cohésion faible et test isolé du hook plus difficile.
- Recommandation : déplacer dans `frontend/lib/useExportProgress.ts` (ou `hooks/`), le composant ne
  gardant que le rendu.

### [Faible] Typage TS non vérifié à l'exécution et casts
- Emplacement : `frontend/lib/api.ts:96`, `frontend/lib/api.ts:98`, `frontend/components/FilterEditor.tsx:113`, `frontend/components/FilterEditor.tsx:136`, `frontend/components/FilterEditor.tsx:166`, `frontend/components/FilterEditor.tsx:181`
- Problème : `undefined as T` et `response.json() as Promise<T>` donnent un typage fort de façade
  (aucune validation) ; les `e.target.value as LogicalOperator` / `as FilterCondition["field"]` sont
  sûrs tant que les `<option>` restent synchronisées avec les types, sans garde du compilateur.
- Recommandation : surcharges `request<void>` séparées pour le 204 ; types générés depuis OpenAPI
  (cf. finding Moyenne) ; petite fonction `isLogicalOperator(v): v is LogicalOperator`.

### [Faible] Duplication des templates `backend.yaml` / `frontend.yaml`
- Emplacement : `charts/zimbra-archivage/templates/backend.yaml:1-82`, `charts/zimbra-archivage/templates/frontend.yaml:1-83`
- Problème : Deployment + Service quasi identiques (pull secrets, securityContext, sondes, blocs
  `nodeSelector`/`affinity`/`tolerations`, Service). Sondes sans `timeoutSeconds`, `periodSeconds`,
  `failureThreshold` explicites, liveness = readiness (voir aussi perf). `service.type` unique pour les
  deux services. Les annotations par défaut de `ingress` (`values.yaml:75-78`) sont spécifiques
  ingress-nginx alors que `className` est vide par défaut.
- Recommandation : helpers `zimbra-archivage.scheduling` / `zimbra-archivage.service` (appelés avec
  `dict "root" . "component" "backend" "values" .Values.backend`), sondes paramétrables par composant
  dans `values.yaml`, `service.type` par composant ; documenter le couplage annotations ↔ nginx.

### [Faible] Workflow de release : `latest` sur une prerelease, version non validée
- Emplacement : `.github/workflows/release.yml:10-12`, `.github/workflows/release.yml:76`, `.github/workflows/release.yml:137`, `docker-compose.yml:9`, `docker-compose.yml:29`
- Problème : chaque release est marquée `--prerelease` mais pousse aussi `:latest`, tag utilisé par
  défaut dans `docker-compose.yml` (`ARCHIVAGE_VERSION:-latest`) : incohérent et non reproductible.
  L'input `version` de `workflow_dispatch` n'est pas validé (un `v0.1.0` donnerait le tag `vv0.1.0`).
  Les notes de release (heredoc l. 102-132) dupliquent le README et divergeront.
- Recommandation : ne pousser `latest` que pour une release stable (ou supprimer `--prerelease`),
  valider la version (`[[ "$V" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]`), renvoyer vers le README dans les notes.

---

## Info

### [Info] Deux chemins de routage `/api` aux comportements divergents
- Emplacement : `frontend/app/api/[...path]/route.ts:1-61`, `charts/zimbra-archivage/templates/ingress.yaml:24-30`
- Problème : en Kubernetes l'ingress envoie `/api` directement au backend ; en Compose/dev, c'est le
  proxy Next.js qui relaie (seulement `GET`/`POST`, filtrage d'en-têtes maison). Un bug ou un
  en-tête manquant ne se reproduira que dans un des deux modes.
- Recommandation : documenter ce choix dans le README (déjà partiellement fait l. 94-96) et tester
  les deux modes, ou utiliser `rewrites` de Next.js si la lecture runtime de `BACKEND_URL` n'est
  plus nécessaire.

### [Info] `toMessageFilter` calculé deux fois par rendu
- Emplacement : `frontend/app/dossiers/page.tsx:84`, `frontend/components/FilterEditor.tsx:80`
- Problème : la page et l'éditeur convertissent chacun le brouillon ; sans gravité mais deux sources de vérité.
- Recommandation : passer le filtre calculé en prop à `FilterEditor`.

### [Info] Compteur global mutable pour les clés React
- Emplacement : `frontend/components/FilterEditor.tsx:34-35`
- Problème : `let lastKey = 0` au niveau module ; fonctionne côté client mais état global implicite.
- Recommandation : acceptable ; alternative `crypto.getRandomValues` ou compteur dans un `useRef`.

### [Info] Écarts mineurs README / code / configuration
- Emplacement : `README.md:20`, `README.md:119-130`, `docker-compose.yml:11-17`, `.env.example`
- Problème : « retentés jusqu'à 5 fois » est exact (5 retries) mais contredit le message d'erreur du
  code (cf. finding retry). `docker-compose.yml` ne relaie pas `SEARCH_BATCH_SIZE`,
  `MAX_FILE_NAME_LENGTH`, `HTTP_TIMEOUT` : les variables documentées dans le README sont sans effet
  en Compose si on les ajoute à `.env`. Le reste du README (endpoints, options, cookie, Helm) est
  cohérent avec le code.
- Recommandation : `env_file: .env` dans le service `backend` du Compose, ou lister ces variables.

---

## Renvois vers les autres axes

- **perf.md** : pagination `SearchRequest` par offset sans `sortBy`, entrelacée avec les
  téléchargements (`zimbra.py:263-284` et `zimbra.py:317-326`, `archive.py:197`) ; un mail arrivé pendant l'export décale les
  offsets (entrée ZIP en double ou message manquant). Traité par perf, à lire aussi comme un bug de
  correction. Également côté perf : délai de retry de 363 s cumulés et sondes du chart.
- **security.md** : messages httpx et Zimbra relayés tels quels au client et dans `erreurs.txt`
  (rejoint le constat « Aucune journalisation » : journaliser le détail côté serveur et renvoyer un
  message générique) ; permissions en écriture et actions non épinglées par SHA dans
  `release.yml` ; chart sans NetworkPolicy ni `automountServiceAccountToken: false`.
- **tests.md** : deux bugs de correction vérifiés par script, non comptés ici.
  - `archive.py:279` : quand la racine d'un message mono-partie est elle-même la pièce jointe
    (`application/pdf` avec `Content-Disposition: attachment`), `extract_attachment_files` ne renvoie
    rien, à cause de la condition `not is_root`.
  - `archive.py:131-138` : le suffixe `_<id>.eml` n'est pas borné, donc un id long dépasse
    `max_file_name_length` (74 caractères pour une limite de 50).

  Ils confirment aussi le retry à 6 essais et le constat sur `SessionExpired` de ce rapport.

---

## Points forts

- Code backend court, bien découpé par responsabilité (`zimbra` = transport, `archive` = ZIP,
  `filters` = traduction de requête, `progress`, `suggestions`), annoté en types partout, dataclasses
  `frozen`, injection de dépendances FastAPI (`Settings`, fabrique de client, registre) qui rend le code testable.
- Docstrings et commentaires en français, utiles et expliquant le « pourquoi » (spooling, ZIP
  séquentiel, dates < 1980, cookies).
- Gestion des noms de fichiers robuste (`sanitize_path_component`, `_unique_name`, troncature avec
  extension) et streaming du ZIP sans stockage.
- Frontend en TypeScript `strict`, composants accessibles (rôles ARIA tree/combobox/progressbar),
  annulation propre des effets (`cancelled`, `clearTimeout`).
- Chart Helm propre : helpers de nommage/labels standard, `required` sur les URLs, `checksum/config`,
  securityContext durci (non-root, rootfs en lecture seule, drop ALL, seccomp) avec volumes `emptyDir`
  adaptés, NOTES.txt utile. README complet et globalement à jour.
