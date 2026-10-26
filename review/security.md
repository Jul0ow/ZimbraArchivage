# Revue de sécurité — ZimbraArchivage

Lentille : sécurité (lecture seule du code source). Périmètre : backend FastAPI, proxy Next.js, Dockerfiles, docker-compose, chart Helm, workflow GitHub.

## Résumé

Le socle est plutôt sain : cookie `HttpOnly` + `Secure` par défaut, URLs Zimbra uniquement issues de la configuration (pas d'entrée utilisateur dans l'URL), vérification TLS activée par défaut, noms de fichiers du ZIP assainis (pas de zip-slip trouvé), `Content-Disposition` construit à partir d'un nom ASCII filtré, valeurs de filtres correctement échappées, aucun CORS ouvert, conteneurs non-root avec `readOnlyRootFilesystem`. Aucun secret en dur n'a été trouvé (`.env` ignoré par git et par `.dockerignore`).

Les principaux risques sont : l'absence de limites de ressources/concurrence sur l'export (DoS par un utilisateur authentifié), l'absence de protection contre le brute-force du login (le backend sert d'oracle de mots de passe Zimbra), la non-révocation du jeton Zimbra à la déconnexion, des fuites d'informations internes dans les messages d'erreur, des en-têtes de sécurité HTTP absents, et un durcissement CI/Helm incomplet (permissions larges, actions non épinglées, pas de NetworkPolicy).

Points vérifiés et jugés corrects (pas de finding) : injection dans les requêtes Zimbra (`filters.py:26-30` échappe `\` et `"`, `folder_id` n'est jamais pris de l'entrée brute mais de l'arbre Zimbra, `main.py:230`), zip-slip (`archive.py:120-123`, `archive.py:283-293`), injection d'en-tête `Content-Disposition` (`main.py:271-276` ne laisse que `[A-Za-z0-9._-]`), `follow_redirects` désactivé par défaut dans httpx.

## Findings

### [Élevée] Aucune limite de ressources ni de concurrence sur l'export (DoS par un utilisateur authentifié)
- Emplacement : `backend/app/main.py:279-332`, `backend/app/main.py:243-258`, `backend/app/archive.py:30`, `backend/app/archive.py:214-250`, `charts/zimbra-archivage/templates/backend.yaml:51-54`
- Problème : un utilisateur authentifié peut lancer un nombre illimité d'exports (et de `/api/export/summary`, qui parcourt tous les messages) en parallèle. Les endpoints sont synchrones donc exécutés dans le pool de threads AnyIO (40 par défaut) : quelques requêtes lentes saturent le pool et rendent aussi `/api/health` (probe liveness) indisponible, ce qui peut faire redémarrer le pod. Aucune limite sur : nombre de messages, taille cumulée de l'archive, taille d'un `.eml` (`download_eml` lit tout ce que renvoie Zimbra), durée totale. Le `.eml` est écrit dans un `SpooledTemporaryFile` au-delà de 10 Mo (`/tmp`, `emptyDir` sans `sizeLimit`) : plusieurs gros messages concurrents peuvent remplir le stockage éphémère du nœud. Le `_ChunkSink` (`archive.py:160-176`) accumule en mémoire tout le contenu compressé d'un message avant `drain()`, donc la mémoire par export vaut la taille du plus gros message (la limite du pod est 512 Mi).
- Recommandation : ajouter une limite de concurrence par session et globale (sémaphore / `asyncio.Semaphore`, ou un compteur par hash de jeton renvoyant 429), un plafond configurable (`MAX_EXPORT_MESSAGES`, `MAX_MESSAGE_SIZE`, `MAX_ARCHIVE_SIZE`) vérifié via `message.size` (déjà fourni par la recherche Zimbra) et en cours de téléchargement, `sizeLimit` sur l'`emptyDir` et `resources.limits.ephemeral-storage` dans le chart. Exemple :
  ```python
  if message.size > settings.max_message_size:
      return f"message trop volumineux ({message.size} octets)"
  ```
  ```yaml
  volumes:
    - name: tmp
      emptyDir: { sizeLimit: 1Gi }
  ```

### [Élevée] Pas de limitation du taux de tentatives sur `/api/auth/login` (oracle de mots de passe, verrouillage de comptes)
- Emplacement : `backend/app/main.py:119-142`, `charts/zimbra-archivage/values.yaml:73-78`
- Problème : l'endpoint relaie les identifiants vers l'`AuthRequest` Zimbra sans aucun rate limiting, ni par IP ni par compte. Toute personne pouvant joindre l'application (souvent exposée sur Internet via l'ingress) peut faire du credential stuffing/brute force contre les comptes Zimbra de l'organisation, ou provoquer le verrouillage de comptes légitimes (déni de service ciblé via la politique de lockout Zimbra). Le backend ne journalise pas non plus les échecs de connexion (pas de piste d'audit).
- Recommandation : ajouter un rate limiting (ex. annotation ingress-nginx `nginx.ingress.kubernetes.io/limit-rps` / `limit-connections` sur `/api/auth/login`, ou middleware type `slowapi`), un délai/backoff par couple IP+identifiant, et journaliser (sans mot de passe) les échecs avec l'IP source. Documenter la dépendance à la politique de lockout Zimbra.

