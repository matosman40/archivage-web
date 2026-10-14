# Archivage Médias — version web dockerisée

Version web de `archivage_films.sh` : même logique (recherche, tri, filtre
Plex, garde-fous de sécurité, copie/déplacement/suppression via `rsync`),
pilotable depuis un navigateur plutôt qu'un terminal.

## 1. Architecture

```
┌─────────────────────────────┐
│         Navigateur          │
│   (frontend/ HTML+CSS+JS)   │
└──────────────┬───────────────┘
      HTTP (fetch) + WebSocket
┌──────────────▼───────────────┐
│     FastAPI (Python 3.12)    │
│  backend/app/main.py (API)   │
│  ├─ media.py    (films)      │
│  ├─ series.py   (séries)     │
│  ├─ plex.py     (filtre vu)  │
│  ├─ disks.py    (destinations)│
│  ├─ security.py (garde-fous) │
│  ├─ jobs.py     (rsync async)│
│  └─ logs.py     (journal)    │
└──────────────┬───────────────┘
      subprocess (rsync -a --info=progress2)
┌──────────────▼───────────────┐
│  Volumes Docker : sources,   │
│  disques de destination,     │
│  journal persistant          │
└───────────────────────────────┘
```

**Pourquoi FastAPI (Python) plutôt que Node** : le script d'origine est déjà
une suite d'opérations sur le système de fichiers et de sous-processus
(`find`, `rsync`, `stat`) — c'est le point fort de Python (`os`, `shutil`,
`asyncio.subprocess`), et FastAPI donne nativement les WebSockets nécessaires
pour la progression de `rsync` en temps réel, avec un typage clair des
requêtes (Pydantic) pour valider les garde-fous côté serveur.

**Un seul conteneur** sert à la fois l'API (`/api/...`, `/ws/...`) et les
fichiers statiques du frontend (`/`), donc `docker compose up` suffit — pas
besoin de reverse-proxy pour une utilisation personnelle sur le réseau local.

## 2. Démarrage rapide

```bash
cp .env.example .env
# édite .env : chemins hôte des sources, token Plex, etc.

docker compose up -d --build
```

Interface disponible sur `http://<ip-du-serveur>:8843`.

## 3. Régler l'application depuis l'interface (icône ⚙ Réglages)

La plupart des paramètres du script d'origine sont maintenant éditables en
direct depuis le navigateur, sans toucher à `.env` ni redémarrer le
conteneur :

- dossiers sources (films, séries, **ebooks**)
- sous-dossier de destination par type de média
- racines scannées pour détecter les disques externes
- marge de sécurité
- extensions vidéo et **extensions ebook** prises en compte
- URL et token du serveur Plex
- mode simulation (dry-run)
- texte de confirmation exigé pour une suppression

Chaque changement est enregistré dans `settings.json` sur le volume
`archivage-data` (donc conservé après un `docker compose up --build`), et
pris en compte immédiatement — pas besoin de relancer le conteneur. Le
bouton "Réinitialiser" restaure les valeurs lues depuis `.env` au démarrage.

Un avertissement s'affiche automatiquement dans Réglages si deux dossiers
sources de types différents se chevauchent (ex. le dossier "Films" contient
aussi le dossier "Séries") — un même fichier apparaîtrait alors dans deux
recherches différentes et risquerait d'être copié dans le mauvais
sous-dossier de destination.

**Limite importante, propre à Docker (pas à cette application)** : un
dossier ne peut être choisi comme source que s'il est déjà *monté* dans le
conteneur via `docker-compose.yml`. L'interface web ne peut pas faire
apparaître un chemin qui n'existe pas côté conteneur — elle peut seulement
choisir *parmi* ce qui a été monté. C'est pourquoi `docker-compose.yml`
monte le dossier hôte au **même chemin** à l'intérieur du conteneur (ex.
`/DATA/Media` → `/DATA/Media`) : ce que tu vois sur ta machine est
exactement ce que tu peux taper dans Réglages, sans traduction de chemin à
retenir. Le bouton "Vérifier les chemins" dans Réglages confirme qu'un
chemin donné est bien visible et lisible depuis le conteneur avant de
l'enregistrer.

Si tes films/séries sont répartis sur plusieurs arborescences sans lien
entre elles (NAS + dossier "vrac" + partage Freebox...), ajoute une ligne
`volumes:` par arborescence dans `docker-compose.yml` (voir le commentaire
`# - /un/autre/chemin/...` déjà présent), toujours au même chemin des deux
côtés, puis redémarre le conteneur une fois pour prendre en compte le
nouveau montage — après quoi tu peux ajouter ce chemin comme source dans
Réglages sans autre redémarrage.

## 4. Accès aux disques externes de l'hôte

Trois options existent pour donner au conteneur l'accès aux disques externes
de l'hôte (habituellement montés sous `/media/<user>/<disque>` ou
`/run/media/<user>/<disque>` par un gestionnaire comme `udisks2`/`devmon`) :

| Option | Description | Recommandé |
|---|---|---|
| **Bind mounts + propagation partagée** | Monter `/media` et `/mnt` de l'hôte dans le conteneur avec `:rslave`, après avoir rendu ces points de montage "partagés" sur l'hôte, à chaque démarrage (service systemd, section 34). Les nouveaux disques montés après coup deviennent visibles dans le conteneur sans redémarrage. | ✅ Oui — c'est l'approche utilisée dans `docker-compose.yml` |
| `--privileged` + accès à `/dev` | Donne un accès total aux périphériques bloc ; le conteneur pourrait alors monter/démonter lui-même des disques. | ❌ Évité : bien plus de surface d'attaque que nécessaire pour ce cas d'usage |
| Un bind mount fixe par disque | Une ligne `volumes:` par disque connu à l'avance. | Acceptable si tu as un nombre fixe de disques et pas de branchement/débranchement fréquent |

Le `docker-compose.yml` fourni utilise la première option. Si ça ne suffit
pas sur ta distribution (certaines ne propagent pas les montages par
défaut), utilise l'option "un bind mount par disque".

## 5. Sécurité — garde-fous reproduits

- **Destination ≠ source** : toute destination (racine du disque **et**
  sous-dossier `MEDIA/FILM`/`MEDIA/SERIES` calculé) qui chevauche un dossier
  source configuré est refusée (`403`), aussi bien pour un disque détecté
  automatiquement qu'un chemin saisi manuellement.
- **Suppression confirmée côté serveur** : `POST /api/jobs` avec
  `operation: "delete"` exige `confirm_delete_text` égal exactement à la
  valeur de `DELETE_CONFIRM_TEXT` (par défaut `SUPPRIMER`). Sans ce champ
  correct, l'API renvoie `403`, indépendamment de ce que fait l'interface —
  un appel API direct ne peut pas contourner la confirmation. Chaque élément
  à supprimer doit en plus être physiquement situé sous un dossier source
  configuré.
- **Chemins limités aux sources** : copier, déplacer et supprimer n'acceptent
  que des éléments *strictement à l'intérieur* d'un dossier source configuré
  (un chemin qui contient une source, ou qui sort de l'arborescence, est
  refusé en `403`, y compris via `..`). Avant, seule la suppression était
  contrôlée.
- **Source retirable vérifiée avant toute copie** : un déplacement ou une
  suppression sur un dossier en lecture seule (`:ro`) ou sans droits est
  refusé en `409` *avant* de copier quoi que ce soit — sinon on obtiendrait
  une copie complète à destination et une source qui ne peut pas disparaître.
- **Vérification avant suppression de la source** : un déplacement ne retire
  la source qu'après avoir contrôlé que chaque fichier est présent à
  destination avec la même taille.
- **Vérification d'espace disque** : avant toute copie/déplacement, l'espace
  libre réel de la destination est comparé à la taille totale de la
  sélection, moins la marge de sécurité (`SAFETY_MARGIN_BYTES`, 500 Mo par
  défaut). Insuffisant → `409`, rien n'est lancé.
- **Mode "dossiers parents complets"** (films) : ne remonte au dossier
  parent que si celui-ci ne contient aucune autre vidéo que le film
  sélectionné, et jamais si ce parent est directement la racine d'un dossier
  source — sinon seul le fichier est traité, avec un avertissement affiché.
- **Token Plex** : jamais codé en dur, lu uniquement depuis l'environnement
  (`.env`, non versionné). *Note : le script bash d'origine fourni en
  référence contenait un token Plex en clair comme valeur par défaut — pense
  à le régénérer si ce script a pu être partagé.*

## 6. Fonctionnalités de l'interface

- Bascule Films / Séries / **Ebooks**.
- Recherche par nom, tri en cliquant sur les en-têtes de colonnes (nom /
  taille / qualité / audio / note pour les films ; nom / taille / sources
  pour les séries ; nom / taille / format pour les ebooks) — reclique pour
  inverser l'ordre.
- **Pour les films, jaquette / genres / note / résumé, avec deux sources
  combinées** :
  - **Plex** (si configuré) : prioritaire, et seule source du statut
    vu/non vu (propre à TA médiathèque). Les jaquettes Plex sont servies
    via un proxy interne (`GET /api/plex/poster`) : le token Plex n'est
    jamais exposé au navigateur.
  - **TMDB** (The Movie Database, si une clé API est renseignée dans
    Réglages) : comble automatiquement ce que Plex ne trouve pas — soit
    parce que le film n'est pas dans ta bibliothèque Plex, soit parce que
    Plex n'est pas configuré du tout. Il n'existe pas d'API IMDb publique
    officielle ; TMDB est la source standard qu'utilisent la plupart des
    outils de gestion de médiathèque (Radarr, Jellyfin, Plex lui-même en
    coulisses) pour ce type de métadonnées. Le résumé est disponible en
    infobulle au survol de la jaquette. La note affiche sa source (Plex ou
    TMDB) au survol.
  - Un film non reconnu par aucune des deux sources affiche simplement "—"
    plutôt qu'une erreur. Le premier appel TMDB pour une grosse
    bibliothèque peut prendre quelques secondes (recherches en parallèle,
    limitées) ; les résultats sont ensuite mis en cache en mémoire pour le
    reste de la session.
- Filtre Plex (Tous / Déjà vus / Non vus) pour films et séries, avec bouton
  "Rafraîchir Plex" pour ne recharger le cache que sur demande (pas à
  chaque recherche). Sans objet pour les ebooks (Plex ne les gère pas).
- Sélection multiple par cases à cocher (+ "tout sélectionner").
- Pour les films et les ebooks : choix "fichiers sélectionnés uniquement" ou
  "dossiers parents complets" (utile pour un livre géré par Calibre avec sa
  couverture et ses métadonnées dans le même dossier), avec avertissements
  affichés en cas de repli de sécurité.
- Aperçu en direct du nombre d'éléments et de la taille totale de la
  sélection avant de lancer une action.
- Choix de la destination parmi les disques détectés automatiquement,
  avec espace libre affiché, et bouton de rafraîchissement.
- Copier / Déplacer / Supprimer avec **modal de confirmation explicite**
  (saisie du mot `SUPPRIMER` pour toute suppression).
- Progression en temps réel par élément (barre de progression alimentée par
  `rsync --info=progress2`, via WebSocket).
- Journal des opérations consultable dans un tiroir latéral.
- **Statuts explicites, jamais de faux succès** :
  - ✓ : copie (et, pour un déplacement, suppression de la source) vérifiées.
  - ⚠ *partiel* : la copie a réussi mais une partie de la source n'a pas pu
    être supprimée (fichier verrouillé, droits...). Le message au survol donne
    la cause. Un ⚠ n'est jamais silencieux.
  - ✕ *erreur* : l'élément n'a pas été traité ; la cause est affichée dans une
    notification ET au survol de la ligne.
  - – *ignoré* : déjà présent à destination (copie) ou conflit avec un contenu
    différent (rien n'est écrasé, la source reste en place). Un déplacement
    ignoré est signalé dans une notification.
  - Un bilan (« 3 OK, 1 erreur… ») s'affiche à la fin de chaque opération.

  Un déplacement où la destination contient déjà la copie à l'identique (cas
  d'une copie faite auparavant) retire simplement la source.

### Cache de la recherche

Les listes Films, Séries et Ebooks ne sont plus reparcourues à chaque ouverture
de page. Elles sont gardées en mémoire, déjà complétées (Plex, TMDB), et une
recherche ne fait que filtrer cette liste : elle est instantanée.

- **Au démarrage** : l'interface affiche la copie disque (`library-cache.json`,
  à côté de `settings.json`) tout de suite, pendant que la liste se reconstruit
  en arrière-plan.
- **Mise à jour** : la liste est reconstruite quand elle a plus de
  `SEARCH_CACHE_SECONDS` secondes (900 par défaut), après chaque opération
  (copie, déplacement, suppression, restauration), après un changement de
  réglages et après « Actualiser Plex ». Pendant la mise à jour, l'ancienne liste
  reste affichée avec la mention « liste en cours d'actualisation… », puis se
  met à jour toute seule.
- Si un disque ne répond pas pendant une reconstruction, la dernière liste
  correcte reste affichée.
- Le cache ne change ni les fichiers ni les réglages : c'est une copie de lecture.

## 7. Mode simulation (dry-run)

`DRY_RUN=true` dans `.env` fait tourner `rsync` avec `--dry-run` et
remplace les suppressions réelles par une ligne de log `[dry-run]` — utile
pour tester une sélection et ses garde-fous sans rien modifier sur disque.
Un badge "simulation (dry-run)" apparaît alors dans l'interface.

## 8. Différences volontaires avec le script bash d'origine

1. **Détection des disques sans `findmnt`** : plutôt que d'exiger
   `util-linux`/`findmnt` et un accès large à l'espace de montage de l'hôte,
   le backend lit directement `/proc/mounts` (toujours accessible dans un
   conteneur) et applique le même filtrage par préfixe. Fonctionnellement
   équivalent, mais sans dépendance supplémentaire ni élévation de
   privilèges.
2. **Sélection/tri/filtre côté client** : la recherche renvoie une seule
   fois la liste indexée (taille/qualité/audio/statut Plex) ; trier,
   filtrer et sélectionner se fait ensuite instantanément dans le
   navigateur, sans aller-retour serveur à chaque clic. Seule la
   *planification* (garde-fous, calcul de taille) et l'*exécution*
   repassent par l'API, qui reste la seule source de vérité pour la
   sécurité.
3. **Pas d'état de session serveur** : chaque appel à `/api/plan/...` ou
   `/api/jobs` transporte les chemins concernés explicitement (pas d'index
   de liste précédente stocké côté serveur). Plus simple à auditer, et
   robuste à un rechargement de page ou plusieurs onglets ouverts en
   parallèle.
4. **Confirmation de suppression par texte, pas par simple bouton** :
   repris du script (qui demandait de taper `SUPPRIMER`), mais désormais
   *également* vérifié côté serveur (voir section 4), pas seulement dans le
   modal du frontend.
5. **Historique des jobs en mémoire** : la liste des opérations en cours/
   terminées (`GET /api/jobs`) vit en mémoire du processus, pas en base de
   données — cohérent avec un outil personnel mono-utilisateur ; le journal
   texte (`LOG_FILE`), lui, est bien persistant (volume Docker).
6. **Configuration éditable à chaud** : le script bash relisait ses
   `SOURCE_DIRS_*` codés en tête de fichier à chaque lancement ; ici, ils
   (et le reste des réglages) sont modifiables depuis l'interface sans
   redémarrer le conteneur, et persistés dans `settings.json` plutôt que
   dans le code (voir section 3). `.env` ne sert plus qu'à amorcer le tout
   premier démarrage et à définir les montages Docker eux-mêmes.

## 9. Variables d'environnement & réglages

