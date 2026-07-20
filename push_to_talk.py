#!/usr/bin/env python3
"""
Local push-to-talk dictation for macOS (Apple Silicon).

Hold a key, talk, release. Your speech runs through the Parakeet model
(locally, via MLX) and the resulting text is injected into whatever app
has focus, as if you typed it.

Runs as a long-lived process: the model is loaded once and stays resident,
and the microphone stream stays open the whole time (recording is just
gated by a flag) so there's no per-utterance warmup cost.

See README.md for the macOS permissions you must grant.
"""

import os
import sys
import time
import queue
import tempfile
import threading
import subprocess

import numpy as np
import sounddevice as sd
import soundfile as sf
from pynput import keyboard

# ------------------------------- config ------------------------------------

# Push-to-talk key. Right Command. Alternatives: keyboard.Key.f8,
# keyboard.Key.alt_r (right Option), etc.
PTT_KEY = keyboard.Key.cmd_r

# Model weights are hosted in a public S3 bucket and cached locally on first
# run (they're too big for git). We download config.json + model.safetensors
# into MODEL_DIR, which parakeet-mlx's from_pretrained() loads as a plain dir.
#
# Overrides:
#   PTT_MODEL      - a HuggingFace id or an existing local model dir; skips S3
#   PTT_MODEL_DIR  - where to cache the downloaded weights
#   PTT_MODEL_S3   - base URL of the bucket holding the files below
_REPO_DIR = os.path.dirname(os.path.abspath(__file__))

MODEL_DIR = os.environ.get("PTT_MODEL_DIR", os.path.join(_REPO_DIR, "model"))

S3_BASE_URL = os.environ.get(
    "PTT_MODEL_S3",
    "https://rburton5403-push-to-talk-model.s3.us-east-2.amazonaws.com",
)

# Local filename -> S3 object key. The keys are the HuggingFace cache blob
# hashes as uploaded; if the model is re-uploaded, update these.
_MODEL_FILES = {
    "config.json": (
        "models--mlx-community--parakeet-tdt-0.6b-v2/blobs/"
        "8955c588b5549ef70811f2121c6c8bda33508992"
    ),
    "model.safetensors": (
        "models--mlx-community--parakeet-tdt-0.6b-v2/blobs/"
        "b958c37a6baa6874a279108755c8f2818e27bf647d72d54800a234a421341dfe"
    ),
}

# Parakeet expects 16 kHz mono audio.
SAMPLE_RATE = 16000

# Ignore taps shorter than this (avoids empty transcriptions on accidental taps).
MIN_SECONDS = 0.3

# Inject via clipboard paste (True, robust + fast + handles Unicode) or by
# simulating individual keystrokes (False, no clipboard side effects).
USE_CLIPBOARD_PASTE = False

# Play a short system sound on start/stop of recording.
PLAY_CUES = True

# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[ptt] {msg}", flush=True)


def play_cue(start: bool) -> None:
    if not PLAY_CUES:
        return
    # Built-in macOS sounds; fire-and-forget so we never block recording.
    sound = "/System/Library/Sounds/Tink.aiff" if start else "/System/Library/Sounds/Pop.aiff"
    try:
        subprocess.Popen(
            ["afplay", sound],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


# ------------------------------ injection ----------------------------------

_kb = keyboard.Controller()


def _pbpaste() -> str:
    try:
        return subprocess.run(
            ["pbpaste"], capture_output=True, text=True, check=True
        ).stdout
    except Exception:
        return ""


def _pbcopy(text: str) -> None:
    subprocess.run(["pbcopy"], input=text, text=True, check=True)


def inject_text(text: str) -> None:
    if not text:
        return
    if USE_CLIPBOARD_PASTE:
        previous = _pbpaste()
        _pbcopy(text)
        # Give the pasteboard a beat to settle before sending Cmd+V.
        time.sleep(0.05)
        with _kb.pressed(keyboard.Key.cmd):
            _kb.press("v")
            _kb.release("v")
        # Restore the user's old clipboard after the paste lands.
        time.sleep(0.15)
        _pbcopy(previous)
    else:
        _kb.type(text)


# ----------------------------- audio + model -------------------------------


class Recorder:
    """Always-open input stream; recording gated by a flag."""

    def __init__(self):
        self._frames: list[np.ndarray] = []
        self._recording = False
        self._lock = threading.Lock()
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status):  # noqa: ARG002
        if status:
            log(f"audio status: {status}")
        with self._lock:
            if self._recording:
                self._frames.append(indata.copy())

    def start(self) -> None:
        with self._lock:
            self._frames = []
            self._recording = True

    def stop(self) -> np.ndarray:
        with self._lock:
            self._recording = False
            if not self._frames:
                return np.zeros(0, dtype="float32")
            return np.concatenate(self._frames).flatten()


def _fmt_mib(n: int) -> str:
    return f"{n / (1 << 20):.0f}"


