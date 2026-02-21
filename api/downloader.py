import logging
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)

MEDIA_DIR = os.environ.get("MEDIA_DIR", "/media")


def download_youtube(url: str, track_id: str) -> tuple[str, str, str]:
    """
    Download a YouTube video as MP3.
    Returns (title, artist, output_path).
    """
    output_template = os.path.join(MEDIA_DIR, "raw", f"{track_id}.%(ext)s")
    os.makedirs(os.path.join(MEDIA_DIR, "raw"), exist_ok=True)

    cmd = [
        "yt-dlp",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--output", output_template,
        "--no-playlist",
        "--write-info-json",
        "--quiet",
        url,
    ]

    logger.info(f"Downloading YouTube: {url}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {result.stderr}")

    output_path = os.path.join(MEDIA_DIR, "raw", f"{track_id}.mp3")
    info_path = os.path.join(MEDIA_DIR, "raw", f"{track_id}.info.json")

    title = "Unknown Title"
    artist = "Unknown Artist"

    if os.path.exists(info_path):
        import json
        with open(info_path) as f:
            info = json.load(f)
        title = info.get("title", title)
        # YouTube videos often have uploader as artist
        artist = info.get("artist") or info.get("uploader") or artist
        os.unlink(info_path)

    if not os.path.exists(output_path):
        # yt-dlp may have saved with a different extension first
        for ext in ["webm", "m4a", "opus"]:
            candidate = os.path.join(MEDIA_DIR, "raw", f"{track_id}.{ext}")
            if os.path.exists(candidate):
                output_path = candidate
                break
        else:
            raise RuntimeError(f"Downloaded file not found for track {track_id}")

    return title, artist, output_path



def convert_to_standard_mp3(input_path: str, track_id: str, comment_tag: str) -> str:
    """
    Convert any audio file to standard MP3/128kbps with ID3 comment tag.
    Returns the output path.
    """
    output_dir = os.path.join(MEDIA_DIR, "tracks")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"{track_id}.mp3")

    cmd = [
        "ffmpeg",
        "-i", input_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-ab", "128k",
        "-ar", "44100",
        "-ac", "2",
        "-id3v2_version", "3",
        "-metadata", f"comment={comment_tag}",
        "-y",
        output_path,
    ]

    logger.info(f"Converting {input_path} -> {output_path}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed: {result.stderr[-500:]}")

    # Clean up raw file
    if os.path.exists(input_path) and input_path != output_path:
        os.unlink(input_path)

    return output_path
