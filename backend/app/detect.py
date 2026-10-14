"""Détection de qualité vidéo / format audio à partir du nom de fichier.

Port fidèle de detect_quality / detect_audio / quality_score / audio_score
du script bash d'origine.
"""
import re

_QUALITY_PATTERNS = [
    ("4K", re.compile(r"2160p|4k|uhd")),
    ("1080p", re.compile(r"1080p|full-?hd")),
    ("720p", re.compile(r"720p")),
]
_SD_PATTERN = re.compile(r"576p|480p|(^|[^a-z0-9])sd([^a-z0-9]|$)")

_QUALITY_SCORE = {"4K": 4, "1080p": 3, "720p": 2, "SD": 1, "?": 0}


def detect_quality(name: str) -> str:
    n = name.lower()
    for label, pattern in _QUALITY_PATTERNS:
        if pattern.search(n):
            return label
    if _SD_PATTERN.search(n):
        return "SD"
    return "?"


def quality_score(label: str) -> int:
    return _QUALITY_SCORE.get(label, 0)


_AUDIO_PATTERNS = [
    ("Atmos", re.compile(r"atmos")),
    ("DTS-HD", re.compile(r"dts[-_.]hd")),
    ("DTS", re.compile(r"dts")),
    ("TrueHD", re.compile(r"truehd")),
    ("E-AC3", re.compile(r"eac3|ddp")),
    ("AC3", re.compile(r"ac3")),
    ("FLAC", re.compile(r"flac")),
    ("AAC", re.compile(r"aac")),
    ("MP3", re.compile(r"mp3")),
]
_AUDIO_SCORE = {
    "Atmos": 8, "TrueHD": 7, "DTS-HD": 6, "DTS": 5,
    "E-AC3": 4, "AC3": 3, "FLAC": 2, "AAC": 1, "MP3": 0, "?": 0,
}


def detect_audio(name: str) -> str:
    n = name.lower()
    for label, pattern in _AUDIO_PATTERNS:
        if pattern.search(n):
            return label
    return "?"


def audio_score(label: str) -> int:
    return _AUDIO_SCORE.get(label, 0)
