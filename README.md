# Pronos Hockey

Application familiale de pronostics sur le hockey belge (championnat hockey.be, EHL, FIH Pro League, Hoofdklasse).
L'app est une page web publiée avec GitHub Pages : https://pierresmons.github.io/pronos-hockey/championnat.html

Ce repo est **public** : il ne contient aucune donnée des joueurs (pronos, prénoms, codes) ni aucun secret.
Les parties sont dans Firebase ; les clés sont gardées dans les secrets GitHub (Settings > Secrets and variables > Actions).

## Les fichiers

| Fichier | Rôle |
|---|---|
| `championnat.html` | L'application (tout est dans ce seul fichier) |
| `index.html` | Adresse principale du site : redirige vers `championnat.html` |
| `sw.js` | Service worker : affiche les notifications de rappel sur le téléphone |
| `manifest.json` | Nom et icône de l'app quand elle est ajoutée à l'écran d'accueil |
| `icon-180.png`, `icon-192.png`, `icon-512.png` | Icône de l'app |
| `logo-uccle-sport.svg` | Logo d'Uccle Sport (absent de hockey.be), appelé par l'app à cette adresse |
| `scripts/snapshot.py` | Robot des scores : lit hockey.be et écrit `data/hockey-snapshot.json` |
| `scripts/ehl.py`, `scripts/fihpl.py`, `scripts/nlhk.py` | Lecture de l'EHL, de la FIH Pro League et de la Hoofdklasse |
| `scripts/notify.py` | Rappels de pronos (notifications push) |
| `data/hockey-snapshot.json` | Données de la saison en cours, lues par l'app |
| `data/archives/saison-AAAA-AA.json` | Archive de chaque saison : tous les matchs joués et les classements |
| `.github/workflows/snapshot.yml` | Planning du robot des scores |
| `.github/workflows/notify.yml` | Planning des rappels |
| `.github/workflows/backup.yml` | Sauvegarde chiffrée de Firebase chaque nuit |

Ne pas déplacer les fichiers dans des dossiers : les icônes des téléphones, l'app et le robot les appellent à ces adresses.

## Le robot des scores (`snapshot.yml`)

Lancé toutes les 30 minutes. À chaque passage, un contrôle décide quoi faire :

- **Lecture complète de hockey.be** : chaque jour à 2h ; le samedi aussi à 12h, 14h, 16h, 18h et 20h ; le dimanche à 12h, 14h, 17h et 20h (heure belge).
- **Mise à jour rapide** EHL, Pro League et Hoofdklasse : toutes les 30 minutes les jours de match, de 9h à minuit.
- **Chasseur de scores** : de 11h à minuit, relit seulement les poules des parties et des équipes favorites où un match est fini sans score.
- **Archive de la saison** : chaque lundi, après la lecture de 2h.

Le bouton « Run workflow » (à la main) force une lecture complète.

## Les rappels (`notify.yml`)

Lancé toutes les 15 minutes. Notification seulement aux joueurs qui ont activé les rappels et à qui il manque un prono :

- la veille de chaque jour de match, entre 18h et 23h (sinon le jour même dès 7h, au plus tard 2 h avant le match) ;
- le mardi entre 18h et 23h pour les matchs du week-end (sinon le mercredi entre 7h et 13h) ;
- entre 1h45 et 15 minutes avant chaque match.

Bouton « Run workflow » : mode `test` (notification d'essai à un joueur) ou `simulation` (ce qui partirait les 7 prochains soirs, sans rien envoyer).

## Lancement à l'heure : cron-job.org

GitHub lance les tâches programmées avec plusieurs heures de retard. Deux tâches sur cron-job.org les lancent donc à l'heure, par l'API GitHub :

- « Pronos - rappels » : toutes les 15 minutes, `notify.yml`, corps `{"ref":"main","inputs":{"mode":"rappel"}}`
- « Pronos - robot hockey.be » : toutes les 30 minutes, `snapshot.yml`, corps `{"ref":"main","inputs":{"auto":"true"}}`

Elles utilisent une clé GitHub « fine-grained » limitée à ce repo, avec le seul droit Actions (lecture et écriture).
**Cette clé expire après 1 an** : la régénérer dans GitHub (Settings > Developer settings > Personal access tokens) et la recoller dans les deux tâches cron-job.org.
Les plannings GitHub restent en place en secours.

## Chaque saison (en juillet)

1. Dans `scripts/snapshot.py`, changer `SEASON_FROM` et `SEASON_TO` (par exemple `2027-07-01` et `2028-06-30`).
2. L'archive de la saison terminée reste dans `data/archives/` ; une nouvelle archive démarre toute seule.
3. Dans l'app, choisir les nouvelles poules de la partie (Réglages > Organisateur).

## Sauvegarde

Chaque nuit, `backup.yml` copie toutes les parties de Firebase, les chiffre avec le mot de passe `BACKUP_PASSWORD` et les range dans les « artifacts » de GitHub pendant 30 jours (Actions > Sauvegarde Firebase > un passage). Rien n'est mis dans le repo.

Pour relire une sauvegarde sur un Mac (Terminal), après l'avoir téléchargée et dézippée :

```
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -in pronos-backup-AAAA-MM-JJ.json.gz.enc -out sauvegarde.json.gz
gunzip sauvegarde.json.gz
```

## Sources des données

Données publiques de hockey.be (Hockey Belgium), de l'Euro Hockey League, de la FIH Pro League et de la Hoofdklasse.
Logos des clubs : hockey.be.
