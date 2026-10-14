"""Recherche et indexation des films (taille, qualité, audio, dates, année)."""
import os

from . import adult_media, config, detect, naming


def _iter_video_files(root: str):
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.lower().endswith(config.settings.video_extensions_tuple):
                yield os.path.join(dirpath, f)


def search_movies(term: str) -> list[dict]:
    term_lower = term.lower().strip()
    results: list[dict] = []
    marked = adult_media.marked_paths()
    seen: set[str] = set()

    # Films et films adultes : même recherche, le drapeau « adult » les distingue.
    sources = config.settings.source_dirs_films + config.settings.source_dirs_adultes
    for source in sources:
        if not os.path.isdir(source):
            continue
        for path in _iter_video_files(source):
            name = os.path.basename(path)
            if path in seen:  # un fichier de deux dossiers sources n'est listé qu'une fois
                continue
            seen.add(path)
            if term_lower and term_lower not in name.lower():
                continue
            try:
                st = os.stat(path)
                size, mtime = st.st_size, int(st.st_mtime)
            except OSError:
                size, mtime = 0, None

            title, year = naming.guess_title_year(name)
            quality = detect.detect_quality(name)
            audio = detect.detect_audio(name)

            results.append({
                "path": path,
                "name": name,
                "title": title,
                "folder": os.path.basename(os.path.dirname(path)),
                "year": year,
                "year_source": "fichier" if year else None,
                "size": size,
                "quality": quality,
                "quality_score": detect.quality_score(quality),
                "audio": audio,
                "audio_score": detect.audio_score(audio),
                "added_at": mtime,
                "added_source": "fichier" if mtime else None,
                "adult": adult_media.is_adult_path(path, marked),
            })

    return results
