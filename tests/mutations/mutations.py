"""Vérification des gardes (version 50) : retire une garde à la fois dans une copie du backend et relance
la suite de tests. Une mutation « attrapée » fait échouer au moins un test ; « NON ATTRAPÉE » signale une garde
que les tests ne protègent pas.

Usage : python tests/mutations/mutations.py [--seulement NOM]
Chaque ancre doit être présente exactement une fois, sinon la mutation est signalée « ancre introuvable ».
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

RACINE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PYTHON = os.environ.get("PYTHON", sys.executable)

# (nom, fichier relatif au backend, texte exact d'origine, texte muté)
MUTATIONS = [
    ("arr : année inconnue acceptée", "app/arr.py",
     'if candidats and not ident["year"]:', "if False:"),
    ("arr : deleteFiles=true envoyé", "app/arr.py",
     'params={"deleteFiles": "false", "addImportListExclusion": "true"}',
     'params={"deleteFiles": "true", "addImportListExclusion": "true"}'),
    ("arr : addImportListExclusion oublié", "app/arr.py",
     'params={"deleteFiles": "false", "addImportListExclusion": "true"}',
     'params={"deleteFiles": "false", "addImportListExclusion": "false"}'),
    ("arr : mode désactiver ignoré", "app/arr.py",
     "if desactiver:", "if False:"),
    ("main : erreur Radarr ne bloque plus la suppression des fichiers", "app/main.py",
     '    except arr.ArrError as exc:\n        raise HTTPException(exc.status, str(exc))\n    for r in resultats:',
     '    except arr.ArrError as exc:\n        log_action("ALERTE", "ARR", str(exc))\n        return\n    for r in resultats:'),
    ("main : clé Radarr renvoyée en clair", "app/main.py",
     '    data["radarr_api_key"] = _masque(data.get("radarr_api_key") or "", 4)',
     '    data["radarr_api_key"] = data.get("radarr_api_key") or ""'),
    ("main : clé Sonarr absente des messages d'erreur", "app/main.py",
     "config.settings.radarr_api_key, config.settings.sonarr_api_key):",
     "config.settings.radarr_api_key):"),
    ("main : /api/arr/test sans jeton d'accès", "app/main.py",
     '"/api/search", "/api/plan/films",', '"/api/search", "/api/arr/test", "/api/plan/films",'),
    ("1fichier : taille jamais comparée", "app/onefichier.py",
     "taille_ok = (taille_distante == taille_attendue) if", "taille_ok = True if"),
    ("1fichier : erreur réseau marque corrompu", "app/onefichier.py",
     'elif result.get("corrompue"):', "else:"),
    ("1fichier : test réussi n'efface pas la marque", "app/onefichier.py",
     '            if result["ok"] is True:\n                current["corrompu"] = False',
     '            if result["ok"] is True:\n                pass'),
    ("1fichier : renvoi d'une archive encore présente", "app/onefichier.py",
     'if entry.get("link_ok") is not False:\n        raise OneFichierError("seule une archive absente',
     'if False:\n        raise OneFichierError("seule une archive absente'),
    ("1fichier : retrait d'une entrée non absente", "app/onefichier.py",
     'if not entry or entry.get("status") != "done" or entry.get("link_ok") is not False:',
     "if not entry:"),
    ("1fichier : historique sans limite", "app/onefichier.py",
     'd["check_history"] = historique[-HISTORY_MAX_CHECKS:]', 'd["check_history"] = historique'),
    ("1fichier : bilan hebdomadaire OK non envoyé", "app/onefichier.py",
     'if trigger == "planifié" and not (report["links_lost"]', 'if False and not (report["links_lost"]'),
]


def appliquer(copie, fichier, ancien, nouveau):
    chemin = os.path.join(copie, fichier)
    with open(chemin, encoding="utf-8") as fh:
        texte = fh.read()
    if texte.count(ancien) != 1:
        return False
    with open(chemin, "w", encoding="utf-8") as fh:
        fh.write(texte.replace(ancien, nouveau))
    return True


IGNORES = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "node_modules")


def copier_depot(destination):
    """Copie tout le dépôt : certains tests lisent des fichiers à côté du backend (scripts/, frontend/)."""
    shutil.copytree(RACINE, destination, ignore=IGNORES)
    return os.path.join(destination, "backend")


def lancer_suite(backend_copie):
    return subprocess.run(
        [PYTHON, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-W", "ignore"],
        cwd=backend_copie, capture_output=True, text=True, timeout=900,
    ).returncode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seulement", help="ne lancer que la mutation dont le nom contient ce texte")
    args = parser.parse_args()
    base = tempfile.mkdtemp(prefix="mutations-")
    try:
        ref = copier_depot(os.path.join(base, "reference"))
        if lancer_suite(ref) != 0:
            print("La suite n'est pas verte sans mutation : arrêt.")
            return 2
        print("Référence : suite verte.")
        resultats = []
        for nom, fichier, ancien, nouveau in MUTATIONS:
            if args.seulement and args.seulement not in nom:
                continue
            copie_racine = os.path.join(base, "m")
            shutil.rmtree(copie_racine, ignore_errors=True)
            copie = copier_depot(copie_racine)
            if not appliquer(copie, fichier, ancien, nouveau):
                resultats.append((nom, "ancre introuvable"))
                print(f"ANCRE   {nom}")
                continue
            code = lancer_suite(copie)
            statut = "attrapée" if code != 0 else "NON ATTRAPÉE"
            resultats.append((nom, statut))
            print(f"{'OK     ' if code != 0 else 'FAIBLE '} {nom} : {statut}")
    finally:
        shutil.rmtree(base, ignore_errors=True)
    manquees = [n for n, s in resultats if s != "attrapée"]
    print(f"\n{len(resultats) - len(manquees)}/{len(resultats)} mutation(s) attrapée(s).")
    return 1 if manquees else 0


if __name__ == "__main__":
    sys.exit(main())
