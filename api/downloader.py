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


def download_spotify(url: str, track_id: str) -> tuple[str, str, str]:
    """
    Download a Spotify track via spotdl.
    Returns (title, artist, output_path).
    Raises RuntimeError on failure (spotdl is known to be unstable as of Feb 2026).
    """
    output_dir = os.path.join(MEDIA_DIR, "raw")
    os.makedirs(output_dir, exist_ok=True)

    cmd = [
        "spotdl",
        "--output", os.path.join(output_dir, "{track-id}"),
        "--format", "mp3",
        "--bitrate", "128k",
        url,
    ]

    logger.info(f"Downloading Spotify: {url}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=output_dir)

    if result.returncode != 0:
        raise RuntimeError(
            f"spotdl failed (Spotify API may be unstable): {result.stderr[:500]}"
        )

    # spotdl names files as "Artist - Title.mp3"
    # Find the newest MP3 in output_dir
    mp3_files = [
        os.path.join(output_dir, f)
        for f in os.listdir(output_dir)
        if f.endswith(".mp3") and not f.startswith(track_id)
    ]
    if not mp3_files:
        raise RuntimeError("spotdl succeeded but no MP3 file found")

    newest = max(mp3_files, key=os.path.getmtime)

    # Parse title/artist from filename "Artist - Title.mp3"
    basename = os.path.splitext(os.path.basename(newest))[0]
    if " - " in basename:
        artist, title = basename.split(" - ", 1)
    else:
        artist = "Unknown Artist"
        title = basename

    # Rename to track_id
    final_path = os.path.join(output_dir, f"{track_id}_raw.mp3")
    os.rename(newest, final_path)

    return title.strip(), artist.strip(), final_path


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
