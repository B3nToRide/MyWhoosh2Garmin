# Synchronisation automatique sur Mac mini avec Docker

Le conteneur utilise uniquement le cloud MyWhoosh : aucune installation de
MyWhoosh sur le Mac mini, aucun fichier local de l'application et aucun port
réseau entrant ne sont nécessaires. PowerShell et Strava ne sont pas utilisés.

## 1. Prérequis

- Docker Desktop avec Docker Compose v2, démarré et accessible dans le Terminal.
- Git et une copie de ce dépôt sur le disque local du Mac.
- Un compte MyWhoosh et un compte Garmin Connect.
- Mac connecté à Internet et **réveillé** aux horaires prévus.

L'image Python est compatible `linux/arm64` (Apple Silicon) et `linux/amd64`
(Mac Intel). Docker choisit l'architecture de la machine ; ne pas forcer amd64
sur Apple Silicon. Les dépendances sont installées avec leurs versions et
empreintes verrouillées, depuis `android/requirements.txt`.

Dans Docker Desktop, activer le démarrage à l'ouverture de session. La politique
`unless-stopped` relance le conteneur quand Docker redémarre, sauf après un arrêt
manuel. Docker Desktop ne garantit pas un démarrage avant l'ouverture de session.
Le Mac ne doit pas se mettre en veille automatiquement si l'exécution exacte à
20 h est importante ; sinon, le rattrapage attendra son réveil et Docker.

Exécuter toutes les commandes suivantes depuis la racine du dépôt.

## 2. Créer les secrets sans les mettre dans l'historique

MyWhoosh attend le mot de passe original via HTTPS : une empreinte SHA-256
ne peut pas le remplacer. Le fichier contient donc le mot de passe en clair,
protégé par le système de fichiers.

Dans le Terminal macOS avec **zsh** (shell par défaut), saisir :

```zsh
(
  umask 077
  secret_dir="$HOME/.config/mywhoosh2garmin"
  mkdir -p "$secret_dir"
  chmod 700 "$secret_dir"
  read -r 'email?Adresse MyWhoosh : '
  read -rs 'password?Mot de passe MyWhoosh (masqué) : '
  print
  printf '%s' "$email" > "$secret_dir/mywhoosh_email"
  printf '%s' "$password" > "$secret_dir/mywhoosh_password"
  chmod 600 "$secret_dir/mywhoosh_email" "$secret_dir/mywhoosh_password"
)
```

Le mot de passe n'est ni affiché ni passé dans une commande, un argument Docker,
une variable d'environnement du conteneur ou un fichier versionné. Les espaces,
apostrophes, `$` et antislashs sont conservés littéralement. Les retours à la ligne
finaux du fichier sont ignorés lors de la lecture.

### Ce que cette protection garantit — et ne garantit pas

- Les secrets sont montés **en lecture seule** dans `/run/secrets/`.
- `.dockerignore` utilise une liste d'autorisation : les secrets, `.env`,
  `.docker.env`, `.garth/`, `data/` et `.git/` ne partent pas au moteur de build.
- Le conteneur est non-root, sans capacités Linux, avec un système de fichiers
  racine en lecture seule et sans port publié ni socket Docker monté.
- **Les secrets Compose basés sur des fichiers ne sont pas un coffre-fort
  chiffré.** Activer FileVault et protéger le compte macOS et les sauvegardes.
- Un administrateur du Mac ou une personne contrôlant Docker peut lire les
  secrets et les jetons. Le script doit avoir accès au mot de passe en mémoire
  pendant la connexion ; cela ne peut pas être supprimé par un hachage.
- Ne pas partager les fichiers de session. Les journaux évitent les mots de
  passe, réponses d'authentification et URL signées, mais contiennent les noms et
  horaires des activités : les anonymiser avant de les publier.

Le Trousseau macOS n'est pas directement accessible depuis un conteneur Linux.
Une intégration au Trousseau nécessiterait un mécanisme supplémentaire côté Mac,
non inclus ici.

