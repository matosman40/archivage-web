"""Recherche des ebooks : une entrée par DOSSIER (une collection de livres),
ou par livre isolé posé à la racine d'une source.

Un dossier « Asimov - Fondation » contenant 7 EPUB et 2 CBZ donne UNE carte :
titre et auteur tirés du nom du dossier, formats et nombre de livres, taille
totale, et la jaquette du premier livre qui en a une (ou une image importée).
"""
import os
import re
from urllib.parse import quote

from . import config, naming

# "Auteur - Titre" (tiret entouré d'espaces) : seul motif fiable pour l'auteur.
_AUTHOR_TITLE_RE = re.compile(r"^(.{2,60}?)\s+-\s+(.+)$")


def _iter_ebook_files(root: str):
    for dirpath, _dirs, files in os.walk(root):
        for f in sorted(files):
            if f.lower().endswith(config.settings.ebook_extensions_tuple):
                yield os.path.join(dirpath, f)


def split_author_title(stem: str) -> tuple[str | None, str]:
    m = _AUTHOR_TITLE_RE.match(stem)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, stem.strip()


def cover_url(path: str) -> str:
    """Adresse de la jaquette d'un dossier ou d'un livre de la bibliothèque."""
    return f"/api/ebooks/cover?path={quote(path, safe='')}"


def _entry(path: str, name: str, books: list[str], is_dir: bool) -> dict:
    total, newest = 0, 0
    for b in books:
        try:
            st = os.stat(b)
            total += st.st_size
            newest = max(newest, int(st.st_mtime))
        except OSError:
            pass
    if is_dir:
        try:
            newest = max(newest, int(os.stat(path).st_mtime))
        except OSError:
            pass
        stem = name
    else:
        stem = os.path.splitext(name)[0]
    author, raw_title = split_author_title(stem)
    title, year = naming.guess_title_year(raw_title)
    formats = sorted({os.path.splitext(b)[1].lstrip(".").upper() or "?" for b in books})
    return {
        "path": path,
        "name": name,
        "title": title,
        "author": author,
        "folder": name if is_dir else os.path.basename(os.path.dirname(path)),
        "year": year,
        "year_source": "fichier" if year else None,
        "size": total,
        "format": " · ".join(formats) if formats else "?",
        "books": len(books),
        "is_collection": is_dir,
        "added_at": newest or None,
        "added_source": "fichier" if newest else None,
        "poster_url": cover_url(path),
    }


def search_ebooks(term: str) -> list[dict]:
    term_lower = term.lower().strip()
    exts = config.settings.ebook_extensions_tuple
    results: list[dict] = []

    for source in config.settings.source_dirs_ebooks:
        if not os.path.isdir(source):
            continue
        try:
            names = sorted(os.listdir(source), key=str.lower)
        except OSError:
            continue
        for name in names:
            if name.startswith("."):
                continue
            full = os.path.join(source, name)
            if os.path.isdir(full) and not os.path.islink(full):
                books = list(_iter_ebook_files(full))
                if not books:
                    continue
                if term_lower and term_lower not in name.lower() \
                        and not any(term_lower in os.path.basename(b).lower() for b in books):
                    continue
                results.append(_entry(full, name, books, is_dir=True))
            elif os.path.isfile(full) and name.lower().endswith(exts):
                if term_lower and term_lower not in name.lower():
                    continue
                results.append(_entry(full, name, [full], is_dir=False))
    return results


def title_author(path: str) -> tuple[str, str | None]:
    """Titre et auteur d'une entrée (dossier ou livre), d'après son nom."""
    name = os.path.basename(path)
    stem = name if os.path.isdir(path) else os.path.splitext(name)[0]
    author, raw = split_author_title(stem)
    title, _year = naming.guess_title_year(raw)
    return title, author
