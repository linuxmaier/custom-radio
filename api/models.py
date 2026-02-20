from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Track:
    id: str
    title: str
    artist: str
    submitter: str
    source_type: str  # 'upload' | 'youtube' | 'spotify'
    source_url: Optional[str]
    file_path: Optional[str]
    duration_s: Optional[float]
    tempo_bpm: Optional[float]
    rms_energy: Optional[float]
    spectral_centroid: Optional[float]
    zero_crossing_rate: Optional[float]
    status: str  # 'pending' | 'processing' | 'ready' | 'failed'
    error_msg: Optional[str]
    submitted_at: str
    ready_at: Optional[str]


@dataclass
class Job:
    id: int
    track_id: str
    status: str  # 'pending' | 'processing' | 'done' | 'failed'
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    error_msg: Optional[str]


@dataclass
class AudioFeatures:
    tempo_bpm: float
    rms_energy: float
    spectral_centroid: float
    zero_crossing_rate: float