## 3. Préparer Compose et le stockage

```sh
cp .docker.env.example .docker.env
id -u
id -g
mkdir -p data
chmod 700 data
```

Éditer `.docker.env` :

- `CONTAINER_UID` : résultat de `id -u`.
- `CONTAINER_GID` : résultat de `id -g`.
- `MYWHOOSH_EMAIL_SECRET_FILE` : chemin absolu vers `mywhoosh_email`.
- `MYWHOOSH_PASSWORD_SECRET_FILE` : chemin absolu vers `mywhoosh_password`.

Les valeurs 501/20 de l'exemple sont courantes sur Mac, mais doivent être
vérifiées. **L'UID/GID doit correspondre au propriétaire des fichiers privés** :
Compose ne remappe pas les permissions des secrets locaux montés depuis un
fichier. Ne pas contourner une erreur de lecture en rendant le mot de passe
lisible par tous.

`.docker.env` ne contient que des chemins et paramètres, pas de mot de passe.
Il est ignoré par Git. Toujours passer `--env-file .docker.env`, pour ne pas
confondre ce fichier avec le `.env` historique du script natif.

Le dossier `data/`, également ignoré par Git, persiste sur le Mac :

| Chemin | Contenu |
|---|---|
| `data/.garth/` | Jetons de session Garmin, sensibles |
| `data/backups/` | Fichiers FIT nettoyés |
| `data/schedule.json` | Échéance, tentatives et dernière réussite |
| `data/mywhoosh_device_id.json` | Identifiant d'appareil MyWhoosh stable, généré au premier login cloud |
| `data/sync.lock` | Verrou de synchronisation |
| `data/scheduler.lock` | Verrou de planification |
| `data/mywhoosh_device.lock` | Verrou de création/lecture de l'identifiant MyWhoosh |

Les sauvegardes cloud portent l'heure de départ UTC, par exemple
`MyWhoosh_2026-11-09_190000Z.fit`, pour éviter les ambiguïtés de changement d'heure.

Les fichiers FIT nettoyés et les conversions GPX déclarent un **Garmin Edge 1040**
comme appareil d'enregistrement (fabricant 1, produit 3843). Le message de
l'appareil principal est mis à jour ou ajouté ; les capteurs et leurs identifiants
restent inchangés. Ce sont des métadonnées : cela ne transforme pas une sortie
MyWhoosh en enregistrement matériel réel et ne garantit pas les métriques Garmin
propres à un Edge. Les activités déjà présentes dans Connect ne sont ni modifiées
ni supprimées/réimportées pour changer leur appareil.

Les verrous sont gérés par le système d'exploitation : leur fichier peut rester
présent après un arrêt sans bloquer l'exécution. Ne pas les supprimer pendant
qu'un processus tourne. Conserver les données sur le disque local, pas sur un
partage réseau dont les garanties de verrouillage peuvent différer.

### Identifiant d'appareil MyWhoosh

Le script génère une seule fois un UUID aléatoire dans
`data/mywhoosh_device_id.json`, avant de contacter MyWhoosh. Il réutilise ensuite
ce même `DeviceId` pour les synchronisations manuelles, planifiées et les nouvelles
tentatives, y compris après un refus de connexion, un timeout ou un `down/up`.
Le `CorrelationId` reste différent pour chaque requête de connexion.

Conserver ce fichier avec les sauvegardes de `data/`. Le supprimer ou changer de
dossier de données crée une nouvelle identité ; cela ne déconnecte pas les
anciennes sessions côté serveur. Un fichier invalide n'est pas remplacé
silencieusement : restaurer une copie valide après avoir arrêté le service.
Les installations indépendantes utilisent leur propre dossier de données et
leur propre UUID ; ne pas recopier l'identifiant d'un autre appareil MyWhoosh.

Cette mesure évite que le script se présente comme un nouvel appareil à chaque
connexion, mais **ne garantit pas la disparition des conflits de session** et
ne permet pas de lister ou de déconnecter les autres appareils. Les jetons
MyWhoosh ne sont pas encore conservés entre les exécutions, et aucune déconnexion
forcée n'est effectuée. La session Garmin reste gérée séparément dans `.garth/`.

