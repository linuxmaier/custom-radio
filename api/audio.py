import logging

import librosa  # ty: ignore[unresolved-import]
import numpy as np
from models import AudioFeatures

logger = logging.getLogger(__name__)


def extract_features(file_path: str) -> AudioFeatures:
    """Extract audio features from an MP3 file using librosa."""
    logger.info(f"Extracting features from {file_path}")

    y, sr = librosa.load(file_path, sr=None, mono=True, duration=120.0)

    # Harmonic/percussive separation for cleaner feature extraction
    y_harmonic, y_percussive = librosa.effects.hpss(y)

    # Tempo (BPM) from percussive signal
    tempo, _ = librosa.beat.beat_track(y=y_percussive, sr=sr)
    tempo_bpm = float(np.atleast_1d(tempo)[0])

    # Short-time Fourier transform for spectral features
    S = np.abs(librosa.stft(y))

    # RMS energy
    rms = librosa.feature.rms(S=S).mean()
    rms_energy = float(rms)

    # Spectral centroid
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr).mean()
    spectral_centroid = float(centroid)

    # Zero crossing rate
    zcr = librosa.feature.zero_crossing_rate(y).mean()
    zero_crossing_rate = float(zcr)

    logger.info(
        f"Features: tempo={tempo_bpm:.1f} rms={rms_energy:.4f} "
        f"centroid={spectral_centroid:.1f} zcr={zero_crossing_rate:.4f}"
    )

    return AudioFeatures(
        tempo_bpm=tempo_bpm,
        rms_energy=rms_energy,
        spectral_centroid=spectral_centroid,
        zero_crossing_rate=zero_crossing_rate,
    )


def normalize_features(features: AudioFeatures, mins: dict, maxs: dict) -> np.ndarray:
    """Normalize features to 0-1 range using running min/max."""

    def norm(val, lo, hi):
        if hi == lo:
            return 0.0
        return (val - lo) / (hi - lo)

    return np.array(
        [
            norm(features.tempo_bpm, mins["tempo_bpm"], maxs["tempo_bpm"]),
            norm(features.rms_energy, mins["rms_energy"], maxs["rms_energy"]),
            norm(
                features.spectral_centroid,
                mins["spectral_centroid"],
                maxs["spectral_centroid"],
            ),
            norm(
                features.zero_crossing_rate,
                mins["zero_crossing_rate"],
                maxs["zero_crossing_rate"],
            ),
        ]
    )


def euclidean_distance(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.sqrt(np.sum((a - b) ** 2)))
