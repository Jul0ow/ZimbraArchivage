# Revue sécurité — zimbraarchivage

Périmètre : backend FastAPI (`backend/app/*`), proxy Next.js (`frontend/app/api/[...path]/route.ts`), frontend, Dockerfiles, docker-compose, chart Helm, CI `release.yml`. Revue en lecture seule ; chaque constat a été vérifié dans le code.

## Synthèse

| Sévérité | Nombre |
|---|---|
| Critique | 0 |
| Élevée | 2 |
| Moyenne | 5 |
| Faible | 6 |
| Info | 6 |

Aucune vulnérabilité exploitable directement pour lire les données d'un autre utilisateur n'a été trouvée. Les points les plus sensibles sont le déni de service (exports non bornés, extraction des pièces jointes en mémoire) et le cycle de vie du jeton Zimbra (logout non révoqué, jeton Zimbra « complet » stocké chez le client).

---

## Élevée

### [Élevée] Déni de service : aucun plafond d'exports simultanés (threadpool partagé)
- Emplacement : `backend/app/main.py:279-332`, `backend/app/main.py:243-258`, `backend/app/zimbra.py:188-200`
- Problème : `/api/export` renvoie un `StreamingResponse` sur un générateur **synchrone**. Starlette l'itère dans le threadpool anyio, limité à 40 threads par défaut et partagé avec tous les endpoints `def` (login, folders, addresses…). Rien ne limite le nombre d'exports par session ni au total, et `/api/export/summary` parcourt lui aussi toute la boîte en synchrone. Scénarios concrets :
  - un utilisateur authentifié (ou un script avec son cookie) ouvre une quarantaine d'exports `folder_id=1` en parallèle. Les autres utilisateurs ne peuvent plus se connecter tant que ces exports tournent, ce qui peut durer des heures ;
  - un site tiers peut déclencher l'export chez une victime connectée par navigation top-level (`window.open("https://archivage/api/export?folder_id=1&extract_attachments=true")`) : le cookie `SameSite=Lax` est envoyé sur un GET top-level ;
  - quand Zimbra répond 502/503/504, chaque appel bloque un thread jusqu'à 3+9+27+81+243 = **363 s** de `sleep` (`zimbra.py:192`), ce qui amplifie la saturation ;
  - aggravant (relevé par « perf ») : `/api/health` (`main.py:114`) est lui aussi un `def` synchrone servi par ce même pool. Pool saturé, la liveness probe échoue, kubelet redémarre le pod, et **tous les exports en cours de tous les utilisateurs sont coupés**.
  
  Le README prévoit `replicaCount: 1` par défaut, donc un seul processus pour tout le monde.
- Recommandation : limiter les exports concurrents par session (clé `sha256(token)`, déjà calculée dans `progress.py:414`) et globalement, avec un sémaphore et une réponse 429 au-delà. Plafonner le backoff (par exemple `min(3**attempt, 10)`) et la durée totale des retries. Exemple :
  ```python
  _export_slots = threading.BoundedSemaphore(settings.max_concurrent_exports)
  if not _export_slots.acquire(blocking=False):
      raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Trop d'exports en cours")
  # release dans le finally de content()
  ```
  Passer `/api/health` en `async def` pour qu'il ne dépende plus du threadpool. Recoupe l'axe perf : la cause racine et l'optimisation sont détaillées dans `review/perf.md`, ce fichier ne garde que l'angle exploitabilité.

