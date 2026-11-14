; Inno Setup recipe for the Windows installer (https://jrsoftware.org/isinfo.php, free).
; Built by CI:  iscc /DAppVersion=0.2.0 /DSourceDir=dist\PhotoLifeboat /DOutputDir=. packaging\installer.iss
;
; Installs for the current person only (no administrator prompt to install), into
; %LOCALAPPDATA%\Programs\Photo Lifeboat, with a Start menu entry, a desktop icon and an entry in
; Settings > Apps so it can be uninstalled like any other program. Rescued photos are never stored in the
; program's folder, so uninstalling never touches them.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef AppNumVersion
  #define AppNumVersion "0.0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\PhotoLifeboat"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
AppId={{4ED649FC-B333-40B2-BA65-363A3C6CC272}
AppName=Photo Lifeboat
AppVersion={#AppVersion}
AppVerName=Photo Lifeboat {#AppVersion}
AppPublisher=Photo Lifeboat (open source)
AppPublisherURL=https://github.com/Benjaminwarren1/photo
AppSupportURL=https://github.com/Benjaminwarren1/photo/issues
AppUpdatesURL=https://github.com/Benjaminwarren1/photo/releases
AppCopyright=GPL-3.0-or-later
VersionInfoVersion={#AppNumVersion}
VersionInfoDescription=Photo Lifeboat installer
DefaultDirName={localappdata}\Programs\Photo Lifeboat
DefaultGroupName=Photo Lifeboat
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=PhotoLifeboat-{#AppVersion}-setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\PhotoLifeboat.exe
UninstallDisplayName=Photo Lifeboat
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Put a Photo Lifeboat icon on the desktop"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Photo Lifeboat"; Filename: "{app}\PhotoLifeboat.exe"; Comment: "Get deleted photos back from a memory card"
Name: "{autodesktop}\Photo Lifeboat"; Filename: "{app}\PhotoLifeboat.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PhotoLifeboat.exe"; Description: "Open Photo Lifeboat now"; Flags: nowait postinstall skipifsilent
