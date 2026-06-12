<#
.SYNOPSIS
    ParcVue — Deploiement de masse de l'agent par GPO (script de demarrage).

.DESCRIPTION
    Script idempotent destine a etre execute via une GPO « Script de demarrage
    de l'ordinateur » (compte SYSTEM) sur 100+ postes Windows.

    Il realise, de maniere sure et repetable :
      1. Cree le repertoire d'installation C:\ProgramData\ParcVue\.
      2. Copie parcvue-agent.exe + config.ini depuis un partage reseau, UNIQUEMENT
         si la source est plus recente que la cible (deploiement de mise a jour).
      3. Installe le service Windows « ParcVueAgent » s'il n'existe pas encore.
      4. Demarre le service (et le redemarre si l'executable a ete mis a jour).

    Idempotence : relancer le script a chaque demarrage est sans effet de bord ;
    rien n'est recopie ni reinstalle si tout est deja a jour.

    L'agent tourne en compte SYSTEM (necessaire a l'inventaire complet et aux
    interventions a distance). Aucun port entrant n'est ouvert : l'agent SORT en
    HTTPS vers le serveur.

.NOTES
    - A adapter : la variable $SourceShare (partage reseau lisible par les postes).
    - Le partage doit etre accessible en lecture par « Ordinateurs du domaine »
      (le compte machine SYSTEM lit le partage, pas l'utilisateur).
    - Journalise dans C:\ProgramData\ParcVue\gpo-install.log.
#>

# Arrete le script a la premiere erreur non geree (robustesse).
$ErrorActionPreference = 'Stop'

# =============================================================================
# Parametres a adapter a votre environnement
# =============================================================================

# Partage reseau contenant l'executable et la config de reference.
# Exemple : \\srv-fichiers\Deploiement\ParcVue
$SourceShare = '\\srv-fichiers\Deploiement\ParcVue'

# Noms des fichiers sources sur le partage.
$SourceExeName    = 'parcvue-agent.exe'
$SourceConfigName = 'config.ini'

# Nom et libelle du service Windows.
$ServiceName        = 'ParcVueAgent'
$ServiceDisplayName = 'ParcVue Agent'
$ServiceDescription = 'Agent de supervision du parc ParcVue (inventaire, metriques, commandes a distance).'

# Repertoire d'installation local (prod : C:\ProgramData\ParcVue).
$InstallDir = Join-Path $env:ProgramData 'ParcVue'

# =============================================================================
# Initialisation & journalisation
# =============================================================================

# Cree le repertoire d'installation si absent (idempotent).
if (-not (Test-Path -LiteralPath $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}

$LogFile = Join-Path $InstallDir 'gpo-install.log'

function Write-Log {
    param([string]$Message)
    $line = ('{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
    # Ecrit dans le journal local ET sur la sortie standard (visible en GPO/debug).
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
    Write-Output $line
}

Write-Log '=== Demarrage du script de deploiement ParcVue ==='

# Chemins cibles locaux.
$TargetExe    = Join-Path $InstallDir 'parcvue-agent.exe'
$TargetConfig = Join-Path $InstallDir 'config.ini'

# Chemins sources sur le partage.
$SourceExe    = Join-Path $SourceShare $SourceExeName
$SourceConfig = Join-Path $SourceShare $SourceConfigName

# Indicateur : faut-il (re)demarrer le service car l'exe a change ?
$exeUpdated = $false

# =============================================================================
# Fonction utilitaire : copie « seulement si la source est plus recente »
# =============================================================================
function Copy-IfNewer {
    param(
        [Parameter(Mandatory)] [string]$Source,
        [Parameter(Mandatory)] [string]$Target
    )

    if (-not (Test-Path -LiteralPath $Source)) {
        throw "Fichier source introuvable : $Source"
    }

    $needCopy = $true
    if (Test-Path -LiteralPath $Target) {
        $srcTime = (Get-Item -LiteralPath $Source).LastWriteTimeUtc
        $dstTime = (Get-Item -LiteralPath $Target).LastWriteTimeUtc
        # On ne recopie que si la source est strictement plus recente.
        if ($srcTime -le $dstTime) {
            $needCopy = $false
        }
    }

    if ($needCopy) {
        Copy-Item -LiteralPath $Source -Destination $Target -Force
        Write-Log "Copie : $Source -> $Target"
        return $true
    }

    Write-Log "Inchange (deja a jour) : $Target"
    return $false
}

# =============================================================================
# 1) Copie de l'executable et de la configuration
# =============================================================================
try {
    # L'executable : si copie => il faudra redemarrer le service.
    if (Copy-IfNewer -Source $SourceExe -Target $TargetExe) {
        $exeUpdated = $true
    }

    # La config : copiee si plus recente. Ne force pas le redemarrage ici
    # (l'agent relit sa config au prochain cycle), mais on logge.
    Copy-IfNewer -Source $SourceConfig -Target $TargetConfig | Out-Null
}
catch {
    Write-Log "ERREUR lors de la copie : $($_.Exception.Message)"
    # Sans executable, inutile de poursuivre.
    if (-not (Test-Path -LiteralPath $TargetExe)) {
        Write-Log 'Abandon : aucun executable disponible localement.'
        exit 1
    }
    Write-Log 'Poursuite avec la version locale existante.'
}

# =============================================================================
# 2) Installation / verification du service Windows
# =============================================================================
$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue

if ($null -eq $service) {
    Write-Log "Service « $ServiceName » absent : installation en cours."

    # L'executable embarque un mode service (parcvue_agent.service via pywin32).
    # On l'enregistre en demarrage automatique, sous le compte LocalSystem.
    #
    # binPath inclut l'argument « run-service » attendu par le wrapper de service
    # de l'agent. Les guillemets autour du chemin gerent les espaces eventuels.
    $binPath = '"{0}" run-service' -f $TargetExe

    # Creation via sc.exe (disponible partout, pas de dependance NSSM ici).
    & sc.exe create $ServiceName binPath= $binPath start= auto DisplayName= "$ServiceDisplayName" obj= LocalSystem | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Log "ERREUR : creation du service echouee (code $LASTEXITCODE)."
        exit 1
    }

    # Description et redemarrage automatique en cas de plantage (resilience).
    & sc.exe description $ServiceName "$ServiceDescription" | Out-Null
    & sc.exe failure $ServiceName reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null

    Write-Log "Service « $ServiceName » installe (demarrage automatique, compte SYSTEM)."
}
else {
    Write-Log "Service « $ServiceName » deja installe (statut : $($service.Status))."
}

# =============================================================================
# 3) Demarrage / redemarrage du service
# =============================================================================
$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue

if ($null -ne $service) {
    if ($exeUpdated -and $service.Status -eq 'Running') {
        # L'executable a ete mis a jour : on redemarre pour charger la nouvelle version.
        Write-Log 'Executable mis a jour : redemarrage du service.'
        Restart-Service -Name $ServiceName -Force
    }
    elseif ($service.Status -ne 'Running') {
        Write-Log 'Service arrete : demarrage.'
        Start-Service -Name $ServiceName
    }
    else {
        Write-Log 'Service deja en cours d''execution : rien a faire.'
    }

    # Verification finale de l'etat.
    $service.Refresh()
    Write-Log "Etat final du service : $($service.Status)"
}
else {
    Write-Log 'ERREUR : le service est introuvable apres installation.'
    exit 1
}

Write-Log '=== Fin du script de deploiement ParcVue ==='
exit 0