## 4. Construire et authentifier Garmin

```sh
docker compose --env-file .docker.env config --quiet
docker compose --env-file .docker.env build --pull
docker compose --env-file .docker.env run --rm sync --login
```

Saisir l'adresse Garmin et son mot de passe dans le terminal interactif, puis
le code MFA si Garmin le demande. `--login` ne synchronise aucune activité et
ne conserve pas le mot de passe Garmin ; il sauvegarde uniquement la session.

Ces jetons permettent d'accéder au compte : les protéger comme un mot de passe.
Ils sont écrits avec des permissions restrictives et renouvelés lors des
synchronisations. Une révocation ou une expiration peut nécessiter une nouvelle
connexion interactive.

## 5. Démarrer

```sh
docker compose --env-file .docker.env up -d
docker compose --env-file .docker.env logs -f --tail=100 sync
```

**Au tout premier démarrage, une synchronisation est lancée immédiatement**, puis
le conteneur attend l'horaire configuré. Fermer l'application MyWhoosh avant une
synchronisation : le service peut refuser une connexion simultanée au compte.

Les futures exécutions ne demandent jamais de saisie : une session Garmin absente
ou invalide produit un message demandant `--login`, puis un échec contrôlé.

## 6. Configurer la planification

### Chaque jour à 20 h, heure de Paris — défaut

```dotenv
TZ=Europe/Paris
SCHEDULE_MODE=daily
SCHEDULE_TIME=20:00
```

Le passage heure d'été/hiver conserve **20 h locales**, pas un intervalle fixe
de 24 heures. Pour un horaire situé dans la transition : un horaire inexistant
au printemps est décalé de la durée du saut ; un horaire répété en automne est
exécuté une seule fois, à sa première occurrence.

### Toutes les 4 heures

```dotenv
TZ=Europe/Paris
SCHEDULE_MODE=interval
SCHEDULE_INTERVAL=4h
```

L'intervalle est mesuré en temps écoulé UTC, même pendant un changement d'heure.
Formats acceptés : entier positif suivi de `s`, `m`, `h` ou `d`, par exemple
`90m`, `4h`, `1d` ; maximum 365 jours. L'ancrage est l'heure de la première
exécution de cette configuration, pas nécessairement minuit.

Les modes sont exclusifs : `SCHEDULE_TIME` est ignoré en mode intervalle,
`SCHEDULE_INTERVAL` en mode quotidien. Il ne s'agit pas d'un interpréteur cron
général : les deux modes couvrent l'horaire quotidien et l'intervalle demandés.

Après une modification de `.docker.env`, recréer le conteneur :

```sh
docker compose --env-file .docker.env up -d --force-recreate
```

Un changement de mode, d'horaire, d'intervalle ou de fuseau initialise un nouvel
ancrage et lance une synchronisation immédiate. Un simple redémarrage sans
changement respecte l'état enregistré.

### Rattrapage et erreurs

- Si l'échéance a été manquée pendant l'arrêt, **une seule synchronisation** est
  lancée au redémarrage. Les échéances manquées ne sont pas rejouées une à une.
- Sans échéance manquée, aucun lancement supplémentaire au redémarrage.
- Les phases d'intervalle sont conservées ; les créneaux déjà passés sont sautés.
- Par défaut, une exécution échouée est tentée **3 fois au total**, à **5 minutes**
  d'intervalle. Cela couvre notamment les indisponibilités réseau et MyWhoosh
  encore ouvert. Après épuisement, attendre le prochain créneau ou lancer
  manuellement. Les compteurs de tentative survivent aux redémarrages.
- Chaque tentative est limitée à **30 minutes** ; un processus bloqué est arrêté.
- Une tentative interrompue reste due au redémarrage. Les sauvegardes atomiques
  et la vérification Garmin réduisent les risques de corruption et de doublon.
