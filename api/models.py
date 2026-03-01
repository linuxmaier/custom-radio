from dataclasses import dataclass


@dataclass
class Track:
    id: str
    title: str
    artist: str
    submitter: str
    source_type: str  # 'upload' | 'youtube' | 'spotify'
    source_url: str | None
    file_path: str | None
    duration_s: float | None
    tempo_bpm: float | None
    rms_energy: float | None
    spectral_centroid: float | None
    zero_crossing_rate: float | None
    status: str  # 'pending' | 'processing' | 'ready' | 'failed'
    error_msg: str | None
    submitted_at: str
    ready_at: str | None


@dataclass
class Job:
    id: int
    track_id: str
    status: str  # 'pending' | 'processing' | 'done' | 'failed'
    created_at: str
    started_at: str | None
    finished_at: str | None
    error_msg: str | None


@dataclass
class AudioFeatures:
    tempo_bpm: float
    rms_energy: float
    spectral_centroid: float
    zero_crossing_rate: float
