[CmdletBinding()]
param(
    [switch]$Zip,
    [string]$OutputDirectory = "dist"
)

$ErrorActionPreference = "Stop"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Step
    )

    & $Executable @Arguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "$Step failed with exit code $exitCode."
    }
}

if ($env:OS -ne "Windows_NT") {
    throw "This build script requires Windows."
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$readme = Join-Path $projectRoot "README.md"
$exampleConfig = Join-Path $projectRoot "examples\config.json"
$launcher = Join-Path $projectRoot "scripts\launch.py"
$sourceRoot = Join-Path $projectRoot "src"
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    throw "OutputDirectory must not be empty."
}
$distRoot = [System.IO.Path]::GetFullPath(
    [System.IO.Path]::Combine($projectRoot, $OutputDirectory)
)
$workRoot = Join-Path $projectRoot "build\pyinstaller"
$specRoot = Join-Path $projectRoot "build\spec"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project .venv is missing. Create it with: py -3.11 -m venv `"$projectRoot\.venv`"; then install with: `"$python`" -m pip install -e `".[dev]`""
}
if (-not (Test-Path -LiteralPath $readme -PathType Leaf)) {
    throw "README.md was not found at $readme."
}
if (-not (Test-Path -LiteralPath $exampleConfig -PathType Leaf)) {
    throw "examples/config.json was not found at $exampleConfig."
}

$runtime = & $python -c "import platform, struct, sys; print(str(sys.version_info.major)+'.'+str(sys.version_info.minor)+'|'+platform.machine()+'|'+str(struct.calcsize('P')*8))"
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect the project .venv Python runtime."
}
$runtimeParts = $runtime.Trim().Split("|")
if ($runtimeParts.Count -ne 3 -or $runtimeParts[0] -ne "3.11") {
    throw "Build requires Python 3.11 in .venv; found '$runtime'."
}
if ($runtimeParts[1] -notin @("AMD64", "x86_64", "amd64") -or $runtimeParts[2] -ne "64") {
    throw "Build requires 64-bit x64 Python in .venv; found '$runtime'."
}

