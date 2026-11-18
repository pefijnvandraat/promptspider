#ifndef AppVersion
  #error AppVersion must be supplied by build.ps1
#endif

[Setup]
AppId={{8540AA4A-557B-47BE-98A9-45A3B374220A}
AppName=Prompt Spider
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\Prompt Spider
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
AppMutex=Local\PromptSpiderDesktop-8540AA4A
UninstallDisplayIcon={app}\PromptSpider.exe
OutputBaseFilename=PromptSpider-Setup
WizardStyle=modern

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\Prompt Spider"; Filename: "{app}\PromptSpider.exe"

[Run]
Filename: "{app}\PromptSpider.exe"; Description: "Open Prompt Spider"; Flags: nowait postinstall skipifsilent