- Une activité échouée n'empêche pas le traitement des autres ; la tentative
  globale est néanmoins considérée comme échouée.

Paramètres facultatifs :

```dotenv
SYNC_MAX_ATTEMPTS=3
SYNC_RETRY_DELAY_SECONDS=300
SYNC_TIMEOUT_SECONDS=1800
```

## 7. Commandes utiles

Synchronisation ponctuelle sans saisie interactive :

```sh
docker compose --env-file .docker.env run --rm sync --once --non-interactive
```

Elle peut être lancée même si le planificateur tourne : le verrou partagé
empêche deux synchronisations simultanées. Elle ne modifie pas l'échéance du
planificateur. Code de sortie 75 si une autre synchronisation est en cours.

**Un `docker compose down` puis `up` ne force pas une nouvelle tentative** :
`data/schedule.json` reste sur le Mac et conserve l'échéance et les compteurs.
Utiliser la commande ponctuelle ci-dessus pour réessayer immédiatement, sans
supprimer l'état. Une réussite manuelle n'annule pas une nouvelle tentative
déjà planifiée ; cette dernière revérifiera les activités présentes sur Garmin.

Consulter l'état et les journaux :

```sh
docker compose --env-file .docker.env ps
docker compose --env-file .docker.env logs --tail=100 sync
cat data/schedule.json
```

Les journaux Docker ont une rotation de 5 MiB × 3 fichiers. Aucun journal
supplémentaire n'est écrit dans le conteneur. `last_success` dans l'état indique
la dernière tentative entièrement réussie, y compris quand rien n'était à envoyer.
Un conteneur démarré n'est pas une preuve que la synchronisation a réussi.
Garmin peut accuser réception d'un fichier avec un `uploadId` et des listes
`successes`/`failures` vides, comme dans la réponse documentée par `garth`. Le log
indique alors `Garmin accepted ... activity processing may continue` : le fichier
a été accepté, mais ce n'est pas une confirmation que l'activité est déjà visible
dans Connect. Un résultat explicite dans `successes` produit `Uploaded ...` ; des
`failures` restent une erreur, avec leurs codes numériques s'ils sont fournis.
Le bilan `submitted` compte les fichiers acceptés ou confirmés, pas uniquement les
activités dont l'affichage dans Connect a été vérifié. Les réponses complètes,
identifiants de compte et messages libres du serveur ne sont pas journalisés.
Les dates enregistrées dans `schedule.json` sont exprimées en UTC.
Les échéances affichées dans les logs sont converties dans le fuseau configuré,
avec date, décalage UTC et nom du fuseau, par exemple
`2026-11-09T21:05:42+01:00 [Europe/Paris]`. Ainsi, `20:05:42+00:00` dans l'état
correspond à **21 h 05 à Paris en hiver**, et non à 20 h 05 locales. Une nouvelle
tentative est calculée depuis la fin de la tentative précédente, pas depuis
l'horaire quotidien de 20 h. Au démarrage, les logs précisent l'heure courante,
la prochaine tentative et si elle est en attente ou déjà due.

Arrêter sans effacer les données :

```sh
docker compose --env-file .docker.env stop sync
```

### Reconnexion Garmin

```sh
docker compose --env-file .docker.env stop sync
docker compose --env-file .docker.env run --rm sync --login
docker compose --env-file .docker.env up -d
```

### Changer le mot de passe MyWhoosh

Arrêter le service, réexécuter la saisie masquée de la section 2 puis relancer
avec `up -d --force-recreate`. Cela remonte les nouveaux fichiers, même si un
éditeur les a remplacés plutôt que modifiés sur place.

### Mise à jour

```sh
docker compose --env-file .docker.env stop sync
git pull
docker compose --env-file .docker.env build --pull
docker compose --env-file .docker.env up -d
```

La session, les sauvegardes et l'état restent dans `data/`. La balise Python
3.13 du Dockerfile reçoit les mises à jour de sécurité lors du rebuild ; les
versions des dépendances Python restent verrouillées.

### Sauvegarde et restauration

