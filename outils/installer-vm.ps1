<#
.SYNOPSIS
    Installe agent-os sur un disque physique, depuis Windows, sans redémarrer.

.DESCRIPTION
    À lancer depuis PowerShell EN ADMINISTRATEUR.

    Le principe : on donne à une machine virtuelle l'accès brut au disque
    physique visé, on y démarre l'image agent-os, et l'installateur écrit
    sur le vrai disque. Windows continue de tourner pendant toute
    l'opération ; un seul redémarrage sera nécessaire, à la fin, pour
    choisir le disque dans le menu d'amorçage.

    LE DISQUE CHOISI EST INTÉGRALEMENT EFFACÉ.

    Point décisif : la machine virtuelle démarre en mode UEFI, comme la
    plupart des PC depuis 2012. Installer en mode BIOS dans la VM alors
    que la machine réelle démarre en UEFI produit un disque qui ne
    démarrera jamais — et rien ne le signale avant l'échec.

.PARAMETER Disque
    Numéro du disque physique, tel que le donne Get-Disk.

.PARAMETER Image
    Chemin de l'ISO agent-os (ou de l'archive ZIP téléchargée depuis
    GitHub Actions).

.PARAMETER Bios
    Force le mode BIOS hérité, pour une machine ancienne sans UEFI.

.EXAMPLE
    .\installer-vm.ps1 -Disque 2 -Image .\agent-os.iso
#>

[CmdletBinding()]
param(
    [int]    $Disque = -1,
    [string] $Image  = "",
    [int]    $MemoireMo = 4096,
    [switch] $Bios,
    [switch] $Nettoyer
)

$ErrorActionPreference = "Stop"
$NomVM = "agent-os-installation"

function Info   { param($m) Write-Host ":: $m" -ForegroundColor Cyan }
function Bien   { param($m) Write-Host "ok $m" -ForegroundColor Green }
function Alerte { param($m) Write-Host "!  $m" -ForegroundColor Yellow }
function Sortir { param($m) Write-Host "échec $m" -ForegroundColor Red; exit 1 }

# --- droits ---------------------------------------------------------------

$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Sortir "l'accès brut à un disque exige les droits administrateur.
    Rouvrir PowerShell par « Exécuter en tant qu'administrateur »."
}

# --- VirtualBox -----------------------------------------------------------

$vbox = @(
    "$env:ProgramFiles\Oracle\VirtualBox\VBoxManage.exe",
    "${env:ProgramFiles(x86)}\Oracle\VirtualBox\VBoxManage.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $vbox) {
    $vbox = (Get-Command VBoxManage.exe -ErrorAction SilentlyContinue).Source
}
if (-not $vbox) {
    Sortir "VirtualBox introuvable.
    L'installer depuis https://www.virtualbox.org puis relancer.
    C'est lui qui sait donner à une machine virtuelle l'accès brut à un
    disque physique — Hyper-V ne le permet pas de la même façon."
}
Bien "VirtualBox : $vbox"

$travail = Join-Path $env:LOCALAPPDATA "agent-os"
New-Item -ItemType Directory -Force -Path $travail | Out-Null
$vmdk = Join-Path $travail "disque-brut.vmdk"

# --- nettoyage d'une exécution précédente ---------------------------------

function Supprimer-VM {
    & $vbox controlvm $NomVM poweroff 2>$null | Out-Null
    Start-Sleep -Seconds 2
    & $vbox unregistervm $NomVM --delete 2>$null | Out-Null
    Remove-Item $vmdk -ErrorAction SilentlyContinue
    Remove-Item ($vmdk -replace '\.vmdk$', '-pt.vmdk') -ErrorAction SilentlyContinue
}

if ($Nettoyer) {
    Info "suppression de la machine virtuelle et du descripteur"
    Supprimer-VM
    Bien "nettoyé"
    exit 0
}

# --- image ----------------------------------------------------------------

if (-not $Image) { Sortir "indiquer l'image avec -Image <chemin de l'ISO ou du ZIP>" }
if (-not (Test-Path $Image)) { Sortir "image introuvable : $Image" }
$iso = (Resolve-Path $Image).Path

if ($iso -like "*.zip") {
    Info "archive détectée, extraction"
    $extrait = Join-Path $travail "image"
    Expand-Archive -Path $iso -DestinationPath $extrait -Force
    $trouve = Get-ChildItem $extrait -Filter *.iso -Recurse | Select-Object -First 1
    if (-not $trouve) { Sortir "aucune ISO dans $iso" }
    $iso = $trouve.FullName
}
Bien "image : $(Split-Path $iso -Leaf)"

# --- choix du disque ------------------------------------------------------

if ($Disque -lt 0) {
    Write-Host ""
    Info "disques physiques — « Systeme » marque celui qui porte Windows"
    Get-Disk | Sort-Object Number | ForEach-Object {
        [PSCustomObject]@{
            N       = $_.Number
            Go      = [math]::Round($_.Size / 1GB, 1)
            Bus     = $_.BusType
            Modele  = $_.FriendlyName
            Systeme = if ($_.IsBoot -or $_.IsSystem) { "OUI" } else { "" }
        }
    } | Format-Table -AutoSize
    $reponse = Read-Host "Numéro du disque à effacer"
    if (-not [int]::TryParse($reponse, [ref]$Disque)) { Sortir "numéro invalide" }
}

$cible = Get-Disk -Number $Disque -ErrorAction SilentlyContinue
if (-not $cible) { Sortir "aucun disque numéro $Disque" }
if ($cible.IsBoot -or $cible.IsSystem) { Sortir "le disque $Disque porte Windows. Refusé." }
if ($cible.Size -lt 120GB) {
    Sortir "disque trop petit : $([math]::Round($cible.Size/1GB,1)) Go, minimum 120 Go"
}

