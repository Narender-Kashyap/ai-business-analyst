# Runs the project's checks and saves all output to logs\check_log.txt.
# Usage (from the project folder):  .\check.ps1          -> tests
#                                   .\check.ps1 eval     -> tests + evaluation (calls Gemini)
param([string]$mode = "tests")

Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
New-Item -ItemType Directory -Force -Path logs | Out-Null
$py  = ".\.venv\Scripts\python.exe"
$log = "logs\check_log.txt"

# 1. The automated test suite (no API key needed); exit code 0 = all passed
"=== check run $(Get-Date -Format s) mode=$mode ===" | Out-File $log -Encoding utf8
Write-Host "Running pytest ..."
"`n### pytest" | Out-File $log -Append -Encoding utf8
& $py -m pytest 2>&1 | ForEach-Object { "$_" } | Out-File $log -Append -Encoding utf8

# 2. Printable demo scripts (two of them call Gemini)
$steps = @(
    "src\test_validator.py",
    "src\test_db.py",
    "src\test_analysis.py",
    "src\list_models.py",
    "src\test_llm.py",
    "src\test_pipeline.py"
)
if ($mode -eq "eval") { $steps += "eval\run_eval.py" }

foreach ($s in $steps) {
    if (Test-Path $s) {
        Write-Host "Running $s ..."
        "`n### $s" | Out-File $log -Append -Encoding utf8
        & $py $s 2>&1 | ForEach-Object { "$_" } | Out-File $log -Append -Encoding utf8
    }
}
Write-Host "`nDone. Output saved to $log"
