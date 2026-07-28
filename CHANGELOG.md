# Changelog

## Unreleased - 2026-07-29

### Fixed

- Restored live-replay capture by waiting for `get_lookback_list` instead of
  stopping early on the unrelated classroom quiz API.
- Added standalone audio capture through `audio.info.get`.
- Kept the latest signed URL when several responses expose the same media path.
- Reloaded an already-open lesson before recapture so an interrupted Ego task can
  produce fresh network events.
- Prevented course text such as "scan to watch" from being treated as an expired
  login session.
- Required both missing media evidence and a failed account-session check before
  reporting that the Xiaoe login has expired.

### Changed

- Split remote HLS/video audio acquisition from local m4a conversion.
- Reused a completed network-stage file when conversion is interrupted.
- Added download and conversion percentages, throughput or ffmpeg speed, and ETA.
- Added a pre-download table for complete, partial, pending, no-media, and
  downloaded-awaiting-conversion lessons.
- Aligned Chinese and Latin CLI table columns by terminal display width.
- Classified explicit text, audio, video, and live-replay catalog entries so
  known no-media items skip unnecessary browser waits.

### Verified

- Account-session check through the configured Xiaoe learning-center page.
- Live replay, standalone audio, and standalone video source resolution.
- 137 automated tests covering the CLI, browser adapters, downloader, ASR,
  structuring, persistence, and resume behavior.
