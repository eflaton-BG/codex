param([switch]$Undo)

$ErrorActionPreference = 'Stop'
$ManifestUrl = 'https://agents-gateway.berkshiregrey.com/ai-gateway/install/bga-connections/manifest.json'
$ConfigDir = if ($env:BGA_CODEX_CONFIG_DIR) {
    $env:BGA_CODEX_CONFIG_DIR
} else {
    Join-Path $env:ProgramData 'OpenAI\Codex'
}
$EnvHome = if ($env:BGA_CODEX_ENV_HOME) {
    $env:BGA_CODEX_ENV_HOME
} else {
    [Environment]::GetFolderPath('UserProfile')
}
$CodexHome = if ($env:CODEX_HOME) {
    $env:CODEX_HOME
} else {
    Join-Path $EnvHome '.codex'
}
$SkillDir = Join-Path $CodexHome 'skills\bga-connections'

function ConvertTo-EmbeddedValue {
    param([string]$Value)

    [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Value))
}

function New-MachineConfigHelper {
    param(
        [string]$GatewayBaseUrl,
        [string]$ResultPath,
        [switch]$Undo
    )

    $undoValue = if ($Undo) { '$true' } else { '$false' }
    $template = @'
$ErrorActionPreference = 'Stop'
$ConfigDir = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String('__CONFIG_DIR__')
)
$GatewayBaseUrl = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String('__GATEWAY_BASE_URL__')
)
$ResultPath = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String('__RESULT_PATH__')
)
$Undo = __UNDO__
$Config = Join-Path $ConfigDir 'config.toml'
$Backup = "$Config.bga-backup"

try {
    if ($Undo) {
        if (
            (Test-Path $Config) -and
            ((Get-Content $Config -Raw) -match 'BG Agents AI Gateway managed config')
        ) {
            Remove-Item $Config -Force
        }
        if (-not (Test-Path $Config) -and (Test-Path $Backup)) {
            Move-Item $Backup $Config
        }
    } else {
        New-Item $ConfigDir -ItemType Directory -Force | Out-Null
        if (
            (Test-Path $Config) -and
            -not ((Get-Content $Config -Raw) -match 'BG Agents AI Gateway managed config')
        ) {
            if (Test-Path $Backup) {
                throw "Refusing to replace $Config because $Backup already exists."
            }
            Copy-Item $Config $Backup
        }

        @(
            '# BG Agents AI Gateway managed config'
            'model_provider = "bg_ai_gateway"'
            ''
            '[model_providers.bg_ai_gateway]'
            'name = "BG AI Gateway"'
            ('base_url = "{0}/codex/v1"' -f $GatewayBaseUrl.TrimEnd('/'))
            'wire_api = "responses"'
            'env_key = "BG_AI_GATEWAY_API_KEY"'
            'supports_websockets = true'
        ) | Set-Content $Config -Encoding utf8
    }

    Set-Content $ResultPath 'OK' -Encoding utf8
} catch {
    Set-Content $ResultPath ("ERROR`n" + ($_ | Out-String)) -Encoding utf8
    exit 1
}
'@
    $helper = $template.Replace(
        '__CONFIG_DIR__',
        (ConvertTo-EmbeddedValue $ConfigDir)
    )
    $helper = $helper.Replace(
        '__GATEWAY_BASE_URL__',
        (ConvertTo-EmbeddedValue $GatewayBaseUrl)
    )
    $helper = $helper.Replace(
        '__RESULT_PATH__',
        (ConvertTo-EmbeddedValue $ResultPath)
    )
    $helper = $helper.Replace('__UNDO__', $undoValue)
    $helper
}

