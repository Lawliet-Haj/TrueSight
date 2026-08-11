# ============================================================================
# TrueSight - Signature Authenticode d'un binaire (agent ou installeur)
# ----------------------------------------------------------------------------
# Signe un .exe avec un certificat de signature de code, soit depuis le magasin
# de certificats (par empreinte), soit depuis un fichier .pfx.
#
# On utilise Set-AuthenticodeSignature (module PKI, INTEGRE a PowerShell) plutot
# que signtool.exe : cela evite d'exiger l'installation du Windows SDK (~2 Go)
# sur le poste de build.
#
# Utilisation :
#   .\sign.ps1 -Path "dist\truesight-agent\truesight-agent.exe" -CertThumbprint <empreinte>
#   .\sign.ps1 -Path "dist\Setup.exe" -PfxPath cert.pfx -PfxPassword (Read-Host -AsSecureString)
#
# L'HORODATAGE (-TimestampUrl) est important : sans lui, la signature devient
# invalide a l'expiration du certificat. Avec lui, elle reste valide au-dela.
#
# NB : ce fichier est volontairement en ASCII PUR. Lance via
# "powershell -File", un .ps1 UTF-8 sans BOM est lu en cp1252 par PowerShell
# 5.1 : les caracteres accentues cassent alors l'analyse du script (incident
# vecu avec postinstall.ps1).
# ============================================================================
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [string]$CertThumbprint,
    [string]$PfxPath,
    [System.Security.SecureString]$PfxPassword,
    # Horodateur RFC 3161 public et gratuit (DigiCert).
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $Path)) {
    Write-Host "Fichier a signer introuvable : $Path" -ForegroundColor Red
    exit 1
}

# --- 1. Resolution du certificat ---------------------------------------------
$cert = $null

if ($PfxPath) {
    if (-not (Test-Path $PfxPath)) {
        Write-Host "PFX introuvable : $PfxPath" -ForegroundColor Red
        exit 1
    }
    try {
        if ($PfxPassword) {
            $cert = Get-PfxCertificate -FilePath $PfxPath -Password $PfxPassword
        } else {
            # Sans mot de passe fourni, Get-PfxCertificate le demande de facon
            # interactive : a eviter en build non interactif.
            $cert = Get-PfxCertificate -FilePath $PfxPath
        }
    } catch {
        Write-Host "Lecture du PFX impossible : $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
} elseif ($CertThumbprint) {
    $tp = ($CertThumbprint -replace '\s', '').ToUpper()
    foreach ($store in @("Cert:\CurrentUser\My", "Cert:\LocalMachine\My")) {
        $found = Get-ChildItem $store -ErrorAction SilentlyContinue |
                 Where-Object { $_.Thumbprint -eq $tp }
        if ($found) { $cert = $found | Select-Object -First 1; break }
    }
    if (-not $cert) {
        Write-Host "Certificat d'empreinte $tp introuvable dans les magasins." -ForegroundColor Red
        Write-Host "Lister les certificats de signature :" -ForegroundColor Yellow
        Write-Host "  Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Select Subject,Thumbprint" -ForegroundColor Yellow
        exit 1
    }
} else {
    Write-Host "Fournir -CertThumbprint ou -PfxPath." -ForegroundColor Red
    exit 1
}

if (-not $cert.HasPrivateKey) {
    Write-Host "Ce certificat n'a pas de cle privee : signature impossible." -ForegroundColor Red
    exit 1
}

# --- 2. Signature -------------------------------------------------------------
Write-Host "Signature de $Path" -ForegroundColor Yellow
Write-Host ("  certificat : " + $cert.Subject) -ForegroundColor DarkGray

$signArgs = @{
    FilePath      = $Path
    Certificate   = $cert
    HashAlgorithm = "SHA256"
}
if ($TimestampUrl) { $signArgs["TimestampServer"] = $TimestampUrl }

try {
    $result = Set-AuthenticodeSignature @signArgs
} catch {
    Write-Host "Signature echouee : $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

if ($result.Status -ne "Valid") {
    # UnknownError est typique d'un certificat AUTO-SIGNE non installe dans les
    # autorites racines de confiance du poste de build : le fichier EST signe,
    # mais la chaine n'est pas verifiable ici. Non bloquant si le certificat est
    # distribue par GPO sur le parc.
    Write-Host ("Statut de signature : " + $result.Status + " - " + $result.StatusMessage) -ForegroundColor Yellow
    Write-Host "(Attendu avec un certificat auto-signe non approuve sur CE poste.)" -ForegroundColor DarkGray
} else {
    Write-Host "Signature valide (SHA256, horodatee)." -ForegroundColor Green
}

# Verification de ce qui est reellement attache au fichier.
$sig = Get-AuthenticodeSignature -FilePath $Path
if (-not $sig.SignerCertificate) {
    Write-Host "AUCUNE signature attachee : echec." -ForegroundColor Red
    exit 1
}
Write-Host ("Signataire : " + $sig.SignerCertificate.Subject) -ForegroundColor DarkGray
Write-Host "=== Fichier signe ===" -ForegroundColor Green
