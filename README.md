# Archivage Zimbra (web)

Application web simple qui permet à un utilisateur de se connecter avec son compte
Zimbra puis de télécharger, sous forme d'archive ZIP, le dossier de mails de son choix
(ou toute sa boîte), éventuellement restreint par des filtres sur l'objet, l'expéditeur et
les destinataires.

Elle reprend la méthode d'échange avec Zimbra du projet
[ZimbraArchivage](https://gitlab.mim-libre.fr/dgddi/archivage-mails/ZImbraArchivage)
(application JavaFX) :

| Étape | Appel Zimbra |
| --- | --- |
| Connexion | SOAP `AuthRequest` (`urn:zimbraAccount`), le jeton obtenu est passé dans le cookie `ZM_AUTH_TOKEN` |
| Liste des dossiers | SOAP `GetFolderRequest` (les partages montés `link` et les dossiers de recherche `search` sont ignorés) |
| Recherche des messages | SOAP `SearchRequest` trié par date croissante et paginé par curseur (`inid:<dossier>` et filtres, lots de 500 par défaut) |
| Téléchargement d'un message | ReST `GET /service/home/~/?id=<id>` au format `.eml` |
| Suggestions d'adresses des filtres | SOAP `AutoCompleteRequest` (contacts, liste globale, correspondants habituels) puis `SearchRequest` (`from:<début>*` ou `tocc:<début>*`, adresses des 100 messages les plus récents) |
| Déconnexion | SOAP `EndSessionRequest` (`logoff="1"`) : le jeton Zimbra n'est plus utilisable |

Comme dans Archivage, les appels sont retentés si Zimbra répond 502, 503 ou 504, et aussi si
Zimbra est injoignable : jusqu'à 5 nouvelles tentatives, après 1, 2, 4, 8 puis 16 secondes
(ou le délai `Retry-After` de Zimbra, plafonné à 30 s). Un export s'arrête après
20 téléchargements consécutifs en échec.
L'API SOAP est appelée dans sa variante JSON (même point d'entrée `/service/soap`).

## Contenu de l'archive ZIP

L'archive reprend l'organisation des fichiers d'Archivage : l'arborescence des dossiers Zimbra
(chemin depuis la racine de la boîte), avec un fichier `.eml` par courriel, nommé
`aaaaMMjj_<sujet>_<id>.eml` (caractères hors `[0-9a-zA-Z ]` du sujet remplacés par `_`,
nom limité à 50 caractères par défaut).

```
Reception_20261022.zip
└── Inbox/
    ├── 20240110_R_union d__quipe_101.eml
    ├── 20240315_Budget_102.eml
    └── Projets/
        └── 20240301_Projet X_201.eml
```

Avec l'option d'extraction des pièces jointes, chaque pièce jointe est aussi enregistrée
dans un dossier portant le nom du `.eml` (les noms identiques sont numérotés, un courriel joint
devient un `.eml`). Le `.eml` reste toujours complet, pièces jointes comprises.

```
Inbox/
├── 20240701_Devis_104.eml
└── 20240701_Devis_104/
    ├── devis.pdf
    └── devis (2).pdf
```

Options d'export :

- **dossier** : un dossier de la boîte, ou **toute la boîte aux lettres** (hors Corbeille et
  Spam, comme la recherche Zimbra) ;
- **inclure les sous-dossiers** (activé par défaut) ;
- **période** : date de début et/ou de fin, bornes incluses, dans le fuseau `ZONE_ID` ;
- **filtres** : conditions « contient / ne contient pas » sur l'objet, l'expéditeur ou le
  destinataire (champs À et Cc), regroupées en groupes. Les conditions d'un groupe sont reliées
  par ET ou par OU, et les groupes entre eux aussi, par exemple
  `(objet contient « facture » ET expéditeur contient « dupont ») OU objet contient « devis »`.
  La recherche se fait par mots, comme dans Zimbra (`budg*` pour un début de mot).
  Pour l'expéditeur et le destinataire, des adresses sont suggérées dès 2 caractères saisis ;
- **uniquement les messages avec pièces jointes** ;
- **extraire les pièces jointes** dans des fichiers séparés.

