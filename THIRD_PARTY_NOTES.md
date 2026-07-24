# Third-Party Notes

This repository is a clean-room implementation. No source code, comments, UI, or internal naming has been copied from the reference projects.

Public projects reviewed for high-level workflow ideas:

- `cjgao2022/xiaoe-to-md`: timestamped transcript workflow.
- `yihan498/xiaoe-replay-audio-capture`: resumable course manifest workflow.
- `Arturio-Kanami/-------------`: browser-assisted media capture and audio-only workflow.
- `jackwener/opencli`: session-expiry checks and interactive browser login recovery patterns.

Official documentation reviewed before implementation:

- Alibaba Cloud Model Studio Qwen-ASR API and non-real-time speech recognition guides.
- OpenAI Audio Transcriptions API.
- Groq Speech-to-Text API.
- Deepgram pre-recorded audio API.
- Volcengine BigModel AUC Turbo recognition API.
- Tencent Cloud flash file transcription API and HMAC-SHA1 signing guide.
- Baidu Cloud short-speech recognition API.
- Chrome for Developers remote debugging changes and Chrome DevTools Protocol.
- Codex CLI local help for ephemeral execution, read-only sandboxing, output schemas, and final-message files.
- Python `http.server` documentation and its local-development security warning.

Runtime dependencies:

- `imageio-ffmpeg`: BSD-2-Clause Python wrapper that provides the ffmpeg executable. The bundled ffmpeg binary retains its own applicable license.
- `websocket-client`: Apache-2.0 WebSocket client used to communicate with the system Chrome CDP endpoint.

No Playwright browser binary is installed or launched by this project.