### [Élevée] Déni de service mémoire par un tiers : extraction des pièces jointes entièrement en RAM
- Emplacement : `backend/app/archive.py:240-249`, `backend/app/archive.py:253-267`, `charts/zimbra-archivage/values.yaml:31-33`
- Problème : avec `extract_attachments=true`, `BytesParser(...).parse(eml)` charge tout le .eml en mémoire. `list(extract_attachment_files(buffer))` conserve ensuite **toutes** les pièces jointes décodées, puis `archive.writestr` en fait une copie supplémentaire. Le contenu du mail est contrôlé par **n'importe quel expéditeur externe**. Un mail de 50 Mo (taille souvent acceptée par Zimbra) peut ainsi occuper 150 à 250 Mo au pic, et une archive `message/rfc822` imbriquée garde de plus un objet `EmailMessage` complet (`inner.as_bytes()`). Avec la limite de 512Mi du backend, un ou deux exports concurrents d'une boîte contenant un tel mail déclenchent un OOMKill qui coupe **tous** les exports en cours de tous les utilisateurs. Le `SpooledTemporaryFile` limite bien la taille du .eml brut, mais pas le parsing.
- Recommandation : ne pas extraire au-delà d'une taille de mail configurable (`message.size`, connue grâce à la recherche ; noter « pièces jointes non extraites (mail trop volumineux) » dans `erreurs.txt`). Écrire chaque pièce jointe dès qu'elle est décodée plutôt que de matérialiser la liste. Borner la profondeur de récursion et le nombre de parties. Recoupe l'axe perf.

---

## Moyenne

### [Moyenne] Logout sans révocation du jeton Zimbra
- Emplacement : `backend/app/main.py:145-147`
- Problème : `/api/auth/logout` supprime seulement le cookie du navigateur. Le jeton Zimbra reste valide côté serveur jusqu'à la fin de `zimbraAuthTokenLifetime` (2 jours par défaut), sur l'application **et** directement sur Zimbra (webmail, SOAP, ReST), puisqu'il s'agit d'un `ZM_AUTH_TOKEN` standard. Un jeton intercepté avant la déconnexion (poste partagé, proxy, cookie copié) reste donc pleinement utilisable après celle-ci, alors que l'utilisateur croit sa session fermée.
- Recommandation : appeler `EndSessionRequest` (`urn:zimbraAccount`, avec `logoff="1"`) avant de supprimer le cookie. Ignorer l'erreur si le jeton a déjà expiré.
  ```python
  @app.post("/api/auth/logout", status_code=204)
  def logout(request: Request, response: Response, client_factory: ClientFactoryDep) -> None:
      if token := request.cookies.get(COOKIE_NAME):
          with client_factory() as client:
              try:
                  client.end_session(token)  # EndSessionRequest logoff=1
              except ZimbraError:
                  pass
      response.delete_cookie(COOKIE_NAME, path="/api")
  ```

