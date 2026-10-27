# Suivi des corrections de la revue

État au 27/10/2026, après les commits « Fiabilise et sécurise l'export côté backend »,
« Corrige les constats de la revue côté frontend » et « Durcit le déploiement et ajoute la CI ».

## Corrigé

| Constat (SYNTHESE.md) | Correction |
|---|---|
| Session expirée avalée pendant l'export | Export interrompu, `EXPORT INTERROMPU` en tête de `erreurs.txt`, raison dans `/api/export/progress` et à l'écran ; ReST 401/403 = session expirée |
| Pagination par offset | Tri `dateAsc`, curseur, dédoublonnage ; messages recensés avant les téléchargements |
| Exports simultanés illimités, `/api/health` sur le pool | Limite globale et par session (429), `/api/health` en `async`, sondes Helm différenciées |
| Extraction des PJ en RAM | Drapeau `a`, taille maximale (25 Mo), profondeur et nombre de parties bornés, écriture au fil de l'eau ; limite mémoire Helm portée à 1 Gi |
| CI seulement au tag | `ci.yml` sur push et PR (ruff, pytest ≥ 95 %, pip-audit, ESLint, tsc, Vitest, build, npm audit, helm lint) réutilisé par la release |
| Faux Zimbra trop permissif | Recherche par mots, joker, `after:`/`before:`, tri et curseur |
| Téléchargements séquentiels | Fenêtre de téléchargements parallèles (`DOWNLOAD_CONCURRENCY`) |
| Logout sans `EndSessionRequest` | Appelé à la déconnexion |
| Jeton brut dans le cookie | Chiffré (Fernet, `SESSION_SECRET`), session plafonnée à 8 h, `SameSite=Strict` |
| Pas de limite au login | Par identifiant et par IP, `X-Originating-IP` transmis à Zimbra |
| Compose sur 0.0.0.0 | Publication sur 127.0.0.1, tmpfs borné, variables relayées |
| Ingress sans TLS, pas d'en-têtes | Rendu refusé sans TLS si cookie Secure, `ssl-redirect`, CSP/HSTS/nosniff/frame côté Next et API |
| Double recherche estimation/export | Le total vient de l'avancement de l'export |
| `httpx.Client` par requête | Client partagé, limites de connexions |
| Délais undici 300 s | Dispatcher sans délai dans le proxy |
| Retries 6/5, 363 s, connexions non retentées | 5 nouvelles tentatives (1 à 16 s, `Retry-After` plafonné à 30 s), erreurs de connexion retentées, coupe-circuit par export, testés |
| Fuseau non testé, tests dépendants de l'environnement | `_env_file=None` et réglages explicites, tests de bornes et de changement d'heure |
| Racine = pièce jointe | Extraite |
| Suffixe `_<id>.eml` non borné | Empreinte stable si l'identifiant est trop long |
| Zip Slip / Content-Disposition non testés | Tests ajoutés (+ noms réservés Windows) |
| Coupure réseau et handler 502 non testés | Tests ajoutés, messages génériques |
| Aucun test frontend | Vitest : relais `/api`, construction des paramètres, filtre |
| Règles « toute la boîte » dupliquées | Entrée calculée par le backend, ids système centralisés dans `zimbra.py` |
| Contrat du filtre recopié, limites absentes | Limites exposées et appliquées dans l'éditeur, 422 détaillé |
| Page `dossiers` trop chargée | `ExportForm`, `useExportOptions`, `useExportProgress`, `useApiErrorHandler` |
| Aucune journalisation | Loggers par module, sans jeton ni contenu |
| Pas de `values.schema.json` | Ajouté ; réglages Python validés au lieu d'être bornés en silence |
| `folder_id=1` sans sous-dossiers | Sous-dossiers toujours inclus |
| `latest` sur prerelease | `latest` seulement pour une version stable, version validée |
| Actions/images non épinglées, `GITHUB_TOKEN` large | SHA et empreintes, Dependabot, droits par job |
| Pas de NetworkPolicy, ServiceAccount | NetworkPolicy optionnelle, `automountServiceAccountToken: false` |
| Erreurs Zimbra relayées | Messages génériques, détail journalisé |
| Faibles divers | Registre d'avancement borné, Swagger désactivé, PJ compressées stockées, `/tmp` borné, duplications (`walk`, cookie, recherche), casts TS, réponses mal formées → 502, `ruff` |

## Reste à faire

- `pip install --require-hashes` (nécessite un fichier de dépendances verrouillé avec empreintes).
- Contrat du filtre généré depuis OpenAPI : le filtre reste un paramètre JSON de requête.
- Tests de composants (Testing Library) et test de bout en bout Playwright.
- `helm lint` n'a pas été exécuté localement (outil absent) : il tourne dans la CI.
- Avancement et limites gardés en mémoire du processus : avec plusieurs réplicas, l'écran
  d'attente passe en attente simple et les limites sont comptées par réplica.
