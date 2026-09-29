param([switch]$Undo)

$ErrorActionPreference = 'Stop'
$ManifestUrl = '{{MANIFEST_URL}}'
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
$ConfigDir = if ($env:BGA_CODEX_CONFIG_DIR) {
    $env:BGA_CODEX_CONFIG_DIR
} else {
    $CodexHome
}
$MachineConfigDir = if ($env:BGA_CODEX_CONFIG_DIR) {
    $env:BGA_CODEX_CONFIG_DIR
} else {
    Join-Path $env:ProgramData 'OpenAI\Codex'
}
$SkillDir = Join-Path $CodexHome 'skills\bga-connections'

function ConvertTo-EmbeddedValue {
    param([string]$Value)

    [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Value))
}

function Get-CodexAuthCommand {
    '$value = [Environment]::GetEnvironmentVariable(''BG_AI_GATEWAY_API_KEY'', [EnvironmentVariableTarget]::User); if (-not $value) { $value = [Environment]::GetEnvironmentVariable(''BG_AI_GATEWAY_API_KEY'') }; [Console]::Out.Write($value)'
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
$MachineConfigDir = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String('__MACHINE_CONFIG_DIR__')
)
$GatewayBaseUrl = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String('__GATEWAY_BASE_URL__')
)
$ResultPath = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String('__RESULT_PATH__')
)
$CodexAuthCommand = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String('__CODEX_AUTH_COMMAND__')
)
$Undo = __UNDO__
$DeprecatedManagedConfig = Join-Path $ConfigDir 'managed_config.toml'
$MachineConfig = Join-Path $MachineConfigDir 'config.toml'

function Remove-BGManagedConfig {
    param([string]$Config)

    $Backup = "$Config.bga-backup"
    if (
        (Test-Path $Config) -and
        ((Get-Content $Config -Raw) -match 'BG Agents AI Gateway managed config')
    ) {
        Remove-Item $Config -Force
    }
    if (-not (Test-Path $Config) -and (Test-Path $Backup)) {
        Move-Item $Backup $Config
    }
}

function Set-BGManagedConfig {
    param([string]$Config)

    $Backup = "$Config.bga-backup"
    New-Item (Split-Path $Config) -ItemType Directory -Force | Out-Null
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
        'supports_websockets = false'
        ''
        '[model_providers.bg_ai_gateway.auth]'
        'command = "powershell.exe"'
        ('args = ["-NoProfile", "-NonInteractive", "-Command", "{0}"]' -f $CodexAuthCommand)
    ) | Set-Content $Config -Encoding utf8
}

try {
    if ($Undo) {
        Remove-BGManagedConfig -Config $DeprecatedManagedConfig
        Remove-BGManagedConfig -Config $MachineConfig
    } else {
        # Native Windows Codex loads ProgramData/OpenAI/Codex/config.toml.
        # CODEX_HOME/managed_config.toml is deprecated in current clients.
        Set-BGManagedConfig -Config $MachineConfig
        if (
            (Test-Path $DeprecatedManagedConfig) -and
            ((Get-Content $DeprecatedManagedConfig -Raw) -match '^# BG Agents AI Gateway managed config\r?\n')
        ) {
            Remove-Item $DeprecatedManagedConfig -Force
        }
        # Preserve any original .bga-backup for uninstall; do not restore an
        # unsupported file during installation or alter an unowned file.
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
        '__MACHINE_CONFIG_DIR__',
        (ConvertTo-EmbeddedValue $MachineConfigDir)
    )
    $helper = $helper.Replace(
        '__GATEWAY_BASE_URL__',
        (ConvertTo-EmbeddedValue $GatewayBaseUrl)
    )
    $helper = $helper.Replace(
        '__RESULT_PATH__',
        (ConvertTo-EmbeddedValue $ResultPath)
    )
    $helper = $helper.Replace(
        '__CODEX_AUTH_COMMAND__',
        (ConvertTo-EmbeddedValue (Get-CodexAuthCommand))
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
        $tokens = $null
        $parseErrors = $null
        [Management.Automation.Language.Parser]::ParseInput(
            $helper,
            [ref]$tokens,
            [ref]$parseErrors
        ) | Out-Null
        if (@($parseErrors).Count -gt 0) {
            throw 'Generated machine configuration helper is invalid.'
        }
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
        Write-Host 'Copy the BG AI Gateway API key to the Windows clipboard.'
        [void](Read-Host 'Press Enter after copying the API key')
        try {
            $apiKey = Get-Clipboard -Raw -ErrorAction Stop
        } catch {
            throw 'Unable to read the BG AI Gateway API key from the Windows clipboard. Copy the key, then try again.'
        }
    }
    $apiKey = $apiKey.Trim()
    if ([string]::IsNullOrWhiteSpace($apiKey)) {
        throw 'The Windows clipboard does not contain a BG AI Gateway API key. Copy the key, then try again.'
    }
    if ($apiKey.Length -lt 20) {
        throw 'The BG AI Gateway API key on the Windows clipboard appears incomplete. Copy the complete key, then try again.'
    }
    $apiKey
}