### [Moyenne] Le jeton Zimbra n'est pas invalidé à la déconnexion et le cookie est persistant
- Emplacement : `backend/app/main.py:145-147`, `backend/app/main.py:133-141`, `backend/app/zimbra.py:238-252`
- Problème : `logout` ne fait que supprimer le cookie côté navigateur ; le jeton `ZM_AUTH_TOKEN` reste valide côté Zimbra jusqu'à son expiration (2 jours par défaut). Un jeton volé (poste partagé, journal, proxy) reste donc exploitable après « déconnexion ». Par ailleurs `max_age=lifetime` rend le cookie persistant (survit à la fermeture du navigateur) alors qu'il donne accès à la totalité de la boîte aux lettres.
- Recommandation : appeler `EndSessionRequest` (`urn:zimbraAccount`, `{"logoff": true}`) avec le jeton dans `logout`, en tolérant l'échec ; envisager un cookie de session (sans `max_age`) ou une durée plafonnée configurable.

### [Moyenne] Messages d'erreur internes renvoyés au client (fuite d'informations)
- Emplacement : `backend/app/zimbra.py:195-196`, `backend/app/main.py:61-65`, `backend/app/zimbra.py:228-232`, `backend/app/archive.py:244-246`
- Problème : `ZimbraError(f"Erreur de connexion avec Zimbra : {e}")` embarque le texte de l'exception httpx (nom d'hôte/IP interne de Zimbra, port, détail du certificat TLS, chemin de CA), et le handler `zimbra_error_handler` le renvoie tel quel dans la réponse 502 (`"Erreur Zimbra : ..."`). Les raisons des `Fault` SOAP sont aussi relayées, ainsi que les exceptions d'extraction de pièces jointes (`pièces jointes non extraites ({e})`) qui finissent dans `erreurs.txt`. Cela facilite la reconnaissance de l'infrastructure par un attaquant (y compris non authentifié via `/api/auth/login`, qui ne requiert pas de session).
- Recommandation : journaliser le détail côté serveur et renvoyer un message générique (« Zimbra injoignable ») ; ne relayer que des codes Zimbra connus (`account.AUTH_FAILED`, etc.).

