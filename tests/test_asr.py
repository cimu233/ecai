import json
import base64
import hashlib
import hmac
import sys
import tempfile
import unittest
from pathlib import Path

from xiaoe_core.asr import (
    AlibabaQwenAsrProvider,
    AsrError,
    BaiduAsrProvider,
    DeepgramAsrProvider,
    LocalQwenAsrProvider,
    OpenAICompatibleAsrProvider,
    TencentFlashAsrProvider,
    VolcengineAsrProvider,
    merge_chunk_results,
)
from xiaoe_core.models import TranscriptResult, TranscriptSegment


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, payload=None, timeout=120.0):
        self.calls.append((method, url, headers, payload))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeBinaryHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, headers, body, timeout=300.0):
        self.calls.append((method, url, headers, body))
        return self.response


class AlibabaQwenAsrProviderTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.audio = Path(self.temp_dir.name) / "part.mp3"
        self.audio.write_bytes(b"ID3\x00sample")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_local_audio_is_base64_and_response_is_normalized(self):
        http = FakeHttp([{
            "choices": [{
                "message": {
                    "content": "第一段课程内容。",
                    "annotations": [{"type": "audio_info", "language": "zh"}],
                }
            }]
        }])
        provider = AlibabaQwenAsrProvider(
            api_key="secret", base_url="https://api.example/v1", language="zh", http=http
        )

        result = provider.transcribe(self.audio)

        self.assertEqual("第一段课程内容。", result.text)
        self.assertEqual("zh", result.segments[0].language)
        payload = http.calls[0][3]
        data = payload["messages"][0]["content"][0]["input_audio"]["data"]
        self.assertTrue(data.startswith("data:audio/mpeg;base64,"))
        self.assertNotIn("secret", json.dumps(payload))
        self.assertEqual("Bearer secret", http.calls[0][2]["Authorization"])

    def test_transient_errors_are_retried(self):
        http = FakeHttp([
            AsrError("provider_http_error", "busy", retryable=True),
            {"choices": [{"message": {"content": "ok", "annotations": []}}]},
        ])
        sleeps = []
        provider = AlibabaQwenAsrProvider(api_key="secret", http=http, sleep=sleeps.append)

        self.assertEqual("ok", provider.transcribe(self.audio).text)
        self.assertEqual([1.0], sleeps)

    def test_missing_api_key_fails_before_network(self):
        provider = AlibabaQwenAsrProvider(api_key=None, http=FakeHttp([]))
        provider.api_key = None
        with self.assertRaises(AsrError) as context:
            provider.transcribe(self.audio)
        self.assertEqual("missing_api_key", context.exception.code)

    def test_chunk_results_keep_offsets(self):
        result = TranscriptResult(
            text="hello",
            segments=[TranscriptSegment(0.0, None, "hello")],
            provider="fake",
            model="fake-model",
            raw={"id": "one"},
        )
        merged = merge_chunk_results([result, result], [0.0, 240.0])
        self.assertEqual([0.0, 240.0], [segment.start_seconds for segment in merged.segments])
        self.assertEqual("hello\nhello", merged.text)


class LocalQwenAsrProviderTest(unittest.TestCase):
    def test_persistent_worker_response_is_normalized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_dir = root / "model"
            model_dir.mkdir()
            (model_dir / "config.json").write_text("{}", encoding="utf-8")
            (model_dir / "model.safetensors.index.json").write_text("{}", encoding="utf-8")
            audio = root / "sample.wav"
            audio.write_bytes(b"RIFFsample")
            worker = root / "worker.py"
            worker.write_text(
                "import json,sys\n"
                "print(json.dumps({'event':'ready'}), flush=True)\n"
                "for line in sys.stdin:\n"
                " request=json.loads(line)\n"
                " if request.get('command') == 'shutdown': break\n"
                " print(json.dumps({'event':'result','id':request['id'],'text':'local text','language':request.get('language')}), flush=True)\n",
                encoding="utf-8",
            )
            provider = LocalQwenAsrProvider(
                model_dir=model_dir,
                runtime_python=Path(sys.executable),
                language="zh",
                worker_path=worker,
            )

            result = provider.transcribe(audio)
            provider.close()

            self.assertEqual("local text", result.text)
            self.assertEqual("Chinese", result.segments[0].language)
            self.assertEqual("local-qwen", result.provider)


class CloudAsrProviderTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.audio = Path(self.temp_dir.name) / "part.mp3"
        self.audio.write_bytes(b"ID3-audio-data")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_openai_compatible_uses_multipart(self):
        http = FakeBinaryHttp({"text": "hello"})
        provider = OpenAICompatibleAsrProvider(
            "openai", "secret", "https://api.example/v1", "gpt-4o-mini-transcribe", "en", http=http
        )
        result = provider.transcribe(self.audio)
        self.assertEqual("hello", result.text)
        self.assertEqual("https://api.example/v1/audio/transcriptions", http.calls[0][1])
        self.assertIn("multipart/form-data", http.calls[0][2]["Content-Type"])
        self.assertIn(b'gpt-4o-mini-transcribe', http.calls[0][3])
        self.assertIn(b"ID3-audio-data", http.calls[0][3])

    def test_deepgram_normalizes_utterances(self):
        http = FakeBinaryHttp({
            "results": {
                "channels": [{"alternatives": [{"transcript": "hello world"}]}],
                "utterances": [{"start": 0.2, "end": 1.1, "transcript": "hello world"}],
            }
        })
        result = DeepgramAsrProvider("secret", language="en", http=http).transcribe(self.audio)
        self.assertEqual("hello world", result.text)
        self.assertEqual(0.2, result.segments[0].start_seconds)
        self.assertIn("model=nova-3", http.calls[0][1])

    def test_volcengine_normalizes_millisecond_utterances(self):
        http = FakeBinaryHttp({
            "result": {"text": "课程内容", "utterances": [{"start_time": 500, "end_time": 1500, "text": "课程内容"}]}
        })
        result = VolcengineAsrProvider("secret", http=http).transcribe(self.audio)
        self.assertEqual("课程内容", result.text)
        self.assertEqual(0.5, result.segments[0].start_seconds)
        payload = json.loads(http.calls[0][3].decode("utf-8"))
        self.assertEqual(base64.b64encode(self.audio.read_bytes()).decode("ascii"), payload["audio"]["data"])

    def test_tencent_signature_and_sentences(self):
        response = {
            "code": 0,
            "flash_result": [{
                "text": "腾讯云。",
                "sentence_list": [{"text": "腾讯云。", "start_time": 0, "end_time": 520, "speaker_id": 0}],
            }],
        }
        http = FakeBinaryHttp(response)
        provider = TencentFlashAsrProvider("125", "sid", "skey", "zh", http=http, clock=lambda: 1000)
        result = provider.transcribe(self.audio)
        path = http.calls[0][1].split("https://asr.cloud.tencent.com", 1)[1]
        expected = base64.b64encode(hmac.new(b"skey", ("POSTasr.cloud.tencent.com" + path).encode(), hashlib.sha1).digest()).decode()
        self.assertEqual(expected, http.calls[0][2]["Authorization"])
        self.assertEqual("腾讯云。", result.text)
        self.assertEqual(0.52, result.segments[0].end_seconds)

    def test_baidu_uses_short_wav_chunks(self):
        wav = self.audio.with_suffix(".wav")
        wav.write_bytes(b"RIFF-audio")
        http = FakeBinaryHttp({"err_no": 0, "result": ["百度识别。"]})
        provider = BaiduAsrProvider("secret", "zh", http=http)
        result = provider.transcribe(wav)
        payload = json.loads(http.calls[0][3].decode("utf-8"))
        self.assertEqual("wav", payload["format"])
        self.assertEqual(55, provider.chunk_seconds)
        self.assertEqual("wav", provider.chunk_format)
        self.assertEqual("百度识别。", result.text)


if __name__ == "__main__":
    unittest.main()
