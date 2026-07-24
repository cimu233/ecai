#!/bin/zsh
set -e

project_dir="${0:A:h:h}"
python_bin="/opt/homebrew/bin/python3.11"
runtime_dir="$project_dir/.local-asr-venv"

if [[ ! -x "$python_bin" ]]; then
  echo "Python 3.11 not found at $python_bin" >&2
  exit 1
fi

if [[ ! -x "$runtime_dir/bin/python" ]]; then
  "$python_bin" -m venv "$runtime_dir"
fi

"$runtime_dir/bin/python" -m pip install --upgrade pip
"$runtime_dir/bin/python" -m pip install "torch==2.9.1"
"$runtime_dir/bin/python" -m pip install "qwen-asr==0.0.6"
"$runtime_dir/bin/python" - <<'PY'
import torch
from qwen_asr import Qwen3ASRModel

print("Local ASR runtime ready")
print("torch:", torch.__version__)
print("MPS:", torch.backends.mps.is_available())
print("Qwen:", Qwen3ASRModel.__name__)
PY