`.env` (voir `.env.example`) amorce la configuration initiale et définit
les montages de volumes (ce que Docker seul peut faire : `HOST_MEDIA_ROOT`,
`DEST_SCAN_ROOTS`...). Une fois l'application lancée, la plupart de ces
valeurs deviennent éditables depuis l'interface (⚙ Réglages, section 3) et
sont alors lues depuis `settings.json` (volume `archivage-data`), pas depuis
`.env` — `.env` n'est donc relu qu'au tout premier démarrage (tant que
`settings.json` n'existe pas encore) ou après un clic sur "Réinitialiser".

API correspondante, si tu veux scripter la configuration :
- `GET /api/settings` — réglages actuels
- `PUT /api/settings` — mise à jour partielle (seuls les champs fournis changent)
- `POST /api/settings/reset` — retour aux valeurs de `.env`
- `GET /api/settings/check-path?path=...` — vérifie qu'un chemin est visible/lisible dans le conteneur

## 10. Développement local sans Docker

```bash
cd backend
pip install -r requirements.txt
FRONTEND_DIR=../frontend SOURCE_DIRS_FILMS=/chemin/films \
  uvicorn app.main:app --reload --port 8080
```

## 11. Interface version 2 (responsive, métadonnées enrichies)

**Ce qui change par rapport à la version 1**

- **Trois affichages adaptés à l'écran.** Ordinateur : grille d'affiches avec
  panneau d'actions à droite. Tablette : grille adaptative, panneau d'actions
  en feuille du bas. Téléphone : grille à deux colonnes, liste compacte,
  barre de sélection collante en bas.
- **Bandeau de statistiques** propre à chaque média : films (volume, vus,
  durée cumulée, en 4K), séries (épisodes, saisons, terminées, en cours),
  ebooks (auteurs, formats).
- **Fiche détaillée** au clic sur un élément : affiche en fond flouté,
  résumé, genres, date d'ajout et sa source, dernière lecture Plex, chemins
  avec bouton « Copier ». Navigation précédent/suivant (flèches du clavier).
- **Filtres** : statut Plex (Vus / Non vus / En cours pour les séries), genre,
  qualité (films), date d'ajout (7 j, 30 j, 90 j, 1 an). Tri par titre,
  année, date d'ajout, taille, note, qualité, audio, épisodes, auteur, format.
- **Raccourcis clavier** : `/` pour la recherche, flèches pour naviguer dans
  la fiche, Échap pour fermer.

**Nouvelles informations affichées**

| Information | Films | Séries | Ebooks | Source |
|---|---|---|---|---|
| Année | oui | oui | oui | Plex, TMDB, ou nom de fichier (par priorité) |
| Date d'ajout | oui | oui | oui | Plex (`addedAt`), sinon date du fichier |
| Dernière lecture | oui | oui | non | Plex |
| Durée | oui | non | non | Plex |
| Classification | oui | non | non | Plex |
| Saisons / épisodes | non | oui | non | disque (et Plex pour la progression) |
| Progression vue | non | oui (4/19) | non | Plex |
| Auteur | non | non | oui | déduit du nom (« Auteur - Titre.epub ») |
| Jaquette, résumé, genres, note | oui | oui | non | Plex, puis TMDB |

**Corrections de logique incluses**

- Une série partiellement vue apparaît « En cours x/y » et non plus « non vue ».
- Un élément absent de Plex apparaît « — » (statut inconnu), et non plus « non vu ».
  Le filtre « Non vus » n'inclut donc que les titres confirmés non vus.
- Deux films au même titre (remake) : l'année sert à les départager, pour ne
  pas afficher les métadonnées de l'un sur l'autre.
- Le nom de fichier `Blade.Runner.2049.2017.2160p` donne désormais le titre
  « Blade Runner 2049 » et l'année 2017.
- TMDB enrichit aussi les séries (jaquette, genres, note, résumé, année), et ne
  remplace jamais une donnée déjà fournie par Plex.

**Clé TMDB** : nécessaire pour les jaquettes et résumés des séries absentes
de Plex. Renseignée dans Réglages, comme auparavant.

**Mise à jour** : `docker compose up -d --build`. Le navigateur peut garder
l'ancienne feuille de style : la version est dans l'URL (`style.css?v=...`) ;
un rafraîchissement forcé (Ctrl+F5) règle le problème.

## 12. Sauvegarde sur 1fichier (versions 5 à 7 : films, séries, ebooks)

Base : version 2. Envoi vers ton compte 1fichier premium, en archives chiffrées,
et restauration dans la bibliothèque. Trois types d'éléments.

**Ce qui est envoyé, et sous quel nom**

| Type | Unité envoyée | Dossier 1fichier | Identifiant |
|---|---|---|---|
| Film | le fichier ou le dossier du film | `tmdb-<id>` | TMDB (film) |
| Série | le dossier entier de la série (une seule archive) | `tmdb-tv-<id>` | TMDB (série) |
| Ebook | le dossier qui contient les livres | `ebook-<empreinte>` | aucun (empreinte du chemin) |

- Une archive = **7z chiffrée en AES-256**, en-têtes compris : ni le titre ni
  le nom des fichiers ne sont lisibles sans le mot de passe.
- Le dossier 1fichier ne montre que l'identifiant ; le titre n'apparaît pas.
- Pour un ebook, sélectionner un livre envoie **tout son dossier** (un envoi par dossier).
  La racine d'une source ne peut pas être envoyée d'un coup.
- **Un mot de passe aléatoire par film**, enregistré dans un index local
  (`1fichier-index.json`, à côté de `settings.json`, donc sur le volume
  `archivage-data`). Ce mot de passe est la seule clé de l'archive.
- Les fichiers locaux **ne sont jamais supprimés** par l'envoi. Tu gardes la
  main : une fois l'envoi vérifié, la suppression se fait avec le bouton
  habituel « Supprimer ».

**Utilisation**

1. Réglages › 1fichier : colle la clé API, puis « Tester la connexion ».
2. Sélectionne des films, puis « Envoyer sur 1fichier » dans le panneau
   d'actions. Un film déjà envoyé est ignoré (pas de doublon).
3. Onglet **1fichier** (barre de navigation) : la bibliothèque des éléments
   envoyés, en jaquettes. Chaque carte porte son type (Film, Série, Ebook), la
   note TMDB, les genres, et un badge d'état (« Sur 1fichier », « Restauré »).
   Filtres : type, statut, genre, recherche par titre, auteur ou année ; tri par
   titre, année, note, durée, taille ou date d'envoi.
4. Un clic sur une jaquette ouvre la fiche (synopsis, accroche, dossier 1fichier,
   dates, emplacement d'origine, lien TMDB pour films et séries, auteur, formats
   et premier livre pour les ebooks) et le bouton de restauration, avec un dossier
   de destination facultatif.
   La restauration ne touche pas à la copie sur 1fichier (voir ci-dessous).

5. Fiche d'une archive terminée :
   - **Récupérer hors de l'application** : « Afficher le lien et le mot de passe » montre le lien 1fichier
     et le mot de passe 7z, chacun avec son bouton « Copier ». Ces valeurs ne sont envoyées à
     l'interface que sur ce clic explicite, jamais dans la liste.
   - **Vérifier le lien sur 1fichier** : contrôle que l'archive existe toujours dans son dossier
     (lien actif ou introuvable), enregistre la date et l'heure de la vérification, et affiche la
     date d'envoi telle que 1fichier la renvoie. Un lien introuvable apparaît en « Lien introuvable »
     dans la grille. Une erreur de connexion ou d'API ne modifie rien.

**Index des mots de passe : sauvegarde (version 29)**

Sans l'index (`1fichier-index.json`), les archives ne s'ouvrent plus. Trois protections :

- **Copies automatiques** : avant chaque écriture de l'index, une copie de l'état précédent est gardée dans
  `1fichier-index.json.backups/` (au plus une par minute, les 100 dernières conservées, droits 0600).
  Ce dossier est sur le même volume : il protège contre une corruption ou une erreur, pas contre la perte du volume.
- **Export chiffré hors serveur** : Réglages › 1fichier › « Exporter l'index chiffré ». Tu choisis une phrase de
  passe (12 caractères minimum, jamais enregistrée). Le navigateur télécharge une archive 7z AES-256. Garde-la
  ailleurs que sur le serveur. L'onglet 1fichier affiche un avertissement tant qu'aucun export n'a été fait.
- **Empreinte SHA-256** : chaque archive reçoit son empreinte avant l'envoi. Une restauration la recalcule après
  téléchargement et refuse de déchiffrer une archive altérée. Les envois antérieurs à la version 29 n'ont pas
  d'empreinte : seul le contrôle CRC 7z s'applique.

Restaurer l'index depuis un export (conteneur arrêté) :

```
7z x 1fichier-index-AAAAMMJJ-HHMMSS.7z -o/tmp/index-restore -p'ta-phrase'
cp /tmp/index-restore/1fichier-index.json /chemin/vers/1fichier-index.json
```

Un export ne contient que les éléments présents au moment de l'export : exporte après chaque envoi important.

**Contrôles automatiques et alertes (version 30)**

- **Chaque nuit à `ONEFICHIER_CHECK_HOUR` (3 h par défaut)** : vérification de tous les liens (l'archive existe-t-elle
  toujours dans son dossier ?), puis test de restauration d'un élément. Le test télécharge l'archive, vérifie son
  empreinte SHA-256 et son CRC 7z avec son mot de passe, puis supprime la copie temporaire. Rien n'est extrait.
  Un élément n'est testé qu'une fois par mois, et seulement si l'archive fait 20 Go au plus.
- **Lancer maintenant** : bouton dans Réglages › 1fichier. Le résultat s'affiche sous l'état de l'index, et l'onglet 1fichier
  signale les liens introuvables.
- **Alertes** : renseigne une URL ntfy (`https://ntfy.sh/ton-sujet`) ou un webhook Discord dans Réglages › 1fichier.
  Une alerte part en cas de lien introuvable, d'erreur de contrôle, de test de restauration échoué, ou de sauvegarde
  qui échoue. Sans URL, rien n'est envoyé. L'URL d'un webhook contient un jeton : elle est masquée dans l'interface.
- Les contrôles sont suspendus pendant un envoi ou une restauration, et ne tournent pas en mode simulation.

**Deuxième copie des archives (version 30)**

Réglages › 1fichier › « Copie de chaque archive sur un disque externe » (ou `ARCHIVE_COPY_DIR`) : après chaque envoi
réussi, l'archive chiffrée est copiée sur ce dossier, relue, et son empreinte est comparée à celle de l'envoi.
Si 1fichier ne répond pas lors d'une restauration, la copie du disque est utilisée, avec la même vérification. Le disque doit être monté dans le conteneur (volume dans `docker-compose.yml`), comme les autres dossiers.
Un échec de copie ne remet pas en cause l'envoi 1fichier : il est signalé (journal et alerte).

**Intégrité des disques (version 30)**

Sur la carte d'un disque connecté :

- **Empreintes** : calcule un SHA-256 pour chaque fichier des dossiers MEDIA. Elles sont gardées dans
  `disk-manifests/` (volume de configuration), donc consultables disque débranché. Un fichier déjà empreinté,
  dont la taille et la date n'ont pas changé, n'est pas relu.
- **Vérifier l'intégrité** : relit tous les fichiers. Une **corruption probable** est un fichier dont la taille et
  la date sont identiques mais dont le contenu a changé : c'est le signe d'un support qui se dégrade.
  Les fichiers modifiés, absents ou nouveaux sont signalés à part.

Ces opérations durent longtemps sur un gros disque : elles tournent en arrière-plan, une à la fois, et ne démarrent
pas pendant une copie ou une restauration. Elles ne sont pas faites automatiquement : lance-les après
chaque grosse copie, puis de temps en temps.

Non couvert : l'état SMART des disques. Le conteneur ne voit pas le matériel ; sur l'hôte, `smartctl` (paquet
smartmontools) peut être lancé par cron et son résultat surveillé séparément.

**Tests**

```
cd backend
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests -q
```

Les tests couvrent les copies de l'index, l'export chiffré, les empreintes d'archive, la restauration de secours,
les contrôles planifiés et les alertes, ainsi que l'intégrité des disques. Les tests qui créent une archive
demandent `7z` sur la machine ; sans lui, ils sont ignorés. Aucun appel à 1fichier n'est fait : le réseau est simulé.

**Restauration depuis 1fichier**

Le menu « Destination de la restauration » de la fiche propose trois choix :

| Choix | Où part l'élément | Remarque |
|---|---|---|
| Dossier de restauration | le dossier défini dans Réglages (films, séries, ebooks) | par défaut `/MVE/MEDIA/VRAC/FILMS`, `/MVE/MEDIA/VRAC/SERIES`, `/MVE/MEDIA/VRAC/EBOOKS` |
| Emplacement d'origine | le chemin où l'élément se trouvait avant l'envoi | refusé si le chemin n'est plus accessible |
| Disque externe | `<disque>/MEDIA/FILM`, `<disque>/MEDIA/SERIES`, `<disque>/MEDIA/EBOOKS` | les disques proposés sont ceux détectés par l'application ; le sous-dossier se règle dans Réglages (« Sous-dossier films / séries / ebooks ») |

- Les trois dossiers de restauration se règlent dans **Réglages › 1fichier, section Restauration** et
  doivent être des chemins absolus (commençant par `/`).
- Le dossier de restauration doit **déjà exister** : il n'est pas créé. Si le
  disque ou le partage qui le contient n'est pas monté, la restauration échoue
  au lieu d'écrire dans le conteneur.
- Un disque externe reçoit son sous-dossier `MEDIA/…`, créé si besoin.
- Un chemin libre n'est pas proposé : on ne peut restaurer que vers les trois
  choix ci-dessus, pour limiter les endroits où l'application écrit.
- Les dossiers `/MVE/MEDIA/...` sont couverts par le montage `HOST_MEDIA_ROOT`
  du `docker-compose.yml` ; un dossier hors de ce montage doit être ajouté
  dans le `volumes:` du service.

**Libérer la place sur le serveur (films et séries)**

Pour récupérer de la place sur le serveur, sans perdre le film ou la série :

1. Dans le panneau Sélection, « Sélectionner les vus » coche les éléments vus
   dans Plex, parmi ceux affichés (les filtres comptent).
2. Choisis ensuite l'action :
   - **Déplacer** (panneau Action) : copie sur le disque choisi, vérifiée, puis
     retrait de la source. Rien n'est envoyé sur 1fichier.
   - **Envoyer sur 1fichier**, puis dans la fenêtre de confirmation :
     - « Garder la copie sur le serveur » : sauvegarde seule (comportement d'origine) ;
     - « Libérer le serveur : supprimer la copie locale » : la copie locale est
       retirée après la sauvegarde vérifiée. Confirmation obligatoire (tape
       SUPPRIMER) ;
     - « Libérer le serveur : copier aussi sur le disque sélectionné » : sauvegarde
       1fichier, puis copie sur le disque (dans `MEDIA/FILM` ou `MEDIA/SERIES`),
       vérifiée, puis retrait de la source. Un disque doit être choisi.

Règles :
- La copie locale n'est retirée qu'**après** une sauvegarde 1fichier dont la
  taille a été vérifiée, ou une copie sur disque dont la taille a été vérifiée.
  Si la suppression échoue, l'élément est signalé « partiel » et la sauvegarde
  reste valable.
- Un film déjà sauvegardé sur 1fichier est retiré du serveur sans nouvel envoi.
- Les ebooks ne sont pas concernés par cette option.
- Pour libérer le serveur, la source doit être montée en écriture (`:rw`).
- Le mode simulation (dry-run) décrit les actions sans rien modifier.

**Jaquettes des ebooks**

- La jaquette est celle du **premier livre** du dossier (ordre alphabétique).
  Formats lus : **EPUB** (image de couverture déclarée dans le livre), **CBZ**
  (première page, ordre naturel : tome 2 avant tome 10) et **PDF** (première
  page rendue). Les MOBI, AZW ou DJVU n'ont pas de jaquette extraite : le
  dossier est envoyé quand même, avec une initiale à la place.
- Si le premier livre n'a pas de jaquette lisible, la suivante est essayée
  (jusqu'à 10 livres). Le livre utilisé est indiqué dans la fiche.
- Les jaquettes sont réduites à 400 px et rangées dans `1fichier-covers/`, à
  côté de l'index : elles font partie de la sauvegarde du volume.
- Les ebooks n'ont pas de fiche TMDB : titre et auteur viennent du nom du
  dossier (« Auteur - Titre »).

**Fiches TMDB**

- Les informations (affiche, résumé, genres, durée, note, accroche) des films
  et des séries sont récupérées sur TMDB au moment de l'envoi, puis gardées dans
  l'index : la bibliothèque s'affiche sans appel réseau.
- Pour une série : saisons, épisodes, statut, et durée d'un épisode.
- Un film introuvable sur TMDB est marqué « fiche indisponible » et n'est
  pas retenté avant une heure. Le bouton « Actualiser les fiches » relance
  la récupération pour tous les films.
- La bibliothèque n'attend jamais TMDB : si des fiches manquent, elles sont
  récupérées en arrière-plan, la liste s'affiche tout de suite et se complète
  toute seule (un message le signale pendant le travail).
- Les fiches viennent de TMDB et peuvent être incomplètes. Les films envoyés
  avant la version 6 n'ont leur fiche qu'après « Actualiser les fiches ».

**Garde-fous**

- Le film doit être identifié sur TMDB : sans identifiant, pas de dossier
  nommable, donc pas d'envoi. TMDB doit donc être activé.
- Vérification de chaque archive (`7z t`) avant envoi, puis de la taille
  côté 1fichier après envoi. Une restauration vérifie le CRC de chaque fichier.
- Un envoi interrompu après le début du transfert reste dans l'index comme
  « Envoi incomplet » avec son mot de passe : rien n'est perdu. Le bouton
  « Vérifier sur 1fichier » cherche l'archive dans son dossier et rétablit le lien
  si elle est bien arrivée. Sinon, « Retirer de la liste » (le fichier partiel
  éventuel reste à supprimer à la main sur 1fichier).
- Un envoi en cours ne peut pas être retiré de l'index.
- Le jeton API n'est envoyé qu'à des serveurs `*.1fichier.com`. Il n'apparaît
  jamais en clair dans l'interface (seuls les 4 derniers caractères) ni
  dans le journal.
- Mode simulation respecté : aucun appel d'envoi ni d'écriture.

**Ce qu'il faut savoir**

- **Sauvegarde le volume `archivage-data`** (ou au moins `1fichier-index.json`
  et `settings.json`). Sans l'index, les archives sont inutilisables.
- **Dossier de travail** : `/MVE/TMP` par défaut, réglable dans Réglages › 1fichier ›
  Dossier de travail. Il doit exister sur l'hôte, être monté dans le conteneur
  (`/MVE/TMP:/MVE/TMP` dans `docker-compose.yml`) et avoir de la place libre. Si
  le dossier n'existe pas dans le conteneur, l'envoi s'arrête avec un message
  clair : rien n'est créé à la place sur le disque du système.
- Il faut un espace libre égal à la taille de l'élément pour la phase de
  compression, dans le dossier de travail. Pour une
  série entière, cela peut donc représenter plusieurs dizaines de Go.
- Une série présente à plusieurs emplacements sources est refusée : envoie-la
  depuis un seul dossier.
- **Pendant un envoi, l'interface reste utilisable.** La bibliothèque lit
  l'index sans verrou et sans toucher au disque d'envoi, et 7z tourne en priorité
  basse (`ionice -c3 nice -n 19`, si le conteneur le permet). Si la page reste
  lente malgré tout, c'est que le disque est saturé : compression et lecture de
  la source se font sur le même disque. Place alors le dossier de travail sur un
  disque séparé (par exemple `/MVE/TMP` sur un autre disque que `/MVE/MEDIA`).
- Compression 0 par défaut : la vidéo est déjà compressée, un niveau plus
  élevé coûte beaucoup de temps pour un gain faible.
- Plafond : 300 Go par fichier côté 1fichier.
- Si la bibliothèque ne répond pas au bout de 15 secondes, un bouton
  « Réessayer » apparaît au lieu d'un chargement infini.
- Une série est envoyée d'un seul bloc, pas saison par saison. Une restauration
  remet toute la série. Un envoi par saison demanderait un autre découpage.
- **Mot de passe dans la commande 7z** : il transite par la ligne de commande
  du processus 7z, visible seulement à l'intérieur du conteneur.

**Mise à jour** : l'image doit être reconstruite (`docker compose up -d --build`).
La version 7 ajoute deux dépendances Python, **Pillow** (réduction des jaquettes)
et **PyMuPDF** (jaquettes des PDF). Sans elles, le reste fonctionne, mais les
jaquettes de PDF et la réduction ne sont pas disponibles.

**Suivre un envoi en cours**

Pendant un envoi, la bibliothèque 1fichier affiche la ligne « Envois en cours et
incomplets » : étape (compression, vérification, envoi, finalisation), pourcentage,
octets envoyés, vitesse, temps restant, source et dossier de destination. Elle se
relit toutes les deux secondes tant qu'un envoi tourne. L'avancement n'est gardé
qu'en mémoire : il disparaît à la fin de l'envoi, et l'index n'est pas réécrit à
chaque seconde.

**Vérification à faire au premier envoi** : les chemins et champs de l'API
1fichier suivent l'implémentation de référence de rclone ; le test
d'intégration réel n'a pas été possible depuis l'environnement de
développement. Lance un petit film d'essai avant de sauvegarder la
bibliothèque entière.

## 13. Onglet Disques : bibliothèque des disques de sauvegarde (versions 13 à 18)

L'onglet **Disques**, à côté de 1fichier, liste ce qui est rangé sur les disques
externes (films, séries, ebooks), dans les sous-dossiers `MEDIA/…` réglés dans
Réglages. Le sous-dossier réglé est lu, ainsi que les noms courants :
`MEDIA/FILM` ou `MEDIA/FILMS`, `MEDIA/SERIES` ou `MEDIA/SERIE`,
`MEDIA/EBOOKS` ou `MEDIA/EBOOK`. Chaque élément affiche le dossier où il a été trouvé.
Si rien n'apparaît, le message indique les dossiers cherchés.
Les nouvelles copies vers un disque, elles, utilisent toujours le sous-dossier réglé.

**Ce qu'on voit**

- Une carte par disque connu. Un disque **connecté** est mis en évidence (bordure
  verte, pastille « Connecté », espace libre). Un disque **hors ligne** garde sa
  carte, marquée « Hors ligne » et « vu le … », et reste consultable.
- Le contenu est inventorié à la connexion puis gardé dans `disk-library.json`,
  à côté de `settings.json`. Un disque débranché reste donc lisible, en lecture
  seule. Le bouton « Rafraîchir les disques » relance l'inventaire ; sans cela,
  un disque déjà inventorié n'est relu qu'après 5 minutes.
- Chaque élément est une carte avec sa fiche, comme la bibliothèque :
  - films et séries : affiche, année, note, durée (films), saisons (séries), genres
    et résumé au survol, d'après TMDB (même recherche que la bibliothèque) ;
  - ebooks : titre et auteur d'après le nom du dossier, nombre de livres, formats,
    jaquette tirée du premier livre qui en a une.
  Les fiches se calculent en arrière-plan (la liste ne les attend pas) et restent
  dans `disk-library.json` : un disque débranché garde ses affiches. Une fiche
  introuvable n'est retentée qu'un jour plus tard, ou tout de suite avec « Rafraîchir ».
  Sans clé TMDB, films et séries n'ont que leur nom ; les ebooks gardent leur fiche.
- Chaque élément indique s'il est déjà « Sur 1fichier » (même nom que l'envoi).
- Filtres : type (films, séries, ebooks), disque (ou clic sur une carte), recherche par nom.
- Recherche (version 18) : la barre de recherche de l'en-tête, comme le champ
  « Filtrer par nom », cherche dans l'onglet Disques. Les résultats sont donnés en
  deux parties : « Dans ce dossier » (le type et le disque affichés) puis
  « Ensemble des disques » (tous les disques et tous les types). Un clic sur un
  résultat de l'ensemble ouvre son disque et son type, et la recherche reste.

**Identifier et renommer (version 17)**

Un film ou une série sans fiche (nom de release illisible pour TMDB, par exemple
`2006-Idiocracy.Mike Judge.720p WEB-DL VOST [MKV Corp].mkv`) porte un bouton
**Identifier** ; une fiche déjà trouvée propose **Corriger le titre**.

- La fenêtre préremplit le titre et l'année lus dans le nom, puis cherche sur TMDB.
  Les résultats montrent l'affiche, l'année et le résumé : on choisit le bon.
- Le **nouveau nom** est éditable ; pour un fichier, l'extension d'origine
  (`.mkv`…) est gardée automatiquement. Les caractères interdits sur exFAT/NTFS
  (`: < > " / \ | ? *`) sont remplacés (« Mission: Impossible » devient
  « Mission - Impossible »).
- Le renommage se fait sur le disque connecté, puis la bibliothèque et la
  reconnaissance TMDB sont mises à jour aussitôt : le prochain envoi 1fichier
  retrouve le titre (`Titre (année)`).
- Refusés : nom vide, nom commençant par un point, nom déjà pris dans le dossier,
  disque hors ligne ou en lecture seule. Chaque renommage est écrit dans le journal.
- Sans clé TMDB, la recherche est désactivée : le message le dit ; le renommage à la
  main reste possible.

**Ce qu'on peut faire (disque connecté seulement)**

- **Copier vers le serveur** ou **Déplacer vers le serveur** : vers un dossier
  source configuré pour ce type (films, séries ou ebooks). Le déplacement retire
  l'élément du disque après une copie vérifiée.
- **Envoyer sur 1fichier** : sauvegarde chiffrée, comme depuis les autres onglets.
  Pour les films et les séries, « Retirer du disque après sauvegarde vérifiée »
  libère le disque (confirmation tapée requise).
- **Supprimer du disque** : confirmation tapée (SUPPRIMER) obligatoire.
- **Retirer de la liste** : pour un disque hors ligne seulement. Ses fichiers ne
  sont pas touchés.

**Progression**

Chaque opération (copie ou déplacement vers le serveur, envoi 1fichier, suppression)
affiche une barre de progression globale, le pourcentage, l'étape en cours
(compression, envoi…) et la progression de chaque élément. Les opérations lancées
depuis l'onglet Disques apparaissent dans la section « Transferts » de cet onglet ;
les autres restent dans la file à droite.

**Règles de sécurité**

- Un élément n'est reconnu que s'il est un **enfant direct** d'un sous-dossier
  `MEDIA/…` d'un disque **connecté** : jamais la racine d'un disque, jamais un
  sous-dossier plus profond, jamais un chemin hors des disques détectés.
- Un disque hors ligne ne permet aucune action : le serveur refuse (403).
- Le dossier de destination d'une copie vers le serveur doit être un dossier
  source configuré pour ce type.
- Un disque monté en lecture seule (`:ro`) ou sans droits d'écriture est refusé
  avant toute copie ou suppression.
- Simulation (dry-run) : les actions sont simulées, rien n'est écrit ni supprimé.

**Vérifications automatiques (version 13)**

- `tests/test_1fichier.py` : sections [26] à [29], 287 contrôles au total (inventaire,
  repérage, hors ligne, transferts, suppression, sauvegarde 1fichier depuis un disque,
  renommage et recherche TMDB).
- `uitest/ui_check.js` : 154 contrôles, dont l'onglet Disques (un disque connecté
  est simulé par un montage tmpfs ; un disque hors ligne par une fiche enregistrée ;
  identification et renommage d'un film non reconnu).

## 14. Ebooks : une collection par dossier, jaquettes (version 19)

Dans l'onglet **Ebooks** comme dans l'onglet **Disques**, un dossier de livres
est **une seule carte** : une collection. Un dossier « Asimov - Fondation »
contenant plusieurs EPUB et CBZ donne une carte, avec le nombre de livres, les
formats (« CBZ · EPUB »), la taille totale et l'auteur et le titre tirés du nom
du dossier. Un livre isolé posé à la racine d'une source garde sa propre carte.

Sélectionner une collection l'envoie entière (sauvegarde 1fichier, copie,
déplacement, suppression) : le plan prend le dossier, sans avertissement.

**Jaquettes** : la jaquette est cherchée dans l'ordre suivant.

1. une image importée à la main (bouton « Changer la jaquette » dans la fiche
   d'un ebook, ou sur la carte d'un ebook de disque) : elle remplace les autres,
   ramenée à 400 px en JPEG, 5 Mo au maximum ;
2. la première couverture trouvée dans les livres du dossier (EPUB, CBZ, PDF ;
   les 10 premiers livres sont examinés) ;
3. en ligne, sur **Open Library** (openlibrary.org), sans clé API : recherche par
   titre et auteur tirés du nom du dossier, jaquette du premier résultat qui en a
   une. C'est l'équivalent de TMDB pour les livres. Une recherche infructueuse
   n'est pas refaite avant un jour ; une panne réseau n'est pas mise en cache.

Les jaquettes sont gardées dans `1fichier-covers/` à côté des réglages. Pour les
désactiver : `EBOOK_ONLINE_COVERS=false` (les points 1 et 2 restent actifs).
Les CBR et les MOBI/AZW3 n'ont pas de jaquette extraite : ils passent par le point 3.

**Limites connues**

- Open Library ne couvre pas toutes les BD ni tous les titres : une jaquette
  manquante reste la couverture générée (lettre et couleur).
- Le nom du dossier doit contenir le titre. Un nom de release comme
  « 2006-Idiocracy.Mike Judge… » ne donne pas un titre propre : renomme le dossier
  (« Auteur - Titre ») pour de meilleurs résultats.

**Vérifications automatiques (version 19)**

- `tests/test_1fichier.py` : section [30], 315 contrôles au total (collection,
  jaquette tirée d'un livre, Open Library simulé, import, plan, disque).
- `uitest/ui_check.js` : 159 contrôles (cartes par dossier, import sur les deux onglets).

## 15. Identifier les films et séries du serveur (version 21)

Depuis la fiche d'un film ou d'une série de la bibliothèque serveur, le bouton
**Identifier** ouvre la fenêtre habituelle : titre et année préremplis depuis le
nom, recherche TMDB (affiche, année, résumé), nom éditable.

**Rien n'est renommé sur le serveur.** Plex, Sonarr et Radarr gardent leurs noms et
leurs chemins. L'identification est enregistrée dans l'application
(`library-identities.json`, à côté de `settings.json`) et sert à :

- **afficher** le bon titre et la bonne année dans la liste (un badge « Identifié »
  apparaît dans la fiche) ;
- **chercher la fiche TMDB** à partir du titre identifié, au lieu du nom de fichier ;
- **nommer la copie** envoyée vers un disque : un film devient « Titre (année) » avec
  son extension d'origine, une série devient le nom identifié du dossier ;
- **1fichier** : si le film ou la série a un identifiant TMDB enregistré, c'est lui qui
  nomme le dossier distant (`tmdb-<id>`), sans nouvelle recherche.

Une copie sans identification garde son nom d'origine. Une même série présente dans
plusieurs sources partage son identification (elle est repérée par le nom du dossier).

Refusés : chemin hors de la bibliothèque, racine d'une source, nom vide ou interdit.
L'identification est écrite dans le journal.

**Vérifications automatiques (version 21)**

- `tests/test_1fichier.py` : section [31], 329 contrôles au total (identification
  sans renommage, liste et recherche, copie renommée pour un film et une série,
  1fichier avec identifiant enregistré).
- `uitest/ui_check.js` : 173 contrôles, dont l'identification d'un film et d'une série
  du serveur, fichiers d'origine inchangés.

## 16. Séries par saison : envoi, restauration et copie (version 22)

Une série trop grosse pour être envoyée ou copiée d'un bloc peut maintenant être
traitée **saison par saison**, ou en entier.

**Fiche d'une série (serveur)** : la section **Saisons** liste chaque saison (épisodes,
taille, état « Sur 1fichier » ou « Non envoyée ») avec trois actions :

- **1fichier** : envoie cette saison seule ;
- **Copier** / **Déplacer** : vers le disque choisi dans la barre d'envoi ;
- la série entière reste envoyée avec les boutons habituels (sélection).

**Repérage des saisons** : un sous-dossier de la série est une saison s'il se nomme
`Season 1`, `Saison 2`, `S03`… Un dossier sans numéro est listé mais n'est pas
envoyable par saison. Les fichiers vidéo posés directement à la racine de la série ne
sont rattachés à aucune saison : ils partent avec la série entière (la fiche le signale).

**Sur 1fichier** : une archive par saison, `tmdb-tv-<id>-S01.7z`, rangée dans le dossier
de la série `tmdb-tv-<id>` (partagé avec la série entière). Dans l'index, la clé est
`tv:<id>:s<n>` ; la série entière reste `tv:<id>`. Une série envoyée en entier compte
toutes ses saisons comme envoyées.

**Restauration** : chaque saison est une entrée distincte de l'onglet 1fichier
(« Breaking Bad · Saison 2 »), restaurable seule. Elle revient :

- à son emplacement d'origine, dans le dossier de sa série ;
- dans le dossier de restauration des séries (réglages), dans un sous-dossier au nom
  de la série ;
- sur un disque externe, dans `MEDIA/SERIES/<série>/<saison>`.

**Copie vers un disque** : la saison part dans `MEDIA/SERIES/<série>/<saison>`, sous le
nom identifié de la série (version 21). Une saison copiée sur un disque qui a déjà la
série est fusionnée dans le dossier de la série, sans rien écraser.

**Déplacer après envoi (« retirer la copie locale »)** : la saison est copiée sur le
disque dans le dossier de sa série, vérifiée, puis retirée du serveur. Les autres saisons
restent en place ; le dossier de la série n'est pas supprimé.

**Interface de l'envoi** : la confirmation affiche « 1 élément » pour une saison, et le
libellé de la file est « Série · Saison N ».

**Nouveautés techniques**

- API `GET /api/series/seasons?path=…` : saisons d'une série, avec leur état 1fichier.
- Correction : `POST /api/1fichier/entries/{key}/recover` ne suppose plus que la clé est
  un identifiant TMDB brut (une clé de série plantait).

**Vérifications automatiques (version 22)**

- `tests/test_1fichier.py` : section [32], 42 contrôles : repérage, classement, envoi de
  deux saisons, saison sans numéro refusée, déjà envoyée ignorée, API, restauration à
  l'origine / dans le dossier de restauration / sur disque, copie vers disque, déplacement
  après envoi. Total : 371 contrôles.
- `uitest/ui_check.js` : 185 contrôles, dont la fiche d'une série en saisons (lignes,
  boutons, dossier sans numéro, racine), et la fenêtre d'envoi d'une saison annulée.

