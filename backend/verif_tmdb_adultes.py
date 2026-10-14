"""Vérification avec une vraie clé TMDB : un film marqué adulte sur TMDB reste caché sans session.

Ce n'est pas un test automatique : il appelle TMDB. Il n'écrit rien dans tes réglages (dossier temporaire),
et n'affiche aucun titre, seulement des identifiants et des résultats.

Usage, depuis le dossier backend :
    TMDB_API_KEY=ta_clé ADULT_TMDB_ID=identifiant_tmdb python3 verif_tmdb_adultes.py

Variables :
    TMDB_API_KEY     ta clé TMDB (obligatoire)
    ADULT_TMDB_ID    identifiant TMDB d'un film marqué adulte sur TMDB (obligatoire)
    NORMAL_TMDB_ID   identifiant d'un film normal, témoin (facultatif, 603 par défaut)

Code de sortie : 0 si tous les contrôles passent, 1 sinon, 2 si les variables manquent.
"""
import asyncio
import os
import sys
import tempfile

ADULT_ID = os.environ.get("ADULT_TMDB_ID", "").strip()
NORMAL_ID = os.environ.get("NORMAL_TMDB_ID", "603").strip()
UNKNOWN_ID = "999999999"  # identifiant qui n'existe pas sur TMDB
CODE = "1234"             # code temporaire, utilisé seulement dans le dossier temporaire


def verifier() -> int:
    key = os.environ.get("TMDB_API_KEY", "").strip()
    if not key or not ADULT_ID.isdigit() or not NORMAL_ID.isdigit():
        print("Il faut TMDB_API_KEY et ADULT_TMDB_ID (identifiant numérique TMDB d'un film marqué adulte).")
        print("NORMAL_TMDB_ID est facultatif (603 par défaut).")
        return 2

    dossier = tempfile.mkdtemp(prefix="verif-adultes-")
    os.environ["SETTINGS_FILE"] = os.path.join(dossier, "settings.json")
    os.environ["LOG_FILE"] = os.path.join(dossier, "log.txt")
    os.environ["ONEFICHIER_INDEX_FILE"] = os.path.join(dossier, "idx", "1fichier-index.json")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    from fastapi.testclient import TestClient
    from app import adult_auth, main as application, tmdb

    resultats = []

    def controle(libelle: str, ok: bool) -> None:
        resultats.append(bool(ok))
        print(("OK      " if ok else "ÉCHEC  ") + libelle)

    def contenu(fiche: dict) -> bool:
        return any(fiche.get(k) for k in ("cast", "directors", "writers", "studios"))

    try:
        try:
            adulte = asyncio.run(tmdb.credits(int(ADULT_ID)))
            normal = asyncio.run(tmdb.credits(int(NORMAL_ID)))
        except Exception as exc:  # réseau, clé refusée, délai dépassé
            print(f"TMDB n'a pas répondu ({type(exc).__name__}). Vérifie la clé et la connexion.")
            return 1

        # Sans ces deux pré-requis, une fiche vide ne prouve rien : TMDB injoignable donne aussi une fiche vide.
        if not adulte.get("adult"):
            print(f"Le film {ADULT_ID} n'est pas reconnu comme adulte par TMDB (identifiant erroné, ou TMDB muet).")
            return 1
        if not contenu(normal):
            print(f"Le film témoin {NORMAL_ID} renvoie une fiche vide : TMDB n'a pas répondu ou identifiant erroné.")
            return 1
        controle(f"film {ADULT_ID} marqué adulte sur TMDB", True)
        controle(f"film {NORMAL_ID} renvoie une fiche complète", True)

        client = TestClient(application.app)
        vide = tmdb.empty_credits()

        sans_fiche = client.get(f"/api/tmdb/credits?tmdb_id={ADULT_ID}").json()
        sans_distribution = client.get(f"/api/tmdb/cast?tmdb_id={ADULT_ID}").json()
        inconnu = client.get(f"/api/tmdb/credits?tmdb_id={UNKNOWN_ID}").json()
        controle("sans session : fiche technique vide", sans_fiche == vide)
        controle("sans session : distribution vide", sans_distribution == {"cast": []})
        controle("sans session : réponse identique à un identifiant inconnu", sans_fiche == inconnu)
        controle("sans session : film normal, fiche complète",
                 contenu(client.get(f"/api/tmdb/credits?tmdb_id={NORMAL_ID}").json()))

        adult_auth.set_code(CODE)
        ouverture = client.post("/api/adult/unlock", json={"code": CODE})
        controle("ouverture de session avec le code temporaire", ouverture.status_code == 200)
        controle("avec session : fiche technique complète",
                 contenu(client.get(f"/api/tmdb/credits?tmdb_id={ADULT_ID}").json()))
        controle("avec session : distribution présente",
                 bool(client.get(f"/api/tmdb/cast?tmdb_id={ADULT_ID}").json().get("cast")))

        adult_auth.reset()  # fermeture de la session
        controle("session fermée : masqué à nouveau",
                 client.get(f"/api/tmdb/credits?tmdb_id={ADULT_ID}").json() == vide)
    except Exception as exc:
        print(f"Contrôle interrompu ({type(exc).__name__}) : {exc}")
        return 1
    finally:
        adult_auth.reset()

    print()
    print(f"Bilan : {sum(resultats)}/{len(resultats)} contrôles réussis")
    return 0 if resultats and all(resultats) else 1


if __name__ == "__main__":
    sys.exit(verifier())
