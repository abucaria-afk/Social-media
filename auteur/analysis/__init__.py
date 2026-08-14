"""Watching the dailies.

Everything the agent knows about a piece of footage it measured itself, frame
by frame and sample by sample. No metadata, no filenames, no trust.
"""

from .dossier import ClipDossier, Take, build_dossier, build_dossiers
from .audio import AudioAnalysis, analyse_audio, find_music_bed
from .video import VideoAnalysis, analyse_video

__all__ = [
    "ClipDossier",
    "Take",
    "build_dossier",
    "build_dossiers",
    "AudioAnalysis",
    "analyse_audio",
    "find_music_bed",
    "VideoAnalysis",
    "analyse_video",
]
