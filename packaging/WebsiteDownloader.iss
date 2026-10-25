; Inno Setup script for Website Downloader (phase 8).
; Build the exe first (packaging\build_windows.ps1), then compile this script
; with Inno Setup 6:  ISCC.exe packaging\WebsiteDownloader.iss
; Playwright browsers are deliberately NOT bundled — the app prompts the
; user to run `playwright install chromium` once if they use headless mode.

#define MyAppName "Website Downloader"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "Website Downloader project"
#define MyAppExeName "WebsiteDownloader.exe"

[Setup]
AppId={{8E7B6C4A-52D1-4C1B-9A63-WEBSITE-DOWNLOADER1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=..\dist\installer
OutputBaseFilename=WebsiteDownloader-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
PrivilegesRequired=lowest
; unsigned builds trigger SmartScreen warnings — code-sign before wide release

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "quicklaunchicon"; Description: "{cm:CreateQuickLaunchIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: quicklaunchicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; user project folders (Documents\WebsiteDownloader\projects) are NOT removed
Type: filesandordirs; Name: "{app}"
