"""Garde-fous de sécurité, repris du script bash.

Ces fonctions sont volontairement séparées et testées indépendamment de la
logique métier : ce sont elles qui empêchent qu'une destination chevauche
une source, ou qu'un mode "dossier parent" supprime accidentellement une
bibliothèque entière.
"""
import os

from . import config


def is_under_source_dirs(candidate: str) -> bool:
    """True si `candidate` est égal à, contenu dans, ou contient une source."""
    candidate = os.path.normpath(candidate)
    for src in config.settings.all_source_dirs:
        src = os.path.normpath(src)
        if candidate == src or candidate.startswith(src + os.sep) or src.startswith(candidate + os.sep):
            return True
    return False


def is_configured_source_dir(candidate: str, source_dirs: list[str]) -> bool:
    candidate = os.path.normpath(candidate)
    return any(candidate == os.path.normpath(s) for s in source_dirs)


def folder_is_dedicated(directory: str, own_file: str, extensions: tuple = None) -> bool:
    """True si `directory` ne contient AUCUN autre fichier du même type que
    `own_file` (extensions vidéo par défaut ; passe `extensions` pour un
    autre type, ex. les ebooks)."""
    if extensions is None:
        extensions = config.settings.video_extensions_tuple
    own_file = os.path.normpath(own_file)
    for root, _dirs, files in os.walk(directory):
        for f in files:
            if not f.lower().endswith(extensions):
                continue
            full = os.path.normpath(os.path.join(root, f))
            if full != own_file:
                return False
    return True


def is_inside_source_dirs(candidate: str) -> bool:
    """True seulement si `candidate` est STRICTEMENT à l'intérieur d'un dossier
    source (ou égal à un de ses sous-éléments). Contrairement à
    is_under_source_dirs, un chemin qui CONTIENT une source (ex. la racine
    /MVE/MEDIA au-dessus de /MVE/MEDIA/FILMS) est refusé : c'est ce qui évite
    qu'une opération de copie/déplacement/suppression porte sur une
    arborescence plus large que la sélection."""
    candidate = os.path.normpath(candidate)
    for src in config.settings.all_source_dirs:
        src = os.path.normpath(src)
        if candidate.startswith(src + os.sep):
            return not _passe_par_un_lien(src, candidate)
    return False


def _passe_par_un_lien(src: str, candidate: str) -> bool:
    """True si un DOSSIER situé entre la source et l'élément est un lien symbolique : l'opération
    sortirait alors de la source (un lien de dossier pointe ailleurs). L'élément lui-même n'est
    pas testé : un lien de fichier se supprime sans toucher à sa cible."""
    courant = src
    for morceau in os.path.relpath(candidate, src).split(os.sep)[:-1]:
        courant = os.path.join(courant, morceau)
        if os.path.islink(courant):
            return True
    return False


def source_is_removable(path: str) -> bool:
    """Vérifie, AVANT toute copie, qu'on pourra retirer `path` de sa source
    (déplacer ou supprimer). Détecte un montage :ro ou des droits refusés :
    sinon on copierait tout à destination puis on échouerait à supprimer la
    source, ce qui crée des doublons.

    Vérification au niveau du dossier parent (et du dossier lui-même pour un
    dossier) — les sous-dossiers restent vérifiés pendant l'opération."""
    path = os.path.normpath(path)
    parent = os.path.dirname(path)
    if not os.access(parent, os.W_OK | os.X_OK):
        return False
    if os.path.isdir(path) and not os.path.islink(path):
        if not os.access(path, os.W_OK | os.X_OK):
            return False
    return True
