#!/bin/bash
# Render build script — ffmpeg + Python deps
set -e
apt-get update -qq && apt-get install -y -qq ffmpeg > /dev/null 2>&1 || echo "ffmpeg install skip"
pip install -r requirements.txt
