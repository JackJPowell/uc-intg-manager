#!/bin/bash
# Docker entrypoint script
# TODO: Update the path if you rename the intg-template folder

cd /usr/src/app
pip install --no-cache-dir -q -r requirements.txt
python intg-template/driver.py