Write-Host "Starting the bot on AWS..." -ForegroundColor Cyan

ssh -i .\tele-monitor.pem ubuntu@18.212.32.72 "sudo systemctl start tele-monitor"

Write-Host ""
Write-Host "Checking status..." -ForegroundColor Cyan
$status = ssh -i .\tele-monitor.pem ubuntu@18.212.32.72 "systemctl is-active tele-monitor"

if ($status -eq "active") {
    Write-Host "AWS bot is now running." -ForegroundColor Green
} else {
    Write-Host "Something went wrong - status is '$status', not 'active'." -ForegroundColor Red
    Write-Host "Check manually with: ssh -i .\tele-monitor.pem ubuntu@18.212.32.72 `"systemctl status tele-monitor`"" -ForegroundColor Yellow
}
