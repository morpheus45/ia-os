# agent-os sur Omarchy

Rendre ta machine Omarchy autonome, sans cesser d'être ton poste de travail.

```bash
git clone https://github.com/morpheus45/ia-os && cd ia-os/omarchy
./install.sh
```

Une seule machine. Le service démarre au boot, avant et indépendamment de
toute session graphique — c'est ce que veut dire « autonome ». Ton bureau
n'est pas remplacé : il gagne un indicateur dans la barre, trois
raccourcis et des notifications.

## Ce que ça installe

**Sur le système**, avec `sudo` :

| | |
|---|---|
| runtime | dans les `site-packages` de Python, chemin demandé à l'interpréteur |
| `agentos.service` | le service, démarré au boot |
| `agentos-model.service` | le serveur de modèle local, si un GGUF est présent |
| `agentos-backup.timer` | instantané quotidien de la mémoire |
| `/etc/agentos/` | configuration et secrets, en 0640 `root:agentos` |
| compléments `bureau.conf` | les bornes mémoire ci-dessous |

**Chez toi**, sans `sudo` :

| | |
|---|---|
| `~/.local/bin/agentos-desktop` | barre, invite de tâche, menu, notifications |
| `~/.config/waybar/config.jsonc` | le module, inséré après sauvegarde |
| `~/.config/hypr/agentos.conf` | les raccourcis, sourcés depuis `bindings.conf` |
| `~/.config/agentos/remote.conf` | le jeton de la console, en 0600 |

Rien n'est écrit dans `~/.local/share/omarchy` : ce sont les fichiers
d'Omarchy, qu'il réécrit à chaque mise à jour.

## La contrainte qui décide de tout : la RAM

Sur 16 Gio partagés avec un bureau, il n'y a pas de place pour tout le
monde. Les plafonds posés par l'installation :

| | Plafond | Pourquoi |
|---|---|---|
| bureau (Hyprland, waybar, navigateur) | 3–5 Gio | mesuré, non contraint |
| runtime agent-os | 2 Gio | c'est un orchestrateur, pas un calculateur |
| serveur de modèle local | 7 Gio | ce qui reste |

Il subsiste 2 à 4 Gio de marge. Conséquence à assumer : **un modèle de 7
milliards de paramètres quantifié en 4 bits passe, un 14 milliards non.**
Avec l'API Claude seule, la question ne se pose pas — elle ne consomme
presque rien en local, et la marge remonte à 9 Gio.

L'agent cède le pas au bureau sur toute la ligne : poids processeur au
cinquième du défaut, priorité d'ordonnancement basse, et surtout un score
qui le désigne au noyau comme victime avant ton navigateur. Le serveur de
modèle est réglé pour partir en premier — il se recharge en quelques
secondes depuis le disque, alors qu'un bureau tué emporte ton travail en
cours.

Il ne prend aussi que deux fils d'exécution : sans cette limite, la frappe
au clavier devient saccadée pendant qu'il génère.

## Usage

**Raccourcis** — `SUPER+ALT` est peu utilisé par Omarchy par défaut :

| | |
|---|---|
| `SUPER+ALT+A` | confier une tâche |
| `SUPER+ALT+M` | menu des actions |
| `SUPER+ALT+C` | console web |

Vérifier les collisions sur ta configuration : le menu des raccourcis de
Walker, ou `hyprctl binds`.

**La barre** affiche l'état : discret au repos, pulsant quand la machine
travaille, rouge quand un travail est mort, éteint quand le service est
arrêté. Clic gauche pour le menu, clic droit pour une tâche, clic milieu
pour la console. L'infobulle donne la mémoire, le cerveau actif, les
travaux et la prochaine échéance.

**Notifications** — par Mako, seulement aux transitions :

- un travail meurt, avec le nom et le début de l'erreur
- un travail *long* se termine — en dessous d'une minute, l'entretien
  périodique noierait l'utile
- le service s'arrête, ou revient

**Ligne de commande :**

```bash
agentosctl status              # état complet
agentosctl ask "…"             # confier une tâche et attendre la réponse
agentos-desktop ask "…"        # déposer en file, notification à la fin
agentosctl doctor              # diagnostic, fonctionne service arrêté
agentosctl memory search "…"   # chercher dans la mémoire
```

## Modèle local hors-ligne

```bash
sudo -u agentos curl -L -o /var/lib/models/modele.gguf "<url du GGUF>"
sudo systemctl start agentos-model
agentosctl doctor
```

Viser un 7B quantifié en Q4_K_M, autour de 4,5 Go. Le service ne démarre
que si un `.gguf` est présent : sans cette condition, une machine sans
modèle verrait systemd relancer en boucle un serveur qui ne peut pas
partir.

Quelques jetons par seconde sur processeur. C'est un repli qui garde la
machine utile sans réseau, pas un équivalent de l'API.

## Piloter une machine distante

Le même outil sert si l'agent tourne ailleurs — sur la machine dédiée
décrite dans le [README principal](../README.md), par exemple. Ramener son
port ici par SSH, et renseigner `AGENTOS_SSH` :

```bash
ssh -N -L 8787:127.0.0.1:8787 admin@machine-agent &
sed -i 's/^AGENTOS_SSH=.*/AGENTOS_SSH=admin@machine-agent/' ~/.config/agentos/remote.conf
```

Passer par SSH plutôt que d'exposer l'API : elle n'a ni TLS ni gestion de
comptes, et SSH fournit déjà les deux.

## Mises à jour

```bash
cd ia-os && git pull && ./omarchy/install.sh
```

Idempotent. La mémoire, les secrets et la configuration ne sont pas
touchés ; le module waybar et les raccourcis ne sont pas dupliqués.

Une mise à jour d'Omarchy qui réécrirait `config.jsonc` ou `bindings.conf`
emporterait le module et les raccourcis. Relancer `install.sh` les
remet — Omarchy laisse une copie `.bak` de tes fichiers dans ce cas.

## Si ça ne marche pas

```bash
systemctl status agentos              # le service tourne-t-il ?
journalctl -u agentos -n 50           # ce qu'il dit
agentosctl doctor                     # fonctionne service arrêté
agentos-desktop check                 # le bureau atteint-il le service ?
```

Le module de la barre reste éteint mais présent quand le service est
arrêté : on distingue ainsi « rien à signaler » de « je ne sais pas ».