Push-Location $projectRoot
try {
    Write-Host "Running tests with project Python 3.11..."
    Invoke-Checked $python @("-m", "pytest") "pytest"

    Write-Host "Running Ruff..."
    Invoke-Checked $python @("-m", "ruff", "check", ".") "ruff"
    Invoke-Checked $python @("-m", "pip", "check") "dependency check"

    $sourceCommit = & git rev-parse --verify HEAD 2>$null
    if ($LASTEXITCODE -ne 0) { $sourceCommit = $null }
    $sourceState = @(& git status --porcelain --untracked-files=normal)
    if ($LASTEXITCODE -ne 0) { throw "Could not read source working-tree state." }
    $appVersion = & $python -c "from auto_ready import __version__; print(__version__)"
    if ($LASTEXITCODE -ne 0) { throw "Could not read application version." }
    $metadataRoot = Join-Path $projectRoot "build\metadata"
    [void](New-Item -ItemType Directory -Path $metadataRoot -Force)
    $buildInfoPath = Join-Path $metadataRoot "build-info.json"
    Invoke-Checked $python @((Join-Path $PSScriptRoot "source_snapshot.py"), $metadataRoot) "source snapshot"
    $sourceSnapshot = Get-Content -LiteralPath (Join-Path $metadataRoot "source-snapshot.json") -Raw | ConvertFrom-Json
    [ordered]@{
        schema_version = 1
        application_version = $appVersion.Trim()
        source_commit = $sourceCommit
        source_dirty = ($sourceState.Count -gt 0)
        source_snapshot_sha256 = $sourceSnapshot.sha256
        built_at_utc = [DateTime]::UtcNow.ToString("o")
        python = $runtime.Trim()
        build_verification = "startup_only_no_real_input"
    } | ConvertTo-Json | Set-Content -LiteralPath $buildInfoPath -Encoding utf8

    $pyinstallerArguments = @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name", "auto-ready",
        "--manifest", (Join-Path $PSScriptRoot "windows.manifest"),
        "--distpath", $distRoot,
        "--workpath", $workRoot,
        "--specpath", $specRoot,
        "--paths", $sourceRoot,
        "--hidden-import", "auto_ready.simulator",
        "--hidden-import", "auto_ready.verification",
        "--collect-all", "windows_capture",
        "--add-data", "$buildInfoPath;.",
        "--add-data", "$readme;.",
        "--add-data", "$exampleConfig;examples"
    )

    # Include project notices when present. Add dependency license texts from
    # the isolated runtime environment so the onedir bundle carries its notices.
    foreach ($noticeName in @("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.txt")) {
        $noticePath = Join-Path $projectRoot $noticeName
        if (Test-Path -LiteralPath $noticePath -PathType Leaf) {
            $pyinstallerArguments += @("--add-data", "$noticePath;.")
        }
    }
    $projectLicenses = Join-Path $projectRoot "licenses"
    if (Test-Path -LiteralPath $projectLicenses -PathType Container) {
        $pyinstallerArguments += @("--add-data", "$projectLicenses;licenses")
    }

    $sitePackages = & $python -c "import sysconfig; print(sysconfig.get_paths()['purelib'])"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not locate the project .venv site-packages directory."
    }
    $sitePackages = $sitePackages.Trim()
    $licensePackages = @(
        @{ Filter = "pyside6-*.dist-info"; Destination = "PySide6" },
        @{ Filter = "pyside6_addons-*.dist-info"; Destination = "PySide6-Addons" },
        @{ Filter = "pyside6_essentials-*.dist-info"; Destination = "PySide6-Essentials" },
        @{ Filter = "shiboken6-*.dist-info"; Destination = "Shiboken6" },
        @{ Filter = "pyinstaller-*.dist-info"; Destination = "PyInstaller" },
        @{ Filter = "numpy-*.dist-info"; Destination = "NumPy" },
        @{ Filter = "windows_capture-*.dist-info"; Destination = "windows-capture" }
    )
    foreach ($package in $licensePackages) {
        $distribution = Get-ChildItem -LiteralPath $sitePackages -Directory `
            -Filter $package.Filter | Select-Object -First 1
        if ($null -ne $distribution) {
            $licensePath = Join-Path $distribution.FullName "licenses"
            if (Test-Path -LiteralPath $licensePath -PathType Container) {
                $pyinstallerArguments += @(
                    "--add-data", "$licensePath;licenses\$($package.Destination)"
                )
            }
        }
    }
    foreach ($opencvName in @("LICENSE.txt", "LICENSE-3RD-PARTY.txt")) {
        $openCvLicense = Join-Path $sitePackages "cv2\$opencvName"
        if (Test-Path -LiteralPath $openCvLicense -PathType Leaf) {
            $pyinstallerArguments += @("--add-data", "$openCvLicense;licenses\OpenCV")
        }
    }
    $pythonLicense = & $python -c "import pathlib,sys; print(pathlib.Path(sys.base_prefix)/'LICENSE.txt')"
    if (Test-Path -LiteralPath $pythonLicense.Trim() -PathType Leaf) {
        $pyinstallerArguments += @("--add-data", "$($pythonLicense.Trim());licenses\Python")
    }

    $pyinstallerArguments += $launcher
    Write-Host "Building the onedir Windows x64 application..."
    # Other desktop tooling can prepend unrelated DLL directories to PATH.
    # In particular, a Poppler ICU can shadow Windows' ICU used by QtCore.
    # Resolve binaries with a minimal process-local PATH; restore it on every exit.
    $savedBuildPath = $env:PATH
    try {
        $env:PATH = @(
            (Split-Path -Parent $python),
            (Join-Path $env:SystemRoot "System32"),
            $env:SystemRoot
        ) -join ";"
        Invoke-Checked $python $pyinstallerArguments "PyInstaller"
    }
    finally {
        $env:PATH = $savedBuildPath
    }

    $application = Join-Path $distRoot "auto-ready\auto-ready.exe"
    if (-not (Test-Path -LiteralPath $application -PathType Leaf)) {
        throw "PyInstaller completed but the executable was not found at $application."
    }
    # Keep user-facing instructions beside the executable, not only in _internal.
    $bundleRoot = Split-Path -Parent $application
    Copy-Item -LiteralPath $buildInfoPath -Destination $bundleRoot
    Copy-Item -LiteralPath (Join-Path $metadataRoot "source-snapshot.zip") -Destination $bundleRoot
    Copy-Item -LiteralPath (Join-Path $metadataRoot "source-snapshot.json") -Destination $bundleRoot
    foreach ($document in @("README.md", "STATUS.md", "ARCHITECTURE.md", "THIRD_PARTY_NOTICES.txt", "requirements-windows.lock.txt")) {
        Copy-Item -LiteralPath (Join-Path $projectRoot $document) -Destination $bundleRoot
    }
    Copy-Item -LiteralPath (Join-Path $projectRoot "examples") -Destination $bundleRoot -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot "docs") -Destination $bundleRoot -Recurse -Force
    foreach ($requiredCaptureFile in @(
        "_internal\windows_capture\windows_capture.pyd",
        "_internal\licenses\windows-capture\LICENCE"
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $bundleRoot $requiredCaptureFile) -PathType Leaf)) {
            throw "Missing packaged capture dependency or license: $requiredCaptureFile"
        }
    }
    $savedSmokeQtPlatform = $env:QT_QPA_PLATFORM
    try {
      $env:QT_QPA_PLATFORM = "offscreen"
      foreach ($captureMode in @("available", "unavailable")) {
        Write-Host "Running packaged no-input startup/exit check: capture $captureMode..."
        $smokeProcess = Start-Process -FilePath $application `
            -ArgumentList @("--smoke-test", "--capture-backend-check", $captureMode) `
            -PassThru -WindowStyle Hidden
        if (-not $smokeProcess.WaitForExit(30000)) {
            $smokeProcess.Kill()
            throw "Packaged smoke test ($captureMode) did not exit within 30 seconds."
        }
        if ($smokeProcess.ExitCode -ne 0) {
            throw "Packaged smoke test ($captureMode) failed: $($smokeProcess.ExitCode)."
        }
      }
    }
    finally {
        $env:QT_QPA_PLATFORM = $savedSmokeQtPlatform
    }

    if ($Zip) {
        $archive = Join-Path $distRoot "auto-ready-windows-x64.zip"
        Compress-Archive -LiteralPath (Join-Path $distRoot "auto-ready") `
            -DestinationPath $archive -CompressionLevel Optimal -Force
        $archiveHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLower()
        "$archiveHash  auto-ready-windows-x64.zip" | Set-Content `
            -LiteralPath "$archive.sha256" -Encoding ascii
        Write-Host "Created $archive"
    }

    Write-Host "Build complete: $application"
}
finally {
    Pop-Location
}
