<#
.SYNOPSIS
    Télécharge l'image agent-os et l'écrit sur une clé USB.

.DESCRIPTION
    À lancer depuis PowerShell EN ADMINISTRATEUR sur Windows.

    Le script récupère la dernière image publiée, vérifie sa somme de
    contrôle, puis l'écrit sur le disque choisi. L'écriture se fait sur le
    disque physique entier, pas sur une lettre de lecteur : une image
    amorçable comprend sa table de partitions, et écrire dans « E: »
    produirait une clé contenant les fichiers mais incapable de démarrer.

    LE DISQUE CHOISI EST INTÉGRALEMENT EFFACÉ.

.PARAMETER Disque
    Numéro du disque, tel que le donne Get-Disk. Sans ce paramètre, le
    script liste les disques USB et demande lequel.

.PARAMETER Image
    Chemin d'une ISO déjà téléchargée, pour éviter un nouveau
    téléchargement.

.EXAMPLE
    .\creer-cle.ps1
    .\creer-cle.ps1 -Disque 2
#>

[CmdletBinding()]
param(
    [int]    $Disque = -1,
    [string] $Image  = "",
    [string] $Depot  = "morpheus45/ia-os"
)

$ErrorActionPreference = "Stop"

function Info  { param($m) Write-Host ":: $m" -ForegroundColor Cyan }
function Bien  { param($m) Write-Host "ok $m" -ForegroundColor Green }
function Alerte{ param($m) Write-Host "!  $m" -ForegroundColor Yellow }
function Sortir{ param($m) Write-Host "échec $m" -ForegroundColor Red; exit 1 }

# --- droits ---------------------------------------------------------------

$identite = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identite)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Sortir "écrire sur un disque physique exige les droits administrateur.
    Rouvrir PowerShell par « Exécuter en tant qu'administrateur »."
}

# --- image ----------------------------------------------------------------

if ($Image -and (Test-Path $Image)) {
    $chemin = (Resolve-Path $Image).Path
    $attendu = $null

    # GitHub emballe les artéfacts d'Actions dans un ZIP : le fichier
    # téléchargé depuis l'onglet Actions n'est pas l'image mais une archive
    # qui la contient. L'écrire telle quelle donnerait une clé illisible.
    if ($chemin -like "*.zip") {
        Info "archive détectée, extraction de l'image"
        $extrait = Join-Path (Split-Path $chemin) ([IO.Path]::GetFileNameWithoutExtension($chemin))
        Expand-Archive -Path $chemin -DestinationPath $extrait -Force
        $trouve = Get-ChildItem $extrait -Filter *.iso -Recurse | Select-Object -First 1
        if (-not $trouve) { Sortir "aucune image ISO dans $chemin" }
        $chemin = $trouve.FullName

        # L'archive contient aussi la somme de contrôle produite à la
        # construction : autant s'en servir.
        $sha = Get-ChildItem $extrait -Filter *.sha256 -Recurse | Select-Object -First 1
        if ($sha) { $attendu = ((Get-Content $sha.FullName -Raw) -split '\s+')[0] }
    }

    Info "image locale : $chemin"
    $iso = $chemin
} else {
    Info "recherche de la dernière image publiée sur $Depot"
    try {
        $release = Invoke-RestMethod "https://api.github.com/repos/$Depot/releases/latest" `
                                     -Headers @{ "User-Agent" = "agent-os" }
    } catch {
        Sortir "aucune version publiée, ou GitHub injoignable : $_
    Construire l'image via l'onglet Actions du dépôt, puis relancer."
    }

    $actifIso = $release.assets | Where-Object { $_.name -like "*.iso" } | Select-Object -First 1
    $actifSha = $release.assets | Where-Object { $_.name -like "*.sha256" } | Select-Object -First 1
    if (-not $actifIso) { Sortir "cette version ne contient pas d'image ISO" }

    $dossier = Join-Path $env:USERPROFILE "Downloads"
    $iso = Join-Path $dossier $actifIso.name
    $tailleMo = [int]($actifIso.size / 1MB)

    if ((Test-Path $iso) -and ((Get-Item $iso).Length -eq $actifIso.size)) {
        Bien "image déjà téléchargée : $iso"
    } else {
        Info "téléchargement de $($actifIso.name) ($tailleMo Mo)"
        # BITS quand il est là : il affiche une progression et reprend après
        # coupure, ce qu'Invoke-WebRequest ne sait pas faire.
        try {
            Start-BitsTransfer -Source $actifIso.browser_download_url -Destination $iso `
                               -Description "agent-os"
        } catch {
            Alerte "BITS indisponible, téléchargement simple (sans progression)"
            Invoke-WebRequest $actifIso.browser_download_url -OutFile $iso -UseBasicParsing
        }
        Bien "téléchargée dans $iso"
    }

    $attendu = $null
    if ($actifSha) {
        $texte = (Invoke-WebRequest $actifSha.browser_download_url -UseBasicParsing).Content
        $attendu = ($texte -split '\s+')[0]
    }
}

# --- intégrité ------------------------------------------------------------

if ($attendu) {
    Info "vérification de la somme de contrôle"
    $obtenu = (Get-FileHash $iso -Algorithm SHA256).Hash.ToLower()
    if ($obtenu -ne $attendu.ToLower()) {
        Sortir "somme de contrôle incorrecte — téléchargement corrompu.
    attendue : $attendu
    obtenue  : $obtenu
    Supprimer $iso et relancer."
    }
    Bien "intégrité vérifiée"
} else {
    Alerte "aucune somme de contrôle disponible : intégrité non vérifiée"
}

