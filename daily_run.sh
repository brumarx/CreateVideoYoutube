#!/bin/bash
cd /home/brmx/Youtube
source .venv/bin/activate
python3 scripts/daily_run.py >> logs/daily_run.log 2>&1