**Non vérifié sur matériel réel** : envoi réel vers 1fichier et disque externe (tests
avec un faux serveur et un montage tmpfs).

## 17. Liste d'attente des envois et copies (version 23)

Une copie ou un envoi de plusieurs éléments affiche désormais **toute** la liste
d'attente dès le départ : chaque élément apparaît en attente (« · »), puis passe
« ▸ » pendant son traitement, puis reçoit son résultat (✓, ✕, ⚠, –). L'en-tête indique
le nombre d'éléments restants (« en cours… · 4 à traiter »).

Côté serveur, l'événement de départ (`job_start`) porte la liste des libellés, dans
l'ordre de traitement. Les libellés sont les mêmes que ceux des événements d'avancement
(saison : « Série · Saison N » ; restauration : nom affiché de l'entrée).

**Vérifications automatiques (version 23)**

- `tests/test_1fichier.py` : section [33], libellés de la liste d'attente pour une copie,
  un envoi (saison) et une restauration (saison, entrée inconnue). Total : 377 contrôles.
- `uitest/ui_check.js` : 189 contrôles, dont la liste d'attente d'une copie de trois
  saisons (trois lignes, compteur jusqu'à « terminé », trois coches).

## 18. Envois : file d'attente, saisons choisies, sagas de films (version 24)

**File d'attente visible dans la vue 1fichier.** La section « Envois en cours et incomplets »
affiche, sous l'envoi en cours, les éléments **en attente** de l'envoi groupé
(« Envoi 2 sur 3 »), avec leur source. Le titre indique le nombre d'éléments en attente.
Une saison en cours affiche son numéro, son nombre d'épisodes et sa taille ; une série
entière affiche son nombre de saisons et sa taille.

**Choix des saisons avant l'envoi.** Quand on envoie une série depuis la liste (bouton
« Envoyer sur 1fichier »), la confirmation liste ses saisons avec une case chacune
(cochées par défaut, sauf celles déjà sur 1fichier). L'option « Série entière » envoie
une seule archive, comme avant. Le compte et la taille annoncés suivent la sélection.

**Sagas de films.** Nouvel affichage « Sagas » (bouton à côté de Grille et Liste, onglet
Films) : une carte par saga (collection TMDB), avec son affiche, le nombre de films
possédés sur le total, et la liste complète des films de la saga : chaque film indique
où il se trouve (Serveur, Disque, 1fichier) ou porte la mention « Manquant ».

- La saga d'un film vient de sa fiche TMDB (collection) ; les films sans identifiant
  TMDB ne sont pas classés (le nombre est indiqué : identifie-les depuis leur fiche).
- Les données TMDB des films et sagas sont gardées dans `sagas.json` (à côté de
  `settings.json`) : elles ne sont pas redemandées après un redémarrage.
- Récupération en arrière-plan : la vue se met à jour pendant que TMDB répond.
- Sans TMDB activé, la vue indique pourquoi.

**Vérifications automatiques (version 24)**

- `tests/test_1fichier.py` : section [34], 15 contrôles (collection et affiche d'une saga,
  films possédés et manquants, ordre de sortie, sources, file d'attente d'un envoi).
  Total : 392 contrôles.
- `uitest/ui_check.js` : 208 contrôles, dont la file d'attente dans la vue 1fichier, le
  choix des saisons à la confirmation, la vue Sagas avec une saga simulée (cartes, films
  possédés et manquants) et le message sans TMDB.

**Non vérifié sur matériel réel** : les collections TMDB réelles (tests avec une fausse
réponse TMDB), et l'envoi réel vers 1fichier.

## 19. Filtre « Infos TMDB » (version 25)

Un menu **Infos TMDB** (Toutes, Sans infos TMDB, Avec infos TMDB) apparaît dans trois vues :

- **Bibliothèque** (films et séries) : à côté des filtres Genre, Qualité et Ajouté. Masqué
  pour les ebooks.
- **1fichier** : à côté du statut et du genre.
- **Disques** : à côté du choix du disque. Il s'applique aussi à la recherche « Ensemble des
  disques ».

**Définition.** Un élément a des infos TMDB s'il porte un identifiant TMDB connu : celui
trouvé par TMDB ou choisi à l'identification (serveur, disque, 1fichier). Pour un disque,
c'est la fiche enregistrée qui compte : une fiche « introuvable » n'a pas d'identifiant.
**Les ebooks n'ont jamais d'infos TMDB** (leurs infos viennent d'Open Library) : ils
apparaissent donc sous « Sans infos TMDB ».

- Le filtre est remis à « Toutes » en changeant d'onglet et par « Réinitialiser ».
- Dans la vue Disques, changer le filtre vide la sélection, comme le choix du disque.
- Le filtre ne supprime ni ne modifie rien : il masque seulement les éléments.

**Vérifications automatiques (version 25)**

- `uitest/tmdb_filter_check.js` : 19 contrôles de la logique (identifiant, ebooks,
  fiches introuvables). Ne demande pas de navigateur.
- `uitest/ui_check.js` : 229 contrôles, dont le filtre dans les trois vues (comptages
  calculés depuis les données affichées), le masquage pour les ebooks, la remise à zéro
  au changement d'onglet et le message « aucun résultat » qui renvoie au filtre.
  Limite : les fixtures n'ont aucun film identifié par TMDB côté disque ni bibliothèque ;
  le cas « Avec infos » y est donc testé à zéro, et le cas positif par le contrôle de
  logique ci-dessus.
- `tests/test_1fichier.py` : 392 contrôles (inchangés, non concernés par ce filtre).

## 20. Cache de travail 1fichier : utilisation et vidage manuel (version 26)

Le **cache de travail** est le dossier de Réglages › 1fichier › Dossier de travail (par
défaut `/MVE/TMP`). Chaque envoi ou restauration y écrit son archive temporaire, dans un
sous-dossier au nom aléatoire, supprimé à la fin du travail. Un arrêt en pleine opération
peut laisser un de ces dossiers.

**Dans la vue 1fichier**, une barre « Cache de travail » indique :

- le nombre de dossiers temporaires et leur taille ;
- la place libre sur le disque du dossier de travail ;
- le nombre d'autres éléments présents dans le dossier (conservés, jamais supprimés) ;
- si le dossier est introuvable (montage manquant), un message le signale.

**Vider le cache** supprime les dossiers temporaires, après confirmation. Le bouton est
bloqué, avec la raison affichée au survol, dans deux cas :

- un envoi ou une restauration 1fichier est en cours ;
- un envoi ou une restauration attend son tour (file d'attente).

Il est aussi bloqué quand il n'y a rien à vider. Le serveur refuse le vidage pendant un
transfert (réponse 409), même si l'interface n'a pas été actualisée.

- Le vidage ne touche que les dossiers au nom de 32 caractères hexadécimaux. Les autres
  fichiers et dossiers du dossier de travail sont conservés.
- En mode simulation (dry-run), rien n'est supprimé ; l'opération est seulement journalisée.
- Les suppressions apparaissent dans le journal (`CACHE`).

Routes : `GET /api/1fichier` renvoie désormais `staging` (utilisation et état `busy`) ;
`POST /api/1fichier/staging/clear` vide le cache et renvoie `removed`, `freed_bytes`.

**Vérifications automatiques (version 26)**

- `tests/test_1fichier.py` : section [35], 16 contrôles (utilisation, refus pendant un
  transfert en attente ou en cours, vidage, dossiers conservés, simulation). Total : 408.
- `uitest/ui_check.js` : 240 contrôles, dont la barre du cache, le blocage pendant un
  transfert simulé, la confirmation et le vidage réel sur le dossier de test.

**Non vérifié sur matériel réel** : le vidage d'un dossier de travail réel (plusieurs Go,
disque de l'hôte).

## 21. Sagas depuis les vues 1fichier et Disques ; erreurs TMDB passagères (version 27)

**Sagas partout.** Un bouton « Sagas » est ajouté dans la barre des vues 1fichier et
Disques. Il affiche le même panneau que la bibliothèque : une carte par saga, et pour
chaque film son emplacement (Serveur, Disque, 1fichier) ou « Manquant ». Le panneau
remplace la liste ; les filtres restent en place, et le bouton revient à la liste.
Les sagas utilisent toutes les sources, pas seulement celle de la vue.

**Erreurs TMDB passagères.** Avant, une erreur réseau, une limite TMDB (429) ou une
erreur serveur pendant la récupération d'une fiche faisait marquer le film « introuvable »
pendant 24 heures : sa saga n'apparaissait pas pendant tout ce temps. Désormais, seule une
réponse 404 de TMDB marque un film introuvable ; une erreur passagère est retentée au
prochain passage.

**Données à reprendre.** Les films déjà marqués « introuvable » à cause d'une ancienne
erreur restent marqués jusqu'à 24 heures. Pour forcer la reprise, supprimer `sagas.json`
dans le volume `/data` (les sagas sont recalculées ; rien d'autre n'en dépend).

**Vérifications automatiques (version 27)**

- `tests/test_1fichier.py` : section [36], 4 contrôles (erreur passagère non marquée,
  reprise avec la saga, 404 marqué introuvable). Total : 412.
- `uitest/ui_check.js` : 254 contrôles, dont les sagas dans les vues 1fichier et Disques
  (bouton, panneau, retour à la liste, emplacement des films).

**Non vérifié sur matériel réel** : la collection TMDB d'une saga précise (la trilogie
du dollars sur votre installation) ; les données TMDB réelles ne sont pas accessibles
depuis l'environnement de développement.

## 22. Plages de transfert, vitesse et priorités (version 31)

Réglages › **Plages de transfert et vitesse**.

**Ce qui est réglé.** Les envois et restaurations 1fichier, et les copies ou déplacements vers un disque
(y compris le déplacement qui suit un envoi). Les suppressions ne sont pas concernées : elles n'utilisent pas le réseau.

**Plages.** Chaque plage a un nom, une heure de début, une heure de fin (`24:00` = minuit ; une plage peut passer
minuit, par exemple `22:00` à `06:00`), une case « Active », et deux vitesses en Mo/s : une pour 1fichier, une pour
les copies disque. `0` = illimité. Par défaut : **Jour** 09:00 à 22:00 (2 Mo/s côté 1fichier, 30 Mo/s côté disque)
et **Nuit** 01:00 à 07:00 (illimité). Les plages se règlent à la main, avec « Ajouter une plage » ou « Supprimer ».

**Hors plage.** Aucun élément ne démarre. La carte de la tâche affiche « Hors plage : reprise à HH:MM ». Avec les
valeurs par défaut, rien ne démarre entre 07:00 et 09:00 ni entre 22:00 et 01:00. Sans plage activée, les transferts
sont en pause. Pour un fonctionnement permanent, active une plage 00:00 à 24:00. Une copie disque déjà commencée est
suspendue hors plage (voir la section 23).

**Un envoi 1fichier commencé va au bout.** Il ne peut pas être repris : l'interrompre le recommencerait à zéro. Un
envoi démarré à 21:50 continue donc après 22:00. Hors plage, il garde la vitesse de la dernière plage active
(2 Mo/s après la plage Jour ; illimité si la nuit l'a démarré et qu'il finit avant 09:00).
**Une copie disque commencée est suspendue** hors plage et reprise à la plage suivante (section 23).

**Un seul envoi à la fois.** Un seul envoi ou une seule restauration 1fichier tourne à la fois (ils partagent la
même liaison). Une seule copie tourne par disque : deux disques différents peuvent être copiés en même temps (section 23). L'emplacement est rendu entre deux archives : une tâche plus prioritaire passe alors, sans attendre que
la tâche en cours soit finie.

**Priorité.** Urgent, Normal (défaut), Basse. À emplacement libéré, la tâche la plus prioritaire passe ; à priorité
égale, la plus ancienne. La priorité se change sur la carte de la tâche, ou dans la file d'attente 1fichier, tant que
la tâche n'est pas terminée. Elle s'applique aux éléments qui restent à traiter.

**Contrôles planifiés.** Le test de restauration (qui télécharge une archive) n'a lieu que pendant une plage ; sinon
il est reporté au contrôle suivant. La vérification des liens n'utilise pas de débit.

**Fuseau horaire.** Les heures des plages sont celles du serveur. Le conteneur reçoit `TZ=Europe/Paris` (variable
modifiable dans `.env`) ; sans cela, il travaille en UTC et les plages seraient décalées de 1 à 2 heures. L'image
installe `tzdata` pour que ce fuseau soit reconnu.

**Limites connues.**
- Un envoi 1fichier ne peut pas être suspendu au milieu : une connexion coupée ou figée est perdue, et son débit ne
  peut pas tomber à zéro sans que le serveur de 1fichier ferme la connexion. La règle « un envoi commencé va au bout »
  le remplace. (Les copies disque, elles, se suspendent : voir la section 23.)
- La file est en mémoire : un redémarrage du conteneur annule les tâches en attente, comme avant.
- Les vitesses sont des limites côté serveur. Le débit réel dépend aussi de la liaison internet et de la box.

**Vérifications.** `tests/test_scheduler.py` couvre les plages (dont celle qui passe minuit), la vitesse et son
maintien hors plage, le débit sans rafale, l'ordre de la file (priorité, puis arrivée, changement de priorité,
annulation), l'attente de plage, `--bwlimit` pour rsync, ainsi que deux tâches complètes : une copie disque réelle
et deux envois 1fichier simulés (la tâche urgente passe entre deux archives).

## 23. Copies disque : suspension, un emplacement par disque, vérification par empreinte (version 32)

**Suspension hors plage.** Une copie vers un disque (ou un déplacement) déjà commencée est suspendue quand la plage
se ferme : le processus rsync reçoit SIGSTOP et ne consomme plus rien. Elle reprend à l'ouverture de la plage
suivante (SIGCONT). La carte de la tâche affiche « Copie suspendue » puis la reprise. Une suspension ne fait aucun
dégât : rien ne transite par le réseau, et le fichier en cours reste ouvert tel quel.

**Relance quand la vitesse change.** rsync ne sait pas changer sa limite de débit en cours de route. Si la plage qui
s'ouvre a une autre vitesse que celle de la copie en cours (par exemple nuit illimitée après une journée à 30 Mo/s),
rsync est arrêté puis relancé avec la nouvelle limite. L'option `--partial` garde ce qui est déjà copié : la relance
reprend là où elle s'était arrêtée, mais relit la partie déjà copiée sur le disque pour la comparer. Le journal note
la relance. Sans changement de vitesse, la copie continue sans relance.

**Un emplacement par disque.** Il n'y a plus une seule file pour toutes les copies : chaque disque a la sienne. Une
copie vers le disque A n'attend plus une copie vers le disque B. Le disque est reconnu à partir de la destination (ou,
pour une copie depuis un disque, à partir de l'élément copié). Le libellé de la file affiche le nom du disque
(« En file (disque MonDisque) : position 2 sur 2 »). Une seule copie à la fois reste la règle pour un même disque.
Le déplacement qui suit un envoi 1fichier (option « Libérer le serveur : copier aussi sur le disque sélectionné ») passe
aussi par l'emplacement de son disque.

**Vérification par empreinte SHA-256.** Avant, une copie était contrôlée par la seule taille des fichiers : une
erreur de contenu de même taille passait inaperçue. Désormais, chaque copie est relue à la source et à la destination,
et les empreintes doivent être identiques fichier par fichier :

- seule une copie vérifiée est annoncée ✓ ;
- un **déplacement ne retire la source qu'après** cette vérification ;
- si les empreintes diffèrent, l'élément passe en erreur et la source est conservée. Une copie créée par ce passage
  est retirée (une copie qui existait déjà, en fusion, est signalée et laissée en place) ;
- un déplacement vers une copie déjà présente, de même taille, vérifie aussi le contenu avant de retirer la source.

**Coût.** Chaque copie est lue deux fois de plus (source et disque). Sur un film de 40 Go, comptez le temps d'une
relecture complète du disque, en plus de la copie. La vérification n'est pas limitée en débit : elle tourne dans
l'emplacement du disque, donc après la copie et pendant la même plage.

**Limite restante.** Une copie simple dont le fichier existe déjà à l'identique (même nom, même taille) est toujours
reconnue par la taille seule : elle est ignorée sans relecture. Sans risque pour les données (rien n'est retiré),
mais une corruption de même taille à destination ne serait pas détectée par cette seule opération : le module
d'intégrité des disques (section 13) sert à la repérer.

**Vérifications.** `tests/test_scheduler.py` ajoute : un emplacement par disque, suspension puis reprise sans relance,
relance quand la vitesse change, fin normale sans intervention, détection d'une copie altérée de même taille, et trois
tâches avec vrai rsync : copie suspendue puis relancée (le fichier final est identique à la source), déplacement
retiré seulement après vérification, déplacement conservé quand la vérification échoue.

## 24. Terrain préparé : hébergeurs, santé des disques (version 33)

Cette version ne change aucun comportement visible. Elle pose deux fondations pour la suite.

**Un socle pour les hébergeurs.** 1fichier n'est plus le seul hébergeur reconnu par le code. Un hébergeur se décrit
par une clé stable, un type de transfert (sa vitesse dans les plages, son emplacement dans la file), ses domaines
autorisés et ses actions. Le dossier `backend/app/hosts/` contient :

- `base.py` : la classe commune. Une action n'existe que si l'hébergeur la redéfinit : l'API n'annonce jamais une
  fonction qui n'existe pas ;
- `onefichier.py` : 1fichier, pour l'instant seulement son test de connexion. L'envoi, le téléchargement, la
  vérification de lien et la suppression restent dans `onefichier.py`, liés à l'index et à la file ; les déplacer
  maintenant prendrait plus de risque que de gain. C'est le prochain chantier, hébergeur par hébergeur ;
- `__init__.py` : le registre, avec des vérifications à l'import (clé unique, type de transfert connu, domaine présent).

Le contrôle des adresses renvoyées par un hébergeur est dans `app/urlguard.py`. Il exige un domaine de la liste
blanche et, pour les nouveaux hébergeurs, le schéma https. Le contrôle de 1fichier garde son comportement exact
(il accepte encore http, comme avant) ; un test vérifie que ses réponses n'ont pas changé.

Les types de transfert sont désormais une seule liste, `config.TRANSFER_KINDS`. Les plages, le planificateur et les
libellés de file la lisent. **Pour ajouter un hébergeur**, il faut aussi un champ de vitesse dans l'interface
(`frontend/app.js`, `twRowHtml`) : sans lui, enregistrer les réglages remet cette vitesse à 0 (illimitée).

**Santé matérielle des disques (SMART).** Le contrôle d'intégrité de la section 13 vérifie le CONTENU des fichiers.
Ce qui manquait : l'état du support lui-même (secteurs réalloués, en attente, erreurs non corrigibles, température).
Le conteneur n'a pas accès à `/dev` et ne le recevra pas. Le travail se fait donc sur l'hôte :

1. `scripts/disk_health.py` (bibliothèque standard, `smartmontools` et `util-linux`) lit le SMART de chaque disque
   monté sous `/media`, `/mnt` ou `/run/media`, et écrit un JSON par disque. Il ne lance aucun autotest et ne modifie
   rien. Il se lance en root :

   ```bash
   sudo python3 scripts/disk_health.py --out /var/lib/archivage-health --dry-run   # affiche sans écrire
   sudo python3 scripts/disk_health.py --out /var/lib/archivage-health
   ```

2. Ce dossier est monté **en lecture seule** dans le conteneur (`HOST_HEALTH_DIR` dans `.env`, défaut
   `/var/lib/archivage-health`, monté sur `/health`).
3. `backend/app/disk_health.py` valide chaque rapport strictement (un fichier invalide est signalé et n'empêche pas
   les autres) et calcule un niveau : **critique** (autotest en échec, secteurs en attente ou erreurs non corrigibles,
   température ≥ 65 °C), **attention** (secteurs réalloués, température ≥ 55 °C), **ok**, ou **inconnu** (SMART
   indisponible sur le boîtier). Un rapport de plus de 36 heures est marqué « périmé ».

Un disque débranché garde son dernier rapport : il apparaît comme « non branché » avec la date de son contrôle.

**Planifier le script (sur l'hôte).** Par exemple un service systemd `oneshot` qui lance la commande ci-dessus, déclenché
chaque jour à 02:30 par un timer. Le script suit les mêmes règles de montage que l'application.

**Limites connues.**

- Beaucoup de boîtiers USB ne transmettent pas le SMART, même avec le pilote SAT que le script essaie automatiquement.
  Le rapport affiche alors « inconnu » : ce n'est pas un disque sain.
- Le script n'est pas encore branché sur l'interface : `GET /api/disks/health` renvoie les rapports, aucun écran ne les
  affiche. C'est le prochain pas.
- Les champs lus sont ceux d'ATA et NVMe les plus courants. Un fabricant qui range ses compteurs ailleurs apparaît comme
  « inconnu ».
- Aucun numéro de série n'est écrit ni renvoyé par l'API.

**Routes.** `GET /api/hosts` (hébergeurs, configuration, actions disponibles, sans clé) et `GET /api/disks/health`
(rapports, niveaux, disques branchés en premier, dernier niveau sur les disques branchés).

**Vérifications.** `tests/test_hosts.py` : registre, actions annoncées, adresses 1fichier inchangées, https pour le socle,
plages ouvertes à un nouveau type, libellés des emplacements, route `/api/hosts`. `tests/test_disk_health.py` : validation,
niveaux, rapport invalide isolé, rapport périmé et disque débranché, dossier absent, route `/api/disks/health`.
`tests/test_disk_health_script.py` : lecture de la sortie smartctl (ATA, NVMe, boîtier sans SMART, valeurs aberrantes),
montages sous les racines, nouvelle tentative avec `-d sat`, absence de smartctl ou de lsblk, écriture atomique, et un
rapport produit par le script qui passe la validation de l'application.

## 25. Médias adultes : classement, affichage désactivé par défaut, scraping TMDB (version 34)

> **Version 36 :** la case « Afficher les médias adultes » et la variable `ADULT_VISIBLE` sont remplacées par un
> code d'accès vérifié par le serveur (section 27). Les passages de cette section qui parlent de la case ou de
> `adult_visible` ne sont plus valables ; le reste (classement, scraping, copies) l'est toujours.

Les films adultes ont leur propre dossier source et leur propre dossier sur le disque. Ils sont classés à part et
**masqués par défaut** : tant que l'affichage n'est pas activé, ils n'apparaissent dans aucune liste, et le serveur
refuse de les envoyer, de les restaurer ou de les identifier. Les prochains hébergeurs restent reportés (section 24).

**Classement.** Un film est adulte si l'une de ces deux conditions est vraie :

- il se trouve dans un dossier « Films adultes » (Réglages › Dossiers sources, réglage `source_dirs_adultes`,
  variable `SOURCE_DIRS_ADULTES`) ;
- il a été marqué à la main avec la case « Contenu adulte » de la fenêtre d'identification. La marque est enregistrée
  dans `library-adult.json`, à côté de `settings.json` (même volume), par chemin normalisé. Décocher la case retire
  la marque.

Un film déjà rangé dans un dossier adulte ne peut pas être « démarqué » : la case décochée ne retire que la marque
manuelle.

**Affichage.** Réglages › Comportement, case « Afficher les médias adultes », désactivée par défaut (réglage
`adult_visible`, variable `ADULT_VISIBLE`). Quand elle est désactivée :

- la recherche, les envois 1fichier, les sagas, la bibliothèque des disques et le plan d'envoi n'affichent aucun
  film adulte. Le plan indique dans ses avertissements combien de films ont été ignorés ;
- côté serveur, une copie vers un disque, un archivage 1fichier, une restauration ou une identification portant sur
  un film masqué est refusée (403). L'interface n'est donc pas la seule barrière.

Quand l'affichage est activé, les films adultes portent une étiquette « Adulte » dans la recherche, et un badge
« Adulte » dans la liste des disques.

Ce réglage **cache l'affichage, il ne verrouille pas l'accès** : quiconque ouvre l'interface peut le réactiver. Un code
d'accès serait une évolution séparée.

**Scraping TMDB.** Les films adultes se recherchent comme les autres : identification automatique, enrichissement des
fiches, recherche manuelle, nom des copies. Seul change le paramètre `include_adult` de TMDB, à `true` pour un film
adulte et `false` sinon. Le cache des réponses TMDB distingue les deux cas, pour qu'un résultat adulte ne serve jamais
à un film ordinaire. Dans la fenêtre d'identification, la case « Contenu adulte » active aussi la recherche pour
adultes.

**Copies et disque.** Une copie adulte vers un disque va dans le sous-dossier `dest_subdir_adultes` (défaut
`MEDIA/ADULTE` ; vide = avec les autres films). La restauration depuis un disque lit ce même dossier. Pour 1fichier,
l'entrée porte le drapeau `adult`, mais le dossier distant reste celui des films (`tmdb-<id>`).

**Réglages à renseigner.**

1. Réglages › Dossiers sources › « Films adultes » : un chemin par ligne, puis « Vérifier les chemins ». Le dossier doit
   être visible dans le conteneur : sous `HOST_MEDIA_ROOT`, ou ajouté comme volume dans `docker-compose.yml`, comme les
   autres sources.
2. Réglages › Destination › « Sous-dossier films adultes sur disque » : `MEDIA/ADULTE` par défaut.
3. Réglages › Comportement : laisser « Afficher les médias adultes » décoché tant que tu ne veux pas les voir.

Les variables `SOURCE_DIRS_ADULTES`, `DEST_SUBDIR_ADULTES` et `ADULT_VISIBLE` sont documentées dans `.env.example`. Comme
les autres, elles ne sont lues qu'au premier démarrage (voir section 9) : sur une installation existante, c'est
Réglages qui fait foi.

**Limites connues.**

- L'affichage est caché, pas verrouillé (voir plus haut).
- Le journal de l'application (`log_action`) et l'historique des tâches ne sont pas filtrés : le nom d'un film adulte
  peut y apparaître.
- Les entrées 1fichier créées avant la v34 n'ont pas de drapeau : elles restent visibles, même si le film est adulte.
  Il n'existe pas encore de moyen de les marquer.
- Certains compteurs (état de l'index 1fichier, sagas partiellement affichées) ne sont pas tous filtrés.
- Le dossier distant 1fichier est commun avec les films ordinaires : rien ne sépare les contenus distants. Un film
  adulte copié garde son identifiant TMDB comme clé 1fichier.
- Le scraping a été testé en remplaçant l'appel HTTP vers TMDB, pas contre l'API réelle, qui était injoignable depuis
  l'environnement de test. Le paramètre `include_adult` est donc vérifié dans la requête envoyée, pas dans la réponse
  de TMDB.
- Avant d'envoyer des contenus adultes vers 1fichier, vérifie ses conditions d'utilisation : un compte suspendu rend
  l'archive distante inaccessible. C'est un risque réel pour tes sauvegardes, indépendant du code.

**Routes.** `GET` et `PUT /api/settings` (champs `source_dirs_adultes`, `dest_subdir_adultes`, `adult_visible`) ;
`POST /api/search` (chaque film porte `adult`, et les films masqués sont retirés selon le réglage) ;
`POST /api/library/identify` (champ `adult` dans le corps, pour les films) ; `GET /api/tmdb/search?kind=films&q=…&adult=true`.

**Vérifications.** `tests/test_adult_media.py` (25 tests) : réglages, classement par dossier et par marque, masquage sur
chaque liste, refus 403 des routes d'écriture, scan sans doublon entre dossiers films et adultes, `include_adult` selon
le cas, cache TMDB séparé, enrichissement élément par élément, destinations disque et restauration. Suite complète :
124 tests, tous verts. Un test de navigateur (Chromium, 20 contrôles) a validé le parcours : dossier renseigné, carte
absente puis présente avec l'étiquette « Adulte », case « Contenu adulte » cochée à l'identification, retrait à la
désactivation, aucune erreur JavaScript.

**Mise en service.** Les variables de `.env` ne sont lues qu'au premier démarrage : sur une installation existante,
renseigne les dossiers dans Réglages. Reconstruire l'image (`docker compose up -d --build`) est nécessaire pour
récupérer le code de la v34.

## 26. Distribution des films : acteurs et photos (version 35)

La fiche d'un film affiche désormais son casting : les 12 premiers rôles TMDB, avec le nom, le rôle et une petite photo
(56 px). Cela vaut pour les films du serveur et pour les films de la bibliothèque 1fichier, adultes compris (ils suivent
le réglage d'affichage de la section 25).

**Comment ça marche.**

- Le casting se charge à l'ouverture de la fiche, pas pendant la recherche : la recherche ne fait aucun appel de plus.
  Il reste ensuite en mémoire, côté serveur (500 films au plus, puis le cache repart de zéro) et côté navigateur pour la
  session. Une fiche rouverte n'appelle plus TMDB.
- Route `GET /api/tmdb/cast?tmdb_id=…` : interroge `/movie/{id}/credits` dans la langue des réglages et renvoie
  `{"cast": [{name, character, photo_url}]}`.
- Les photos viennent directement de image.tmdb.org, en taille w185, comme les jaquettes. Le navigateur n'envoie pas
  l'adresse de l'application à TMDB (`referrerpolicy="no-referrer"`).
- Acteur sans photo sur TMDB : ses initiales s'affichent à la place. Photo introuvable : elle est retirée, les initiales
  restent visibles.
- Sans identifiant TMDB, sans clé TMDB ou en cas de panne, la section reste masquée. Un échec n'est pas mis en cache :
  la fiche réessaie à l'ouverture suivante.
- Sur téléphone, la rangée de photos défile dans sa boîte ; la page ne défile pas horizontalement.

**Ce qui ne change pas.**

- Les séries n'ont pas de distribution : TMDB la fournit par un autre appel, qui n'est pas fait.
- La grille et les listes n'affichent pas le casting : un appel par carte serait trop lourd. La fiche est le seul endroit.
- L'onglet Disques n'ouvre pas de fiche de film : rien à afficher là.

**Masquage des films adultes.** Depuis la version 40 (section 31), la route refuse la distribution d'un film pour adultes sans session déverrouillée : elle répond une distribution vide, comme pour un identifiant inconnu.

**Limites connues.**

- Beaucoup d'acteurs n'ont pas de photo sur TMDB, surtout dans les films anciens ou peu diffusés. Les initiales servent
  alors de repli.
- La distribution est celle de TMDB : elle peut être incomplète, et les noms de rôles restent dans la langue de TMDB quand
  aucune traduction n'existe.
- L'environnement de développement n'accède pas à TMDB. Le rendu a été vérifié avec une distribution et des photos
  fournies par le banc de test (réponses interceptées), pas avec les vraies photos de TMDB. Le premier affichage sur ton
  serveur, avec ta clé, reste à regarder.

**Mise en service.** Rien de plus à configurer : la clé TMDB existante suffit. Reconstruire l'image
(`docker compose up -d --build`) pour récupérer le code de la v35.

**Vérifications.** `tests/test_cast.py` (9 tests) : ordre de facturation, noms vides ignorés, photos en w185, limite de
12 acteurs, langue, cache (une seule requête par film), échec non mis en cache, TMDB désactivé, route (identifiant
invalide refusé, liste vide sans TMDB). Suite complète : 133 tests, tous verts. Banc navigateur (Chromium, 18 contrôles) :
section affichée, identifiant transmis, photos chargées, initiales de repli, photo cassée retirée, pas de nouvel appel à la
réouverture, section masquée sans distribution, défilement interne sur téléphone (390 px), fiche 1fichier, aucune erreur
JavaScript.

## 27. Code d'accès aux médias adultes (version 36)

Le code remplace la case « Afficher les médias adultes » de la section 25. Le réglage `adult_visible` et la variable
`ADULT_VISIBLE` n'existent plus. Les médias adultes ne s'affichent que dans un navigateur qui a saisi le code, et c'est
le serveur qui le vérifie : un réglage visible dans l'interface ne peut plus ouvrir l'accès.

**Créer le code.** Réglages › Comportement › « Créer le code » (4 à 64 caractères, saisis deux fois). Le code est
enregistré dans `adult-code.json`, à côté de `settings.json` (même volume), sous forme d'empreinte PBKDF2-SHA256 avec
sel aléatoire. Il n'est jamais renvoyé par l'API ni affiché, et le fichier n'est lisible que par son propriétaire
(mode 0600).

**Déverrouiller.** Toucher cinq fois, en deux secondes, le titre « Archivage » de l'en-tête ouvre la saisie du code.
Rien n'indique ce geste à l'écran. Sans code défini, le geste n'affiche qu'un message.

**Session.** Un code juste crée une session : un jeton aléatoire, envoyé dans le cookie `archivage_adult`. Le cookie est
HttpOnly (hors de portée du JavaScript), SameSite=Strict et sans date d'expiration : le navigateur l'oublie à sa
fermeture. Le serveur refuse en plus un jeton âgé de plus de 24 h, et un redémarrage efface toutes les sessions. Tant
que la session est ouverte, un cadenas ouvert, teinté d'orange, apparaît dans l'en-tête ; un clic verrouille. Entre 1100 et 1279 px, les libellés « Journal » et « Réglages » disparaissent pour laisser la place à la recherche.

**Changer le code.** Réglages › Comportement › « Changer le code » exige l'ancien code. Le navigateur qui le change reste
déverrouillé. Les autres sessions sont fermées : elles devront saisir le nouveau code.

**Essais.** Cinq échecs suspendent les essais pendant cinq minutes. Les échecs du déverrouillage et ceux du changement de
code sont comptés ensemble, pour qu'on ne puisse pas deviner l'ancien code en boucle. Un bon code remet le compteur à
zéro. Après la pause, un seul nouvel échec suspend à nouveau. Le compteur est en mémoire : un redémarrage le remet à zéro.

**Ce qui est protégé.** Quand la session est verrouillée, le serveur masque ou refuse tout élément adulte, en lecture
comme en action :

- la recherche, les sagas, le plan d'envoi, la bibliothèque 1fichier et la bibliothèque des disques ;
- la fiche d'un élément, le lien et le mot de passe d'une archive, la vérification et la récupération ;
- les tâches (liste, détail, progression en direct), la file d'attente des transferts et le journal ;
- les copies, envois, restaurations, renommages, identifications et suppressions ;
- l'export chiffré de l'index 1fichier, qui contient le mot de passe de chaque archive ;
- la recherche TMDB pour adultes (`adult=true`).

Un élément adulte demandé sans session répond 404 « Élément introuvable », comme s'il n'existait pas. Une entrée 1fichier
ancienne, sans drapeau, est classée adulte d'après son chemin d'origine quand celui-ci est connu.

**Code oublié.** Le code ne peut pas être relu. Sur le serveur, supprime `adult-code.json` (dans le dossier de
`settings.json`, donc dans le volume `/data`) : sans ce fichier, aucun code n'est défini et les sessions ouvertes ne
valent plus rien. Crée ensuite un nouveau code dans Réglages. Cette procédure passe par le serveur, pas par l'interface.

**Limites connues.**

- La protection porte sur les médias adultes, pas sur l'application. Sans session, quelqu'un qui atteint l'interface voit
  toujours les films ordinaires, et peut créer le code s'il n'en existe pas encore. Pour protéger l'application entière, il
  faut une authentification devant elle, par exemple un reverse-proxy.
- En HTTP sur le réseau local, le cookie n'est pas marqué `Secure` : le jeton circule en clair, et quelqu'un qui écoute le
  réseau peut le relever. Une protection réelle passe par HTTPS.
- Le journal est filtré au mieux : une ligne qui cite un film adulte connu est masquée, mais une ligne dont le nom n'est
  plus connu du serveur ne l'est pas. La création et le changement du code y sont aussi journalisés.
- `/api/tmdb/cast` (distribution, section 26) reste public. Il ne renvoie que des données TMDB, mais il n'est pas filtré.

**Routes.** `GET /api/adult/state` renvoie `{code_set, unlocked, retry_in}`. `POST /api/adult/unlock` (`{code}`),
`POST /api/adult/lock` et `POST /api/adult/code` (`{current, new}` ; `current` est exigé dès qu'un code existe).
`PUT /api/settings` ignore désormais le champ `adult_visible`, et ne l'enregistre plus.

**Mise en service.** Rien à configurer : le code se crée au premier usage, dans Réglages. Reconstruire l'image
(`docker compose up -d --build`) pour récupérer le code de la v36. Un `adult_visible` resté dans `settings.json` est
ignoré, et les médias restent masqués jusqu'à la saisie du code.

**Vérifications.** `tests/test_adult_auth.py` (21 tests) : empreinte et permissions du fichier, longueur du code, essais
et pauses, changement de code qui exige l'ancien et compte dans les échecs, expiration et révocation des sessions, routes,
attributs du cookie, WebSocket refusé sans session. Suite complète : 163 tests, tous verts ; pyflakes propre sur les
modules modifiés. Banc navigateur (Chromium, 29 contrôles) : pastille absente par défaut, geste sans code, création du
code, erreurs de saisie, film adulte visible puis masqué au verrouillage, cookie HttpOnly invisible au JavaScript, session
conservée au rechargement, mauvais puis bon code, changement de code, téléphone sans défilement horizontal, aucune erreur
JavaScript.

## 28. Catégorie Adultes, déplacement, renommage et dossier 1fichier ADULTES (version 37)

Les médias adultes ont désormais leur propre catégorie. Elle reste cachée derrière le code de la section 27 : sans
session déverrouillée, l'onglet « Adultes » et le sous-filtre disque n'apparaissent pas, et les routes répondent 404.
Si la session se verrouille pendant qu'on regarde la catégorie, la vue revient aux films.

**Catégorie Adultes.** Dans les médias du serveur, l'onglet « Adultes » ne montre que les films adultes ; dans l'onglet
Disques, le sous-filtre « Adultes » ne montre que les films adultes du disque. La recherche envoie
`media_type: "adultes"` : sans session, elle renvoie une liste vide. Dans l'onglet Films, un film adulte reste visible
avec l'étiquette « Adulte », comme en v36.

**Ranger un film dans Adultes, sur le serveur.** La fiche d'un film propose « Déplacer dans Adultes » (ou « Sortir de la
catégorie Adultes »). Le fichier ne bouge pas : la catégorie est une marque gardée par l'application (`library-adult.json`),
et Plex n'est pas touché. Route : `POST /api/library/category` avec `{paths, adult}`. Un film qui se trouve déjà dans un
dossier adulte source ne peut pas sortir de la catégorie : 409.

**Ranger un film dans Adultes, sur un disque.** Sélectionne des films connectés, puis « Déplacer vers Adultes » (ou
« Sortir de la catégorie Adultes »). Route : `POST /api/disk-library/category` avec `{paths, adult}`.

- Le film est déplacé dans le sous-dossier adulte du disque, `dest_subdir_adultes` (par défaut `MEDIA/ADULTE`), créé s'il
  manque. Le déplacement se fait sur le même disque (`os.rename`) : rien n'est copié.
- Les éléments déjà au bon endroit sont ignorés et comptés dans `unchanged`.
- Si un élément porte déjà le même nom dans le dossier cible, la requête répond 409 et rien n'est déplacé.
- Si le disque est monté en lecture seule, ou si un élément n'est plus connecté, la requête échoue sans rien déplacer.
- Le mode simulation (section 7) consigne `SIMULE` dans le journal et ne déplace rien.
- Seuls des films se rangent dans la catégorie ; une série ou un ebook dans la sélection donne 400.

**Dossier ADULTES sur 1fichier.** Un film adulte envoyé vers 1fichier va dans `ADULTES/tmdb-<id>/`. Le dossier racine
`ADULTES` est créé si besoin, à la racine du compte, avant le sous-dossier. Les films ordinaires restent à la racine
comme avant. La clé de cache du dossier est distincte (`adult:<clé>`), donc un film adulte et un film ordinaire de même
identifiant TMDB ne partagent pas de dossier. Les entrées envoyées avant la v37 gardent leur dossier racine `tmdb-<id>`.

**Renommer un film.** La fiche d'un film du serveur propose « Identifier / renommer ». La fenêtre affiche une case
« Renommer aussi le fichier sur le serveur », décochée par défaut :

- décochée : seul le nom affiché et celui des copies change, comme avant ; le fichier garde son nom ;
- cochée : le fichier est renommé sur le serveur. Un seul film à la fois (400 sinon). Si le nom cible existe déjà, la
  requête répond 409 et rien n'est renommé. L'identification et la marque adulte suivent le nouveau nom ; en simulation,
  le renommage est consigné sans être fait.

Route : `POST /api/library/identify` avec `rename_file: true`. La réponse contient `renamed_path` ; l'interface rappelle
alors qu'il faut actualiser Plex (Réglages › Plex), qui rattache un film à son chemin.

**Recherche d'identification plus fluide.**

- La recherche TMDB part pendant la frappe, après une pause de 300 ms ; la modification de l'année déclenche la même
  recherche. Cocher « Contenu adulte » relance la recherche tout de suite.
- Une réponse plus ancienne que la saisie courante est ignorée ; les 60 dernières recherches de la fenêtre sont gardées
  dans le navigateur, donc revenir sur un titre ne refait pas l'appel.
- Côté serveur, la recherche TMDB est gardée 10 minutes (300 entrées au plus). Une erreur réseau n'est jamais gardée.
- Entrée prend le premier résultat. Si la recherche n'est pas encore revenue, Entrée est mémorisée et le premier résultat
  est repris dès son arrivée ; toute frappe supplémentaire annule cette demande. ↓ parcourt la liste.
- Le titre deviné d'après le nom du fichier se pré-remplit seulement si rien n'a été tapé pendant son chargement : la
  saisie n'est plus écrasée.

**Corrections trouvées au banc navigateur.**

- Dans la liste des résultats, les lettres de remplacement des affiches s'étendaient à tout l'écran et interceptaient les
  clics : « Annuler » ne répondait plus. Le conteneur de vignette est désormais positionné.
- Une fenêtre plus haute que l'écran (téléphone, liste de résultats) était coupée en haut, sans défilement. Les fenêtres
  modales défilent désormais, et restent centrées quand elles tiennent à l'écran.

**Verrouillage.** Sans session : la recherche « Adultes » est vide ; les routes de catégorie, de renommage d'un film
adulte et de recherche d'un titre adulte répondent 404 ou 403 comme en v36 ; l'onglet et le sous-filtre restent cachés.

**Limites connues.**

- 1fichier n'a pas été testé contre le vrai service : la clé API n'était pas disponible et le proxy sortant refuse ce
  domaine (403). Le dossier `ADULTES` et le dépôt dans `tmdb-<id>` sont couverts par des tests avec un client factice.
- TMDB : la recherche et son cache sont testés sans clé, avec des réponses simulées. Les résultats réels n'ont pas été
  vérifiés.
- Renommage sur le serveur : Plex doit être actualisé. Les entrées Plex déjà rattachées à l'ancien chemin ne suivent pas
  toutes seules.
- Dans la vue Disques, le sous-filtre « Adultes » est un filtre sur les films : les fichiers restent dans leurs dossiers
  tant qu'on ne les déplace pas.
- Le déplacement sur disque ne va que d'un dossier à l'autre du même disque ; il ne copie rien vers un autre disque.
- L'onglet 1fichier n'a pas de sous-filtre « Adultes » séparé : les entrées adultes portent l'étiquette quand la session
  est ouverte.
- La case « Renommer aussi le fichier » n'existe que pour un film du serveur. Les identifications d'un film sur disque
  gardent leur comportement d'avant.

**Mise en service.** Rien à configurer. Reconstruire l'image (`docker compose up -d --build`). Le dossier `MEDIA/ADULTE`
sur chaque disque et les marques adultes se créent au premier usage.

**Vérifications.** `tests/test_adult_v37.py` (22 tests) : recherche « Adultes » selon la session, routes de catégorie
serveur et disque (déplacement aller-retour, dossier créé, collision, simulation, verrouillage), renommage avec suivi de
l'identité et de la marque, cache TMDB, dossier `ADULTES` sur 1fichier sans nouvel appel. Suite complète : 185 tests,
tous verts ; pyflakes propre sur les modules modifiés. Banc navigateur (Chromium, TMDB simulé côté serveur) : 25
contrôles, passés deux fois, couvrant l'onglet Adultes, la bascule serveur et disque, la case de renommage, la recherche
pendant la frappe, Entrée, le verrouillage et le téléphone. Non-régression sur le banc v36 : 29 contrôles réussis. Deux tests de la file d'attente 1fichier (archivage, contrôle des liens) épinglent désormais une plage de transfert ouverte : ils ne dépendent plus de l'heure. Avant, ils bloquaient ou échouaient hors des plages par défaut (Jour 09:00–22:00, Nuit 01:00–07:00).

## 29. Fiche technique, studios et filmographie des acteurs (version 38)

La fiche d'un film affiche désormais le réalisateur, les scénaristes et les studios de production, en plus de la
distribution. Un acteur ou un réalisateur s'ouvre dans sa propre fiche, avec sa photo, sa biographie et sa filmographie.
Chaque film de la filmographie indique s'il est déjà dans ta bibliothèque, et où : serveur, disque ou 1fichier.

**Fiche d'un film.**

- « Réalisation » et « Scénario » : les noms, cliquables pour ouvrir la fiche de la personne.
- « Studios » : le logo du studio quand TMDB en fournit un. Le nom reste affiché dans tous les cas, donc il reste lisible
  si le logo est absent ou ne charge pas.
- La distribution, dans l'ordre de facturation, avec des noms cliquables.

**Comment ça marche.**

- Route `GET /api/tmdb/credits?tmdb_id=…` : un seul appel TMDB par film (`/movie/{id}` avec `append_to_response=credits`)
  donne la distribution, les réalisateurs, les scénaristes et les studios. Le cache serveur garde 300 entrées au plus,
  puis repart de zéro. Le navigateur ne redemande pas un film déjà chargé pendant la session. Un échec n'est jamais mis
  en cache.
- Route `GET /api/tmdb/person?person_id=…` : la fiche de la personne et sa filmographie (`/person/{id}` avec
  `append_to_response=movie_credits`). Si la biographie n'existe pas dans la langue des réglages, elle est prise en
  anglais. Cache de 300 entrées au plus, comme pour les crédits.
- La filmographie est regroupée par rôle : Jeu, Réalisation, Scénario, puis Autres fonctions (production, montage,
  musique, décors…). Les intitulés sont en français. Les films sont classés du plus récent au plus ancien.
- Une personne qui n'a qu'un rôle voit un simple titre de section. Avec plusieurs rôles, ce sont des onglets, avec le
  nombre de films de chaque rôle.
- La biographie longue est repliée ; « Lire la suite » la déplie.
- Un film présent dans la bibliothèque porte l'étiquette de son lieu (Serveur, Disque, 1fichier). Les autres sont
  marqués « Absent ». Un clic sur un film présent ouvre la bibliothèque de ce lieu et cherche le fichier.
- « ← Retour » revient à la fiche du film d'où l'on venait. Échap ferme d'abord la fiche de la personne, puis le reste.

**Présence dans la bibliothèque.** Les films possédés sont cherchés par identifiant TMDB dans les trois lieux
(`_owned_by_tmdb`). Le terme de recherche est le nom du fichier sur le serveur et sur les disques, parce que la recherche
de la bibliothèque porte sur les noms de fichiers. Pour 1fichier, c'est le titre, comme dans la bibliothèque 1fichier.

**Masquage des films adultes.** Sans session déverrouillée, `/api/tmdb/person` retire les films adultes au moment de
la lecture. Ils n'apparaissent pas, et ne comptent ni dans le décompte des films ni dans les présences. La route `/api/tmdb/credits` fait de même depuis la version 40 (section 31) : sans session, un film pour adultes donne une fiche vide, comme un identifiant inconnu.

**Erreurs.** TMDB désactivé : 409, et la fiche de la personne l'indique. TMDB en panne : 502. Personne absente de TMDB :
404, affichée comme « Fiche indisponible », sans page vide.

**Limites connues.**

- TMDB n'a pas été appelé pour de vrai : pas de clé ni de réseau dans l'environnement de développement. Les réponses ont
  été simulées à la frontière HTTP. Les vraies données, les vraies photos et les vrais logos restent à regarder sur ton
  serveur, avec ta clé.
- La filmographie ne liste que les films. Les séries où la personne joue ou réalise n'y figurent pas : TMDB les donne par
  un autre appel, qui n'est pas fait.
- La présence est celle que l'application connaît : le scan du serveur, l'index des disques et l'index 1fichier. Un film
  rangé ailleurs ne sera pas repéré.
- Les caches sont en mémoire : un redémarrage les vide.
- Les photos et les logos viennent de image.tmdb.org, comme les jaquettes. Le navigateur n'envoie pas l'adresse de
  l'application à TMDB.

**Mise en service.** Rien à configurer : la clé TMDB existante suffit. Reconstruire l'image
(`docker compose up -d --build`) pour récupérer le code de la v38.

**Vérifications.** `tests/test_person_v38.py` (16 tests) : lecture des crédits (réalisateurs, scénaristes, studios,
distribution limitée à 12), chemin et paramètres de la requête, cache, échec non mis en cache, TMDB désactivé ;
filmographie par rôle, intitulés français, tri, filtrage des adultes selon la session, repli de biographie, 404, cache
de la personne ; routes (verrouillé : adulte absent et sans présence ; déverrouillé : adulte possédé visible ; 404, 409,
502). Suite complète : 201 tests, tous verts ; pyflakes propre sur les modules modifiés. Les avertissements existants dans
`app/sagas.py` et `tests/test_scheduler.py` ne viennent pas de cette version.

Banc navigateur (Chromium, TMDB simulé) : 26 contrôles. Ils couvrent la fiche technique, les acteurs cliquables, le
repli du logo, la filmographie par rôle, la biographie repliée, le retour, Échap, un film possédé ouvert dans sa
bibliothèque, la fiche introuvable, la session fermée puis ouverte, le téléphone (390 px) et l'absence d'erreur
JavaScript. Non-régression : banc v36 29/29, banc v37 25/25. Le banc v37 avait d'abord échoué parce que le serveur de test
ne simulait pas la recherche TMDB ; c'était le banc, pas l'application.

## 30. Performances : grandes listes et contrôle des médias adultes (version 39)

Aucune fonctionnalité nouvelle. Trois séries de corrections, sur les chemins qui coûtaient le plus quand la bibliothèque
grossit. Le comportement et les réponses de l'API ne changent pas.

**Rendu des listes (navigateur).**

- Les dates, les tailles et les tris par titre créaient un formateur Intl à chaque appel (`toLocaleString` avec options).
  Chaque carte en faisait trois. Les formateurs et les comparateurs sont maintenant créés une seule fois au chargement
  (`DF_SHORT`, `NF_SIZE_0`, `NF_SIZE_1`, `COLLATOR_TITLE`…).
- Au-delà de 120 cartes (`LARGE_GRID`), la grille reçoit la classe `is-large`. Le navigateur ne met alors en page que les
  cartes à l'écran (`content-visibility: auto`). La carte survolée reste entièrement rendue, pour que son ombre ne soit
  pas coupée. En dessous de 120 cartes, rien ne change.
- Mesures (Chromium, 3 000 cartes synthétiques) : rendu d'environ 700 ms à environ 150 ms ; mise en page d'environ 600 ms
  à environ 30 ms. Pour 6 000 cartes : rendu d'environ 1,3 s à environ 0,3 s, mise en page d'environ 60 ms.

**Contrôle des médias adultes (serveur).**

- Sans session déverrouillée, chaque entrée qui a un chemin d'origine relisait le fichier des marques adultes
  (`library-adult.json`). Pour 3 000 entrées 1fichier et 300 marques, la liste mettait environ 170 ms. La requête se
  répète à chaque ouverture de l'onglet et, pendant un envoi, toutes les 2 à 3 s. Les marques sont maintenant lues une
  fois par requête : environ 6 ms.
- Même correction pour la restauration (une lecture pour toute la sélection), l'export de l'index et les fragments du
  journal qui désignent un film adulte.

**Ce qui ne change pas.** Les règles d'affichage des films adultes, les codes HTTP et le contenu des réponses sont les
mêmes. Dans une liste, une session déverrouillée ne lit pas les marques.

**Limites connues.**

- Le rendu reste proportionnel au nombre de cartes. Au-delà de quelques milliers, la pagination ou une liste virtualisée
  serait l'étape suivante. Elle n'est pas faite, car elle change la navigation.
- La vue 1fichier se redessine à chaque interrogation pendant un envoi, même si rien n'a changé. Coût mesuré : environ 40
  à 80 ms pour 3 000 entrées.
- Le serveur lit encore l'index 1fichier deux fois à chaque interrogation (une fois pour la file, une fois pour les
  fiches TMDB en attente). Quelques millisecondes pour 3 000 entrées.
- Les mesures viennent d'un navigateur sans interface, avec des données synthétiques. Ton navigateur et ta bibliothèque
  donneront d'autres valeurs.

**Mise en service.** Rien à configurer. Reconstruire l'image (`docker compose up -d --build`) pour récupérer le code de la
v39. Le navigateur recharge les fichiers CSS et JavaScript grâce à leur nouvelle version (`?v=20261012-v38b`).

**Vérifications.** `tests/test_perf_v39.py` (4 tests) : une seule lecture des marques pour une liste, aucune lecture
session ouverte, film marqué masqué en session verrouillée, marques passées en argument. Deux de ces tests (lecture répétée, argument refusé)
échouent sur la version précédente. Suite complète : 205 tests, tous verts ; pyflakes propre sur les modules modifiés.
Banc navigateur : v38 26/26, v37 25/25, v36 29/29. Les scripts de mesure ne sont pas dans le zip.

## 31. Films adultes sans fuite, index 1fichier en cache, banc navigateur unique (version 40)

Trois séries de corrections. Rien de visible pour qui n'est pas en session adulte, sauf la fiche technique des films
adultes, désormais masquée comme le reste.

**Fiche technique des films adultes.** Les routes `/api/tmdb/credits` et `/api/tmdb/cast` ne vérifiaient pas la session.
Un identifiant TMDB d'un film pour adultes donnait sa distribution, ses réalisateurs et ses studios, sans code. Corrigé :

- sans session déverrouillée, un film pour adultes répond une fiche vide (`credits`) ou une distribution vide (`cast`),
  exactement comme un identifiant inconnu. La réponse ne dit ni que le film existe, ni ce qu'il est ;
- avec session, rien ne change ;
- le drapeau `adult` de TMDB est gardé dans le cache du serveur et n'est jamais renvoyé au navigateur. La route en fait
  une copie : le cache n'est jamais modifié, donc une session ouverte puis fermée ne laisse aucune trace ;
- pour `cast`, une session fermée consulte le drapeau par la fiche technique, déjà en cache. Les films non adultes gardent
  le même chemin qu'avant.

**Index 1fichier en cache.** L'index était relu et parsé à chaque interrogation. `/api/1fichier` le lisait deux fois, et
la page l'interroge toutes les 2 à 3 secondes pendant un envoi.

- Le cache reste valable tant que le fichier garde le même inode, la même date de modification et la même taille, et que
  l'application n'a rien écrit depuis (un compteur est incrémenté à chaque écriture).
- Le cache ne garde ni mot de passe ni lien. Les opérations qui en ont besoin (contrôle d'un lien, envoi, restauration)
  relisent le fichier complet, comme avant.
- Toute mise à jour relit le fichier, jamais le cache, et le cache n'est jamais modifié après coup.
- Mesure (3 000 entrées) : `list_entries()` passe d'environ 13 ms à environ 4,5 ms par appel. Le gain grandit avec la
  taille de l'index.

**Banc navigateur unique.** `v33ui/banc.py` lance les suites v36, v37 et v38 en une commande, sur un seul serveur de test
(`serve_banc.py`). Il remet l'état initial avant chaque suite, nettoie après, et affiche un bilan. Résultat de cette
version : v36 29/29, v37 25/25, v38 26/26, en environ 35 secondes. Les anciens scripts `run_server_v3x.sh` et
`reset_v3x.sh` ne servent plus. Le banc n'est pas dans le zip.

**Sections 26 et 29 corrigées.** Elles disaient que les routes de distribution et de fiche technique ne vérifiaient pas la
session. C'est corrigé depuis cette version.

**Limites connues.**

- TMDB n'a pas été appelé pour de vrai : le drapeau `adult` est simulé dans les tests. Avec ta clé, vérifie qu'un film
  réellement marqué pour adultes sur TMDB est bien masqué sans session.
- Le cache de l'index est en mémoire : un redémarrage le reconstruit à la première lecture.
- L'application écrit l'index par fichier neuf (`os.replace`), donc chaque écriture change l'inode et le cache la voit.
  Un fichier modifié sur place par un autre processus, avec la même taille et dans la même fraction de seconde, pourrait
  ne pas être vu avant la prochaine écriture de l'application. Cela ne se produit pas en usage normal.
- La liste d'un index de 3 000 entrées reste reconstruite à chaque appel (environ 4,5 ms). Il n'y a pas de cache de la liste
  elle-même, seulement de la lecture.

**Mise en service.** Rien à configurer. Reconstruire l'image (`docker compose up -d --build`) pour récupérer le code de la
v40. Le navigateur n'a pas besoin de recharger les fichiers : l'interface n'a pas changé dans cette version.

**Vérifications.**

- `tests/test_credits_adultes_v39.py` (8 tests) : fiche vide sans session, réponse identique à un identifiant inconnu,
  fiche complète avec session, cache non modifié par la route, distribution masquée, TMDB désactivé. Cinq de ces tests
  échouent sur la version précédente.
- `tests/test_index_cache_v39.py` (8 tests) : une seule lecture pour plusieurs appels, écritures et remplacements visibles
  aussitôt, aucun mot de passe ni lien dans le cache, mise à jour sans modification du cache, index absent ou illisible.
  Trois de ces tests échouent sur la version précédente.
- Suite complète : 221 tests, tous verts ; pyflakes propre sur les modules modifiés.
- Banc navigateur unique : v36 29/29, v37 25/25, v38 26/26.

## 32. Disques au repos, interface en modules (version 41)

**Le calcul des fiches ne tourne plus à vide.** La route `/api/disk-library` lançait le calcul des fiches TMDB puis lisait
aussitôt son drapeau `meta_running`. Le calcul ne démarre qu'après la réponse : le drapeau valait donc toujours `true`. La
page relisait alors la liste toutes les 3 secondes, et chaque relecture relançait un calcul vide. Cette boucle sollicitait
aussi `/api/disks/integrity`.

Correction : `disk_meta.kick()` ne lance le calcul que s'il reste des fiches à faire, avec le même critère que le calcul
lui-même, et renvoie l'état réel. Le drapeau retombe dès que la dernière fiche est écrite ou marquée introuvable.

Mesure, 25 secondes sans action sur l'onglet Disques : 18 requêtes avant la correction (9 `/api/disk-library`, 9
`/api/disks/integrity`), 2 après (une de chaque, au rythme normal de 20 secondes). L'onglet 1fichier reste à 0 requête.

**Interface découpée en modules.** Le fichier `frontend/app.js`, devenu trop long pour être relu, est remplacé par dix
fichiers dans `frontend/js/` : `core.js`, `list.js`, `person.js`, `shell.js`, `onefichier.js`, `disks.js`, `identify.js`,
`jobs.js`, `settings.js`, `app.js`. Ils partagent un même espace global : l'ordre des balises `<script>` dans
`index.html` compte. `core.js` vient en premier et `app.js` (événements et démarrage) en dernier. Chaque fichier passe
`node --check`, et les suites du banc passent sans changement.

**Mise à jour poussée : non réalisée.** Au repos, l'onglet Disques ne fait plus qu'une relecture toutes les 20 secondes, et
l'onglet 1fichier aucune. Une liaison WebSocket ne supprimerait que cette relecture. Son seul gain serait de voir un disque
branché ou débranché tout de suite, au lieu de sous 20 secondes : cela ne justifie pas un canal de plus à maintenir. Les
progressions pendant un envoi ou une copie sont suivies comme avant.

**Pagination ou liste virtuelle : non réalisées.** Cela modifie la navigation dans les grandes listes. C'est à décider avant
de toucher à la navigation.

**Limites connues.**

- La relecture des disques toutes les 20 secondes reconstruit la liste complète à chaque appel. Son coût avec des milliers
  d'éléments sur un même disque n'a pas été mesuré. Le calcul des fiches, lui, reste en arrière-plan.
- La correction ne change rien aux fiches déjà introuvables : elles sont retentées après 24 heures, comme avant.

**Mise en service.** Reconstruire l'image (`docker compose up -d --build`), puis recharger la page du navigateur : les
fichiers de l'interface portent une nouvelle version (`v41`).

**Vérifications.**

- `tests/test_disk_meta_v41.py` (4 tests) : aucun calcul lancé quand tout est à jour ; un élément sans fiche lance le calcul,
  puis le drapeau retombe ; la route ne signale aucun calcul quand tout est à jour ; TMDB désactivé. Le test de la route
  échoue sur la version précédente (`True is False`).
- Suite complète : 225 tests, tous verts ; pyflakes propre sur les modules modifiés.
- `node --check` sur les dix fichiers de `frontend/js/`.
- Banc navigateur unique : v36 29/29, v37 25/25, v38 26/26.
- Mesure d'inactivité décrite plus haut (18 puis 2 requêtes par 25 secondes).
- Les tests n'écrivent plus dans `/data/archivage.log`. Un fixture automatique (`tests/conftest.py`) envoie le journal
  dans un dossier temporaire propre à chaque test. Avant, chaque lancement de la suite ajoutait des lignes au journal
  de l'environnement. Le code de l'application n'a pas changé.

**Vérification avec une vraie clé TMDB : à faire chez toi.** L'accès à `api.themoviedb.org` est refusé par la politique
de sortie de l'environnement où le code a été préparé (réponse 403 du proxy). La vérification ne peut donc pas être
faite ici. Elle se lance avec `backend/verif_tmdb_adultes.py` :

    TMDB_API_KEY=ta_clé ADULT_TMDB_ID=identifiant_tmdb python3 verif_tmdb_adultes.py

`ADULT_TMDB_ID` est l'identifiant TMDB d'un film marqué adulte sur TMDB. `NORMAL_TMDB_ID` (603 par défaut) sert de témoin.
Le script n'affiche aucun titre, seulement des résultats, et n'écrit que dans un dossier temporaire. Il s'arrête en échec
si TMDB ne répond pas ou ne reconnaît pas le film adulte, pour qu'une fiche vide ne passe jamais pour un masquage réussi.
Avec des réponses TMDB simulées, il passe 10 contrôles sur 10 ; avec TMDB injoignable, il s'arrête au premier contrôle.

**Écart constaté, non corrigé.** La bibliothèque ne masque pas un élément d'après le seul drapeau TMDB. Elle se fie au dossier
adulte, à l'origine du fichier ou à la marque « Contenu adulte » posée à la main. Le drapeau TMDB masque seulement la fiche
technique et la distribution d'un film, ainsi que la filmographie d'un acteur. La recherche TMDB n'inclut pas les titres
adultes pour un film hors dossier adulte, sans marque. Un film marqué adulte sur TMDB mais rangé dans un dossier normal
reste donc visible dans la bibliothèque. Corriger cela change le comportement de la bibliothèque : c'est à valider.

## 33. Audit complet : sécurité, justesse et lenteurs (version 42)

Audit en trois volets (sécurité, justesse des résultats, performance) suivi de corrections. Chaque constat qui a
été corrigé a un test de non-régression dans `backend/tests/test_audit_v42.py` (64 tests). Une partie de l'interface
a été vérifiée dans un navigateur (banc unique, voir plus bas).

### Sécurité

- **Secrets.** `GET /api/settings` ne renvoie plus les secrets en clair : la clé 1fichier, la clé TMDB et le jeton Plex
  ne montrent que leurs 4 derniers caractères, et l'URL de notification que ses 6 derniers (le reste est remplacé par
  `••••••••`). Un envoi qui renvoie une valeur masquée ne réécrit pas le secret existant. Les messages d'erreur (Plex,
  TMDB) ne contiennent plus aucun secret.
- **Affiches Plex.** Le proxy d'affiches refuse un chemin qui changerait d'hôte (400). Avant, un chemin pouvait faire
  interroger un hôte arbitraire.
- **CORS retiré.** L'interface est servie par le même serveur : aucun site tiers n'a besoin d'appeler l'API.
- **Liens symboliques.** Un dossier intermédiaire qui est un lien symbolique fait refuser l'élément (il sortirait de la
  source). Un lien de fichier se supprime sans toucher à sa cible.
- **Garde à l'exécution.** Une suppression ne s'exécute que pour un élément strictement à l'intérieur d'une source ou
  d'un élément d'un disque connecté. La création de la tâche le vérifiait déjà ; la garde couvre aussi une tâche
  enregistrée avec un autre chemin. La suppression de la source après un déplacement est gardée de la même façon.
- **Réglages.** Un sous-dossier contenant `..` ou `.` est refusé. Un débit NaN ou infini est refusé (il faisait relancer
  rsync en boucle). Une marge de sécurité négative est refusée. Une variable `SAFETY_MARGIN_BYTES` invalide ne bloque plus
  le démarrage : elle revient à 500 Mio.
- **Écriture des réglages.** Écriture atomique (fichier temporaire, `fsync`, remplacement). Si l'écriture échoue, les
  réglages en mémoire restent ceux d'avant. Un `settings.json` illisible force la simulation (`dry_run`) jusqu'à un
  enregistrement depuis l'interface.
- **Code d'accès adulte.** Un fichier de code présent mais illisible ne passe plus pour « aucun code » : la définition
  d'un nouveau code et le déverrouillage répondent 503, et une session déjà ouverte est refusée.
- **Fuites des médias adultes corrigées** (sans session déverrouillée) : file d'attente d'envoi, contrôles 1fichier,
  intégrité des disques (exemples de fichiers), notifications, journal (le nom d'un adulte n'est pas écrit), plan
  de la bibliothèque (il ne compte plus les adultes), étiquette des envois sortants. La connexion WebSocket d'une tâche
  adulte est fermée (code 4404) dès que sa session n'est plus valide, au prochain événement de la tâche.
- **Export de l'index 1fichier.** Sans session, l'export répond toujours la même chose, que l'index contienne ou non une
  archive adulte. **Changement de comportement :** l'export exige désormais le déverrouillage, même pour un index sans
  adulte.
- **Copie d'un film adulte.** Refusée (400) si la destination n'est pas un sous-dossier adulte. Avant, la copie pouvait
  atterrir dans le dossier des films normaux.

### Justesse des résultats

- **Simulation.** Elle n'écrit plus dans l'index 1fichier, ne crée plus de dossier de destination et ne retire plus un
  envoi de l'index.
- **Marque « adulte ».** Une marque posée à la main est prise en compte même si la liste était en cache.
- **Copie interrompue.** Une copie dont le début correspond à la source est reprise à la relance. Un fichier au contenu
  différent n'est jamais repris ni écrasé (`already_exists`).
- **Journal.** Une limite de 0 renvoie une ligne au lieu de tout le journal, et la lecture ne charge plus tout le fichier
  en mémoire.
- **TMDB.** Un échec réseau ou un 5xx n'est plus mis en cache comme « aucun résultat » : la recherche est refaite au
  prochain passage. Les vraies absences restent en cache. Le cache est borné à 5 000 entrées.
- **Liste de recherche.** Un changement des sources pendant une reconstruction n'est plus perdu : la reconstruction
  est relancée aussitôt. Avant, la liste restait fausse jusqu'à 15 minutes.
- **Tests.** Chaque test travaille dans son dossier temporaire, y compris les réglages et les codes adultes. Un test
  écrivait auparavant `/data/adult-code.json`.

### Lenteurs

Mesures faites sur le même banc avant et après (3 000 films, Plex simulé, réseau coupé) :

| Mesure | Avant | Après |
|---|---|---|
| Libellés d'une restauration de 100 clés (`restore_labels`) | 4 826 ms | 42 ms |
| Libellés d'une restauration de 300 clés | 12 532 ms | 39 ms |
| Blocage de la boucle au lancement d'une restauration de 300 clés | 11 696 ms | 31 ms |
| `POST /api/search`, terme « film » (médiane de 5 appels) | 114 ms | 23 ms |
| Blocage de la boucle pendant cette recherche | 118 ms | 19 ms |

Le contenu de la réponse de recherche est le même dans les deux cas. Seul le chemin du dossier de banc diffère : il est
plus long de 4 caractères dans le second banc, soit 12 000 octets sur 3 000 films.

Corrections sans mesure chiffrée (le gain est en nombre de lectures ou d'écritures, vérifié par les tests) :

- **Identifications.** `library-identities.json` n'était pas relu à chaque élément. Un cache est relu seulement quand le
  fichier change (date et taille) et il est invalidé à chaque écriture.
- **Fiches des disques.** Un tour de calcul des fiches écrit `disk-library.json` une seule fois, au lieu d'une fois par
  élément.
- **Accès à l'index 1fichier** depuis les routines asynchrones déportés dans un thread, comme le reste des accès.
- **Rapprochement Plex** des films et séries, et identifications, déportés dans un thread : ils ne bloquent plus la boucle
  pendant une reconstruction de la liste.
- **Réponses JSON** de `/api/1fichier`, `/api/search` et `/api/disk-library` envoyées directement (sans passer par le
  convertisseur de FastAPI, environ 170 ms sur une grande bibliothèque).
- **Tâches terminées.** Seules les 300 plus récentes sont gardées en mémoire ; les tâches en cours ne sont jamais retirées.
- **Référence de tâche.** Le calcul des fiches garde une référence à sa tâche asynchrone.

### Interface

- **Réponses périmées.** Une recherche ou un plan de bibliothèque qui répond après un autre est ignoré (`searchSeq`,
  `planSeq`, `disksSeq`).
- **Doubles envois.** Les boutons d'action, l'enregistrement des réglages, le changement de catégorie et la restauration
  sont bloqués pendant l'envoi (`actionBusy`, `savingSettings`, `categoryBusy`, `restoreBusy`).
- **Changement de session adulte.** Verrouiller ou déverrouiller vide les données adultes déjà affichées, et relance les
  listes (`purgeAdultData`, `refreshAfterAdultChange`).
- **Tâches.** Après une coupure du WebSocket, la tâche se reconnecte avec des délais de 1, 2, 4, 8 puis 10 secondes, et
  relit son état par `/api/jobs/{id}`. Le compteur de tâches en cours est recalculé à partir de l'état réel
  (`recountRunningJobs`).
- **Confirmations.** La touche Échap annule une confirmation (`askConfirm`).
- **Destination.** Le sélecteur de destination est un vrai `<select>`.
- **Cache du navigateur.** Les fichiers de l'interface portent la version `v42`.

**Non vérifié dans un navigateur :** le défilement, le focus au clavier, une vraie reconnexion WebSocket, les sagas,
`setServerCategory` et `restoreEntry`. Ces parties passent la relecture et `node --check`, pas un test de bout en bout.

### Modules modifiés

Backend : `main.py`, `config.py`, `security.py`, `jobs.py`, `onefichier.py`, `adult_auth.py`, `adult_media.py`, `logs.py`,
`plex.py`, `tmdb.py`, `identities.py`, `disk_library.py`, `disk_meta.py`, `scan_cache.py`, `sagas.py`.
Frontend : `index.html` et `js/` (`core`, `list`, `person`, `shell`, `onefichier`, `disks`, `jobs`, `settings`).
Tests : `conftest.py`, `test_adult_media.py` (deux tests changés, voir ci-dessous), `test_scheduler.py` (un test),
`test_audit_v42.py` (nouveau).

Deux tests existants ont changé de sens, volontairement :

- `test_plan_films_retire_les_adultes_masques` : le plan sans session n'affiche plus d'avertissement « adulte(s)
  ignoré(s) », car cet avertissement révélait leur présence.
- `test_export_index_refuse_sans_session_meme_sans_adultes` (ex-`test_export_index_autorise_sans_adultes`) : voir
  « Export de l'index » plus haut.

`test_deplacement_verifie_puis_retire_la_source` déclare désormais son dossier comme source. Un déplacement hors source
n'est pas possible par l'interface ; le test reproduisait une situation qu'aucune tâche ne peut créer.

### Vérifications

- Suite complète : 289 tests, tous verts. `test_audit_v42.py` : 64 tests.
- pyflakes propre sur `app/` et `tests/` (les deux avertissements de la version 41 sont corrigés).
- `node --check` sur les dix fichiers de `frontend/js/`.
- Banc navigateur unique (`v33ui/banc.py`) : v36 29/29, v37 25/25, v38 26/26.
- Aucun serveur de test ne reste lancé, et rien n'est écrit dans `/data`.

### Limites connues, non corrigées

- **Authentification des routes d'écriture.** Les réglages, les copies, les déplacements et les suppressions ne demandent
  aucun identifiant, sauf les médias adultes (code d'accès). Qui peut joindre le serveur peut donc modifier les
  réglages ou supprimer des fichiers. Il faut restreindre l'accès au réseau, ou ajouter un jeton ou une authentification
  devant l'application (reverse proxy). C'est à décider avec toi.
- **Copie identifiée par sa taille.** Une copie de même taille que la source est considérée comme déjà faite
  (`already_copied`), même si son contenu diffère. Un déplacement, lui, est protégé par une vérification SHA-256 avant de
  retirer la source. Vérifier le contenu de chaque copie coûterait une relecture complète des fichiers ; la date de
  modification ne suffit pas, car `cp` sans `-p` la change.
- **Copies héritées.** Une copie adulte faite avant la version 37 et rangée hors du dossier adulte n'est pas reconnue
  comme adulte.
- **Rapprochement Plex.** Le repli par inclusion parcourt tout l'index pour chaque titre sans correspondance exacte. Il
  est maintenant hors de la boucle, mais son algorithme n'a pas changé.
- **Journal.** `log_action` avale les erreurs d'écriture : une ligne peut être perdue sans avertissement.
- **Pagination.** Les grandes listes restent envoyées en entier (comme avant).
- **Vérification TMDB avec une vraie clé.** Toujours à faire chez toi, avec `backend/verif_tmdb_adultes.py` (voir section 32).

**Mise en service.** Reconstruire l'image (`docker compose up -d --build`), puis recharger la page du navigateur. Les
réglages et les codes existants sont lus tels quels ; aucune migration n'est nécessaire. Pense à l'export de l'index :
il demande maintenant le déverrouillage.

## 34. Disques branchés après le démarrage : propagation des montages (version 43)

**Symptôme.** L'onglet Disques affiche des disques « hors ligne » alors qu'ils sont montés sur l'hôte (`ls /media/devmon`
les montre).

**Cause.** L'application ne voit que les montages présents **dans le conteneur**. `/media` et `/mnt` y sont montés en
`:rslave` : un montage fait sur l'hôte n'arrive dans le conteneur que si ce dossier était partagé sur l'hôte **avant** le
démarrage de Docker. Sans cela (cas par défaut d'un hôte), le conteneur ne voit pas les disques montés après son démarrage,
et ne les voit pas non plus après un redémarrage de la machine si l'hôte ne rétablit pas le partage.

**Pourquoi `sudo mount --make-rshared /media` seul ne suffit pas.** Cette commande échoue si `/media` n'est pas un point de
montage, ce qui est le cas le plus courant : `/media` n'est qu'un dossier, les disques sont montés dessous. Les versions
précédentes du README et du `docker-compose.yml` donnaient cette commande telle quelle.

**Correctif durable.** Le service `scripts/archivage-media-shared.service` s'exécute à chaque démarrage, avant Docker. Il
rend `/media` et `/mnt` points de montage (bind sur eux-mêmes), puis les passe en partage récursif. Il est idempotent.

Installation, une fois, sur l'hôte :

    sudo cp scripts/archivage-media-shared.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now archivage-media-shared.service
    docker restart archivage-films

Le `docker restart` est nécessaire une seule fois après l'activation : le conteneur en cours a été créé avec l'ancienne
propagation.

**Vérifier.**

    findmnt -no TARGET,PROPAGATION /media /mnt
    # doit afficher « shared » pour /media et /mnt

    docker exec archivage-films grep devmon /proc/mounts
    # doit lister les disques branchés

Ensuite, débrancher puis rebrancher un disque, ou le remonter, sans toucher au conteneur : il doit apparaître dans
l'onglet Disques (la liste se met à jour toutes les 20 secondes).

**Ce qui a été vérifié.** Dans un espace de montage isolé, sans Docker ni systemd réels :

- avec une propagation privée (cas par défaut), un disque monté après coup est visible de l'hôte mais pas du conteneur
  simulé : c'est le symptôme ci-dessus ;
- avec la commande du service, exécutée deux fois, `/media` et `/mnt` passent en `shared`, et un disque monté après coup
  est visible du conteneur simulé ;
- `systemd-analyze verify` accepte l'unité.

**Non vérifié** : sur ta machine (jaina), avec le vrai Docker et les vrais disques. Le plus sûr est de lancer les
commandes de « Vérifier » ci-dessus après l'installation.

**Limite.** Le service suppose que `/media` et `/mnt` existent et que le conteneur s'appelle `archivage-films`. Si le
nom change, adapter la dernière commande d'installation.


## 35. Vue Disques : scans en arrière-plan, erreurs visibles (version 44)

**Symptôme.** La vue Disques restait sur « Lecture des disques… », sans aucun disque affiché. Après 30 secondes, le
navigateur abandonnait la requête, et le message d'erreur ne s'affichait pas.

**Causes.**

1. La route `/api/disk-library` attendait la fin du scan complet de chaque disque. Le scan parcourt tous les dossiers, et
   calcule la taille de chaque film en lisant tous ses fichiers. Sur un gros disque, un partage réseau ou un disque lent,
   cela prend plusieurs minutes.
2. Pendant ce temps, chaque interrogation (toutes les 20 secondes) relançait un scan du même disque, car le marquage
   « scanné » n'était écrit qu'à la fin.
3. Au premier chargement, un message d'erreur ou de délai dépassé n'était jamais affiché : la sous-ligne restait sur
   « Lecture des disques… ».

Depuis que le conteneur voit les disques montés sous `/media` (section 34), ces trois points se produisent pour les
disques réellement branchés.

**Correctif.**

- Un seul scan par disque à la fois, exécuté dans un thread.
- La réponse attend au plus 4 secondes. Au-delà, elle part avec l'état connu et `scanning: true`. Pendant un scan,
  l'interface relit la liste toutes les 3 secondes, avec le mécanisme déjà utilisé pour les fiches TMDB (`meta_running`).
  Le disque apparaît tout de suite ; ses films s'ajoutent à la fin du scan.
- Les erreurs s'affichent toujours, y compris au premier chargement.

**Mesures.** Scan factice de 45 secondes sur un même disque, sur l'ancienne et la nouvelle version :

| Mesure | Avant (v42) | Après (v44) |
|---|---|---|
| Réponse de `/api/disk-library` au premier appel | 45 s | 4 s |
| Scans lancés sur un même disque pendant 60 s d'interrogation toutes les 20 s | 3 | 1 |
| Vue Disques à froid, 15 secondes après l'ouverture | « Lecture des disques… », aucun disque | disque affiché, « 1 disque(s) connecté(s) » |
| Erreur HTTP 500 au premier chargement | « Lecture des disques… » | « Erreur HTTP 500 » |

**Limite.** Le scan reste complet : sur un très gros disque, les disques s'affichent tout de suite, mais les films et
leurs tailles n'apparaissent qu'à la fin du scan. Le coût du scan lui-même n'a pas été réduit (taille calculée à la
demande, ou mise en cache dossier par dossier). Ce n'est pas fait.

**Mise en service.** Reconstruire l'image (`docker compose up -d --build`), puis recharger la page du navigateur (les
fichiers portent la version `v44`).

**Vérifications.**

- `tests/test_audit_v42.py` : trois tests nouveaux. Un scan lent ne bloque pas la réponse ; deux lectures pendant un scan
  ne lancent pas deux scans ; la route répond pendant un scan avec `scanning: true`.
- Suite complète : 292 tests, tous verts ; pyflakes propre ; `node --check` sur les dix fichiers JS.
- Banc navigateur unique : v36 29/29, v37 25/25, v38 26/26.
- Restriction adulte, sur les films et sur les disques, sans session puis avec session, vérifiée sur l'application
  lancée : les copies adultes n'apparaissent qu'une fois déverrouillé, et le déverrouillage (cinq touches rapides sur le
  titre « Archivage ») fait apparaître l'onglet Adultes.
