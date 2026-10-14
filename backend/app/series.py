"""Recherche et regroupement des séries par 'pack' (même nom, plusieurs sources).

Pour chaque dossier de série on parcourt l'arborescence UNE seule fois pour
obtenir : taille, nombre d'épisodes, saisons détectées, date la plus récente
d'un fichier vidéo (= date d'ajout côté disque).
"""
import os
import re

from . import config, naming

_EPISODE_RE = re.compile(r"[Ss](\d{1,2})[Ee]\d{1,3}")
_SEASON_FOLDER_RE = re.compile(r"(?:season|saison)[\s._-]*(\d{1,2})\b", re.IGNORECASE)
_SEASON_FOLDER_SHORT_RE = re.compile(r"^[Ss](\d{1,2})$")


def _season_from_folder(folder_name: str) -> int | None:
    m = _SEASON_FOLDER_RE.search(folder_name) or _SEASON_FOLDER_SHORT_RE.match(folder_name)
    return int(m.group(1)) if m else None


def scan_pack(path: str) -> dict:
    size = 0
    episodes = 0
    seasons: set[int] = set()
    newest = 0.0
    video_exts = config.settings.video_extensions_tuple

    for dirpath, _dirs, files in os.walk(path):
        folder_season = _season_from_folder(os.path.basename(dirpath))
        for f in files:
            full = os.path.join(dirpath, f)
            try:
                st = os.stat(full)
            except OSError:
                continue
            size += st.st_size
            if not f.lower().endswith(video_exts):
                continue
            episodes += 1
            newest = max(newest, st.st_mtime)
            m = _EPISODE_RE.search(f)
            if m:
                seasons.add(int(m.group(1)))
            elif folder_season is not None:
                seasons.add(folder_season)

    return {
        "size": size,
        "episodes": episodes,
        "season_numbers": sorted(seasons),
        "added_at": int(newest) or None,
    }


def search_series(term: str) -> list[dict]:
    term_lower = term.lower().strip()
    packs: dict[str, list[str]] = {}
    order: list[str] = []

    for source in config.settings.source_dirs_series:
        if not os.path.isdir(source):
            continue
        try:
            entries = sorted(os.listdir(source))
        except OSError:
            continue

        for name in entries:
            full = os.path.join(source, name)
            if not os.path.isdir(full):
                continue
            if term_lower and term_lower not in name.lower():
                continue
            if name not in packs:
                packs[name] = []
                order.append(name)
            packs[name].append(full)

    results = []
    for name in order:
        agg = {"size": 0, "episodes": 0, "added_at": None}
        seasons_union: set[int] = set()
        has_video = False
        for p in packs[name]:
            info = scan_pack(p)
            if info["episodes"] == 0:
                continue
            has_video = True
            agg["size"] += info["size"]
            agg["episodes"] += info["episodes"]
            if info["added_at"]:
                agg["added_at"] = max(agg["added_at"] or 0, info["added_at"])
            seasons_union.update(info["season_numbers"])
        if not has_video:
            continue  # pas de vidéo : ce n'est pas une série exploitable

        title, year = naming.guess_title_year(name)
        results.append({
            "name": name,
            "title": title,
            "year": year,
            "year_source": "fichier" if year else None,
            "paths": packs[name],
            "size": agg["size"],
            "source_count": len(packs[name]),
            "episodes": agg["episodes"],
            "seasons": len(seasons_union) or None,
            "added_at": agg["added_at"],
            "added_source": "fichier" if agg["added_at"] else None,
        })
    return results


def season_number(folder_name: str) -> int | None:
    """Numéro de saison d'un dossier (« Season 1 », « Saison 02 », « S03 »), sinon None."""
    return _season_from_folder(folder_name)


def seasons_of(show: str) -> dict:
    """Saisons d'une série : un sous-dossier par saison (« Season 1 », « S01 »…).

    Un dossier sans vidéo est ignoré. Les fichiers vidéo posés directement à la
    racine de la série ne sont rattachés à aucune saison : ils ne partent qu'avec
    l'envoi de la série entière (`root_episodes` / `root_size` les comptent)."""
    video_exts = config.settings.video_extensions_tuple
    try:
        names = sorted(os.listdir(show))
    except OSError:
        names = []

    seasons = []
    root_episodes = 0
    root_size = 0
    for name in names:
        if name.startswith("."):
            continue
        full = os.path.join(show, name)
        if os.path.islink(full):
            continue
        if os.path.isfile(full):
            if name.lower().endswith(video_exts):
                try:
                    root_size += os.path.getsize(full)
                except OSError:
                    continue
                root_episodes += 1
            continue
        if not os.path.isdir(full):
            continue
        info = scan_pack(full)
        if info["episodes"] == 0:
            continue
        seasons.append({
            "name": name,
            "path": full,
            "season": _season_from_folder(name),
            "episodes": info["episodes"],
            "size": info["size"],
            "added_at": info["added_at"],
        })
    seasons.sort(key=lambda s: (s["season"] is None, s["season"] or 0, s["name"].lower()))
    return {"seasons": seasons, "root_episodes": root_episodes, "root_size": root_size}
