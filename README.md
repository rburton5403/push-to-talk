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
- [Homebrew](https://brew.sh) and `ffmpeg` (used to decode the recorded audio):

  ```bash
  brew install ffmpeg
  ```

## Setup

```bash
cd ~/repos/push-to-talk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # Python deps; ffmpeg is installed separately (above)
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

The process needs **three** permissions in **System Settings → Privacy &
Security**. Crucially, the OS attaches them to *whatever launched python* —
your terminal app when you run it by hand, or the `.venv/bin/python3` binary
when launchd starts it. Running from **Terminal**? Grant all three to
**Terminal** (using another terminal like iTerm? grant that instead).

| Permission | Why |
|---|---|
| **Microphone** | record your voice |
| **Input Monitoring** | detect the global push-to-talk key |
| **Accessibility** | inject the transcribed text as keystrokes |

Enable each one — the panes are all under **System Settings → Privacy &
Security**:

1. **Accessibility** → find **Terminal** in the list and toggle it **on** (if
   it's not listed, click **+**, then Applications → Utilities → Terminal).
2. **Input Monitoring** → toggle **Terminal** **on** the same way.
3. **Microphone** → toggle **Terminal** **on**. (macOS also prompts for this
   automatically the first time the app records — you can just click *Allow*.)

Notes:

- After you enable a permission, macOS may ask you to **quit and reopen
  Terminal** — do it, so the new permission takes effect.
- If Terminal then refuses to relaunch ("*Terminal is not open anymore*"),
  **restart the Mac** — it clears the stale launch state, and the permissions
  stay granted.
- If the key isn't detected or text won't inject afterward, it's almost always
  a permission granted to the *wrong* binary. Using launchd instead of a
  terminal? Add `.venv/bin/python3` explicitly (in the picker, `⌘⇧G` → paste
  the full path).
- You'll also get a Gatekeeper prompt the first time — allow it.

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