Arrêter le service avant de copier `data/`, `.docker.env` et les deux fichiers
de secrets dans une sauvegarde **privée et chiffrée**. `data/.garth/` est caché :
vérifier qu'il est bien inclus. Ne pas publier cette sauvegarde ni la versionner.

Pour restaurer, replacer ces fichiers aux chemins configurés, vérifier le
propriétaire UID/GID et les permissions (dossiers privés 700, secrets et jetons
600), puis relancer Compose. Sans session récupérable, refaire `--login`.
Sans état de planification, le premier démarrage déclenche une synchronisation
immédiate ; Garmin est interrogé avant tout nouvel envoi.

## 8. Dépannage et limites

| Symptôme | Action |
|---|---|
| `Permission denied` | Vérifier les UID/GID et propriétaires des secrets et de `data/`, puis les autorisations de partage de fichiers Docker Desktop. Ne pas utiliser `chmod 777`. |
| Session Garmin absente/expirée | Arrêter le planificateur et refaire `--login`. |
| MyWhoosh déjà connecté ailleurs | Quitter l'application sur les autres appareils, attendre une tentative ou synchroniser manuellement. |
| Échec réseau | Consulter les logs ; les tentatives sont espacées et limitées. |
| Pas d'exécution à 20 h | Vérifier Docker Desktop, la veille du Mac, `TZ`, l'état et les logs. |
| Autre instance en cours | Attendre sa fin. Ne pas supprimer les fichiers de verrou. |
| `Invalid schedule.json` | Arrêter le service, conserver le fichier pour diagnostic, restaurer une copie valide ou le déplacer hors de `data/`, puis relancer. |
| Fichier FIT rejeté | La sauvegarde reste disponible ; les autres activités continuent. Ne pas partager un FIT sans considérer ses données personnelles. |

**Limites fonctionnelles :**

- Le rattrapage MyWhoosh concerne les **10 dernières activités**, dans un délai
  maximal de **21 jours en arrière**. Une longue indisponibilité ou plus de
  10 sorties peut nécessiter un import manuel depuis MyWhoosh.
- La détection Garmin compare l'heure à ±60 secondes pour les activités de type
  vélo virtuel, ou vélo nommées MyWhoosh. Ce n'est pas une identité parfaite : une
  autre activité virtuelle simultanée peut être confondue ; un import manuel
  renommé/reclassé peut ne pas être reconnu. Garmin vérifie aussi les doublons
  au moment de l'envoi.
- Les moyennes complétées sont calculées par échantillon, sans pondération
  temporelle ; la conversion GPX estime distance/vitesse depuis les coordonnées.
- Les API MyWhoosh et Garmin peuvent changer sans préavis. Cette installation
  n'apporte pas une garantie de disponibilité des services tiers.
- Le mode `--local` reste disponible en natif mais n'est pas utilisé par Compose.
- Strava est un outil optionnel distinct, non installé ni planifié dans l'image.

## 9. Tests sans identifiants

Depuis un poste disposant de `uv` :

```sh
uv run --python 3.13 --with-requirements android/requirements.txt --with-requirements tests/requirements.txt python -m unittest discover -s tests -v
```

Les tests simulent les services. Ils couvrent notamment les mesures manquantes,
les écritures atomiques, les erreurs partielles, les secrets, les verrous, les
deux modes de planification, le rattrapage, les nouvelles tentatives et les
transitions heure d'été/hiver. Ils ne remplacent pas une première synchronisation
réelle avec tes comptes, à effectuer après l'installation.

Après avoir construit l'image, un test d'intégration Compose utilise uniquement
des secrets factices et un répertoire temporaire, avec le réseau désactivé :

```sh
python3 tests/docker_smoke.py
```

Il vérifie les permissions, les montages, l'exécution non-root, les tentatives
limitées, l'arrêt et le redémarrage persistant. Il ne lance aucun service durable
et ne modifie pas ton dossier `data/`. Les tests sont aussi configurés dans
`.github/workflows/tests.yml`.