def _download(url: str, dest: str, timeout: float = 30.0) -> None:
    """Stream a URL to dest atomically (via a .part file), with live progress.

    Resumes a partial .part file via an HTTP Range request. `timeout` applies
    to each socket operation, so a stalled connection raises instead of hanging
    forever (it does not cap the total transfer time).
    """
    import urllib.request

    tmp = dest + ".part"
    have = os.path.getsize(tmp) if os.path.exists(tmp) else 0

    req = urllib.request.Request(url)
    if have:
        req.add_header("Range", f"bytes={have}-")

    to_tty = sys.stdout.isatty()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resuming = resp.status == 206  # server honored the Range request
        remaining = int(resp.headers.get("Content-Length", 0))
        total = (have + remaining) if resuming else remaining
        if not resuming:
            have = 0  # server ignored Range; restart from scratch
        done = have
        mode = "ab" if resuming else "wb"

        if resuming:
            log(f"  resuming at {_fmt_mib(have)} MiB")

        start = time.monotonic()
        last_emit = 0.0
        next_pct = 10
        with open(tmp, mode) as f:
            while True:
                chunk = resp.read(1 << 20)  # 1 MiB
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)

                now = time.monotonic()
                elapsed = now - start
                speed = (done - have) / elapsed / (1 << 20) if elapsed > 0 else 0
                if to_tty:
                    # Live single-line progress, refreshed a few times a second.
                    if now - last_emit >= 0.25 or done == total:
                        last_emit = now
                        pct = f"{done * 100 // total}%" if total else "?"
                        eta = (
                            f" eta {int((total - done) / (speed * (1 << 20)))}s"
                            if total and speed > 0
                            else ""
                        )
                        sys.stdout.write(
                            f"\r[ptt]   {pct}  {_fmt_mib(done)}/"
                            f"{_fmt_mib(total) if total else '?'} MiB  "
                            f"{speed:.1f} MB/s{eta}   "
                        )
                        sys.stdout.flush()
                elif total and done * 100 // total >= next_pct:
                    # Non-TTY (log file / launchd): one line per 10%.
                    log(f"  {done * 100 // total}%  {_fmt_mib(done)}/"
                        f"{_fmt_mib(total)} MiB  {speed:.1f} MB/s")
                    next_pct += 10
        if to_tty:
            sys.stdout.write("\n")
            sys.stdout.flush()
    os.replace(tmp, dest)


def ensure_model() -> str:
    """Return a local model dir, downloading weights from S3 if not cached."""
    override = os.environ.get("PTT_MODEL")
    if override:
        return override  # HuggingFace id or an existing local path

    os.makedirs(MODEL_DIR, exist_ok=True)
    for fname, key in _MODEL_FILES.items():
        dest = os.path.join(MODEL_DIR, fname)
        if os.path.exists(dest):
            continue
        log(f"downloading {fname} from S3 (first run only)...")
        try:
            _download(f"{S3_BASE_URL}/{key}", dest)
        except Exception as e:
            raise SystemExit(
                f"failed to download {fname}: {e}\n"
                f"  URL: {S3_BASE_URL}/{key}\n"
                "  Check the bucket is reachable and the objects are public."
            )
    return MODEL_DIR


def load_model():
    model_path = ensure_model()
    log(f"loading model from {model_path}...")
    from parakeet_mlx import from_pretrained

    model = from_pretrained(model_path)
    log("model ready.")
    return model


def transcribe(model, audio: np.ndarray) -> str:
    # Write to a temp wav so we use parakeet-mlx's most stable code path.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        path = tmp.name
    try:
        sf.write(path, audio, SAMPLE_RATE)
        result = model.transcribe(path)
        return (getattr(result, "text", "") or "").strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# -------------------------------- main loop --------------------------------


def main() -> None:
    recorder = Recorder()
    work: "queue.Queue[np.ndarray]" = queue.Queue()
    pressed = threading.Event()  # debounce key auto-repeat
    ready = threading.Event()

    def worker():
        # Load the model here so it lives on the SAME thread that runs
        # inference. parakeet-mlx's custom Metal kernels capture the GPU
        # stream of whatever thread loads the model; calling them from any
        # other thread raises "There is no Stream(gpu, 0) in current thread."
        model = load_model()
        ready.set()
        while True:
            audio = work.get()
            secs = len(audio) / SAMPLE_RATE
            if secs < MIN_SECONDS:
                log(f"ignored short clip ({secs:.2f}s)")
                continue
            log(f"transcribing {secs:.1f}s...")
            t0 = time.time()
            try:
                text = transcribe(model, audio)
            except Exception as e:
                log(f"transcription error: {e}")
                continue
            log(f"done in {time.time() - t0:.1f}s: {text!r}")
            if text:
                inject_text(text)

    threading.Thread(target=worker, daemon=True).start()
    ready.wait()  # block until the model is loaded before accepting input

    def on_press(key):
        if key == PTT_KEY and not pressed.is_set():
            pressed.set()
            play_cue(start=True)
            recorder.start()

    def on_release(key):
        if key == PTT_KEY and pressed.is_set():
            pressed.clear()
            play_cue(start=False)
            work.put(recorder.stop())

    log(f"ready. hold {PTT_KEY} to talk. ctrl-c to quit.")
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("bye.")
        sys.exit(0)
