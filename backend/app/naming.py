"""Extraction du titre et de l'année depuis un nom de fichier ou de dossier.

Un même moteur sert à l'affichage, à la recherche TMDB et au repli d'année.
Règles :
- l'année entre parenthèses est prioritaire : "Blade Runner 2049 (2017)"
  donne titre "Blade Runner 2049", année 2017 ;
- sinon la première année isolée qui n'ouvre pas le nom : "Inception.2010.1080p"
  donne "Inception", 2010 ;
- une année en tête suivie d'un séparateur est lue comme année : "2006-Idiocracy"
  donne "Idiocracy", 2006 ;
- tout ce qui suit l'année (tags de release) est ignoré.
"""
import re

MEDIA_EXT_RE = re.compile(
    r"\.(mkv|mp4|avi|m4v|mov|ts|wmv|epub|mobi|azw3?|pdf|cbz|cbr|djvu)$", re.IGNORECASE
)
PAREN_YEAR_RE = re.compile(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]")
STANDALONE_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
# Année en tête, style « 2006-Idiocracy.Mike Judge… » (les points sont déjà remplacés par des espaces).
LEADING_YEAR_RE = re.compile(r"^((?:19|20)\d{2})[\s-]+(?=\S)")
SEASON_START_RE = re.compile(r"(S\d|Saison\b|Season\b)", re.IGNORECASE)  # « 1923 S01 » : c'est une série
BRACKET_RE = re.compile(r"[\[(].*?[\])]")
RELEASE_TAG_RE = re.compile(
    r"\b(2160p|1080p|720p|576p|480p|4k|uhd|hdr(10)?|dv|bluray|blu-ray|web-?dl|webrip|"
    r"dvdrip|hdtv|x264|x265|hevc|h264|h265|aac|dts(-?hd)?|ddp?5?1?|atmos|truehd|multi|"
    r"vostfr|vff?2?|truefrench|french|remux|repack|proper|hybrid|imax|extended|"
    r"theatrical|cut|edition|vost|tmdbid-\d+)\b",
    re.IGNORECASE,
)


def _pick_year(stem: str) -> tuple[int | None, str]:
    """Retourne (année, texte avant l'année). Voir la docstring du module."""
    paren = list(PAREN_YEAR_RE.finditer(stem))
    if paren:
        m = paren[-1]
        return int(m.group(1)), stem[: m.start()]

    tag = RELEASE_TAG_RE.search(stem)
    limit = tag.start() if tag else len(stem)
    # Dans "Blade Runner 2049 2017 2160p", l'année du film est celle qui précède
    # les tags de release : la dernière année isolée avant ce point.
    candidates = [
        m for m in STANDALONE_YEAR_RE.finditer(stem)
        if m.start() > 0 and m.end() <= limit
    ]
    if candidates:
        m = candidates[-1]
        return int(m.group(1)), stem[: m.start()]
    lead = LEADING_YEAR_RE.match(stem)
    if lead and not SEASON_START_RE.match(stem[lead.end():]):
        # Aucune autre année : c'est celle du début, et le titre suit.
        return int(lead.group(1)), stem[lead.end():]
    return None, stem


def guess_title_year(name: str) -> tuple[str, int | None]:
    """Retourne (titre lisible, année ou None). Approximatif par nature."""
    stem = MEDIA_EXT_RE.sub("", name)
    stem = stem.replace("_", " ").replace(".", " ")

    year, head = _pick_year(stem)

    title = BRACKET_RE.sub(" ", head)
    title = RELEASE_TAG_RE.sub(" ", title)
    title = re.sub(r"[^\w\s'&-]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        title = re.sub(r"\s+", " ", stem).strip() or name
    return title, year