### [Moyenne] En-têtes de sécurité HTTP absents (CSP, anti-framing, nosniff, HSTS, Referrer-Policy)
- Emplacement : `frontend/next.config.ts:1-9`, `backend/app/main.py:21`
- Problème : aucun en-tête de sécurité n'est configuré (seul `poweredByHeader: false`). La page de login peut être mise en iframe (clickjacking / redressement d'interface pour voler des identifiants Zimbra), aucune CSP n'atténue un éventuel XSS futur, pas de HSTS ni de `X-Content-Type-Options`. Le contenu venant de Zimbra (noms de dossiers, adresses) est rendu par React (pas de `dangerouslySetInnerHTML` trouvé), ce qui est sain, mais la défense en profondeur manque.
- Recommandation : ajouter dans `next.config.ts` :
  ```ts
  async headers() {
    return [{ source: "/:path*", headers: [
      { key: "X-Frame-Options", value: "DENY" },
      { key: "X-Content-Type-Options", value: "nosniff" },
      { key: "Referrer-Policy", value: "no-referrer" },
      { key: "Content-Security-Policy", value: "default-src 'self'; frame-ancestors 'none'; ..." },
      { key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" },
    ]}];
  }
  ```
  Penser à poser les mêmes en-têtes sur les réponses `/api` (l'ingress route `/api` directement vers le backend, sans passer par Next, `charts/.../ingress.yaml:25-31`) via un middleware FastAPI ou une annotation d'ingress.

### [Moyenne] Chart Helm : pas de NetworkPolicy, token du ServiceAccount monté, pas de `sizeLimit`
- Emplacement : `charts/zimbra-archivage/templates/backend.yaml:24-54`, `charts/zimbra-archivage/templates/frontend.yaml:55`, `charts/zimbra-archivage/values.yaml:57-66`
- Problème : (1) aucune `NetworkPolicy` : le backend, qui détient des jetons de session Zimbra et envoie des requêtes HTTP sortantes, peut joindre n'importe quelle destination (pas de limitation d'egress), et reste joignable depuis tout pod du cluster ; (2) pas de `ServiceAccount` dédié ni `automountServiceAccountToken: false` : le jeton de l'API Kubernetes est monté inutilement dans des pods qui n'en ont pas besoin ; (3) `emptyDir` sans `sizeLimit` (`backend.yaml:52-54`, `frontend.yaml:54-55`) ; (4) pas de `PodDisruptionBudget`. Les points positifs : `runAsNonRoot`, `seccomp RuntimeDefault`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem`, `drop ALL`, ressources définies (mais voir plus bas pour `ephemeral-storage`).
- Recommandation : ajouter un template `networkpolicy.yaml` (backend : ingress uniquement depuis le frontend et l'ingress controller, egress uniquement vers DNS et l'hôte Zimbra ; frontend : egress vers le backend), `automountServiceAccountToken: false` dans les `spec` de pod, `emptyDir.sizeLimit`, et `limits.ephemeral-storage`.

### [Moyenne] Workflow de release : permissions d'écriture globales et actions non épinglées par SHA
- Emplacement : `.github/workflows/release.yml:14-16`, `.github/workflows/release.yml:25`, `27`, `36`, `62`, `64`, `70`, `89`
- Problème : `permissions: contents: write, packages: write` est déclaré au niveau du workflow, donc le job `test` (qui exécute `pip install` et `npm ci`, c'est-à-dire du code tiers de la chaîne d'approvisionnement) dispose d'un `GITHUB_TOKEN` en écriture sur le dépôt et les packages. Toutes les actions (`actions/checkout@v4`, `setup-python@v5`, `setup-node@v4`, `docker/*`, `azure/setup-helm@v4`) sont référencées par tag mutable et non par SHA de commit ; `azure/setup-helm` et les actions `docker/*` sont des actions tierces dont le compromis permettrait de publier des images malveillantes. Aucune vérification ni attestation des images produites (pas de scan Trivy, pas de signature cosign, pas de provenance/SBOM : `docker/build-push-action` à `provenance`/`sbom` non configurés).
- Recommandation : `permissions: {}` (ou `contents: read`) au niveau racine, puis `contents: write` uniquement dans `release`, `packages: write` uniquement dans `images`, `contents: read` pour `test` ; épingler chaque action par SHA complet (`uses: actions/checkout@<sha> # v4.x.y`) avec Dependabot (`github-actions`) pour les mises à jour ; ajouter `provenance: true`, `sbom: true`, un scan Trivy/Grype bloquant, et une signature cosign.

### [Moyenne] Aucun scan de dépendances ni de CI sur les PR, dépendances transitives non verrouillées côté Python
- Emplacement : `backend/requirements.txt:1-4`, `backend/Dockerfile:11`, `.github/` (pas de `dependabot.yml`), `.github/workflows/release.yml:8-11`
- Problème : `requirements.txt` n'épingle que les 4 dépendances directes ; starlette, pydantic, h11, anyio, etc. sont résolues à chaque build (builds non reproductibles, versions vulnérables possibles sans visibilité), sans hashs (`--require-hashes`). Le seul workflow ne s'exécute qu'au tag ou en `workflow_dispatch` : les tests/lint ne tournent pas sur les PR et il n'y a ni `pip-audit`/`npm audit`, ni CodeQL, ni Dependabot. Je n'ai pas pu vérifier l'existence de CVE sur les versions listées (fastapi 0.159.1, uvicorn 0.54.0, httpx 0.28.1, pydantic-settings 2.16.1, next 16.4.2, react 19.3.1) : à contrôler avec `pip-audit` et `npm audit`.
- Recommandation : générer un fichier verrouillé avec hashs (`pip-compile --generate-hashes`, ou `uv lock`), ajouter `.github/dependabot.yml` (pip, npm, docker, github-actions), un workflow `ci.yml` sur `pull_request` (tests + `pip-audit` + `npm audit --omit=dev` + CodeQL).

### [Moyenne] Proxy Next.js : corps de requête entièrement mis en mémoire sans limite, et normalisation de chemin non maîtrisée
- Emplacement : `frontend/app/api/[...path]/route.ts:33-36`, `frontend/app/api/[...path]/route.ts:44`
- Problème : (1) `await request.arrayBuffer()` charge tout le corps des requêtes POST en mémoire (limite du pod : 256 Mi) sans contrôle de taille, avant toute authentification : une requête POST volumineuse (ou plusieurs en parallèle) vers `/api/auth/login` suffit à saturer le frontend. (2) Les segments du chemin sont passés à `encodeURIComponent`, qui laisse intacts `.` et `..` ; `new URL("/api/../redoc", BACKEND_URL)` normalise ensuite le chemin vers `/redoc`. Un segment `..` encodé en `%2e%2e` pourrait permettre de sortir du préfixe `/api/` pour atteindre d'autres routes du backend (ex. `/redoc`, `/docs/oauth2-redirect`, routes futures non préfixées). Impact actuel limité (le backend ne possède que des routes `/api/*` plus la doc), mais le proxy ne garantit pas le confinement à `/api/`. À vérifier par un test (non réalisé ici).
- Recommandation : rejeter explicitement les segments `.`/`..`/vides, et vérifier que `target.pathname.startsWith("/api/")` ; limiter la taille du corps (`Content-Length` > 16 Ko pour le login → 413) et ne pas bufferiser (`duplex: "half"`, stream `request.body`) ; ne relayer que les méthodes/chemins attendus (liste blanche des routes).
  ```ts
  if (path.some((s) => s === "." || s === ".." || s === "")) return new Response(null, { status: 400 });
  ```

### [Moyenne] Cookie `SameSite=Lax` et export en GET : déclenchement d'actions cross-site, pas de protection CSRF explicite
- Emplacement : `backend/app/main.py:133-141`, `backend/app/main.py:279-288`, `backend/app/main.py:119-142`
- Problème : `SameSite=Lax` envoie le cookie sur les navigations top-level cross-site en GET. Une page tierce peut donc faire télécharger/lancer `GET /api/export?folder_id=1...` (travail lourd côté serveur et Zimbra, lancé au nom de la victime, cf. DoS ci-dessus) en la faisant naviguer. La lecture du résultat reste impossible (pas de CORS, ce qui est bien). Le login est aussi vulnérable au « login CSRF » (la victime est connectée sur le compte de l'attaquant) car aucun en-tête Origin/Fetch-Metadata n'est vérifié, et le corps JSON n'est pas verrouillé sur `Content-Type: application/json` (FastAPI exige JSON pour les modèles Pydantic, ce qui limite déjà les formulaires classiques).
- Recommandation : passer à `samesite="strict"` (l'application est mono-origine, le coût UX est faible puisque l'export est lancé depuis l'application elle-même), et/ou refuser les requêtes dont `Sec-Fetch-Site` vaut `cross-site` sur les endpoints sensibles via un middleware.

### [Faible] Vérification TLS désactivable sans avertissement et sans option de CA personnalisée ; URLs Zimbra non validées (HTTP accepté)
- Emplacement : `backend/app/config.py:14-16`, `backend/app/config.py:26-27`, `backend/app/config.py:32-34`, `backend/app/zimbra.py:173-177`, `.env.example:5-6`, `charts/zimbra-archivage/values.yaml:7`
- Problème : (1) `VERIFY_TLS=false` désactive toute validation (le jeton Zimbra, les identifiants et les mails transitent alors sans authentification du serveur) sans avertissement au démarrage ; faute d'option pour fournir un bundle de CA interne (`VERIFY_TLS` ne prend qu'un booléen), les administrateurs avec CA interne sont poussés vers `false`. (2) `zimbra_soap_url` / `zimbra_rest_url` ne sont pas validés (schéma) : une URL `http://` envoie identifiants et jeton en clair ; aucune cohérence imposée entre les deux URLs (hôtes différents → jeton envoyé à un autre hôte). Ces valeurs sont issues de la configuration d'un administrateur (pas de SSRF depuis les utilisateurs : aucune URL n'est dérivée d'une entrée utilisateur), donc risque de mauvaise configuration seulement.
- Recommandation : accepter `VERIFY_TLS` comme booléen ou chemin de fichier CA (`ZIMBRA_CA_BUNDLE`), journaliser un `WARNING` si désactivé, valider via `pydantic.AnyHttpUrl` que le schéma est `https` (avec exception explicite `ALLOW_INSECURE_HTTP`), et vérifier que SOAP et REST ciblent le même hôte.

### [Faible] Jeton du cookie injecté tel quel dans l'en-tête `Cookie` envoyé à Zimbra
- Emplacement : `backend/app/zimbra.py:213`, `backend/app/zimbra.py:335`, `backend/app/main.py:39-43`
- Problème : la valeur du cookie `zimbra_session` n'est pas validée (format, longueur, caractères) avant d'être concaténée dans `Cookie: ZM_AUTH_TOKEN=<token>`. Un client pouvant forger son propre cookie peut y glisser `;` et d'autres cookies Zimbra (`ZM_AUTH_TOKEN=x; ZM_TRUST_TOKEN=...`) ; l'impact reste limité à sa propre session (httpx/h11 rejettent les retours à la ligne). Par ailleurs le jeton complet est la clé du registre de progression (haché, ce qui est bien).
- Recommandation : valider le jeton (`^[A-Za-z0-9_.~-]{1,4096}$`) dans `get_token` et renvoyer 401 sinon ; passer par `httpx.Cookies`/`cookies=` plutôt que par une chaîne d'en-tête.

### [Faible] Noms de fichiers : caractères Unicode de contrôle bidirectionnel et noms réservés Windows non traités
- Emplacement : `backend/app/archive.py:27`, `backend/app/archive.py:120-123`, `backend/app/archive.py:283-293`
- Problème : l'assainissement (`INVALID_PATH_CHARS`) neutralise bien `/`, `\`, `..` (transformé en `_` via `rstrip(".")`) et les contrôles `\x00-\x1f`, donc pas de zip-slip. En revanche : les caractères Unicode d'inversion (`U+202E` RLO, etc.) ne sont pas supprimés (un pièce jointe `facture‮fdp.exe` s'affiche comme `facturexe.pdf`), les noms réservés Windows (`CON`, `NUL`, `COM1`…) ne sont pas traités, les caractères `\x7f` et `\x80-\x9f` non plus. Les noms d'extraction sont de plus entièrement contrôlés par l'expéditeur du mail. Les noms de pièces jointes issus de `get_filename()` ne sont pas limités en octets (limite en caractères seulement, `MAX_ATTACHMENT_NAME_LENGTH`), ce qui peut dépasser 255 octets en UTF-8 à l'extraction.
- Recommandation : supprimer les catégories Unicode `Cc`/`Cf` (`unicodedata.category`), préfixer `_` les noms réservés Windows, tronquer sur 255 octets UTF-8. Conserver l'extraction « à plat » (un seul niveau) actuelle.

### [Faible] Extraction des pièces jointes : tout le contenu décodé gardé en mémoire, pas de limite de profondeur/nombre
- Emplacement : `backend/app/archive.py:244`, `backend/app/archive.py:253-270`, `backend/app/archive.py:264`
- Problème : `list(extract_attachment_files(buffer))` décode et conserve en mémoire toutes les pièces jointes du message en même temps (`get_payload(decode=True)`), en plus du parsing complet du `.eml` par `BytesParser`. Un message contenant de nombreuses/volumineuses parties (ou fortement imbriqué : `message/rfc822` dans `message/rfc822`, `as_bytes()` re-sérialise) multiplie l'usage mémoire par 3 à 4 par rapport à la taille du `.eml`, d'où un risque d'OOM (limite 512 Mi) déclenchable par un expéditeur externe (le mail piégé n'a pas besoin d'être ouvert, il suffit qu'un utilisateur archive sa boîte avec l'option activée). `except Exception` (ligne 245) attrape les `RecursionError` mais ne protège pas d'un `MemoryError` fatal pour le processus.
- Recommandation : traiter les pièces jointes au fil de l'eau (écrire chaque partie directement dans l'archive au lieu de `list(...)`), fixer des plafonds (nombre de parties, taille décodée par partie, profondeur) et vérifier `message.size` avant d'activer l'extraction.

### [Faible] Documentation OpenAPI exposée et `/redoc` actif par défaut
- Emplacement : `backend/app/main.py:21`, `charts/zimbra-archivage/templates/ingress.yaml:25-31`
- Problème : `docs_url="/api/docs"` et `openapi_url="/api/openapi.json"` sont actifs en production et publiés par l'ingress (préfixe `/api`) sans authentification ; `redoc_url` n'est pas désactivé (route `/redoc`). Cela décrit l'intégralité de la surface d'API aux attaquants et la page Swagger charge des scripts depuis un CDN externe.
- Recommandation : désactiver en production (`FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` sauf si `ENABLE_DOCS=true`).

### [Faible] Journaux et mémoire : paramètres de requête sensibles dans les access logs, registre de progression non borné
- Emplacement : `backend/Dockerfile:20`, `frontend/lib/api.ts:126-127`, `frontend/lib/api.ts:133-134`, `backend/app/progress.py:35-44`
- Problème : les filtres de recherche (mots-clés d'objet, adresses d'expéditeurs/destinataires) et les saisies d'autocomplétion transitent en query string GET (`/api/export?...filter=...`, `/api/addresses?q=...`) et sont écrits dans les access logs d'uvicorn et de l'ingress : cela journalise des données personnelles et le périmètre de ce qu'un utilisateur archive. Les identifiants/jetons ne sont, eux, pas journalisés (POST + cookie), ce qui est correct. Côté mémoire, `ProgressRegistry.start` ne purge les entrées expirées qu'à l'ouverture d'un export et n'a pas de plafond : un utilisateur peut y créer un nombre illimité d'entrées via des `export_id` distincts pendant une heure.
- Recommandation : désactiver ou masquer la query string dans les access logs (`--no-access-log` ou formatteur personnalisé), ou passer les filtres en POST ; plafonner le nombre d'entrées par session dans `ProgressRegistry`.

### [Faible] Docker : images de base non épinglées, pas de HEALTHCHECK frontend, compose sans durcissement ni liaison locale du port
- Emplacement : `backend/Dockerfile:1`, `frontend/Dockerfile:1`, `frontend/Dockerfile:6`, `frontend/Dockerfile:13`, `docker-compose.yml:33-34`, `docker-compose.yml:7-30`, `.env.example:11`
- Problème : `python:3.13-slim` et `node:22-alpine` sont des tags mutables (pas de digest) : builds non reproductibles. Les utilisateurs sont bien non-root (`USER 10001` / `USER 1000`), le backend n'est pas publié et les systèmes de fichiers sont en lecture seule (points positifs). En revanche : `docker-compose.yml:33-34` publie le frontend sur `0.0.0.0:3000` en HTTP avec `COOKIE_SECURE: "false"` (ligne 17 du compose) : identifiants Zimbra et jeton circulent en clair sur le réseau local ; pas de `cap_drop: [ALL]` ni `security_opt: [no-new-privileges:true]` ; `ARCHIVAGE_VERSION` vaut `latest` par défaut (image mutable) ; pas de `HEALTHCHECK` dans les Dockerfiles ; uvicorn avec `--host 0.0.0.0` sans `--proxy-headers`/`--forwarded-allow-ips` maîtrisé (les IP clients des logs seront celles du proxy).
- Recommandation : épingler les images par digest (`python:3.13-slim@sha256:...`), lier le port à `127.0.0.1:${ARCHIVAGE_PORT:-3000}:3000` par défaut (et documenter un reverse proxy TLS pour tout autre usage), ajouter `cap_drop: [ALL]` et `security_opt: ["no-new-privileges:true"]` aux deux services, épingler `ARCHIVAGE_VERSION`.

### [Faible] Ingress : TLS facultatif et non forcé, pas de limites de taille ni de débit
- Emplacement : `charts/zimbra-archivage/values.yaml:71-83`, `charts/zimbra-archivage/templates/ingress.yaml:16-19`
- Problème : `ingress.tls: []` par défaut et aucune annotation de redirection HTTPS (`ssl-redirect`) ni d'émetteur cert-manager : un déploiement par défaut expose le login en HTTP (le cookie `Secure` empêchera au moins la session de fonctionner, mais les identifiants seront envoyés en clair si le navigateur essaie). Pas de `proxy-body-size` ni de `limit-rps` sur `/api`. Les `proxy-read-timeout` / `proxy-send-timeout` de 3600 s s'appliquent à toute la route et facilitent l'épuisement de connexions (slowloris sur `/api`).
- Recommandation : rendre `ingress.tls` obligatoire (`required`) quand `ingress.enabled`, ajouter `nginx.ingress.kubernetes.io/ssl-redirect: "true"`, `proxy-body-size: "64k"` pour `/api` (le seul corps volumineux est le login JSON), et des limites de débit, avec des timeouts longs réservés au chemin `/api/export`.

### [Info] Proxy Next.js : retransmission de tous les en-têtes entrants (dont `X-Forwarded-*`)
- Emplacement : `frontend/app/api/[...path]/route.ts:9-26`, `frontend/app/api/[...path]/route.ts:43`
- Problème : tous les en-têtes du client sont relayés au backend à l'exception d'une courte liste noire ; un client peut donc forger `X-Forwarded-For`/`X-Forwarded-Host`/`X-Real-IP` si le backend les utilisait un jour (rate limiting par IP, journalisation). Aucune route ne s'en sert aujourd'hui.
- Recommandation : passer à une liste blanche (`cookie`, `content-type`, `accept`, `user-agent`) et poser un `X-Forwarded-For` contrôlé par le proxy.

### [Info] Pas de CORS ni de secrets en dur (points positifs à préserver)
- Emplacement : `backend/app/main.py:21-29`, `.gitignore:6`, `backend/.dockerignore:5`, `.env.example`, `backend/.env.example`
- Problème : aucun `CORSMiddleware` (même origine uniquement, correct), aucun secret/identifiant commité (les fichiers `.env.example` ne contiennent que des URLs d'exemple), `.env` exclu de git et des contextes de build Docker, aucun `pickle`/`eval`/`shell=True`, aucun `dangerouslySetInnerHTML` côté React, couleur des dossiers validée par regex (`zimbra.py:42`), `export_id` validé par regex (`main.py:51`).
- Recommandation : conserver ces pratiques ; ajouter un test automatisé qui échoue si un `CORSMiddleware` permissif est introduit, et un scan de secrets (gitleaks) en CI.