### [Moyenne] Le cookie de session contient le jeton Zimbra brut (portée bien plus large que l'application)
- Emplacement : `backend/app/main.py:133-141`
- Problème : la valeur du cookie `zimbra_session` est le `authToken` Zimbra lui-même. Le cookie est bien protégé (HttpOnly, Secure par défaut, SameSite=Lax, Path=/api ; testé dans `test_api.py:68`). Mais toute fuite donne un accès **complet** à la messagerie, au-delà de la lecture : envoi de mails, suppression, règles de transfert. Les vecteurs de fuite plausibles sont un déploiement en `COOKIE_SECURE=false` sur HTTP, un proxy ou un ingress qui journalise les cookies, ou un poste partagé. La durée de vie du cookie est alignée sur celle du jeton (jusqu'à plusieurs jours), sans expiration d'inactivité propre à l'application.
- Recommandation : en défense en profondeur, chiffrer ou authentifier le jeton avec une clé serveur (Fernet/AES-GCM, clé dans un `Secret` k8s). Un jeton copié hors de l'application ne serait alors plus utilisable tel quel sur Zimbra. À défaut, documenter le risque. Envisager aussi une durée de session applicative plus courte (`max_age` plafonné, par exemple 8 h) indépendante de `lifetime`.

### [Moyenne] Pas de limitation des tentatives de connexion ; le backend sert de relais de brute force vers Zimbra
- Emplacement : `backend/app/main.py:119-142`, `backend/app/zimbra.py:238-252`
- Problème : `/api/auth/login` relaie chaque tentative vers `AuthRequest`, sans limitation de débit par IP ni par compte. Toutes les requêtes arrivent chez Zimbra depuis l'IP du backend, sans `X-Originating-IP`. Conséquences : (a) la protection anti-DoS de Zimbra (DoSFilter, blocage par IP) voit une seule IP et peut **bloquer le backend pour tous les utilisateurs** ; (b) les journaux Zimbra ne permettent pas d'identifier l'attaquant ; (c) avec `zimbraPasswordLockoutEnabled`, un attaquant peut verrouiller n'importe quel compte connu (DoS ciblé). Si le verrouillage est désactivé, c'est un oracle de mot de passe sans limite.
- Recommandation : limiter le débit sur `/api/auth/login` par IP client et par identifiant (par exemple slowapi, ou une annotation ingress-nginx `nginx.ingress.kubernetes.io/limit-rpm` sur un Ingress dédié à `/api/auth/login`). Transmettre l'IP réelle à Zimbra via l'en-tête `X-Originating-IP`, après avoir déclaré le backend dans `zimbraMailTrustedIP`. Uvicorn doit être lancé avec `--proxy-headers --forwarded-allow-ips` pour lire l'IP derrière l'ingress.

### [Moyenne] docker-compose expose l'application en HTTP clair sur toutes les interfaces
- Emplacement : `docker-compose.yml:16`, `docker-compose.yml:32-33` ; idem dans les notes de release `.github/workflows/release.yml:114-117`
- Problème : `COOKIE_SECURE: "false"` est forcé, et le port est publié en `"${ARCHIVAGE_PORT:-3000}:3000"`, c'est-à-dire sur `0.0.0.0`. Le commentaire parle d'un usage « sur le poste », mais n'importe quelle machine du réseau local (Wi‑Fi d'entreprise ou public) peut joindre l'application. Les **mots de passe Zimbra et les jetons circulent alors en clair** et peuvent être sniffés. Docker contourne de plus les règles UFW/firewalld pour les ports publiés.
- Recommandation : publier sur la boucle locale uniquement, `"127.0.0.1:${ARCHIVAGE_PORT:-3000}:3000"`, et faire de même dans les commandes `docker run -p 127.0.0.1:3000:3000` des notes de release. Les navigateurs acceptent un cookie `Secure` sur `http://localhost`, donc `COOKIE_SECURE` pourrait même rester à `true` en local.

### [Moyenne] Ingress sans TLS par défaut et sans en-têtes de sécurité HTTP
- Emplacement : `charts/zimbra-archivage/values.yaml:71-83`, `charts/zimbra-archivage/templates/ingress.yaml`, `frontend/next.config.ts:3-6`, `backend/app/main.py:21`
- Problème : `ingress.tls: []` par défaut, et la commande Helm des notes de release (`--set ingress.enabled=true`) ne configure pas TLS. On obtient donc un formulaire de mot de passe en HTTP ; le cookie `Secure` n'est pas posé et la connexion échoue « silencieusement », ce qui pousse l'opérateur à passer `cookieSecure=false`. Aucune redirection HTTP vers HTTPS n'est forcée. Côté application, aucun en-tête de sécurité n'est émis : ni `Strict-Transport-Security`, ni `Content-Security-Policy` / `frame-ancestors`, ni `X-Content-Type-Options`, ni `Referrer-Policy`. Le formulaire de login peut être encadré (clickjacking) et HSTS n'est pas en place.
- Recommandation : refuser au rendu `ingress.enabled && !ingress.tls && config.cookieSecure` (via `fail`), ou documenter TLS comme obligatoire. Ajouter `nginx.ingress.kubernetes.io/ssl-redirect: "true"`. Dans `next.config.ts` :
  ```ts
  async headers() {
    return [{ source: "/:path*", headers: [
      { key: "Content-Security-Policy", value: "default-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'" },
      { key: "X-Content-Type-Options", value: "nosniff" },
      { key: "Referrer-Policy", value: "no-referrer" },
      { key: "Strict-Transport-Security", value: "max-age=31536000" },
    ]}];
  },
  ```
  Ajouter aussi `X-Content-Type-Options: nosniff` sur les réponses FastAPI (middleware), puisque l'ingress route `/api` directement vers le backend.

