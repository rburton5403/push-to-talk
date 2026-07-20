# push-to-talk

Local, offline push-to-talk dictation for **macOS on Apple Silicon**. Hold a
key in any app, talk, release — your speech runs through the
[Parakeet](https://huggingface.co/mlx-community/parakeet-tdt-0.6b-v2) model
locally (via Apple MLX) and the text is typed into the focused app.

Nothing leaves your machine. No app-store signing, no notarization — it just
runs.

## Requirements

- Apple Silicon Mac (M1 or newer). The MLX model needs it.
- Python 3.10+

## Setup

```bash
cd ~/repos/push-to-talk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The model weights (~2.3 GB) live in a public S3 bucket, not in this repo. On
first run they're downloaded into `model/` and cached there; every run after
loads from disk. Env overrides:

- `PTT_MODEL` — a HuggingFace id or an existing local model dir; skips S3.
- `PTT_MODEL_DIR` — where to cache the downloaded weights (default `./model`).
- `PTT_MODEL_S3` — base URL of the bucket holding the weights.

## Run

```bash
source .venv/bin/activate
python3 push_to_talk.py
```

Hold **right Command (⌘)**, speak, release. Edit `PTT_KEY` near the top of
`push_to_talk.py` to change the key.

## macOS permissions (the only fiddly part)

The process needs three permissions in **System Settings → Privacy &
Security**. Crucially, the OS attaches them to *whatever launched python* —
your terminal app when you run it by hand, or the `.venv/bin/python3` binary
when launchd starts it.

| Permission | Why |
|---|---|
| **Microphone** | record your voice |
| **Input Monitoring** | detect the global push-to-talk key |
| **Accessibility** | inject the transcribed text as keystrokes |

If the key isn't detected or text won't inject, it's almost always a missing
permission on the *wrong* binary. Running from Terminal? Grant Terminal. Using
launchd? Add `.venv/bin/python3` explicitly (drag it in with `⌘⇧G` →
the full path).

You'll also get a Gatekeeper prompt the first time — allow it.

## Using it with Claude Code / terminal apps

This is the primary use case, so `USE_CLIPBOARD_PASTE` is set to `False`
(simulated keystrokes). Reasons:

- Claude Code collapses a ⌘V paste into a `[Pasted text]` placeholder, whereas
  typed characters land in the prompt as real, editable text.
- The transcription is `.strip()`'d, so there's no trailing newline — your
  prompt is **not** auto-submitted. Review it, then press Enter yourself.

**Gotcha — Secure Keyboard Entry.** Terminal.app and iTerm2 can block all
synthetic keystrokes. If transcription logs look fine but nothing appears in
Claude Code, turn it off:

- Terminal.app: menu bar → *Terminal* → uncheck *Secure Keyboard Entry*
- iTerm2: menu bar → *iTerm2* → uncheck *Secure Keyboard Entry*

Run push-to-talk in one terminal window and Claude Code in another. Keystrokes
go to whichever has focus when you release the key.

## Run at login (optional)

See `com.rodney.pushtotalk.plist`. Edit the paths inside it, then:

```bash
mkdir -p ~/Library/LaunchAgents
cp com.rodney.pushtotalk.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.rodney.pushtotalk.plist
```

Logs go to `ptt.log`.

## Tuning

All knobs are constants at the top of `push_to_talk.py`:

- `PTT_KEY` — the push-to-talk key.
- `MODEL_DIR` / `S3_BASE_URL` — where weights are cached / fetched from (or set
  the `PTT_MODEL`, `PTT_MODEL_DIR`, `PTT_MODEL_S3` env vars).
- `USE_CLIPBOARD_PASTE` — `True` pastes via ⌘V (fast, robust, briefly uses your
  clipboard and restores it); `False` simulates each keystroke instead.
- `MIN_SECONDS` — ignore accidental short taps.
- `PLAY_CUES` — start/stop sound feedback.

## How it works

The model loads once and stays resident; the mic stream stays open the whole
time and recording is just gated by the key, so there's no per-utterance
warmup. On release, the buffered audio is written to a temp WAV, transcribed,
and injected. Transcription runs on a background thread so the key listener
never blocks.
