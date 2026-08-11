# ============================================================================
# TrueSight - Creation d'un certificat de signature de code AUTO-SIGNE (interne)
# ----------------------------------------------------------------------------
# Objectif : signer l'agent et l'installeur pour que Windows affiche un EDITEUR
# IDENTIFIE au lieu de "Editeur inconnu", sans acheter de certificat.
#
# CE QU'IL FAIT / CE QU'IL NE FAIT PAS
#   - Sur un parc GERE : en deployant le .cer produit dans les magasins
#     "Editeurs approuves" + "Autorites racines de confiance" (par GPO),
#     l'agent devient un editeur reconnu sur vos postes. C'est la solution
#     gratuite adaptee a un parc interne.
#   - Hors de votre parc (poste inconnu, telechargement navigateur), un
#     certificat auto-signe n'apporte RIEN : seul un certificat OV/EV achete
#     aupres d'une autorite (~300-600 EUR/an, cle sur token materiel depuis
#     2023) supprime les avertissements partout.
#
# Utilisation (PowerShell) :
#   .\make-signing-cert.ps1
#   .\make-signing-cert.ps1 -Subject "Ma Societe" -Years 5 -ExportPfx
#
# Sortie : empreinte du certificat (a passer a build.ps1 -CertThumbprint) et
#          fichier .cer a deployer par GPO.
#
# NB : fichier en ASCII PUR (cf. sign.ps1) - un .ps1 UTF-8 sans BOM lance via
# "powershell -File" est lu en cp1252 par PowerShell 5.1 et ne parse plus.
# ============================================================================
param(
    [string]$Subject = "Medicofi / Tire-Lait Express",
    [int]$Years = 3,
    [string]$OutDir,
    # Exporte aussi un .pfx (cle privee) - utile pour signer depuis une autre
    # machine ou une CI. A proteger comme un secret.
    [switch]$ExportPfx
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
if (-not $OutDir) { $OutDir = Join-Path $scriptDir "signing" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

Write-Host "=== Certificat de signature de code (auto-signe) ===" -ForegroundColor Cyan
Write-Host "Sujet : CN=$Subject - validite : $Years an(s)" -ForegroundColor Yellow

# Type CodeSigningCert => EnhancedKeyUsage 1.3.6.1.5.5.7.3.3 (Code Signing).
# Sans ce type, le certificat ne peut PAS signer du code.
$cert = New-SelfSignedCertificate `
    -Subject "CN=$Subject" `
    -Type CodeSigningCert `
    -KeyAlgorithm RSA `
    -KeyLength 3072 `
    -HashAlgorithm SHA256 `
    -KeyExportPolicy Exportable `
    -CertStoreLocation "Cert:\CurrentUser\My" `
    -NotAfter (Get-Date).AddYears($Years)

$thumb = $cert.Thumbprint
Write-Host "Empreinte : $thumb" -ForegroundColor Green

# --- Export du certificat PUBLIC (.cer) : c'est CE fichier qu'on deploie -------
$cerPath = Join-Path $OutDir "truesight-codesign.cer"
Export-Certificate -Cert $cert -FilePath $cerPath -Type CERT | Out-Null
Write-Host "Certificat public : $cerPath" -ForegroundColor Green

if ($ExportPfx) {
    $pfxPath = Join-Path $OutDir "truesight-codesign.pfx"
    $pwd = Read-Host "Mot de passe pour proteger le .pfx" -AsSecureString
    Export-PfxCertificate -Cert $cert -FilePath $pfxPath -Password $pwd | Out-Null
    Write-Host "Cle privee (A PROTEGER) : $pfxPath" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "--- Etapes suivantes ---" -ForegroundColor Cyan
Write-Host "1. Signer les livrables :" -ForegroundColor Yellow
Write-Host ("     .\build.ps1 -CertThumbprint " + $thumb) -ForegroundColor White
Write-Host ("     .\build-installer.ps1 -Token JETON -CertThumbprint " + $thumb) -ForegroundColor White
Write-Host "2. Faire approuver l'editeur sur le parc (GPO) - cf. SIGNING.md :" -ForegroundColor Yellow
Write-Host ("     deployer " + $cerPath) -ForegroundColor White
Write-Host "     dans 'Editeurs approuves' ET 'Autorites racines de confiance'." -ForegroundColor White
Write-Host ""
Write-Host "NOTE : conservez ce certificat. Le regenerer changera l'editeur et" -ForegroundColor DarkGray
Write-Host "obligera a redeployer le .cer sur tout le parc." -ForegroundColor DarkGray