---

## Faible

### [Faible] Export déclenchable cross-site via GET (CSRF « de charge »)
- Emplacement : `backend/app/main.py:279`, `backend/app/main.py:140`
- Problème : l'export est un `GET` et le cookie est en `SameSite=Lax`, qui est envoyé sur navigation top-level. Un site malveillant peut donc lancer chez une victime connectée le téléchargement de **toute sa boîte** (avec extraction des pièces jointes). Il n'y a pas de fuite : le fichier arrive sur le disque de la victime et la réponse n'est pas lisible cross-origin. L'effet est une forte charge sur le backend et sur Zimbra (voir le DoS ci-dessus). Les autres endpoints sont protégés : le login exige `application/json` (FastAPI refuse de parser du JSON en `text/plain`), donc un preflight CORS qui échoue. Le logout est forgeable mais sans impact sérieux.
- Recommandation : exiger un en-tête personnalisé ou vérifier `Sec-Fetch-Site` (`same-origin`/`none`) sur `/api/export`. Le frontend utilise `window.location.assign`, donc `Sec-Fetch-Site: same-origin` est bien envoyé. Autre option : passer le cookie en `SameSite=Strict` (l'application n'a pas de lien entrant qui nécessite la session).

### [Faible] Messages d'erreur Zimbra relayés tels quels au client (recoupe « quality » : gestion d'erreurs incohérente)
- Emplacement : `backend/app/main.py:61-65`, `backend/app/zimbra.py:196`, `backend/app/archive.py:234`
- Problème : `ZimbraError.message` est renvoyé au navigateur (`Erreur Zimbra : …`) et écrit dans `erreurs.txt`. Il contient la `Reason` des fautes SOAP et le texte des exceptions httpx, par exemple `Erreur de connexion avec Zimbra : [Errno -2] Name or service not known` ou des détails de certificat TLS. Cela révèle l'infrastructure interne (noms d'hôtes, état TLS) et les messages bruts de Zimbra.
- Recommandation : journaliser le détail côté serveur (`logger.warning`) et renvoyer un message générique pour les erreurs de connexion. Ne relayer la `Reason` SOAP que pour une liste blanche de codes utiles à l'utilisateur.

