# Installer depuis Windows, sans redémarrer

Une machine virtuelle reçoit l'accès brut au disque physique visé, y
démarre l'image agent-os, et l'installateur écrit sur le vrai disque.
Windows continue de tourner pendant toute l'opération. Un seul
redémarrage sera nécessaire, à la fin, pour choisir le disque dans le
menu d'amorçage.

C'est la méthode la plus confortable pour un disque externe ou un second
disque interne : aucune clé USB à préparer, aucun redémarrage à l'aveugle.

## Ce qui rend l'opération valide

**Le mode d'amorçage de la machine virtuelle doit être celui de ton PC.**
C'est le seul point qui ruine tout s'il est manqué, et rien ne le signale
avant l'échec : installer en mode BIOS dans la machine virtuelle alors
que le PC démarre en UEFI produit un disque que le micrologiciel ne
proposera jamais. Le script force l'UEFI par défaut, qui est le mode de
presque toutes les machines depuis 2012 ; `-Bios` sert aux plus
anciennes.

Vérifier le mode de ton PC :

```powershell
$env:firmware_type
# UEFI  ou  Legacy
```

**Le disque doit être hors ligne côté Windows.** Sinon Windows garde des
poignées ouvertes sur ses volumes, et soit VirtualBox ne peut pas écrire,
soit les deux écrivent en même temps. Le script s'en charge.

**Le matériel de la machine virtuelle n'est pas celui du PC.** Sans
importance ici : le système installé est un Debian avec un noyau
générique et les micrologiciels embarqués. C'est précisément pour cela
que le projet s'appuie sur une distribution éprouvée plutôt que sur un
noyau écrit pour l'occasion.

## La marche à suivre

VirtualBox est requis — Hyper-V ne sait pas donner l'accès brut à un
disque de la même façon.

```powershell
# PowerShell EN ADMINISTRATEUR
cd $env:USERPROFILE\Downloads
Set-ExecutionPolicy -Scope Process Bypass -Force
irm https://raw.githubusercontent.com/morpheus45/ia-os/main/outils/installer-vm.ps1 -OutFile installer-vm.ps1
Unblock-File .\installer-vm.ps1
.\installer-vm.ps1 -Image ".\agent-os.iso"
```

`Set-ExecutionPolicy -Scope Process` n'assouplit la règle que pour cette
fenêtre, et `Unblock-File` retire la marque que Windows appose sur les
fichiers venus d'Internet. Sans ces deux lignes, PowerShell refuse le
script. Ne pas se placer dans `C:\WINDOWS\system32`, où PowerShell
s'ouvre par défaut en administrateur.

Le script liste les disques en marquant celui qui porte Windows, refuse
de l'écraser, demande confirmation, met le disque choisi hors ligne, crée
la machine virtuelle en UEFI et la démarre.

Dans la fenêtre qui s'ouvre :

```bash
sudo agentos-materiel                  # ce que la machine voit
sudo agentos-installer --simulation    # le plan, sans rien écrire
sudo agentos-installer                 # installer
```

Sur un disque **externe**, ajouter `--externe` aux deux dernières
commandes ; le script PowerShell le rappelle au moment voulu. Voir
[Particularités d'un disque externe](#particularités-dun-disque-externe)
pour la raison — elle n'est pas facultative.

Le disque à choisir est `/dev/sda`. **Vérifier qu'il affiche la taille de
ton disque physique** — c'est le seul contrôle qui garantit qu'on ne vise
pas le bon numéro par erreur.

Une fois terminé, éteindre la machine virtuelle puis :

```powershell
.\installer-vm.ps1 -Nettoyer
```

## Démarrer dessus

Redémarrer, ouvrir le menu d'amorçage — F12 chez Dell et Lenovo, F9 chez
HP, Échap ailleurs — et choisir ce disque.

Windows reste le système par défaut : l'installateur pose l'amorceur sur
la partition EFI du disque cible avec `--removable --no-nvram`, sans rien
écrire dans la mémoire de la carte mère. Débrancher le disque externe
remet le PC exactement dans son état d'origine.

**Le Secure Boot doit être désactivé** : l'amorceur n'est signé par
personne, et un micrologiciel avec Secure Boot actif ignore le disque
sans message.

## Particularités d'un disque externe

Un disque débranchable exige deux réglages qu'un disque interne n'a pas :

- **`rootdelay=5`** — un disque USB met plusieurs secondes à s'annoncer au
  noyau. Sans ce délai, l'initramfs cherche la racine avant qu'elle
  n'existe et tombe dans un shell de secours sur `ALERT! UUID=… does not
  exist`, alors que le disque est bien branché.
- **Pas d'hibernation** — reprendre depuis une image écrite sur un disque
  qu'on peut débrancher est impossible, et remonter ensuite ce disque dans
  l'état où l'hibernation l'a laissé corrompt le système de fichiers.

Démarré depuis une clé, l'installateur reconnaît un disque USB tout seul.
**Depuis une machine virtuelle, il ne le peut pas** : VirtualBox présente
le disque brut au système invité comme un disque SATA, quel que soit son
branchement réel. La déduction conclurait « interne », et le système
installé s'arrêterait au premier démarrage sur le vrai ordinateur.

D'où l'option `--externe`, que `installer-vm.ps1` affiche de lui-même
quand Windows rapporte un bus USB — Windows, lui, connaît le vrai
branchement. `--simulation` annonce la nature retenue :

```
Support     : externe — délai de démarrage, pas d'hibernation (déclaré)
```

`déclaré` signifie que la nature vient de l'option, `déduit` qu'elle vient
du noyau. Dans une machine virtuelle, seul `déclaré` est fiable.

Un disque dur externe ou un SSD convient. Une clé USB ordinaire non : sa
mémoire flash n'a ni cache ni bonne répartition de l'usure, et la mémoire
de l'agent est une base écrite en continu.
