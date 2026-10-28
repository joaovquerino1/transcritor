; Instalador Windows (Inno Setup 6). Compile com:
;   iscc /DAppVersion=1.0.0 packaging\windows\installer.iss
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6F1C2B7E-3A9D-4C5E-9B1F-7D2A8E4C6B10}
AppName=Transcritor
AppVersion={#AppVersion}
AppPublisher=Transcritor
DefaultDirName={autopf}\Transcritor
DefaultGroupName=Transcritor
DisableProgramGroupPage=yes
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\..\dist
OutputBaseFilename=Transcritor-{#AppVersion}-Windows-x64-Setup
SetupIconFile=..\..\assets\icon.ico
UninstallDisplayIcon={app}\Transcritor.exe
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\..\dist\Transcritor\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Transcritor"; Filename: "{app}\Transcritor.exe"
Name: "{group}\{cm:UninstallProgram,Transcritor}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Transcritor"; Filename: "{app}\Transcritor.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Transcritor.exe"; Description: "{cm:LaunchProgram,Transcritor}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Os modelos baixados (~2 GB) ficam em %LOCALAPPDATA%\Transcritor e são removidos aqui.
Type: filesandordirs; Name: "{localappdata}\Transcritor"
