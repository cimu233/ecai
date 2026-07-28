# Xiaoe Audio Pipeline

An original local-first pipeline for authorized Xiaoe courses:

`Xiaoe catalog -> audio-only download -> local Qwen3-ASR -> Codex structure -> Markdown`

The CLI owns all write operations. A loopback read API is available for a future optional frontend.

The desktop launcher shows courses in a ten-row paginated picker. Menu items 5, 6, and 7 accept a course number and pass the course ID automatically.

Double-click `Xiaoe Audio Pipeline.command` in the project root, or use the
desktop forwarding launcher, to open the complete terminal menu.

## Install

```bash
cd "/Users/cimu_lumi/Desktop/【項目】小工具/xiaoe-tools/xiaoe-audio-pipeline"
/usr/bin/python3 -m pip install --user -e .
```

For development without installing the command:

```bash
PYTHONPATH=src /usr/bin/python3 -m xiaoe_cli --help
```

Runtime data defaults to `~/.xiaoe-audio-pipeline`. Use `--data-dir` or `XIAOE_DATA_DIR` to change it.

## First Run

1. Open the managed system Chrome profile and complete Xiaoe login once:

```bash
xiaoe auth start --url "YOUR_AUTHORIZED_COURSE_URL"
```

The project launches the installed Google Chrome application with a dedicated
local profile. Chrome stores its own session cookies locally. Optional automatic
login credentials are read from macOS Keychain first, with a private local file
available as a fallback.

Microsoft Edge is supported through the same Chromium/CDP implementation while
keeping its managed login data in a separate local profile:

```bash
xiaoe browser use edge --json
xiaoe auth start --url "YOUR_AUTHORIZED_COURSE_URL"
```

Ego Lite is also supported. It runs the automation in an isolated Task Space and
inherits the login state stored by Ego:

```bash
xiaoe browser use ego --json
xiaoe auth start --url "YOUR_AUTHORIZED_COURSE_URL"
```

Complete interactive login in the handed-off Ego Task Space, then run the normal
`auth check`, `course refresh`, `download`, or `run` commands. Switch back at any time:

```bash
xiaoe browser use chrome --json
xiaoe browser current --json
```

Use `--browser chrome`, `--browser edge`, or `--browser ego` before a command for
a one-off override without changing the saved selection.

2. Scan the purchased-course list, or add one authorized course manually:

```bash
xiaoe course scan-account --json
xiaoe course add "YOUR_AUTHORIZED_COURSE_URL" --title "Course Name" --json
xiaoe course refresh COURSE_ID --json
```

`scan-account` opens the saved Xiaoe store session, reads every page under
“我的课程”, and imports course containers into the local course list. Individual
live-session purchases remain in `~/.xiaoe-audio-pipeline/account-catalog.raw.json`
for auditing and are not imported as standalone courses. If several Xiaoe store
origins already exist in the local list, each origin is scanned once.

3. Prepare the isolated local ASR runtime. It reads the Qwen3-ASR model already downloaded by OpenLess:

```bash
./scripts/setup_local_asr.sh
```

The runtime lives in `.local-asr-venv`. It does not change `PATH` or shell configuration and does not copy the model weights. Desktop menu item 10 selects another ASR service and stores its credentials in macOS Keychain.

Supported ASR providers:

| Provider | Default model/interface | Credential |
| --- | --- | --- |
| Local | Qwen3-ASR 1.7B on Apple MPS | None |
| Alibaba | Qwen3-ASR-Flash | DashScope API Key |
| Volcengine | BigModel AUC Turbo | X-Api-Key |
| Tencent Cloud | Flash file transcription | AppID + SecretID + SecretKey |
| Baidu Cloud | Short speech recognition | API Key |
| OpenAI | GPT-4o mini Transcribe | API Key |
| Groq | Whisper Large V3 Turbo | API Key |
| Deepgram | Nova-3 | API Key |
| Custom | OpenAI-compatible transcription | API Key + Base URL |