function Test-ApiKey {
    param(
        [string]$GatewayBaseUrl,
        [string]$ApiKey
    )

    try {
        Invoke-RestMethod `
            -Uri ($GatewayBaseUrl.TrimEnd('/') + '/v1/models') `
            -Headers @{ Authorization = "Bearer $ApiKey" } `
            -TimeoutSec 15 | Out-Null
    } catch {
        $statusCode = $null
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            $statusCode = [int]$_.Exception.Response.StatusCode
        }
        if ($statusCode -eq 401 -or $statusCode -eq 403) {
            throw 'BG AI Gateway rejected the API key. Copy a current API key from BG Agents, then try again.'
        }
        throw 'Unable to reach BG AI Gateway while validating the API key. Check the company network or VPN, then try again.'
    }
}

function Test-CodexAuthCommand {
    param([string]$ExpectedApiKey)

    $resolvedApiKey = & powershell.exe `
        -NoProfile `
        -NonInteractive `
        -Command (Get-CodexAuthCommand)
    if ($LASTEXITCODE -ne 0 -or $resolvedApiKey -ne $ExpectedApiKey) {
        throw 'Codex authentication validation failed. The API key was saved but could not be read by Codex.'
    }
}

function Test-CodexGatewayRouting {
    param(
        [System.Management.Automation.CommandInfo]$Codex,
        [string]$GatewayBaseUrl
    )

    $doctorOutput = (& $Codex.Source doctor --json 2>$null) | Out-String
    try {
        $doctor = $doctorOutput | ConvertFrom-Json
    } catch {
        throw 'Codex gateway routing validation failed. Update Codex CLI, then rerun the installer.'
    }

    $configCheck = $doctor.checks.'config.load'
    if (
        -not $configCheck -or
        $configCheck.status -notin @('ok', 'warning') -or
        $configCheck.details.'config.toml parse' -ne 'ok'
    ) {
        throw 'Codex gateway routing validation failed. Codex configuration could not be validated. Run codex.cmd doctor --json for details.'
    }
    if ($configCheck.details.'model provider' -ne 'bg_ai_gateway') {
        throw 'Codex gateway routing validation failed. Codex did not load the BG AI Gateway provider.'
    }

    $networkCheck = $doctor.checks.'network.provider_reachability'
    if (-not $networkCheck -or $networkCheck.status -ne 'ok') {
        throw 'Codex gateway routing validation failed. The BG AI Gateway provider endpoint is not reachable.'
    }

    $inferenceUrl = $networkCheck.details.PSObject.Properties |
        Where-Object { $_.Name -like '* API inference URL' } |
        Select-Object -First 1 -ExpandProperty Value
    if (
        [string]::IsNullOrWhiteSpace($inferenceUrl) -or
        -not ([string]$inferenceUrl).StartsWith(
            ($GatewayBaseUrl.TrimEnd('/') + '/'),
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw 'Codex gateway routing validation failed. Codex resolved a different provider URL.'
    }
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
    [Environment]::SetEnvironmentVariable(
        'ANTHROPIC_BASE_URL',
        $null,
        [EnvironmentVariableTarget]::User
    )
    [Environment]::SetEnvironmentVariable(
        'ANTHROPIC_AUTH_TOKEN',
        $null,
        [EnvironmentVariableTarget]::User
    )
    Remove-Item Env:BG_AI_GATEWAY_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:BG_AI_GATEWAY_BASE_URL -ErrorAction SilentlyContinue
    Remove-Item Env:ANTHROPIC_BASE_URL -ErrorAction SilentlyContinue
    Remove-Item Env:ANTHROPIC_AUTH_TOKEN -ErrorAction SilentlyContinue

    $codex = Find-CodexCommand
    if ($codex) {
        & $codex.Source logout
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'Codex logout did not complete.'
        }
    }

    Write-Host 'Removed BG AI Gateway Codex config, skill, environment, and Codex login.'
    Write-Host 'Fully exit all Codex and terminal windows before testing a clean install.'
}

try {
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
Write-Host 'Validating BG AI Gateway API key...'
Test-ApiKey -GatewayBaseUrl $gatewayBaseUrl -ApiKey $apiKey
Write-Host 'API key validated.'
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
    Write-Host "Downloading BG AI Gateway package $($manifest.version)..."
    Invoke-WebRequest -Uri $packageUrl -OutFile $zip

    Write-Host 'Verifying and installing the BG AI Gateway package...'
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
        Write-Host 'Configuring Codex...'
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
    [Environment]::SetEnvironmentVariable(
        'ANTHROPIC_BASE_URL',
        $gatewayBaseUrl,
        [EnvironmentVariableTarget]::User
    )
    [Environment]::SetEnvironmentVariable(
        'ANTHROPIC_AUTH_TOKEN',
        $apiKey,
        [EnvironmentVariableTarget]::User
    )
    $env:BG_AI_GATEWAY_API_KEY = $apiKey
    $env:BG_AI_GATEWAY_BASE_URL = $gatewayBaseUrl
    $env:ANTHROPIC_BASE_URL = $gatewayBaseUrl
    $env:ANTHROPIC_AUTH_TOKEN = $apiKey

    Write-Host 'Validating Codex authentication...'
    Test-CodexAuthCommand -ExpectedApiKey $apiKey
    Write-Host 'Codex authentication validated.'

    $codex = Find-CodexCommand
    if ($codex) {
        Write-Host 'Validating Codex gateway routing...'
        Test-CodexGatewayRouting `
            -Codex $codex `
            -GatewayBaseUrl $gatewayBaseUrl
        Write-Host 'Codex gateway routing validated.'

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
} catch {
    $message = $_.Exception.Message
    if ([string]::IsNullOrWhiteSpace($message)) {
        $message = 'BG AI Gateway installation failed.'
    }
    [Console]::Error.WriteLine($message)
    exit 1
}
