#!/bin/bash
# Reinicia o painel web (web/app.py) se ele não estiver rodando — o processo
# já foi derrubado pelo sistema por pouca memória (pressão vem de outros
# serviços na máquina, não deste projeto). Rodado via cron a cada 5min.
if ! pgrep -f "web/app.py" > /dev/null; then
    cd /home/brmx/Youtube
    nohup nice -n 19 ionice -c 3 .venv/bin/python3 web/app.py >> logs/dashboard.log 2>&1 &
    disown
    echo "$(date '+%Y-%m-%d %H:%M:%S') watchdog: painel estava caído, reiniciado" >> logs/dashboard.log
fi
