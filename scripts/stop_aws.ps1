$pem = if (Test-Path "$PSScriptRoot\..\tele-monitor.pem") { "$PSScriptRoot\..\tele-monitor.pem" } else { ".\tele-monitor.pem" }

Write-Host "Stopping the bot on AWS..." -ForegroundColor Cyan

ssh -i "$pem" ubuntu@18.212.32.72 "sudo systemctl stop tele-monitor"

Write-Host "AWS bot stopped." -ForegroundColor Green
