#!/bin/bash
# Docker entrypoint script

cd /usr/src/app
pip install --no-cache-dir -q -r requirements.txt
python intg-manager/driver.py