L'archive est construite et envoyée au fil de l'eau : rien n'est stocké côté serveur. Les
messages de tous les dossiers sont d'abord recensés (ce qui fige la liste et donne le total
affiché pendant le téléchargement), puis téléchargés à plusieurs en parallèle
(`DOWNLOAD_CONCURRENCY`) et ajoutés dans l'ordre.
Pendant le téléchargement, un écran d'attente affiche le nombre de messages déjà ajoutés à
l'archive et se ferme une fois l'archive terminée. Cet avancement est gardé en mémoire par le
backend : avec plusieurs réplicas, l'écran se contente d'une attente simple si l'avancement est
suivi par un autre réplica.
Si certains messages n'ont pas pu être téléchargés, ils sont listés dans un fichier
`erreurs.txt` à la racine de l'archive. Si l'export est interrompu (session Zimbra expirée,
Zimbra qui ne répond plus), la première ligne de `erreurs.txt` commence par
`EXPORT INTERROMPU` et l'écran d'attente l'indique : l'archive est alors incomplète.
Les pièces jointes d'un courriel de plus de 25 Mo (`MAX_ATTACHMENT_EXTRACTION_SIZE`) ne sont
pas extraites ; elles restent dans le `.eml`.

## Architecture

```
navigateur ──► Next.js (frontend/, port 3000) ──/api/*──► FastAPI (backend/, port 8000) ──► Zimbra
```

