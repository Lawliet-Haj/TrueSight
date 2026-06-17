; ============================================================================
; TrueSight Agent - Installeur Windows (Inno Setup 6)
; ----------------------------------------------------------------------------
; Emballe le dossier onedir (dist\truesight-agent\) dans un seul setup.exe :
;   - installe dans C:\Program Files\TrueSight (droits hérités = Utilisateurs R+X,
;     requis pour relancer le helper/compagnon en session utilisateur) ;
;   - assistant : URL serveur + jeton d'enrôlement ;
;   - post-install : config.ini (ProgramData restreint) + service SYSTEM +
;     reprise sur échec + tâche compagnon + démarrage (postinstall.ps1) ;
;   - désinstalleur : arrête/supprime service + tâche (preuninstall.ps1).
;
; Mode SILENCIEUX (parc / GPO / Intune) :
;   setup.exe /VERYSILENT /SUPPRESSMSGBOXES /SERVERURL=https://srv778935.hstgr.cloud /TOKEN=<jeton> [/VERIFYTLS=true]
;
; Compilation : build-installer.ps1 (passe /DAppVersion=<version>) ou
;   ISCC.exe /DAppVersion=1.0.0 installer\truesight.iss
; ============================================================================

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{A2F4C1E8-7B3D-4E9A-9C21-5D6F8B0A1E23}
AppName=TrueSight Agent
AppVersion={#AppVersion}
AppPublisher=Medicofi / Tire-Lait Express
AppPublisherURL=https://srv778935.hstgr.cloud
DefaultDirName={autopf}\TrueSight
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=TrueSightAgent-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=TrueSight Agent {#AppVersion}
UninstallDisplayIcon={app}\truesight-agent.exe
VersionInfoVersion={#AppVersion}
VersionInfoCompany=Medicofi / Tire-Lait Express
VersionInfoDescription=Agent de supervision TrueSight

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Files]
; Dossier onedir complet (exe + _internal\ + companion.vbs généré au runtime).
Source: "..\dist\truesight-agent\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; Scripts de cycle de vie (configuration système au post-install / désinstall).
Source: "postinstall.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "preuninstall.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Run]
; Post-installation : configuration + service + compagnon + démarrage.
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\postinstall.ps1"" -AppDir ""{app}"" -ServerUrl ""{code:GetServerUrl}"" -Token ""{code:GetToken}"" -VerifyTls ""{code:GetVerifyTls}"""; \
  StatusMsg: "Configuration du service TrueSight..."; \
  Flags: runhidden waituntilterminated

[UninstallRun]
; Arrêt/suppression du service + tâche avant le retrait des fichiers.
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\preuninstall.ps1"""; \
  RunOnceId: "RemoveTrueSightAgent"; \
  Flags: runhidden waituntilterminated

[Code]
var
  ServerPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  { Page de saisie : URL serveur + jeton. Pré-remplie depuis les paramètres de
    ligne de commande (/SERVERURL= /TOKEN=) — ce qui rend le mode silencieux
    fonctionnel sans afficher la page. }
  ServerPage := CreateInputQueryPage(wpWelcome,
    'Configuration de l''agent',
    'Connexion au serveur TrueSight',
    'Renseignez l''URL du serveur et le jeton d''enrôlement fournis par l''administrateur.');
  ServerPage.Add('URL du serveur (https://...)', False);
  ServerPage.Add('Jeton d''enrôlement', False);
  ServerPage.Values[0] := ExpandConstant('{param:SERVERURL|https://srv778935.hstgr.cloud}');
  ServerPage.Values[1] := ExpandConstant('{param:TOKEN|}');
end;

function GetServerUrl(Param: String): String;
begin
  Result := Trim(ServerPage.Values[0]);
end;

function GetToken(Param: String): String;
begin
  Result := Trim(ServerPage.Values[1]);
end;

function GetVerifyTls(Param: String): String;
begin
  Result := ExpandConstant('{param:VERIFYTLS|true}');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = ServerPage.ID then
  begin
    if Trim(ServerPage.Values[0]) = '' then
    begin
      MsgBox('Veuillez indiquer l''URL du serveur.', mbError, MB_OK);
      Result := False;
    end
    { Le jeton n'est exigé que sur une machine VIERGE : si un config.ini existe
      déjà (réinstallation / mise à jour), on peut laisser le jeton vide et
      conserver la configuration en place. }
    else if (Trim(ServerPage.Values[1]) = '') and
            (not FileExists(ExpandConstant('{commonappdata}\TrueSight\config.ini'))) then
    begin
      MsgBox('Veuillez indiquer le jeton d''enrôlement (Réglages > Déploiement sur le dashboard),'
        + #13#10 + 'ou laissez vide si l''agent est déjà configuré sur ce poste.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure StopExistingAgent();
var
  rc: Integer;
begin
  { Libère les fichiers verrouillés par une installation précédente avant la copie. }
  Exec('sc.exe', 'stop TrueSightAgent', '', SW_HIDE, ewWaitUntilTerminated, rc);
  Sleep(2000);
  Exec('schtasks.exe', '/End /TN "TrueSight Companion"', '', SW_HIDE, ewWaitUntilTerminated, rc);
  Exec('taskkill.exe', '/F /IM truesight-agent.exe', '', SW_HIDE, ewWaitUntilTerminated, rc);
  Sleep(1000);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopExistingAgent();
  Result := '';
end;
