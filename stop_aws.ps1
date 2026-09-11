Write-Host "Stopping the bot on AWS..." -ForegroundColor Cyan

ssh -i .\tele-monitor.pem ubuntu@18.212.32.72 "sudo systemctl stop tele-monitor"

Write-Host "AWS bot stopped." -ForegroundColor Green
