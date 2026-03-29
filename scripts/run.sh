#!/bin/bash
# Wrapper to run voice scripts with CUDA library paths set
VENV="$HOME/claude-voice-venv/.venv"
PYTHON="$VENV/bin/python3"
SITE_PKGS="$VENV/lib/python3.13/site-packages"

export LD_LIBRARY_PATH="$SITE_PKGS/nvidia/cublas/lib:$SITE_PKGS/nvidia/cudnn/lib:$SITE_PKGS/nvidia/cufft/lib:${LD_LIBRARY_PATH:-}"

exec "$PYTHON" "$@"