### [Faible] Aucune NetworkPolicy, ServiceAccount non restreint
- Emplacement : `charts/zimbra-archivage/templates/backend.yaml`, `charts/zimbra-archivage/templates/frontend.yaml`
- Problème : le backend (`:8000`) est joignable par n'importe quel pod du cluster, ce qui permet de contourner le frontend et tout futur contrôle placé sur l'ingress (rate limiting, en-têtes). Aucune egress policy ne limite le backend au seul serveur Zimbra. Le ServiceAccount `default` est utilisé avec `automountServiceAccountToken` implicite à `true` : en cas de RCE, le jeton Kubernetes est disponible dans le conteneur.
- Recommandation : ajouter une `NetworkPolicy` optionnelle (ingress backend autorisé depuis l'ingress-controller et le frontend uniquement ; egress vers DNS et Zimbra). Poser `automountServiceAccountToken: false` dans les deux pods.

### [Faible] Images de base et actions CI non épinglées ; GITHUB_TOKEN en écriture sur tous les jobs
- Emplacement : `.github/workflows/release.yml:10-12`, `:21-36`, `:58-77` ; `backend/Dockerfile:1`, `frontend/Dockerfile:1,7,14` ; `values.yaml:24,41` ; `docker-compose.yml:12,33`
- Problème : (a) `permissions: contents: write, packages: write` est défini au niveau du workflow, donc le job `test` (qui exécute `pip install` et `npm ci`, du code tiers) dispose aussi d'un jeton en écriture sur le dépôt et les packages. Une dépendance compromise pourrait pousser une image GHCR ou un tag. (b) Les actions sont référencées par tag mutable (`@v4`, `@v3`, `@v6`), pas par SHA. (c) Les images `python:3.13-slim` et `node:22-alpine` ne sont pas épinglées par digest, et `pip install` est fait sans `--require-hashes`. (d) Le tag `latest` est publié et utilisé par défaut dans docker-compose.
- Recommandation : `permissions: contents: read` au niveau du workflow, puis `packages: write` sur le job `images` seulement et `contents: write` sur le job `release` seulement. Épingler les actions par SHA (Dependabot sait les tenir à jour). Épingler les images par digest. Les déclencheurs (`push` de tag, `workflow_dispatch`) ne font pas tourner de code de PR externe, ce qui atténue le risque.

### [Faible] Registre de progression non borné en nombre d'entrées
- Emplacement : `backend/app/progress.py:416-424`, `backend/app/main.py:300`
- Problème : chaque `export_id` différent crée une entrée, purgée seulement après 1 h d'inactivité. Un client authentifié peut lancer des milliers d'exports (même interrompus aussitôt) avec des `export_id` distincts. Chaque entrée est petite, mais le nettoyage parcourt **tout** le dictionnaire sous verrou à chaque `start` (complexité O(n²) cumulée). L'impact est limité, car chaque export coûte aussi un `GetFolderRequest` à l'attaquant.
- Recommandation : limiter le nombre d'exports suivis par session (par exemple 5) et globalement, en évinçant les plus anciens.

### [Faible] OpenAPI et Swagger UI exposés publiquement
- Emplacement : `backend/app/main.py:21`, `charts/zimbra-archivage/templates/ingress.yaml:25-31`
- Problème : `/api/docs` et `/api/openapi.json` sont servis en production, via l'ingress et via le proxy Next. Ils décrivent toute la surface d'API (paramètres, formats de filtres). Le gain pour un attaquant est faible, mais la Swagger UI charge aussi des scripts depuis un CDN (jsdelivr).
- Recommandation : désactiver la documentation hors développement (`docs_url=None, openapi_url=None` derrière un réglage `enable_docs`).

---

## Info (contrôles vérifiés, sans vulnérabilité)

### [Info] Pas de Zip Slip : les chemins de l'archive sont correctement assainis
- Emplacement : `backend/app/archive.py:120-138`, `backend/app/archive.py:283-293`
- Problème : vérifié. Les composants de dossier sont découpés sur `/`, puis `sanitize_path_component` remplace `<>:"/\|?*` et les caractères de contrôle. `strip().rstrip(".")` transforme `..` et `.` en `_`. Les noms de pièces jointes ne gardent que le dernier segment après `/` ou `\`, puis passent par le même assainissement (`..` donne `_`). Le sujet est réduit à `[0-9a-zA-Z ]` et aucun chemin ne commence par `/`. Seul `../devis.pdf` est testé (`tests/test_api.py:312`). Le teammate « tests » a confirmé ce comportement en exécutant un script : `/../../etc` devient `_/_/etc`, `..` devient `_`, `C:\x` devient `x`, et les CRLF et guillemets deviennent `_`.
- Recommandation : ajouter des tests de non-régression pour `..`, `..\..\x`, `/etc/passwd`, `C:\x` et un dossier nommé `..` (message envoyé à « tests »). Optionnel : préfixer d'un `_` les noms réservés Windows (`CON`, `NUL`, `COM1`…), qui feraient échouer l'extraction sous Windows.

### [Info] Pas d'injection d'en-tête dans Content-Disposition
- Emplacement : `backend/app/main.py:271-276`, `:327`
- Problème : vérifié. Le nom est translittéré en ASCII puis réduit à `[A-Za-z0-9._-]`, donc ni guillemet ni CRLF possibles.
- Recommandation : aucune.

### [Info] Pas d'IDOR sur la progression des exports
- Emplacement : `backend/app/progress.py:411-414`, `frontend/lib/api.ts:140-143`
- Problème : vérifié. La clé est `(sha256(jeton), export_id)`, donc la progression d'une autre session est introuvable (testé dans `test_api.py:273`). L'identifiant fait 128 bits aléatoires (`crypto.getRandomValues`) et son format est validé (`^[A-Za-z0-9-]{8,64}$`). Aucune archive n'est stockée côté serveur, donc il n'y a rien à récupérer.
- Recommandation : aucune.

### [Info] Pas de SSRF ; l'injection dans le langage de recherche Zimbra est maîtrisée
- Emplacement : `backend/app/config.py:15-17`, `backend/app/zimbra.py:331-336`, `backend/app/filters.py:26-30`, `backend/app/suggestions.py:41-43`, `backend/app/main.py:230`
- Problème : vérifié. Les URLs Zimbra viennent de la configuration uniquement. `message_id` (fourni par Zimbra) passe dans `params=` et est donc encodé. `folder_id` doit exister dans l'arbre de l'utilisateur (`root.find`). Les valeurs de filtres sont soit restreintes à `[\w@.+*-]` hors mots réservés, soit mises entre guillemets avec échappement de `\` et `"`. Les suggestions appliquent la même règle. Même une injection n'agirait que sur la propre boîte de l'utilisateur, avec son propre jeton : aucune élévation possible. Le TLS vers Zimbra est vérifié par défaut (`verify_tls=True`).
- Recommandation : aucune.

### [Info] Proxy Next.js : pas de proxy ouvert
- Emplacement : `frontend/app/api/[...path]/route.ts:7`, `:33-36`
- Problème : vérifié. L'hôte cible est fixe (`BACKEND_URL`, variable d'environnement serveur) et chaque segment passe par `encodeURIComponent`. Un segment `..` (encodé `%2E%2E`) est résolu par `new URL` hors de `/api` (par exemple `/health`), mais le backend ne sert rien hors `/api/*`, donc sans impact. Seuls GET et POST sont exportés, avec `redirect: "manual"`. Le proxy relaie l'intégralité des en-têtes client (`Cookie`, `X-Forwarded-*`) et le corps sans limite de taille (`request.arrayBuffer()`), ce qui est à surveiller si on y ajoute de la logique fondée sur ces en-têtes.
- Recommandation : par robustesse, rejeter les segments `.` et `..` et préfixer explicitement `BACKEND_URL` + `/api/`, plutôt que de s'appuyer sur la résolution d'URL.

