#!/usr/bin/env bash
set -euxo pipefail
python -m pip install --upgrade pip setuptools wheel build
pip install -r requirements.txt