$tailleIso = (Get-Item $iso).Length

# --- choix du disque ------------------------------------------------------

function Montrer-Disques {
    Get-Disk | Sort-Object Number | ForEach-Object {
        [PSCustomObject]@{
            N       = $_.Number
            Go      = [math]::Round($_.Size / 1GB, 1)
            Bus     = $_.BusType
            Modele  = $_.FriendlyName
            Systeme = if ($_.IsBoot -or $_.IsSystem) { "OUI" } else { "" }
        }
    } | Format-Table -AutoSize
}

if ($Disque -lt 0) {
    Write-Host ""
    Info "disques détectés — la colonne « Systeme » marque celui qui porte Windows"
    Montrer-Disques
    Alerte "Débrancher puis rebrancher la clé et relancer cette liste lève tout doute :
    le disque qui apparaît et disparaît est le bon."
    $reponse = Read-Host "Numéro du disque à effacer"
    if (-not [int]::TryParse($reponse, [ref]$Disque)) { Sortir "numéro invalide" }
}

$cible = Get-Disk -Number $Disque -ErrorAction SilentlyContinue
if (-not $cible) { Sortir "aucun disque numéro $Disque" }

# --- garde-fous -----------------------------------------------------------

if ($cible.IsBoot -or $cible.IsSystem) {
    Sortir "le disque $Disque porte Windows. Refusé."
}
if ($cible.Size -lt $tailleIso) {
    Sortir "disque trop petit : $([math]::Round($cible.Size/1GB,1)) Go pour une image de $([math]::Round($tailleIso/1GB,2)) Go"
}
if ($cible.BusType -ne "USB") {
    Alerte "le disque $Disque n'est pas un périphérique USB ($($cible.BusType))."
}

Write-Host ""
Write-Host "  Disque $Disque : $($cible.FriendlyName)" -ForegroundColor White
Write-Host "  Taille        : $([math]::Round($cible.Size/1GB,1)) Go   Bus : $($cible.BusType)"
Write-Host "  Image         : $(Split-Path $iso -Leaf)"
Write-Host ""
Write-Host "  TOUT LE CONTENU DE CE DISQUE SERA DÉFINITIVEMENT EFFACÉ." -ForegroundColor Red
Write-Host ""
if ((Read-Host 'Taper « EFFACER » pour confirmer') -cne "EFFACER") {
    Write-Host "Annulé."; exit 1
}

# --- écriture -------------------------------------------------------------

Info "démontage et nettoyage du disque"
# Sans cela, Windows garde les volumes montés et refuse l'accès brut, ou
# réécrit derrière nous ce qu'on vient d'écrire.
Clear-Disk -Number $Disque -RemoveData -RemoveOEM -Confirm:$false -ErrorAction SilentlyContinue
Set-Disk -Number $Disque -IsOffline $true -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Info "écriture de l'image (quelques minutes, ne pas débrancher)"
$source = $null; $destination = $null
try {
    $source = [IO.File]::OpenRead($iso)
    $destination = New-Object IO.FileStream("\\.\PhysicalDrive$Disque",
                        [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::None)

    $tampon = New-Object byte[] (4MB)
    $ecrit = 0L
    $dernier = -1
    while (($lu = $source.Read($tampon, 0, $tampon.Length)) -gt 0) {
        $destination.Write($tampon, 0, $lu)
        $ecrit += $lu
        $pourcent = [int](100 * $ecrit / $tailleIso)
        if ($pourcent -ne $dernier) {
            Write-Progress -Activity "Écriture sur le disque $Disque" `
                           -Status "$([math]::Round($ecrit/1MB)) Mo sur $([math]::Round($tailleIso/1MB)) Mo" `
                           -PercentComplete $pourcent
            $dernier = $pourcent
        }
    }
    $destination.Flush($true)
} catch {
    Sortir "écriture impossible : $_
    Vérifier que la clé n'est pas protégée en écriture, et qu'aucun
    antivirus ne verrouille le disque."
} finally {
    if ($destination) { $destination.Dispose() }
    if ($source) { $source.Dispose() }
    Write-Progress -Activity "Écriture" -Completed
}

Set-Disk -Number $Disque -IsOffline $false -ErrorAction SilentlyContinue
Bien "image écrite sur le disque $Disque"

# --- suite ----------------------------------------------------------------

Write-Host @"

  Éjecter la clé proprement avant de la retirer : Windows peut n'avoir pas
  encore vidé son cache d'écriture.

  AVANT DE REDÉMARRER — désactiver le Secure Boot dans le firmware
  (F2, Suppr, F10 ou Échap au démarrage, onglet Boot ou Security).
  L'amorceur de cette image n'est signé par personne : Secure Boot actif,
  la clé est simplement ignorée, sans message.

  Puis démarrer dessus par le menu d'amorçage (F12 chez Dell et Lenovo,
  F9 chez HP), et dans la session live :

      sudo agentos-materiel     vérifier que le matériel est reconnu
      sudo agentos-installer    installer sur un disque — l'efface entièrement

"@ -ForegroundColor Cyan

$sb = try { Confirm-SecureBootUEFI } catch { $null }
if ($sb -eq $true) {
    Alerte "Secure Boot est ACTIF sur cette machine — à désactiver avant de démarrer sur la clé."
} elseif ($sb -eq $false) {
    Bien "Secure Boot déjà désactivé sur cette machine"
}
