"""AI DJ interlude generation: Gemini LLM script + TTS -> MP3."""

import logging
import os
import subprocess
import tempfile
import threading
import uuid
import wave
from datetime import datetime
from zoneinfo import ZoneInfo

from database import set_config

logger = logging.getLogger(__name__)

# Sentinel track ID used when Liquidsoap reports a DJ interlude started.
# Not a real tracks.id — handled specially in /internal/track-started.
DJ_SENTINEL = "dj-interlude"


def _dj_dir() -> str:
    media_dir = os.environ.get("MEDIA_DIR", "/media")
    path = os.path.join(media_dir, "dj")
    os.makedirs(path, exist_ok=True)
    return path


def _round_time_label(dt: datetime) -> str:
    """Return a natural round-number time label, e.g. '3 o\'clock PM' or 'half past 2 PM'."""
    minute = dt.minute
    hour = dt.hour % 12 or 12
    next_hour = ((dt.hour + 1) % 12) or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    next_ampm = "AM" if (dt.hour + 1) % 24 < 12 else "PM"

    if minute < 8:
        return f"{hour} o'clock {ampm}"
    elif minute < 23:
        return f"quarter past {hour} {ampm}"
    elif minute < 38:
        return f"half past {hour} {ampm}"
    elif minute < 53:
        return f"quarter to {next_hour} {next_ampm}"
    else:
        return f"{next_hour} o'clock {next_ampm}"


def _generate_script(recent_tracks: list[dict], next_track: dict, ct_label: str, pt_label: str) -> str:
    """Generate a DJ script using Gemini Flash Lite."""
    from google import genai  # type: ignore[import]

    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY not configured")

    client = genai.Client(api_key=api_key)

    recent_lines = "\n".join(f'- "{t["title"]}" by {t["artist"]}, submitted by {t["submitter"]}' for t in recent_tracks)

    ad_examples = (
        "artisanal bird sticker makers, a pet rock boarding kennel, "
        "a financial investment firm that only deals in Monopoly money, "
        "phone screen protectors that completely block all light to reduce phone usage"
    )
    prompt = (
        "You are a warm, witty FM radio DJ hosting a cozy family internet radio station"
        " where real people submit their favourite songs.\n\n"
        "Write a short radio DJ interlude, about 90 words when spoken aloud. Structure it as:\n"
        "1. A warm recap of the last few songs and who submitted them\n"
        f"2. A fake radio ad (~20 seconds) for a silly, well-meaning-but-useless small business."
        f" Think Portlandia energy: {ad_examples}."
        " Earnest, absurdly specific, confident about the value they provide.\n"
        f'3. A time check: "It\'s heading up on {ct_label} Central, {pt_label} Pacific."\n'
        "4. A brief warm intro for the next track.\n\n"
        f"Recent tracks played:\n{recent_lines}\n\n"
        f'Next track: "{next_track["title"]}" by {next_track["artist"]},'
        f" submitted by {next_track['submitter']}\n\n"
        "Write only spoken words. No stage directions, no brackets, no sound effect descriptions."
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=prompt,
    )
    return response.text.strip()


def _synthesize_to_mp3(script: str, output_path: str):
    """Synthesize script to MP3 via Gemini TTS -> PCM -> WAV -> ffmpeg."""
    from google import genai  # type: ignore[import]
    from google.genai import types  # type: ignore[import]

    api_key = os.environ.get("GOOGLE_API_KEY", "")
    client = genai.Client(api_key=api_key)

    tts_prompt = (
        "Speak like a warm, enthusiastic FM radio DJ — friendly, clear, "
        f"natural energy, a smile in your voice: {script}"
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash-preview-tts",
        contents=tts_prompt,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Charon"))
            ),
        ),
    )

    pcm_bytes = response.candidates[0].content.parts[0].inline_data.data

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav_file:
        wav_path = wav_file.name

    try:
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit signed PCM
            wf.setframerate(24000)
            wf.writeframes(pcm_bytes)

        result = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "ffmpeg",
                "-y",
                "-i",
                wav_path,
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "128k",
                "-ar",
                "44100",
                "-metadata",
                f"comment={DJ_SENTINEL}",
                output_path,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {result.stderr}")
    finally:
        if os.path.exists(wav_path):
            os.unlink(wav_path)


def generate_interlude(recent_tracks: list[dict], next_track: dict, estimated_play_time: datetime) -> str:
    """Generate a DJ interlude MP3. Returns the file path."""
    ct = estimated_play_time.astimezone(ZoneInfo("America/Chicago"))
    pt = estimated_play_time.astimezone(ZoneInfo("America/Los_Angeles"))
    ct_label = _round_time_label(ct)
    pt_label = _round_time_label(pt)

    logger.info(
        "Generating DJ interlude: next=%r ct=%s pt=%s",
        next_track["title"],
        ct_label,
        pt_label,
    )

    script = _generate_script(recent_tracks, next_track, ct_label, pt_label)
    logger.info("DJ script: %.120s", script)

    clip_path = os.path.join(_dj_dir(), f"{uuid.uuid4()}.mp3")
    _synthesize_to_mp3(script, clip_path)
    logger.info("DJ interlude written to %s", clip_path)
    return clip_path


def trigger_generation(recent_tracks: list[dict], next_track: dict, estimated_play_time: datetime):
    """Kick off DJ generation in a background daemon thread.

    On success, sets dj_pending_file in config.
    On failure, logs the error and leaves dj_pending_file empty so
    get_next_track falls through to dj_reserved_track_id as the fallback.
    """

    def _run():
        try:
            path = generate_interlude(recent_tracks, next_track, estimated_play_time)
            set_config("dj_pending_file", path)
        except Exception:
            logger.error("DJ interlude generation failed", exc_info=True)

    threading.Thread(target=_run, daemon=True, name="dj-generator").start()
