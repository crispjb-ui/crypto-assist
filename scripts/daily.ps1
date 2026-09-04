# Unattended daily diligence pipeline for Windows Task Scheduler.
# Register (from an ordinary PowerShell prompt):
#   schtasks /create /tn "crypto-assist-daily" /sc daily /st 09:00 /tr "powershell -NoProfile -ExecutionPolicy Bypass -File %USERPROFILE%\crypto-assist\scripts\daily.ps1"
# Inspect results any time: data\daily.log
Set-Location (Join-Path $HOME "crypto-assist")
New-Item -ItemType Directory -Force -Path "data" | Out-Null
python -m src.onchain.daily 2>&1 |
    Tee-Object -Append -FilePath (Join-Path "data" "daily.log")
