"""Speech-to-text provider contracts and local/cloud adapters."""

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import TranscriptResult, TranscriptSegment
from .secrets import MacKeychainSecretStore


class AsrError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class AsrProvider(Protocol):
    name: str
    model: str

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        ...


class JsonHttpClient:
    def request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = 120.0,
    ) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = self._safe_error(error)
            raise AsrError(
                "provider_http_error",
                "ASR provider returned HTTP {}: {}".format(error.code, detail),
                retryable=error.code == 429 or error.code >= 500,
            ) from error
        except (URLError, TimeoutError) as error:
            raise AsrError("provider_network_error", "ASR provider network request failed.", True) from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AsrError("provider_invalid_response", "ASR provider returned invalid JSON.") from error

    @staticmethod
    def _safe_error(error: HTTPError) -> str:
        try:
            parsed = json.loads(error.read().decode("utf-8"))
            return str(parsed.get("message") or parsed.get("error", {}).get("message") or "request failed")[:300]
        except Exception:
            return "request failed"


class BinaryJsonHttpClient:
    def __init__(self, max_attempts: int = 3, sleep: Callable[[float], None] = time.sleep) -> None:
        self.max_attempts = max_attempts
        self.sleep = sleep

    def request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: bytes,
        timeout: float = 300.0,
    ) -> Dict[str, Any]:
        for attempt in range(1, self.max_attempts + 1):
            request = Request(url, data=body, headers=headers, method=method)
            try:
                with urlopen(request, timeout=timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as error:
                retryable = error.code == 429 or error.code >= 500
                if retryable and attempt < self.max_attempts:
                    error.close()
                    self.sleep(float(2 ** (attempt - 1)))
                    continue
                detail = JsonHttpClient._safe_error(error)
                raise AsrError(
                    "provider_http_error",
                    "ASR provider returned HTTP {}: {}".format(error.code, detail),
                    retryable=retryable,
                ) from error
            except (URLError, TimeoutError) as error:
                if attempt < self.max_attempts:
                    self.sleep(float(2 ** (attempt - 1)))
                    continue
                raise AsrError("provider_network_error", "ASR provider network request failed.", True) from error
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise AsrError("provider_invalid_response", "ASR provider returned invalid JSON.") from error
        raise RuntimeError("ASR retry loop ended unexpectedly.")


def _multipart_audio(audio_path: Path, fields: Dict[str, str]) -> tuple:
    boundary = "xiaoe-{}".format(uuid.uuid4().hex)
    chunks: List[bytes] = []
    for name, value in fields.items():
        chunks.extend([
            "--{}\r\n".format(boundary).encode("ascii"),
            'Content-Disposition: form-data; name="{}"\r\n\r\n'.format(name).encode("ascii"),
            value.encode("utf-8"),
            b"\r\n",
        ])
    mime = mimetypes.guess_type(audio_path.name)[0] or "application/octet-stream"
    chunks.extend([
        "--{}\r\n".format(boundary).encode("ascii"),
        'Content-Disposition: form-data; name="file"; filename="{}"\r\n'.format(
            audio_path.name.replace('"', "")
        ).encode("utf-8"),
        "Content-Type: {}\r\n\r\n".format(mime).encode("ascii"),
        audio_path.read_bytes(),
        b"\r\n",
        "--{}--\r\n".format(boundary).encode("ascii"),
    ])
    return b"".join(chunks), "multipart/form-data; boundary={}".format(boundary)


def _segments_from_items(items: List[Dict[str, Any]], language: Optional[str] = None) -> List[TranscriptSegment]:
    segments = []
    for item in items:
        text = str(item.get("text") or item.get("transcript") or "").strip()
        if not text:
            continue
        segments.append(TranscriptSegment(
            start_seconds=float(item.get("start", item.get("start_time", 0.0))) / (1000.0 if "start_time" in item else 1.0),
            end_seconds=(
                float(item.get("end", item.get("end_time"))) / (1000.0 if "end_time" in item else 1.0)
                if item.get("end", item.get("end_time")) is not None else None
            ),
            text=text,
            speaker=str(item["speaker_id"]) if item.get("speaker_id") is not None else item.get("speaker"),
            language=language,
        ))
    return segments


class OpenAICompatibleAsrProvider:
    """Multipart transcription adapter shared by OpenAI, Groq, and compatible gateways."""

    chunk_seconds = 240

    def __init__(
        self,
        name: str,
        api_key: Optional[str],
        base_url: str,
        model: str,
        language: Optional[str] = None,
        response_format: str = "json",
        http: Optional[BinaryJsonHttpClient] = None,
    ) -> None:
        self.name = name
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.language = language
        self.response_format = response_format
        self.http = http or BinaryJsonHttpClient()

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        if not self.api_key:
            raise AsrError("missing_api_key", "API key is missing for {}.".format(self.name))
        if not audio_path.is_file():
            raise AsrError("audio_missing", "Audio file does not exist: {}".format(audio_path))
        fields = {"model": self.model, "response_format": self.response_format}
        if self.language:
            fields["language"] = self.language
        body, content_type = _multipart_audio(audio_path, fields)
        raw = self.http.request(
            "POST",
            self.base_url + "/audio/transcriptions",
            {"Authorization": "Bearer {}".format(self.api_key), "Content-Type": content_type},
            body,
        )
        text = str(raw.get("text") or "").strip()
        if not text:
            raise AsrError("empty_transcript", "{} returned an empty transcript.".format(self.name))
        segments = _segments_from_items(raw.get("segments") or [], self.language)
        if not segments:
            segments = [TranscriptSegment(0.0, None, text, language=self.language)]
        return TranscriptResult(text, segments, self.name, self.model, raw)


class DeepgramAsrProvider:
    name = "deepgram"
    chunk_seconds = 240

    def __init__(self, api_key: Optional[str], model: str = "nova-3", language: Optional[str] = None,
                 http: Optional[BinaryJsonHttpClient] = None) -> None:
        self.api_key = api_key
        self.model = model
        self.language = language
        self.http = http or BinaryJsonHttpClient()

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        if not self.api_key:
            raise AsrError("missing_api_key", "API key is missing for Deepgram.")
        if not audio_path.is_file():
            raise AsrError("audio_missing", "Audio file does not exist: {}".format(audio_path))
        parameters = {"model": self.model, "smart_format": "true", "utterances": "true"}
        if self.language:
            parameters["language"] = self.language
        raw = self.http.request(
            "POST",
            "https://api.deepgram.com/v1/listen?{}".format(urlencode(parameters)),
            {
                "Authorization": "Token {}".format(self.api_key),
                "Content-Type": mimetypes.guess_type(audio_path.name)[0] or "audio/mpeg",
            },
            audio_path.read_bytes(),
        )
        try:
            alternative = raw["results"]["channels"][0]["alternatives"][0]
            text = str(alternative["transcript"]).strip()
        except (KeyError, IndexError, TypeError) as error:
            raise AsrError("provider_invalid_response", "Deepgram response has no transcript.") from error
        segments = _segments_from_items(raw.get("results", {}).get("utterances") or [], self.language)
        if not segments:
            segments = [TranscriptSegment(0.0, None, text, language=self.language)]
        return TranscriptResult(text, segments, self.name, self.model, raw)


class VolcengineAsrProvider:
    name = "volcengine"
    model = "bigmodel"
    chunk_seconds = 240

    def __init__(self, api_key: Optional[str], app_key: Optional[str] = None,
                 http: Optional[BinaryJsonHttpClient] = None) -> None:
        self.api_key = api_key
        self.app_key = app_key or "xiaoe-audio-pipeline"
        self.http = http or BinaryJsonHttpClient()

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        if not self.api_key:
            raise AsrError("missing_api_key", "API key is missing for Volcengine.")
        if not audio_path.is_file():
            raise AsrError("audio_missing", "Audio file does not exist: {}".format(audio_path))
        payload = {
            "user": {"uid": self.app_key},
            "audio": {"data": base64.b64encode(audio_path.read_bytes()).decode("ascii")},
            "request": {"model_name": self.model},
        }
        raw = self.http.request(
            "POST",
            "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash",
            {
                "X-Api-Key": self.api_key,
                "X-Api-Resource-Id": "volc.bigasr.auc_turbo",
                "X-Api-Request-Id": str(uuid.uuid4()),
                "Content-Type": "application/json",
            },
            json.dumps(payload).encode("utf-8"),
        )
        result = raw.get("result") or {}
        text = str(result.get("text") or "").strip()
        if not text:
            raise AsrError("provider_invalid_response", "Volcengine response has no transcript.")
        segments = _segments_from_items(result.get("utterances") or [], "zh")
        if not segments:
            segments = [TranscriptSegment(0.0, None, text, language="zh")]
        return TranscriptResult(text, segments, self.name, self.model, raw)


class TencentFlashAsrProvider:
    name = "tencent"
    model = "16k_zh_en"
    chunk_seconds = 240

    def __init__(self, app_id: Optional[str], secret_id: Optional[str], secret_key: Optional[str],
                 language: Optional[str] = None, http: Optional[BinaryJsonHttpClient] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.app_id = app_id
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.language = language
        self.http = http or BinaryJsonHttpClient()
        self.clock = clock

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        if not all((self.app_id, self.secret_id, self.secret_key)):
            raise AsrError("missing_api_key", "Tencent AppID, SecretID, and SecretKey are required.")
        if not audio_path.is_file():
            raise AsrError("audio_missing", "Audio file does not exist: {}".format(audio_path))
        formats = {".mp3": "mp3", ".wav": "wav", ".m4a": "m4a", ".aac": "aac", ".ogg": "ogg-opus"}
        voice_format = formats.get(audio_path.suffix.lower(), "mp3")
        engine = "16k_multi_lang" if self.language and self.language not in ("zh", "en", "yue") else self.model
        parameters = {
            "convert_num_mode": "1",
            "engine_type": engine,
            "filter_dirty": "0",
            "filter_modal": "0",
            "filter_punc": "0",
            "first_channel_only": "1",
            "secretid": str(self.secret_id),
            "speaker_diarization": "0",
            "timestamp": str(int(self.clock())),
            "voice_format": voice_format,
            "word_info": "1",
        }
        query = urlencode(sorted(parameters.items()))
        path = "/asr/flash/v1/{}?{}".format(self.app_id, query)
        signature = base64.b64encode(hmac.new(
            str(self.secret_key).encode("utf-8"),
            ("POSTasr.cloud.tencent.com" + path).encode("utf-8"),
            hashlib.sha1,
        ).digest()).decode("ascii")
        raw = self.http.request(
            "POST",
            "https://asr.cloud.tencent.com" + path,
            {"Authorization": signature, "Content-Type": "application/octet-stream"},
            audio_path.read_bytes(),
        )
        if raw.get("code") != 0:
            raise AsrError("provider_error", str(raw.get("message") or "Tencent ASR failed."))
        results = raw.get("flash_result") or []
        text = "\n".join(str(item.get("text") or "").strip() for item in results).strip()
        sentences = [sentence for item in results for sentence in (item.get("sentence_list") or [])]
        segments = _segments_from_items(sentences, self.language)
        if not text:
            raise AsrError("empty_transcript", "Tencent ASR returned an empty transcript.")
        return TranscriptResult(text, segments or [TranscriptSegment(0.0, None, text)], self.name, self.model, raw)


class BaiduAsrProvider:
    name = "baidu"
    model = "short-speech"
    chunk_seconds = 55
    chunk_format = "wav"

    def __init__(self, api_key: Optional[str], language: Optional[str] = None,
                 http: Optional[BinaryJsonHttpClient] = None) -> None:
        self.api_key = api_key
        self.language = language
        self.http = http or BinaryJsonHttpClient()

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        if not self.api_key:
            raise AsrError("missing_api_key", "API key is missing for Baidu ASR.")
        if not audio_path.is_file():
            raise AsrError("audio_missing", "Audio file does not exist: {}".format(audio_path))
        data = audio_path.read_bytes()
        model = {"en": 1737, "yue": 1637}.get(self.language or "zh", 1537)
        payload = {
            "format": audio_path.suffix.lower().lstrip("."),
            "rate": 16000,
            "channel": 1,
            "cuid": "xiaoe-audio-pipeline",
            "dev_pid": model,
            "len": len(data),
            "speech": base64.b64encode(data).decode("ascii"),
        }
        raw = self.http.request(
            "POST",
            "https://vop.baidu.com/server_api",
            {"Authorization": "Bearer {}".format(self.api_key), "Content-Type": "application/json"},
            json.dumps(payload).encode("utf-8"),
        )
        if raw.get("err_no") != 0:
            raise AsrError("provider_error", str(raw.get("err_msg") or "Baidu ASR failed."))
        text = "\n".join(raw.get("result") or []).strip()
        if not text:
            raise AsrError("empty_transcript", "Baidu ASR returned an empty transcript.")
        return TranscriptResult(text, [TranscriptSegment(0.0, None, text, language=self.language)], self.name, self.model, raw)


class AlibabaQwenAsrProvider:
    """Local-file adapter for the OpenAI-compatible Qwen3-ASR-Flash endpoint."""

    name = "alibaba"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "qwen3-asr-flash",
        base_url: Optional[str] = None,
        language: Optional[str] = None,
        enable_itn: bool = True,
        http: Optional[JsonHttpClient] = None,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_key = (
            api_key
            or os.environ.get("DASHSCOPE_API_KEY")
            or MacKeychainSecretStore().get_dashscope_api_key()
        )
        self.model = model
        self.base_url = (base_url or os.environ.get("DASHSCOPE_COMPATIBLE_BASE_URL") or
                         "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
        self.language = language
        self.enable_itn = enable_itn
        self.http = http or JsonHttpClient()
        self.max_attempts = max_attempts
        self.sleep = sleep

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        if not self.api_key:
            raise AsrError("missing_api_key", "Set DASHSCOPE_API_KEY before using Alibaba ASR.")
        if not audio_path.is_file():
            raise AsrError("audio_missing", "Audio file does not exist: {}".format(audio_path))
        if audio_path.stat().st_size > 10 * 1024 * 1024:
            raise AsrError("audio_too_large", "Qwen3-ASR-Flash input must be no larger than 10 MB.")

        mime = mimetypes.guess_type(audio_path.name)[0] or "audio/mpeg"
        encoded = base64.b64encode(audio_path.read_bytes()).decode("ascii")
        options: Dict[str, Any] = {"enable_itn": self.enable_itn}
        if self.language:
            options["language"] = self.language
        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "input_audio",
                    "input_audio": {"data": "data:{};base64,{}".format(mime, encoded)},
                }],
            }],
            "stream": False,
            "asr_options": options,
        }
        raw = self._request_with_retry(payload)
        try:
            message = raw["choices"][0]["message"]
            text = str(message["content"]).strip()
            annotations = message.get("annotations") or []
        except (KeyError, IndexError, TypeError) as error:
            raise AsrError("provider_invalid_response", "Alibaba ASR response has no transcript.") from error
        if not text:
            raise AsrError("empty_transcript", "Alibaba ASR returned an empty transcript.")
        annotation = annotations[0] if annotations else {}
        segment = TranscriptSegment(
            start_seconds=0.0,
            end_seconds=None,
            text=text,
            language=annotation.get("language"),
        )
        return TranscriptResult(text=text, segments=[segment], provider=self.name, model=self.model, raw=raw)

    def _request_with_retry(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {"Authorization": "Bearer {}".format(self.api_key), "Content-Type": "application/json"}
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self.http.request(
                    "POST", self.base_url + "/chat/completions", headers=headers, payload=payload
                )
            except AsrError as error:
                if not error.retryable or attempt == self.max_attempts:
                    raise
                self.sleep(float(2 ** (attempt - 1)))
        raise RuntimeError("ASR retry loop ended unexpectedly.")


class LocalQwenAsrProvider:
    """Persistent subprocess adapter for a local Qwen3-ASR model directory."""

    name = "local-qwen"
    default_model_dir = (
        Path.home()
        / "Library/Application Support/OpenLess/models/qwen3-asr/qwen3-asr-1.7b"
    )

    def __init__(
        self,
        model_dir: Optional[Path] = None,
        runtime_python: Optional[Path] = None,
        language: Optional[str] = None,
        device: str = "auto",
        worker_path: Optional[Path] = None,
    ) -> None:
        self.model_dir = Path(model_dir or self.default_model_dir).expanduser()
        project_root = Path(__file__).resolve().parents[2]
        self.runtime_python = Path(
            runtime_python or project_root / ".local-asr-venv/bin/python"
        ).expanduser()
        self.worker_path = Path(worker_path or Path(__file__).with_name("local_asr_worker.py"))
        self._project_src = project_root / "src"
        self._custom_worker = worker_path is not None
        self.language = language
        self.device = device
        self.model = self.model_dir.name
        self._process: Optional[subprocess.Popen[str]] = None
        self._request_id = 0

    def check(self) -> Dict[str, Any]:
        missing = []
        if not self.runtime_python.is_file():
            missing.append("runtime_python")
        if not self.model_dir.is_dir():
            missing.append("model_dir")
        if not (self.model_dir / "config.json").is_file():
            missing.append("config.json")
        if not (self.model_dir / "model.safetensors.index.json").is_file():
            missing.append("model.safetensors.index.json")
        return {
            "ready": not missing,
            "missing": missing,
            "runtime_python": str(self.runtime_python),
            "model_dir": str(self.model_dir),
            "device": self.device,
        }

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        if not audio_path.is_file():
            raise AsrError("audio_missing", "Audio file does not exist: {}".format(audio_path))
        self._ensure_started()
        self._request_id += 1
        payload = {
            "id": self._request_id,
            "audio_path": str(audio_path.resolve()),
            "language": self._qwen_language(self.language),
        }
        assert self._process is not None and self._process.stdin is not None
        try:
            self._process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise AsrError("local_worker_stopped", "Local ASR worker stopped unexpectedly.") from error
        response = self._read_response(self._request_id)
        if response.get("event") == "error":
            raise AsrError(
                str(response.get("code") or "local_inference_failed"),
                str(response.get("message") or "Local Qwen ASR inference failed."),
            )
        text = str(response.get("text") or "").strip()
        if not text:
            raise AsrError("empty_transcript", "Local Qwen ASR returned an empty transcript.")
        language = response.get("language")
        segment = TranscriptSegment(0.0, None, text, language=language)
        return TranscriptResult(
            text=text,
            segments=[segment],
            provider=self.name,
            model=self.model,
            raw=response,
        )

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None and process.stdin is not None:
            try:
                process.stdin.write('{"command":"shutdown"}\n')
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        if process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()

    def _ensure_started(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        check = self.check()
        if not check["ready"]:
            raise AsrError(
                "local_runtime_missing",
                "Local ASR is not ready; missing: {}.".format(", ".join(check["missing"])),
            )
        if self._custom_worker:
            command = [str(self.runtime_python), str(self.worker_path)]
        else:
            bootstrap = (
                "import runpy,sys; sys.path.insert(0, {!r}); "
                "runpy.run_module('xiaoe_core.local_asr_worker', run_name='__main__')"
            ).format(str(self._project_src))
            command = [str(self.runtime_python), "-c", bootstrap]
        command.extend(["--model-dir", str(self.model_dir), "--device", self.device])
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            bufsize=1,
        )
        response = self._read_response(None)
        if response.get("event") != "ready":
            self.close()
            raise AsrError(
                str(response.get("code") or "local_model_load_failed"),
                str(response.get("message") or "Local Qwen ASR model could not be loaded."),
            )

    def _read_response(self, request_id: Optional[int]) -> Dict[str, Any]:
        assert self._process is not None and self._process.stdout is not None
        while True:
            line = self._process.stdout.readline()
            if not line:
                code = self._process.poll()
                raise AsrError(
                    "local_worker_stopped",
                    "Local ASR worker exited unexpectedly (code {}).".format(code),
                )
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if request_id is None or response.get("id") == request_id:
                return response

    @staticmethod
    def _qwen_language(language: Optional[str]) -> Optional[str]:
        if not language:
            return None
        aliases = {
            "zh": "Chinese",
            "zh-cn": "Chinese",
            "zh-tw": "Chinese",
            "yue": "Cantonese",
            "en": "English",
            "ja": "Japanese",
            "ko": "Korean",
        }
        return aliases.get(language.lower(), language)

    def __del__(self) -> None:
        self.close()


def merge_chunk_results(results: List[TranscriptResult], offsets: List[float]) -> TranscriptResult:
    if not results:
        raise AsrError("empty_transcript", "No audio chunks were transcribed.")
    segments: List[TranscriptSegment] = []
    for result, offset in zip(results, offsets):
        for segment in result.segments:
            segments.append(
                TranscriptSegment(
                    start_seconds=segment.start_seconds + offset,
                    end_seconds=None if segment.end_seconds is None else segment.end_seconds + offset,
                    text=segment.text,
                    speaker=segment.speaker,
                    language=segment.language,
                )
            )
    return TranscriptResult(
        text="\n".join(result.text for result in results if result.text.strip()),
        segments=segments,
        provider=results[0].provider,
        model=results[0].model,
        raw={"chunks": [result.raw for result in results]},
    )
