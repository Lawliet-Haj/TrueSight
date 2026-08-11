# Signature de code de l'agent TrueSight

Sans signature, Windows présente l'installeur comme provenant d'un **« éditeur
inconnu »** : SmartScreen affiche un avertissement et l'invite UAC est rouge. Ce
document explique les deux voies possibles et comment signer.

---

## Choisir sa voie

| | Certificat **auto-signé** (gratuit) | Certificat **OV/EV** acheté |
|---|---|---|
| Coût | 0 € | ~300–600 €/an |
| Contrainte | déployer le certificat sur le parc (GPO) | clé sur **token matériel / HSM** (obligatoire depuis 2023) |
| Sur **vos** postes gérés | éditeur reconnu ✅ | éditeur reconnu ✅ |
| Sur un poste **hors parc** | aucun effet ❌ | reconnu partout ✅ |
| SmartScreen (fichier téléchargé) | inchangé | réputation acquise (immédiate en EV) |

**Recommandation pour un parc interne géré : le certificat auto-signé + GPO.**
Il supprime « éditeur inconnu » là où ça compte — vos postes — pour 0 €. Et comme
l'installeur est distribué par partage réseau / GPO / clé USB plutôt que par
téléchargement navigateur, la « marque de provenance » (MOTW) n'est pas apposée :
SmartScreen n'entre même pas en jeu.

Passez à un certificat **OV/EV** seulement si vous devez distribuer l'installeur
hors du parc (prestataires, postes non gérés).

---

## Voie 1 — Certificat auto-signé (parc interne)

### 1. Créer le certificat (une seule fois)

```powershell
cd <dépôt>\agent
powershell -ExecutionPolicy Bypass -File .\make-signing-cert.ps1
```

Le script affiche l'**empreinte** (thumbprint) et écrit le certificat public dans
`agent\signing\truesight-codesign.cer`.

> ⚠️ **Conservez ce certificat.** Le régénérer change l'identité de l'éditeur et
> oblige à redéployer le `.cer` sur tout le parc. La clé privée reste dans votre
> magasin utilisateur (`Cert:\CurrentUser\My`) ; ajoutez `-ExportPfx` si vous
> devez signer depuis une autre machine.

### 2. Signer les livrables

```powershell
.\build.ps1 -CertThumbprint <empreinte>
.\build-installer.ps1 -Token <jeton> -CertThumbprint <empreinte>
```

La signature est appliquée **avant** l'empaquetage : le `.zip` d'auto-update et le
`setup.exe` portent tous deux un binaire signé et horodaté (SHA-256).

> Le script signale `Statut : UnknownError` si le certificat auto-signé n'est pas
> approuvé **sur le poste de build** : c'est attendu, le fichier est bien signé.
> Il vérifie ensuite qu'une signature est réellement attachée.

### 3. Faire approuver l'éditeur sur le parc (GPO)

Déployez `truesight-codesign.cer` dans **deux** magasins de l'ordinateur :

1. **Autorités de certification racines de confiance** — pour que la chaîne soit
   valide (un certificat auto-signé est sa propre racine) ;
2. **Éditeurs approuvés** — pour que l'éditeur soit reconnu.

Console **Gestion de stratégie de groupe** → votre GPO → 
`Configuration ordinateur` → `Paramètres Windows` → `Paramètres de sécurité` →
`Stratégies de clé publique` → clic droit sur le magasin → **Importer**.

Vérification sur un poste après application (`gpupdate /force`) :

```powershell
Get-ChildItem Cert:\LocalMachine\Root, Cert:\LocalMachine\TrustedPublisher |
  Where-Object { $_.Subject -like "*Tire-Lait*" } | Select-Object Subject, Thumbprint
```

### 4. Contrôler qu'un binaire est bien signé

```powershell
Get-AuthenticodeSignature .\dist\TrueSightAgent-Setup-<version>.exe |
  Select-Object Status, StatusMessage, SignerCertificate
```

`Status = Valid` sur un poste où le certificat est approuvé.

---

## Voie 2 — Certificat OV/EV acheté

Fournisseurs courants : DigiCert, Sectigo, GlobalSign, SSL.com. Depuis juin 2023,
la clé privée doit résider sur un **token matériel** ou un **HSM** (y compris les
offres cloud type *Azure Trusted Signing*, souvent la plus simple).

Une fois le certificat installé, **rien ne change dans les scripts** :

```powershell
# certificat sur token/magasin
.\build-installer.ps1 -Token <jeton> -CertThumbprint <empreinte>

# ou depuis un .pfx
.\build-installer.ps1 -Token <jeton> -PfxPath cert.pfx -PfxPassword (Read-Host -AsSecureString)
```

> Si votre autorité impose son propre outil (token PKCS#11, `signtool` avec un
> CSP dédié), signez `dist\truesight-agent\truesight-agent.exe` **puis**
> `dist\TrueSightAgent-Setup-<version>.exe` avec cet outil — l'ordre est le même
> que celui des scripts (agent d'abord, installeur ensuite).

---

## Horodatage

Les scripts horodatent via `http://timestamp.digicert.com` (gratuit, RFC 3161).
C'est **important** : sans horodatage, la signature devient invalide à
l'expiration du certificat ; avec, elle reste valide au-delà. Pour changer
d'horodateur : `-TimestampUrl <url>`.