- `backend/` : API FastAPI (Python 3.13).
  - `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`
  - `GET /api/folders` : arborescence des dossiers de mails, précédée de l'entrée
    « Toute la boîte aux lettres » (`id` 1, nombre de messages hors Corbeille et Spam)
  - `GET /api/addresses?q=…&field=from|to` : adresses suggérées (10 au plus) pour les filtres
  - `GET /api/export/summary` : nombre de messages et taille estimée d'un export
  - `GET /api/export?folder_id=…&include_subfolders=…&start=AAAA-MM-JJ&end=AAAA-MM-JJ` : archive ZIP
    (`folder_id=1` pour toute la boîte). Paramètres facultatifs : `only_with_attachments=true`,
    `extract_attachments=true` et `filter`, un filtre JSON (voir `backend/app/filters.py`) :
    `{"operator": "or", "groups": [{"operator": "and", "conditions": [{"field": "subject",
    "operator": "contains", "value": "facture"}]}]}` (champs `subject`, `from`, `to` ;
    opérateurs `contains`, `not_contains`)
  - `GET /api/export/progress?export_id=…` : avancement d'un export lancé avec le paramètre
    `export_id` (identifiant choisi par l'interface), visible de la seule session qui l'a lancé :
    messages traités, total, fin et raison d'une interruption
  - Documentation OpenAPI : `/api/docs`, uniquement avec la variable `ENABLE_DOCS=true`
- `frontend/` : interface Next.js (App Router). Les appels `/api/*` sont relayés vers le
  backend (`BACKEND_URL`, lu à l'exécution). En Kubernetes, l'ingress envoie directement
  `/api` au backend.
- `charts/zimbra-archivage/` : chart Helm.

Le mot de passe n'est jamais stocké : seul le jeton Zimbra est conservé, **chiffré** avec la
clé `SESSION_SECRET`, dans un cookie `HttpOnly`, `SameSite=Strict` et `Secure` (par défaut),
limité au chemin `/api`. Un cookie copié n'est donc pas utilisable directement sur Zimbra.
La session dure au plus `SESSION_MAX_AGE` (8 h par défaut), et la déconnexion ferme aussi la
session Zimbra.

Garde-fous : 5 connexions échouées par identifiant bloquent les nouvelles tentatives pendant
5 minutes (une limite par adresse IP est possible, voir `MAX_LOGIN_FAILURES_PER_IP`) ; chaque session peut lancer 2 exports ou estimations à la fois,
et le backend 6 au total (réponse 429 au-delà) ; un export ne peut pas être lancé depuis un
autre site (`Sec-Fetch-Site`).

## Test rapide avec Docker Compose

```bash
cp .env.example .env     # renseigner ZIMBRA_SOAP_URL et ZIMBRA_REST_URL
docker compose up -d     # utilise les images publiées sur GHCR
```

Puis ouvrir http://localhost:3000/. L'application n'est publiée que sur `127.0.0.1` : elle
est servie en HTTP, les mots de passe ne doivent pas circuler en clair sur le réseau.
Options utiles dans `.env` : `ARCHIVAGE_VERSION` (version des images, `latest` = dernière
version stable), `ARCHIVAGE_PORT` (port local, `3000` par défaut), `SESSION_SECRET` (pour
garder les sessions après un redémarrage), `VERIFY_TLS=false` si le certificat du serveur
Zimbra est interne ou auto-signé et `ZIMBRA_PROXY_URL` si Zimbra n'est joignable que par un
proxy.

Pour tester les sources du dépôt plutôt que les images publiées :
`docker compose up -d --build`. Arrêt : `docker compose down`.

## Configuration du backend

Variables d'environnement (voir `backend/.env.example`) :

| Variable | Défaut | Description |
| --- | --- | --- |
| `ZIMBRA_SOAP_URL` | (obligatoire) | URL SOAP, ex. `https://mail.domaine.fr/service/soap` |
| `ZIMBRA_REST_URL` | (obligatoire) | URL ReST, ex. `https://mail.domaine.fr/service` |
| `ZONE_ID` | `Europe/Paris` | Fuseau des filtres de dates et des noms de fichiers |
| `SEARCH_BATCH_SIZE` | `500` | Messages par page de `SearchRequest` |
| `MAX_FILE_NAME_LENGTH` | `50` | Longueur maximale des noms de `.eml` (50 à 180) |
| `HTTP_TIMEOUT` | `120` | Délai maximal d'un appel à Zimbra (secondes ; 10 s pour établir la connexion) |
| `VERIFY_TLS` | `true` | Vérification du certificat de Zimbra |
| `ZIMBRA_PROXY_URL` | (vide) | Proxy HTTP(S) par lequel joindre Zimbra, ex. `http://utilisateur:mot_de_passe@proxy.domaine.fr:3128`. Prioritaire sur `HTTPS_PROXY` / `HTTP_PROXY` (ou `https_proxy` / `http_proxy`), utilisées sinon |
| `NO_PROXY` | (vide) | Hôtes joints sans proxy, séparés par des virgules (`mail.domaine.fr`, `.domaine.fr`, `10.0.0.1`, `*`), aussi avec `ZIMBRA_PROXY_URL` |
| `DOWNLOAD_CONCURRENCY` | `4` | Courriels téléchargés en parallèle par export (1 à 16) |
| `MAX_CONCURRENT_EXPORTS` | `6` | Exports et estimations simultanés, pour tout le backend |
| `MAX_EXPORTS_PER_SESSION` | `2` | Exports et estimations simultanés par session |
| `MAX_ATTACHMENT_EXTRACTION_SIZE` | `26214400` | Taille (octets) au-delà de laquelle les PJ ne sont pas extraites |
| `MAX_LOGIN_FAILURES_PER_USER` | `5` | Connexions échouées tolérées par identifiant sur 5 minutes |
| `MAX_LOGIN_FAILURES_PER_IP` | `0` | Connexions échouées tolérées par adresse IP sur 5 minutes (`0` : pas de limite). À n'activer qu'avec `FORWARDED_ALLOW_IPS` restreint aux proxys de confiance |
| `COOKIE_SECURE` | `true` | Cookie de session réservé à HTTPS (`false` en dev HTTP uniquement) |
| `SESSION_MAX_AGE` | `28800` | Durée maximale d'une session (secondes) |
| `SESSION_SECRET` | (aléatoire) | Clé Fernet de chiffrement des sessions ; sans elle, les sessions sont perdues au redémarrage et non partagées entre réplicas |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxys de confiance pour `X-Forwarded-For` (lu par uvicorn) : adresses de l'ingress-controller, jamais `*` |
| `ENABLE_DOCS` | `false` | Documentation OpenAPI sur `/api/docs` |

Avec un proxy, tous les appels à Zimbra (SOAP et ReST) passent par lui, les URLs `https` par un
tunnel `CONNECT` : le certificat vérifié reste celui de Zimbra. Un proxy injoignable ou qui refuse
la connexion est traité comme un Zimbra injoignable (mêmes nouvelles tentatives). L'URL du proxy
n'apparaît ni dans les journaux ni dans les erreurs de configuration.

Une clé `SESSION_SECRET` se génère avec
`python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`.
Pour que Zimbra journalise et limite les connexions selon l'adresse réelle des utilisateurs
(en-tête `X-Originating-IP` envoyé par le backend), déclarer le backend dans
`zimbraMailTrustedIP`.

Côté frontend, seule `BACKEND_URL` (défaut `http://localhost:8000`) est utilisée.

## Développement

Backend :

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env   # renseigner les URLs Zimbra, COOKIE_SECURE=false en local
.venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/pytest --cov # tests (avec un faux serveur Zimbra), 95 % de couverture minimum
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Frontend :

```bash
cd frontend
npm install
npm run dev            # http://localhost:3000, relaie /api vers http://localhost:8000
npm run lint           # ESLint et vérification TypeScript
npm test               # tests Vitest (relais /api, utilitaires)
```

## Images Docker

Les images publiées sont `ghcr.io/jul0ow/zimbraarchivage-backend` et
`ghcr.io/jul0ow/zimbraarchivage-frontend`. Pour les construire localement :

```bash
docker build -t zimbra-archivage-backend:0.1.0 backend
docker build -t zimbra-archivage-frontend:0.1.0 frontend
```

Les deux images tournent avec un utilisateur non root et supportent un système de fichiers
racine en lecture seule (prévoir un volume temporaire sur `/tmp` pour le backend et
`/app/.next/cache` pour le frontend, ce que fait le chart Helm).

## Déploiement Helm

```bash
helm install archivage charts/zimbra-archivage \
  --set zimbra.soapUrl=https://mail.domaine.fr/service/soap \
  --set zimbra.restUrl=https://mail.domaine.fr/service \
  --set ingress.enabled=true \
  --set ingress.className=nginx \
  --set ingress.host=archivage.domaine.fr
```

L'ingress route `/api` vers le backend et le reste vers le frontend. Les annotations par
défaut (ingress-nginx) désactivent la mise en tampon, allongent les délais pour les
exports volumineux et redirigent vers HTTPS. Avec `config.cookieSecure=true` (par défaut),
le rendu échoue sans `ingress.tls`, sauf si `ingress.tlsTerminatedUpstream=true`.
La clé des sessions est générée à l'installation et conservée dans un Secret (ou fournie par
`session.secret` / `session.existingSecret`). Un proxy vers Zimbra se configure avec
`zimbra.proxy.url` (conservée dans un Secret) ou `zimbra.proxy.existingSecret`, et ses
exclusions avec `zimbra.proxy.noProxy`.
`networkPolicy.enabled=true` restreint les accès au backend à l'ingress-controller et au
frontend ; ses sorties vers un proxy qui n'écoute pas sur le port 443 ou 80 s'ajoutent avec
`networkPolicy.extraEgress`. Les valeurs sont vérifiées par
`values.schema.json` ; voir `charts/zimbra-archivage/values.yaml` pour toutes les options.

## Publier une version

Pousser un tag `vX.Y.Z`, ou lancer le workflow « Release » depuis l'onglet Actions
en indiquant la version, déclenche `.github/workflows/release.yml`, qui :

1. relance la CI (`.github/workflows/ci.yml`, aussi exécutée à chaque push et pull request :
   tests et lint du backend, lint, tests et build du frontend, `helm lint`, audit des dépendances) ;
2. publie les images `:X.Y.Z` sur GHCR, et `:latest` pour une version stable ;
3. crée une release GitHub contenant le chart Helm `zimbra-archivage-X.Y.Z.tgz`.

Une version avec suffixe (`vX.Y.Z-rc.1`) est publiée en prerelease et ne déplace pas `latest`.

```bash
git tag v0.1.0
git push origin v0.1.0
```