### [Info] Conteneurs et dépendances
- Emplacement : `backend/Dockerfile:14-15`, `frontend/Dockerfile:22-23`, `values.yaml:57-66`, `backend/requirements.txt`, `frontend/package.json`
- Problème : vérifié. Les conteneurs tournent en non-root (UID 10001 et 1000). Les pods ont `runAsNonRoot`, `seccompProfile: RuntimeDefault`, `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false` et `drop: ALL`. docker-compose est en `read_only`. Aucun secret dans `values.yaml`, la ConfigMap ou les `.env.example`. Le XSS côté frontend est écarté : pas de `dangerouslySetInnerHTML`, et la couleur de dossier est validée côté serveur (`RGB_PATTERN` ou palette fixe) avant d'aller dans `style`. Pas de redirection ouverte (cibles de `router.replace/push` codées en dur). `npm audit --omit=dev` : **0 vulnérabilité**. `pip-audit` n'a pas pu être exécuté (outil absent) ; versions épinglées : fastapi 0.159.1, uvicorn 0.54.0, httpx 0.28.1, pydantic-settings 2.16.1. Les URLs d'export (filtres : sujets, adresses) apparaissent dans les journaux d'accès uvicorn et ingress, ce qui touche des données personnelles mineures.
- Recommandation : lancer `pip-audit` en CI. Ajouter `emptyDir.sizeLimit` sur `/tmp` du backend (spool des .eml de plus de 10 Mo) pour éviter de saturer le disque du nœud.
