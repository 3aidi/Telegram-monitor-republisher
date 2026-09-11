Write-Host "Checking if the AWS bot is running..." -ForegroundColor Cyan

$status = ssh -i .\tele-monitor.pem ubuntu@18.212.32.72 "systemctl is-active tele-monitor"

if ($status -eq "active") {
    Write-Host ""
    Write-Host "BLOCKED: The AWS bot is currently running (active)." -ForegroundColor Red
    Write-Host "Stop it first with:" -ForegroundColor Yellow
    Write-Host "  ssh -i .\tele-monitor.pem ubuntu@18.212.32.72 `"sudo systemctl stop tele-monitor`"" -ForegroundColor Yellow
    Write-Host "Then run this script again." -ForegroundColor Yellow
    exit 1
}

Write-Host "AWS bot is stopped. Safe to run locally." -ForegroundColor Green
Write-Host ""
python main.py