function Invoke-MachineConfigChange {
    param(
        [string]$GatewayBaseUrl = '',
        [switch]$Undo
    )

    $helperRoot = Join-Path (
        [IO.Path]::GetTempPath()
    ) ('bga-machine-config-' + [Guid]::NewGuid().ToString('N'))
    $resultPath = Join-Path $helperRoot 'result.txt'
    New-Item $helperRoot -ItemType Directory -Force | Out-Null

    try {
        $icaclsPath = Join-Path $env:SystemRoot 'System32\icacls.exe'
        if (Test-Path $icaclsPath) {
            & $icaclsPath `
                $helperRoot `
                '/grant' `
                '*S-1-5-32-544:(OI)(CI)F' `
                '/Q' | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw 'Unable to prepare the machine configuration handoff.'
            }
        }

        $helper = New-MachineConfigHelper `
            -GatewayBaseUrl $GatewayBaseUrl `
            -ResultPath $resultPath `
            -Undo:$Undo
        $encodedHelper = [Convert]::ToBase64String(
            [Text.Encoding]::Unicode.GetBytes($helper)
        )

        $powerShellPath = Join-Path $env:SystemRoot (
            'System32\WindowsPowerShell\v1.0\powershell.exe'
        )
        if (-not (Test-Path $powerShellPath)) {
            $powerShellPath = 'powershell.exe'
        }
        $arguments = @(
            '-NoProfile'
            '-ExecutionPolicy'
            'Bypass'
            '-EncodedCommand'
            $encodedHelper
        )
        if ($env:BGA_CODEX_CONFIG_DIR) {
            $process = Start-Process `
                -FilePath $powerShellPath `
                -Wait `
                -PassThru `
                -ArgumentList $arguments
        } else {
            $process = Start-Process `
                -FilePath $powerShellPath `
                -Verb RunAs `
                -Wait `
                -PassThru `
                -ArgumentList $arguments
        }
        $result = if (Test-Path $resultPath) {
            Get-Content $resultPath -Raw
        } else {
            ''
        }
        if ($process.ExitCode -ne 0 -or -not $result.StartsWith('OK')) {
            if ($result.StartsWith('ERROR')) {
                throw $result.Substring(5).Trim()
            }
            throw (
                'Machine configuration update failed with exit code ' +
                "$($process.ExitCode) without returning details. " +
                'Windows application control or elevation access may have blocked it.'
            )
        }
    } finally {
        Remove-Item $helperRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Find-CodexCommand {
    $command = Get-Command codex.cmd -CommandType Application -ErrorAction SilentlyContinue
    if (-not $command) {
        $command = Get-Command codex.exe -CommandType Application -ErrorAction SilentlyContinue
    }
    if (-not $command) {
        $command = Get-Command codex -CommandType Application -ErrorAction SilentlyContinue
    }
    $command
}

function Read-ApiKey {
    $apiKey = $env:BG_AI_GATEWAY_API_KEY
    if (-not $apiKey) {
        $apiKey = [Environment]::GetEnvironmentVariable(
            'BG_AI_GATEWAY_API_KEY',
            [EnvironmentVariableTarget]::User
        )
    }
    if (
        $apiKey -and
        ((Read-Host 'Keep the existing BG AI Gateway API key? [Y/n]') -match '^(n|no)$')
    ) {
        $apiKey = ''
    }
    if (-not $apiKey) {
        $secureKey = Read-Host 'BG AI Gateway API key' -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
        try {
            $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
    }
    if ([string]::IsNullOrWhiteSpace($apiKey)) {
        throw 'BG AI Gateway API key is required.'
    }
    $apiKey
}

function Remove-Install {
    Invoke-MachineConfigChange -Undo
    Remove-Item $SkillDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item "$SkillDir.backup" -Recurse -Force -ErrorAction SilentlyContinue

    [Environment]::SetEnvironmentVariable(
        'BG_AI_GATEWAY_API_KEY',
        $null,
        [EnvironmentVariableTarget]::User
    )
    [Environment]::SetEnvironmentVariable(
        'BG_AI_GATEWAY_BASE_URL',
        $null,
        [EnvironmentVariableTarget]::User
    )
    Remove-Item Env:BG_AI_GATEWAY_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:BG_AI_GATEWAY_BASE_URL -ErrorAction SilentlyContinue

    $codex = Find-CodexCommand
    if ($codex) {
        & $codex.Source logout
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'Codex logout did not complete.'
        }
    }

    Write-Host 'Removed BG AI Gateway machine config, skill, environment, and Codex login.'
    Write-Host 'Fully exit all Codex and terminal windows before testing a clean install.'
}

if ($Undo) {
    Remove-Install
    exit 0
}

$manifest = Invoke-RestMethod -Uri $ManifestUrl
if (-not $manifest.version -or -not $manifest.sha256 -or -not $manifest.gatewayBaseUrl) {
    throw 'Invalid BG AI Gateway package manifest.'
}

$apiKey = Read-ApiKey
$gatewayBaseUrl = ([string]$manifest.gatewayBaseUrl).TrimEnd('/')
$temp = Join-Path (
    [IO.Path]::GetTempPath()
) ('bga-connections-' + [Guid]::NewGuid().ToString('N'))
$zip = Join-Path $temp 'package.zip'
$stage = Join-Path $temp 'stage'
$backup = "$SkillDir.backup"
New-Item $temp -ItemType Directory -Force | Out-Null

try {
    $packageUrl = ($ManifestUrl -replace '/manifest.json$', '') +
        "/versions/$($manifest.version)/package.zip"
    Invoke-WebRequest -Uri $packageUrl -OutFile $zip

    $actualHash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLowerInvariant()
    $expectedHash = ([string]$manifest.sha256).ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw 'BG AI Gateway package checksum mismatch.'
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($zip)
    try {
        foreach ($entry in $archive.Entries) {
            if (
                $entry.FullName -match '(^|/)\.\.(/|$)' -or
                $entry.FullName.StartsWith('/')
            ) {
                throw 'Unsafe BG AI Gateway package path.'
            }
        }
    } finally {
        $archive.Dispose()
    }

    Expand-Archive $zip -DestinationPath $stage -Force
    $source = Join-Path $stage 'bga-connections'
    foreach ($required in @('SKILL.md', 'bga-connections.py', 'package.json')) {
        if (-not (Test-Path (Join-Path $source $required))) {
            throw 'BG AI Gateway package is incomplete.'
        }
    }

    New-Item (Split-Path $SkillDir) -ItemType Directory -Force | Out-Null
    Remove-Item $backup -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $SkillDir) {
        Move-Item $SkillDir $backup
    }
    try {
        Copy-Item $source $SkillDir -Recurse
        Invoke-MachineConfigChange -GatewayBaseUrl $gatewayBaseUrl
    } catch {
        Remove-Item $SkillDir -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path $backup) {
            Move-Item $backup $SkillDir
        }
        throw
    }

    [Environment]::SetEnvironmentVariable(
        'BG_AI_GATEWAY_API_KEY',
        $apiKey,
        [EnvironmentVariableTarget]::User
    )
    [Environment]::SetEnvironmentVariable(
        'BG_AI_GATEWAY_BASE_URL',
        $gatewayBaseUrl,
        [EnvironmentVariableTarget]::User
    )
    $env:BG_AI_GATEWAY_API_KEY = $apiKey
    $env:BG_AI_GATEWAY_BASE_URL = $gatewayBaseUrl

    $codex = Find-CodexCommand
    if ($codex) {
        $apiKey | & $codex.Source login --with-api-key
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'Codex API-key login did not complete.'
        }
    } else {
        Write-Warning 'Codex CLI was not found; API-key login was skipped.'
    }

    Remove-Item $backup -Recurse -Force -ErrorAction SilentlyContinue
} finally {
    Remove-Item $temp -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "Installed BG AI Gateway package $($manifest.version)."
Write-Host 'Fully exit all Codex and terminal windows, then relaunch them.'
