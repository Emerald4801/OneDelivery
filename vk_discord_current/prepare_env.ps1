$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) { & $launcher.Source -3 -m venv (Join-Path $PSScriptRoot '.venv') }
    else { & python -m venv (Join-Path $PSScriptRoot '.venv') }
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10+ is required.' }
}
& $python -c "import importlib.metadata as m,pathlib,sys; pins=[x.strip().split('==') for x in pathlib.Path('requirements.lock').read_text().splitlines() if x.strip() and not x.startswith('#')]; found={d.metadata['Name'].lower().replace('_','-'):d.version for d in m.distributions()}; sys.exit(any(found.get(n.lower().replace('_','-'))!=v for n,v in pins))"
if ($LASTEXITCODE -eq 0) { return }
& $python -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements.lock')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $python -c "import importlib.metadata as m,pathlib; p=pathlib.Path('requirements.lock'); pins=[x.strip().split('==') for x in p.read_text().splitlines() if x.strip() and not x.startswith('#')]; assert all(m.version(n)==v for n,v in pins)"
if ($LASTEXITCODE -ne 0) { throw 'Dependency verification failed.' }
