"""Long-lived Qwen3-ASR worker using a line-delimited JSON protocol."""

import argparse
import contextlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def emit(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def select_device(torch: Any, requested: str) -> Tuple[str, Any]:
    if requested != "auto":
        dtype = torch.float32 if requested == "cpu" else torch.float16
        return requested, dtype
    if torch.backends.mps.is_available():
        return "mps", torch.float16
    return "cpu", torch.float32


def load_model(model_dir: Path, requested_device: str) -> Tuple[Any, str]:
    import torch
    from qwen_asr import Qwen3ASRModel

    device, dtype = select_device(torch, requested_device)
    with contextlib.redirect_stdout(sys.stderr):
        model = Qwen3ASRModel.from_pretrained(
            str(model_dir),
            dtype=dtype,
            device_map=device,
            max_inference_batch_size=1,
            max_new_tokens=512,
        )
    return model, device


def result_fields(result: Any) -> Tuple[str, Optional[str]]:
    item = result[0] if isinstance(result, (list, tuple)) else result
    if isinstance(item, dict):
        return str(item.get("text") or ""), item.get("language")
    return str(getattr(item, "text", "")), getattr(item, "language", None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--device", choices=["auto", "mps", "cpu"], default="auto")
    arguments = parser.parse_args()
    try:
        model, device = load_model(arguments.model_dir, arguments.device)
    except Exception as error:
        emit({"event": "error", "code": "local_model_load_failed", "message": str(error)})
        return 1
    emit({"event": "ready", "device": device, "model_dir": str(arguments.model_dir)})

    for line in sys.stdin:
        request: Dict[str, Any] = {}
        try:
            request = json.loads(line)
            if request.get("command") == "shutdown":
                return 0
            request_id = request["id"]
            with contextlib.redirect_stdout(sys.stderr):
                result = model.transcribe(
                    audio=request["audio_path"],
                    language=request.get("language"),
                )
            text, language = result_fields(result)
            emit({"event": "result", "id": request_id, "text": text, "language": language, "device": device})
        except Exception as error:
            emit({
                "event": "error",
                "id": request.get("id"),
                "code": "local_inference_failed",
                "message": str(error),
            })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
