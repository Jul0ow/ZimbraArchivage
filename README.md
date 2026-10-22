# Archivage Zimbra (web)

Application web simple qui permet à un utilisateur de se connecter avec son compte
Zimbra puis de télécharger, sous forme d'archive ZIP, le dossier de mails de son choix.

Elle reprend la méthode d'échange avec Zimbra du projet
[ZimbraArchivage](https://gitlab.mim-libre.fr/dgddi/archivage-mails/ZImbraArchivage)
(application JavaFX) :

| Étape | Appel Zimbra |
| --- | --- |
| Connexion | SOAP `AuthRequest` (`urn:zimbraAccount`), le jeton obtenu est passé dans le cookie `ZM_AUTH_TOKEN` |
| Liste des dossiers | SOAP `GetFolderRequest` (les partages montés `link` et les dossiers de recherche `search` sont ignorés) |
| Recherche des messages | SOAP `SearchRequest` paginé (`inid:<dossier>`, lots de 500 par défaut) |
| Téléchargement d'un message | ReST `GET /service/home/~/?id=<id>` au format `.eml` |

Comme dans Archivage, les appels sont retentés (jusqu'à 5 fois) si Zimbra répond 502, 503 ou 504.
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

Options d'export :

- **inclure les sous-dossiers** (activé par défaut) ;
- **période** : date de début et/ou de fin, bornes incluses, dans le fuseau `ZONE_ID`.

L'archive est construite et envoyée au fil de l'eau : rien n'est stocké côté serveur.
Si certains messages n'ont pas pu être téléchargés, ils sont listés dans un fichier
`erreurs.txt` à la racine de l'archive.

## Architecture

```
navigateur ──► Next.js (frontend/, port 3000) ──/api/*──► FastAPI (backend/, port 8000) ──► Zimbra
```

- `backend/` : API FastAPI (Python 3.13).
  - `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`
  - `GET /api/folders` : arborescence des dossiers de mails
  - `GET /api/export/summary` : nombre de messages et taille estimée d'un export
  - `GET /api/export?folder_id=…&include_subfolders=…&start=AAAA-MM-JJ&end=AAAA-MM-JJ` : archive ZIP
  - Documentation OpenAPI : `/api/docs`
- `frontend/` : interface Next.js (App Router). Les appels `/api/*` sont relayés vers le
  backend (`BACKEND_URL`, lu à l'exécution). En Kubernetes, l'ingress envoie directement
  `/api` au backend.
- `charts/zimbra-archivage/` : chart Helm.

Le mot de passe n'est jamais stocké : seul le jeton Zimbra est conservé, dans un cookie
`HttpOnly`, `SameSite=Lax` et `Secure` (par défaut), limité au chemin `/api`. La session dure
le temps de vie du jeton Zimbra.

## Configuration du backend

Variables d'environnement (voir `backend/.env.example`) :

| Variable | Défaut | Description |
| --- | --- | --- |
| `ZIMBRA_SOAP_URL` | (obligatoire) | URL SOAP, ex. `https://mail.domaine.fr/service/soap` |
| `ZIMBRA_REST_URL` | (obligatoire) | URL ReST, ex. `https://mail.domaine.fr/service` |
| `ZONE_ID` | `Europe/Paris` | Fuseau des filtres de dates et des noms de fichiers |
| `SEARCH_BATCH_SIZE` | `500` | Messages par page de `SearchRequest` |
| `MAX_FILE_NAME_LENGTH` | `50` | Longueur maximale des noms de `.eml` (50 à 180) |
| `HTTP_TIMEOUT` | `120` | Délai maximal d'un appel à Zimbra (secondes) |
| `VERIFY_TLS` | `true` | Vérification du certificat de Zimbra |
| `COOKIE_SECURE` | `true` | Cookie de session réservé à HTTPS (`false` en dev HTTP uniquement) |

Côté frontend, seule `BACKEND_URL` (défaut `http://localhost:8000`) est utilisée.

## Développement

Backend :

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env   # renseigner les URLs Zimbra, COOKIE_SECURE=false en local
.venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/pytest       # tests (avec un faux serveur Zimbra)
```

Frontend :

```bash
cd frontend
npm install
npm run dev            # http://localhost:3000, relaie /api vers http://localhost:8000
npm run lint           # vérification TypeScript
```

## Images Docker

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
  --set backend.image.repository=registry.domaine.fr/zimbra-archivage-backend \
  --set frontend.image.repository=registry.domaine.fr/zimbra-archivage-frontend \
  --set ingress.enabled=true \
  --set ingress.className=nginx \
  --set ingress.host=archivage.domaine.fr
```

L'ingress route `/api` vers le backend et le reste vers le frontend. Les annotations par
défaut (ingress-nginx) désactivent la mise en tampon et allongent les délais, pour les
exports volumineux. Voir `charts/zimbra-archivage/values.yaml` pour toutes les options.
