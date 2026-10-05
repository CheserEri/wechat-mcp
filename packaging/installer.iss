; 灵语（wechat-mcp）Windows 安装脚本 —— Inno Setup 6。
;
; 编译（仓库根目录，或用 packaging/build.py --installer 自动调用）：
;
;     "F:\SoftWare\Inno Setup 6\ISCC.exe" packaging\installer.iss
;
; 产物：dist\wechat-mcp-setup-x64.exe
;
; 设计取舍：
; * **按用户安装**（PrivilegesRequired=lowest + {localappdata}）：不弹 UAC，
;   且安装目录可写——程序会在 exe 同级写 logs\，装到 Program Files 反而会失败。
; * 安装程序自己写文件，**不会**带上「Internet 区域」标记（该标记只由浏览器
;   下载/资源管理器解压产生），从根上避免 pythonnet 被 .NET 拒绝加载而黑屏。

#ifndef AppVersion
  #define AppVersion "0.8.1"
#endif

#define AppName "灵语"
#define AppExe "wechat-mcp.exe"
#define SourceDir "..\dist\wechat-mcp"

[Setup]
; 应用唯一标识（升级/卸载靠它匹配，不要随意改）。
AppId={{7F3A9C2E-4B18-4D6A-9E5F-1C8B0A6D3E72}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=wechat-mcp
DefaultDirName={localappdata}\Programs\wechat-mcp
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=wechat-mcp-setup-x64
SetupIconFile=wechat-mcp.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
LicenseFile=..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

[Languages]
Name: "chinese"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 随附的许可证与第三方声明，便于合规查阅。
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--gui"; Comment: "启动灵语"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--gui"; Comment: "启动灵语"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "--gui"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