Inspect or change the non-secret selection from the CLI:

```bash
xiaoe asr providers --json
xiaoe asr current --json
xiaoe asr use openai --model gpt-4o-mini-transcribe
```

4. Run the complete workflow:

```bash
xiaoe run COURSE_ID --language zh --json
```

Run a small learning batch first:

```bash
xiaoe run COURSE_ID --limit 1 --language zh --json
```

## Stage Commands

```bash
xiaoe download COURSE_ID --limit 1 --json
xiaoe transcribe COURSE_ID --limit 1 --language zh --json
xiaoe structure COURSE_ID --limit 1 --json
xiaoe status --json
```

Completed artifacts are checksum-checked and skipped on later runs. Use `--force-transcription` or `--force-structure` with `xiaoe run` when regeneration is intentional.

Authentication checks:

```bash
xiaoe auth login --json
xiaoe auth check --url "YOUR_AUTHORIZED_COURSE_URL" --json
xiaoe auth check --url "YOUR_AUTHORIZED_COURSE_URL" --recover --json
xiaoe auth credentials --template --json
xiaoe auth stop
```

When `--recover` finds an expired login, it first reads `xiaoe.login.username`
and `xiaoe.login.password` from macOS Keychain. If they are absent, it reads the
private `~/.xiaoe-audio-pipeline/xiaoe-login.json` file. The desktop launcher
prints that path when credentials are missing and never opens another app. Its
permissions are forced to `600`.

The password form at `https://study.xiaoe-tech.com/#/acount` is submitted in the
selected browser's managed profile. Automated browser work always stays in the
background. When QR code, image code, slider, SMS, or device verification is
required, the CLI prints the login URL so the user can open it manually later.
Desktop menu item 1 runs `auth login` directly and never asks for a course URL.

## Processing Details

- Account discovery reads the authorized “我的课程” list and imports columns, large columns, camps, memberships, and course catalogs.
- Course discovery listens to authorized page JSON responses and uses Xiaoe `resource_id` as the stable lesson key.
- Direct audio preserves original bytes and supports HTTP Range resume.
- HLS prefers an independent audio rendition; mixed video streams are reduced to their first audio track.
- Standard AES-128 HLS is handled by ffmpeg. SAMPLE-AES and DRM are reported as unsupported.
- Long audio is split locally into provider-sized, 16 kHz mono chunks. Baidu uses 55-second WAV chunks; the other current providers use four-minute MP3 chunks. The local Qwen worker loads the model once and transcribes all chunks through Apple MPS.
- Raw provider responses, normalized transcripts, plain text, structured JSON, and Markdown notes are all retained.
- Codex runs ephemerally in a read-only sandbox and returns schema-constrained note data.

Per-lesson output:

```text
audio.source.<ext>
download.json
asr_chunks/chunk_0000.mp3
transcript.raw.json
transcript.json
transcript.txt
notes.data.json
notes.md
```

## Local API

```bash
xiaoe serve --port 8765
```

Read endpoints:

- `GET /api/health`
- `GET /api/status`
- `GET /api/courses`
- `GET /api/courses/{course_id}/lessons`

The service binds only to `127.0.0.1`. It is intended for a local frontend and has no remote-production security model.

## Current Verification Boundary

Automated tests cover database migration, paginated course selection, Chrome session helpers, direct and HLS downloads, AES-128, cloud and persistent local ASR adapters, cloud request signing/normalization, chunk merging, Codex output handling, pipeline orchestration, and the local API.

The authorized Xiaoe course catalog, paginated picker, media-source capture, download start, and local Qwen inference have been live-tested. Cloud adapters have protocol-level tests; each cloud service still needs a first live request with your own credential. Interactive login challenges still require user action when Xiaoe expires the saved session.

## Safety

Use this project only with content you own or are explicitly authorized to download. Runtime files, browser profile data, cookies, media, transcripts, and notes are excluded from Git.
