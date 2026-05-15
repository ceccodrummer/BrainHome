# Install Git hooks by setting the repository hooks path to the .githooks folder.
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot
git config core.hooksPath .githooks
Write-Host "Git hooks installed: .githooks" -ForegroundColor Green