$partitions = Get-Partition -DiskNumber $Disque -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "  Disque $Disque : $($cible.FriendlyName)" -ForegroundColor White
Write-Host "  Taille        : $([math]::Round($cible.Size/1GB,1)) Go   Bus : $($cible.BusType)"
if ($partitions) {
    Write-Host "  Contient      :"
    $partitions | ForEach-Object {
        $lettre = if ($_.DriveLetter) { "$($_.DriveLetter):" } else { "(sans lettre)" }
        Write-Host "      $lettre  $([math]::Round($_.Size/1GB,1)) Go  $($_.Type)"
    }
}
Write-Host "  Mode          : $(if ($Bios) { 'BIOS hérité' } else { 'UEFI' })"
if ($cible.BusType -eq 'USB') {
    Write-Host "  Support       : externe (USB) — signalé à l'installateur"
}
Write-Host ""
Write-Host "  TOUT LE CONTENU DE CE DISQUE SERA DÉFINITIVEMENT EFFACÉ." -ForegroundColor Red
Write-Host "  Windows continuera de tourner pendant l'opération." -ForegroundColor Cyan
Write-Host ""
if ((Read-Host 'Taper « EFFACER » pour confirmer') -cne "EFFACER") {
    Write-Host "Annulé."; exit 1
}

# --- préparation ----------------------------------------------------------

Info "mise hors ligne du disque dans Windows"
# Sans cela, Windows garde des poignées ouvertes sur les volumes et
# VirtualBox ne peut pas prendre le disque en écriture — ou pire, les deux
# écrivent en même temps.
Set-Disk -Number $Disque -IsOffline $true -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Info "préparation de la machine virtuelle"
Supprimer-VM

& $vbox internalcommands createrawvmdk -filename "$vmdk" `
    -rawdisk "\\.\PhysicalDrive$Disque" 2>&1 | Out-Null
if (-not (Test-Path $vmdk)) {
    Set-Disk -Number $Disque -IsOffline $false -ErrorAction SilentlyContinue
    Sortir "création du descripteur de disque brut impossible.
    Vérifier que le disque n'est pas utilisé et que VirtualBox est à jour."
}
Bien "accès brut au disque physique $Disque"

& $vbox createvm --name $NomVM --ostype Debian_64 --register | Out-Null
& $vbox modifyvm $NomVM --memory $MemoireMo --cpus 2 --vram 32 `
        --firmware $(if ($Bios) { "bios" } else { "efi" }) `
        --boot1 dvd --boot2 disk --nic1 nat --audio-driver none | Out-Null
& $vbox storagectl $NomVM --name SATA --add sata --controller IntelAhci --portcount 2 | Out-Null
& $vbox storageattach $NomVM --storagectl SATA --port 0 --device 0 `
        --type hdd --medium "$vmdk" | Out-Null
& $vbox storageattach $NomVM --storagectl SATA --port 1 --device 0 `
        --type dvddrive --medium "$iso" | Out-Null

# VirtualBox présente le disque brut au système invité comme un disque
# SATA, quel que soit son branchement réel. L'installateur, qui décide du
# délai d'attente au démarrage et de l'hibernation d'après le transport
# rapporté par le noyau, conclurait donc « interne » pour un disque USB —
# et le système installé ne démarrerait pas une fois branché sur le vrai
# ordinateur. Windows, lui, connaît le vrai bus : on le lui transmet.
$externe = ($cible.BusType -eq 'USB')
$option  = if ($externe) { " --externe" } else { "" }
$noteExterne = if ($externe) {
    "`n  Ce disque est branché en USB : quand l'assistant demande s'il" +
    "`n  s'agit d'un disque externe, répondre OUI. La machine virtuelle le" +
    "`n  montre en SATA et ne peut pas le deviner ; sans cette réponse, le" +
    "`n  système installé s'arrêterait au démarrage sur" +
    "`n  « ALERT! UUID=... does not exist ».`n"
} else { "" }

Info "démarrage de la machine virtuelle"
& $vbox startvm $NomVM --type gui | Out-Null

Write-Host @"

  La fenêtre VirtualBox est ouverte. Windows tourne toujours.

  Dans la machine virtuelle :

  L'assistant démarre tout seul : choisir « Installer agent-os sur un
  disque », puis le disque dans la liste. Il pose lui-même les questions
  et n'écrit rien avant confirmation.
$noteExterne
  Pour piloter l'installateur à la main :

      sudo agentos-materiel                      vérifier ce qui est vu
      sudo agentos-installer$option --simulation   afficher le plan
      sudo agentos-installer$option                installer

  Le disque à choisir est /dev/sda — c'est ton disque physique $Disque,
  vu directement par la machine virtuelle. Il doit afficher la bonne
  taille : $([math]::Round($cible.Size/1GB,1)) Go. Si ce n'est pas le cas, ARRÊTER.

  Une fois l'installation terminée, éteindre la machine virtuelle, puis :

      .\installer-vm.ps1 -Nettoyer

  pour supprimer la VM et rendre le disque à Windows.

  Ensuite, redémarrer le PC et ouvrir le menu d'amorçage — F12 chez Dell
  et Lenovo, F9 chez HP — pour choisir ce disque. Windows reste le
  système par défaut : rien n'a été changé dans l'ordre de démarrage.

"@ -ForegroundColor Cyan

if (-not $Bios) {
    $sb = try { Confirm-SecureBootUEFI } catch { $null }
    if ($sb -eq $true) {
        Alerte "Secure Boot est actif sur ce PC. Il faudra le désactiver pour
    démarrer sur agent-os : son amorceur n'est signé par personne."
    }
}
