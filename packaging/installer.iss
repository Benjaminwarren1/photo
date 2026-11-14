; Inno Setup recipe for the Windows installer (https://jrsoftware.org/isinfo.php, free).
; Built by CI:  iscc /DAppVersion=0.2.0 /DSourceDir=dist\PhotoRecovery /DOutputDir=. packaging\installer.iss
;
; Installs for the current person only (no administrator prompt to install), into
; %LOCALAPPDATA%\Programs\Photo Recovery, with a Start menu entry, a desktop icon and an entry in
; Settings > Apps so it can be uninstalled like any other program. Rescued photos are never stored in the
; program's folder, so uninstalling never touches them.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef AppNumVersion
  #define AppNumVersion "0.0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\PhotoRecovery"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
AppId={{7C1E9A52-3F64-4C8B-9E1D-5B2A7F0C4D11}
AppName=Photo Recovery
AppVersion={#AppVersion}
AppVerName=Photo Recovery {#AppVersion}
AppPublisher=Photo Recovery (open source)
AppPublisherURL=https://github.com/Benjaminwarren1/photo
AppSupportURL=https://github.com/Benjaminwarren1/photo/issues
AppUpdatesURL=https://github.com/Benjaminwarren1/photo/releases
AppCopyright=GPL-3.0-or-later
VersionInfoVersion={#AppNumVersion}
VersionInfoDescription=Photo Recovery installer
DefaultDirName={localappdata}\Programs\Photo Recovery
DefaultGroupName=Photo Recovery
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=PhotoRecovery-{#AppVersion}-setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\PhotoRecovery.exe
UninstallDisplayName=Photo Recovery
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
Name: "desktopicon"; Description: "Put a Photo Recovery icon on the desktop"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Photo Recovery"; Filename: "{app}\PhotoRecovery.exe"; Comment: "Get deleted photos back from a memory card"
Name: "{autodesktop}\Photo Recovery"; Filename: "{app}\PhotoRecovery.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PhotoRecovery.exe"; Description: "Open Photo Recovery now"; Flags: nowait postinstall skipifsilent
