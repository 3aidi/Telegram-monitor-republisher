# Suggested Commands (Windows & Remote AWS)

## Local Development & Testing (Windows PowerShell)
- Run test suite: `.\venv\Scripts\pytest test_system.py -q`
- Run single test: `.\venv\Scripts\pytest test_system.py -k "<TestName>" -v`
- Run local bot: `.\run.ps1` (or `.\scripts\run.ps1`, checks AWS status first to prevent dual-instance conflict)
- Add new forwarder session: `python add_forwarder.py` (or `python scripts/add_forwarder.py`)
- Start bot on AWS EC2: `.\start_aws.ps1`
- Stop bot on AWS EC2: `.\stop_aws.ps1`

## Remote Server (AWS EC2 Ubuntu)
- SSH: `ssh -i .\tele-monitor.pem ubuntu@18.212.32.72`
- Service check: `systemctl status tele-monitor --no-pager`
- Service restart: `sudo systemctl restart tele-monitor`
- Live journal logs: `journalctl -u tele-monitor -f`
