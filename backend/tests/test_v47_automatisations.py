"""Version 47 : automatisations (règles « quand … alors … »).

Les tâches réelles sont remplacées par des faux : on vérifie ce que le moteur décide (quels films,
quelle action, quand, combien d'essais), et que l'API valide ce qu'on lui envoie.
"""
import asyncio
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, main, onefichier, scheduler, workflows

from tests import test_v46_reprises as v46

# Envoi 1fichier simulé (compression, réseau et index réels de la version 46).
envoi = v46.envoi


class Tache:
    """Tâche factice : terminée tout de suite, avec un nombre d'erreurs choisi."""

    def __init__(self, erreurs=0):
        self.status = "done"
        self.events = [{"type": "item_error", "item": f"e{i}"} for i in range(erreurs)]


@pytest.fixture
def moteur(monkeypatch):
    """Moteur isolé : films et tâches fournis par le test, état vide, journal et réglages temporaires."""
    monkeypatch.setattr(config, "ALLOW_DELETE", True)
    monkeypatch.setattr(config.settings, "dry_run", False)
    monkeypatch.setattr(config.settings, "notify_url", "")
    monkeypatch.setattr(config.Settings, "onefichier_enabled", property(lambda self: True))
    monkeypatch.setattr(config.Settings, "tmdb_enabled", property(lambda self: True))
    monkeypatch.setattr(config.settings, "source_dirs_films", ["/sources/films"])
    monkeypatch.setattr(config.settings, "source_dirs_adultes", [])

    anciennes = dict(workflows._fonctions)
    anciens_verrous = dict(workflows._verrous)
    workflows.recharger()
    workflows._verrous.clear()
    workflows._dernier_passage = None

    etat = {"films": [], "lancements": [], "erreurs": 0, "erreurs_par_appel": None, "listages": 0}

    async def lister():
        etat["listages"] += 1
        return [dict(f) for f in etat["films"]]

    async def lancer(action, chemins):
        etat["lancements"].append((dict(action), list(chemins)))
        erreurs = etat["erreurs_par_appel"].pop(0) if etat["erreurs_par_appel"] else etat["erreurs"]
        return Tache(erreurs)

    async def attendre(tache):
        return sum(1 for e in tache.events if e["type"] == "item_error")

    workflows.configurer(lister=lister, lancer=lancer, attendre=attendre)
    etat["maintenant"] = datetime(2026, 10, 14, 12, 0)
    monkeypatch.setattr(workflows, "_maintenant", lambda: etat["maintenant"])
    yield etat
    workflows._fonctions.update(anciennes)
    workflows._verrous.clear()
    workflows._verrous.update(anciens_verrous)
    workflows.recharger()


def film(nom, *, vu=False, note=7.5, genres=("Action",), adult=False):
    return {"path": f"/sources/films/{nom}", "watched": vu, "rating": note,
            "genres": list(genres), "adult": adult}


def regle(**surcharges):
    base = {"nom": "Vus vers disque", "actif": True, "declencheur": "plex_vu", "note_min": None,
            "genre": "", "inclure_deja_vus": False, "quand": "immediat", "heure": "01:00",
            "actions": [{"type": "copier_disque", "destination": "/media/disque1"}]}
    base.update(surcharges)
    return base


def run(coroutine):
    """Exécute la coroutine puis attend les exécutions lancées en fond (elles tournent dans la même boucle)."""
    async def scenario():
        resultat = await coroutine
        await workflows.attendre_taches()
        return resultat
    return asyncio.run(scenario())


# --- Déclenchement -----------------------------------------------------------------------------

def test_creation_ne_traite_pas_les_films_deja_vus(moteur):
    moteur["films"] = [film("A (2001).mkv", vu=True)]
    r = workflows.creer(regle())
    run(workflows.premier_releve(r["id"]))
    assert moteur["lancements"] == []
    assert workflows.trouver(r["id"])["etat"]["connus"] == ["/sources/films/A (2001).mkv"]


def test_film_marque_vu_declenche_la_copie(moteur):
    moteur["films"] = [film("A (2001).mkv")]
    r = workflows.creer(regle())
    run(workflows.premier_releve(r["id"]))
    moteur["films"][0]["watched"] = True
    run(workflows.traiter(workflows.trouver(r["id"]), moteur["films"], forcer=False))
    assert len(moteur["lancements"]) == 1
    action, chemins = moteur["lancements"][0]
    assert action["type"] == "copier_disque" and action["destination"] == "/media/disque1"
    assert chemins == ["/sources/films/A (2001).mkv"]
    assert workflows.trouver(r["id"])["etat"]["file_attente"] == []


def test_un_film_n_est_fait_qu_une_fois(moteur):
    moteur["films"] = [film("A (2001).mkv", vu=True)]
    r = workflows.creer(regle(inclure_deja_vus=True))
    run(workflows.premier_releve(r["id"]))
    assert len(moteur["lancements"]) == 1
    # Relevé suivant : le film est toujours vu, il ne doit pas être recopié.
    run(workflows.traiter(workflows.trouver(r["id"]), moteur["films"], forcer=True))
    assert len(moteur["lancements"]) == 1


def test_nouveau_film_declenche_avec_son_declencheur(moteur):
    moteur["films"] = [film("A (2001).mkv")]
    r = workflows.creer(regle(declencheur="nouveau_film"))
    run(workflows.premier_releve(r["id"]))
    moteur["films"].append(film("B (2005).mkv", vu=False))
    run(workflows.traiter(workflows.trouver(r["id"]), moteur["films"], forcer=False))
    assert [c for _a, c in moteur["lancements"]] == [["/sources/films/B (2005).mkv"]]


def test_films_adultes_jamais_traites(moteur):
    moteur["films"] = [film("X (2001).mkv")]
    r = workflows.creer(regle(inclure_deja_vus=True))
    run(workflows.premier_releve(r["id"]))
    moteur["films"] = [film("Y (2002).mkv", vu=True, adult=True)]
    run(workflows.traiter(workflows.trouver(r["id"]), moteur["films"], forcer=False))
    assert moteur["lancements"] == []


# --- Filtres -----------------------------------------------------------------------------------

def test_filtre_de_note_et_de_genre(moteur):
    moteur["films"] = [film("Base (2000).mkv")]
    r = workflows.creer(regle(note_min=8, genre="Comédie"))
    run(workflows.premier_releve(r["id"]))
    moteur["films"] = [
        film("Faible (2001).mkv", vu=True, note=7.5, genres=("Comédie",)),   # note trop basse
        film("Sans note (2002).mkv", vu=True, note=None, genres=("Comédie",)),  # pas de note : ignoré
        film("Autre genre (2003).mkv", vu=True, note=9, genres=("Drame",)),  # mauvais genre
        film("Retenu (2004).mkv", vu=True, note=8.5, genres=("comédie", "Action")),
    ]
    run(workflows.traiter(workflows.trouver(r["id"]), moteur["films"], forcer=False))
    assert [c for _a, c in moteur["lancements"]] == [["/sources/films/Retenu (2004).mkv"]]


# --- Échecs, essais, actions -------------------------------------------------------------------

def test_echec_retente_puis_est_abandonne(moteur):
    moteur["films"] = [film("Base (2000).mkv")]
    r = workflows.creer(regle())
    run(workflows.premier_releve(r["id"]))
    moteur["films"] = [film("Base (2000).mkv", vu=True)]
    moteur["erreurs"] = 1  # chaque tentative échoue
    for _ in range(workflows.ESSAIS_MAX + 2):
        run(workflows.traiter(workflows.trouver(r["id"]), moteur["films"], forcer=True))
    assert len(moteur["lancements"]) == workflows.ESSAIS_MAX
    etat = workflows.trouver(r["id"])["etat"]
    assert etat["file_attente"] == [], "abandonné : sorti de la file"
    assert "abandonné" in etat["dernier_resultat"]
    assert [h["ok"] for h in workflows.liste_publique()["historique"]] == [False] * workflows.ESSAIS_MAX


def test_actions_enchainees_dans_l_ordre(moteur):
    moteur["films"] = [film("Base (2000).mkv")]
    r = workflows.creer(regle(actions=[
        {"type": "copier_disque", "destination": "/media/disque1"},
        {"type": "envoyer_1fichier", "apres": "garder"},
    ]))
    run(workflows.premier_releve(r["id"]))
    moteur["films"] = [film("Base (2000).mkv", vu=True)]
    run(workflows.traiter(workflows.trouver(r["id"]), moteur["films"], forcer=False))
    types = [a["type"] for a, _c in moteur["lancements"]]
    assert types == ["copier_disque", "envoyer_1fichier"]


def test_simulation_ne_memorise_rien(moteur, monkeypatch):
    monkeypatch.setattr(config.settings, "dry_run", True)
    moteur["films"] = [film("Base (2000).mkv")]
    r = workflows.creer(regle())
    run(workflows.premier_releve(r["id"]))
    moteur["films"] = [film("Base (2000).mkv", vu=True)]
    run(workflows.traiter(workflows.trouver(r["id"]), moteur["films"], forcer=False))
    etat = workflows.trouver(r["id"])["etat"]
    assert etat["faits"] == {} and etat["file_attente"] == ["/sources/films/Base (2000).mkv"]
    assert workflows.liste_publique()["historique"][0]["detail"].startswith("simulation")


def test_notification_sans_fichier_copie(moteur, monkeypatch):
    envoyes = []

    async def faux_envoi(titre, message):
        envoyes.append((titre, message))

    monkeypatch.setattr(workflows.notify, "send", faux_envoi)
    monkeypatch.setattr(config.settings, "notify_url", "https://ntfy.example/t")
    moteur["films"] = [film("Base (2000).mkv", vu=True)]
    r = workflows.creer(regle(actions=[{"type": "notifier"}], inclure_deja_vus=True))
    run(workflows.premier_releve(r["id"]))
    assert len(envoyes) == 1 and "Base (2000).mkv" in envoyes[0][1]
    assert moteur["lancements"] == []


# --- Moment : immédiat ou nuit -----------------------------------------------------------------

def test_nuit_attend_l_heure_puis_passe_une_fois(moteur):
    moteur["films"] = [film("Base (2000).mkv")]
    r = workflows.creer(regle(quand="nuit", heure="01:00"))  # créée à 12:00 : la nuit du 14 est passée
    run(workflows.premier_releve(r["id"]))
    moteur["films"] = [film("Base (2000).mkv", vu=True)]
    run(workflows.traiter(workflows.trouver(r["id"]), moteur["films"], forcer=False))
    assert moteur["lancements"] == [], "la nuit : rien tant que l'heure n'est pas venue"
    assert len(workflows.trouver(r["id"])["etat"]["file_attente"]) == 1

    rule = workflows.trouver(r["id"])
    assert not workflows._heure_atteinte(rule, datetime(2026, 10, 15, 0, 30))
    assert workflows._heure_atteinte(rule, datetime(2026, 10, 15, 1, 5))
    moteur["maintenant"] = datetime(2026, 10, 15, 1, 5)
    run(workflows.executer_nuit(rule))
    assert len(moteur["lancements"]) == 1
    assert not workflows._heure_atteinte(rule, datetime(2026, 10, 15, 3, 0)), "une seule fois par nuit"


def test_regle_nuit_creee_apres_son_heure_attend_le_lendemain(moteur):
    moteur["maintenant"] = datetime(2026, 10, 14, 15, 0)
    r = workflows.creer(regle(quand="nuit", heure="01:00"))
    assert workflows.trouver(r["id"])["etat"]["derniere_nuit"] == "2026-10-14"


# --- Validation --------------------------------------------------------------------------------

@pytest.mark.parametrize("modif, message", [
    ({"nom": "  "}, "nom"),
    ({"actions": []}, "au moins une action"),
    ({"actions": [{"type": "copier_disque", "destination": ""}]}, "disque de destination"),
    ({"actions": [{"type": "copier_disque", "destination": "/sources/films/sous"}]}, "chevauche"),
    ({"actions": [{"type": "deplacer_disque", "destination": "/media/d", "confirme": False}]}, "Coche la confirmation"),
    ({"actions": [{"type": "envoyer_1fichier", "apres": "liberer", "confirme": False}]}, "Coche la confirmation"),
    ({"actions": [{"type": "deplacer_disque", "destination": "/media/d", "confirme": True},
                  {"type": "copier_disque", "destination": "/media/e"}]}, "dernière"),
    ({"actions": [{"type": "deplacer_disque", "destination": "/media/d", "confirme": True},
                  {"type": "envoyer_1fichier", "apres": "liberer", "confirme": True}]}, "Une seule"),
    ({"quand": "nuit", "heure": "25:00"}, "HH:MM"),
])
def test_validation_refuse_les_regles_impossibles(moteur, modif, message):
    with pytest.raises(ValueError) as erreur:
        workflows.creer(regle(**modif))
    assert message in str(erreur.value)


def test_suppression_refusee_sans_allow_delete(moteur, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_DELETE", False)
    with pytest.raises(ValueError, match="ALLOW_DELETE"):
        workflows.creer(regle(actions=[{"type": "deplacer_disque", "destination": "/media/d", "confirme": True}]))


def test_1fichier_et_notification_exigent_leur_configuration(moteur, monkeypatch):
    monkeypatch.setattr(config.Settings, "onefichier_enabled", property(lambda self: False))
    with pytest.raises(ValueError, match="1fichier n'est pas configuré"):
        workflows.creer(regle(actions=[{"type": "envoyer_1fichier", "apres": "garder"}]))
    with pytest.raises(ValueError, match="notification"):
        workflows.creer(regle(actions=[{"type": "notifier"}]))


def test_modification_et_suppression(moteur):
    r = workflows.creer(regle())
    maj = workflows.modifier(r["id"], regle(nom="Renommée", quand="nuit", heure="02:30"))
    assert maj["nom"] == "Renommée" and maj["heure"] == "02:30" and maj["id"] == r["id"]
    workflows.supprimer(r["id"])
    assert workflows.trouver(r["id"]) is None
    with pytest.raises(KeyError):
        workflows.supprimer(r["id"])


# --- Persistance, boucle -----------------------------------------------------------------------

def test_etat_survit_a_un_redemarrage(moteur):
    moteur["films"] = [film("Base (2000).mkv", vu=True)]
    r = workflows.creer(regle())
    run(workflows.premier_releve(r["id"]))
    workflows.recharger()  # relecture du fichier, comme après un redémarrage
    assert workflows.trouver(r["id"]) is not None
    assert workflows.trouver(r["id"])["etat"]["connus"] == ["/sources/films/Base (2000).mkv"]


def test_fichier_illisible_est_mis_de_cote(moteur, tmp_path):
    chemin = workflows._fichier()
    with open(chemin, "w", encoding="utf-8") as fh:
        fh.write("{pas du json")
    workflows.recharger()
    assert workflows.liste_publique()["regles"] == []
    assert (tmp_path / "workflows.json.corrompu").exists()


def test_boucle_ignore_les_regles_inactives(moteur):
    workflows.creer(regle(actif=False))
    run(workflows._tick())
    assert moteur["listages"] == 0


def test_tick_releve_puis_execute_les_regles_immediates(moteur):
    moteur["films"] = [film("Base (2000).mkv", vu=True)]
    workflows.creer(regle(inclure_deja_vus=True))
    run(workflows._tick())
    assert moteur["listages"] == 1 and len(moteur["lancements"]) == 1


def test_historique_et_journal(moteur):
    moteur["films"] = [film("Base (2000).mkv", vu=True)]
    r = workflows.creer(regle(inclure_deja_vus=True))
    run(workflows.premier_releve(r["id"]))
    entree = workflows.liste_publique()["historique"][0]
    assert entree["regle"] == "Vus vers disque" and entree["ok"] is True and entree["elements"] == 1
    journal = config.LOG_FILE
    assert "AUTOMATISATION" in open(journal, encoding="utf-8").read()


# --- API ---------------------------------------------------------------------------------------

@pytest.fixture
def client(moteur, monkeypatch):
    # Les relevés de création et les lancements manuels ne sont pas lancés dans le client de test.
    monkeypatch.setattr(main, "_lancer_en_fond", lambda coroutine: coroutine.close())
    return TestClient(main.app)


def test_api_cycle_de_vie(client):
    reponse = client.post("/api/workflows", json=regle(actions=[{"type": "copier_disque", "destination": "/media/disque1"}]))
    assert reponse.status_code == 201, reponse.text
    identifiant = reponse.json()["id"]

    liste = client.get("/api/workflows").json()
    assert [r["id"] for r in liste["regles"]] == [identifiant]
    assert "copier_disque" in liste["catalogue"]["actions"]
    assert liste["capacites"]["allow_delete"] is True

    maj = client.put(f"/api/workflows/{identifiant}", json=regle(nom="Autre"))
    assert maj.status_code == 200 and maj.json()["nom"] == "Autre"

    assert client.post(f"/api/workflows/{identifiant}/lancer").status_code == 202
    assert client.delete(f"/api/workflows/{identifiant}").json() == {"ok": True}
    assert client.get("/api/workflows").json()["regles"] == []


def test_api_refuse_une_regle_invalide_et_une_action_inconnue(client):
    refus = client.post("/api/workflows", json=regle(actions=[{"type": "deplacer_disque", "destination": "/media/d", "confirme": False}]))
    assert refus.status_code == 400 and "Coche la confirmation" in refus.json()["detail"]
    inconnue = client.post("/api/workflows", json=regle(actions=[{"type": "effacer_tout"}]))
    assert inconnue.status_code == 422


def test_api_404_pour_une_regle_absente(client):
    assert client.put("/api/workflows/inconnue", json=regle()).status_code == 404
    assert client.delete("/api/workflows/inconnue").status_code == 404
    assert client.post("/api/workflows/inconnue/lancer").status_code == 404


def test_api_modifications_protegees_par_le_jeton(client, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_TOKEN", "jeton-de-test-47")
    assert client.post("/api/workflows", json=regle()).status_code == 401
    assert client.get("/api/workflows").status_code == 200
    assert client.post("/api/workflows", json=regle(),
                       headers={"X-Access-Token": "jeton-de-test-47"}).status_code == 201


# --- Bout en bout : vraie copie par les routes de tâches --------------------------------------

def test_integration_copie_reelle_par_les_routes(moteur, tmp_path, monkeypatch):
    """Le moteur crée la tâche avec les mêmes contrôles que l'interface, et le fichier arrive sur le disque."""
    src = tmp_path / "films"
    src.mkdir()
    (src / "Base (2000).mkv").write_bytes(b"x" * 4096)
    disque = tmp_path / "disque" / "Disque1"
    disque.mkdir(parents=True)
    monkeypatch.setattr(config.settings, "source_dirs_films", [str(src)])
    monkeypatch.setattr(config.settings, "safety_margin_bytes", 0)
    # Hors plage de transfert, la copie attendrait : on ouvre toute la journée pour ce test.
    monkeypatch.setattr(scheduler, "active_window",
                        lambda now=None: {"name": "test", "start": "00:00", "end": "23:59",
                                          "onefichier_mbps": 0, "disk_mbps": 0})
    workflows.configurer(lister=main._lister_films_automatisation,
                         lancer=main._lancer_automatisation,
                         attendre=main._attendre_automatisation)
    moteur["films"] = []  # sans effet : le vrai listage est utilisé

    async def scenario():
        r = workflows.creer(regle(declencheur="nouveau_film", inclure_deja_vus=True,
                                  actions=[{"type": "copier_disque", "destination": str(disque)}]))
        await workflows.premier_releve(r["id"])
        return workflows.trouver(r["id"])

    regle_finale = run(scenario())
    copies = list(disque.rglob("Base*.mkv"))
    assert copies, "le fichier doit être copié sur le disque"
    assert regle_finale["etat"]["file_attente"] == []
    assert regle_finale["etat"]["dernier_resultat"] == "ok"
    assert workflows.liste_publique()["historique"][0]["ok"] is True


def test_integration_envoi_1fichier_par_le_moteur(envoi, moteur, monkeypatch):
    """L'action 1fichier passe par la route d'envoi réelle : l'entrée arrive dans l'index, en « done »."""
    monkeypatch.setattr(config.Settings, "onefichier_enabled", property(lambda self: True))
    monkeypatch.setattr(config.Settings, "tmdb_enabled", property(lambda self: True))
    monkeypatch.setattr(config.settings, "source_dirs_films", [str(Path(envoi["unit"]).parents[1])])

    async def lister():
        return [film_chemin(envoi["unit"])]

    workflows.configurer(lister=lister, lancer=main._lancer_automatisation,
                         attendre=main._attendre_automatisation)

    async def scenario():
        r = workflows.creer(regle(declencheur="nouveau_film", inclure_deja_vus=True,
                                  actions=[{"type": "envoyer_1fichier", "apres": "garder"}]))
        await workflows.premier_releve(r["id"])

    run(scenario())
    assert envoi["uploads"] == 1
    assert onefichier._get_entry("101")["status"] == "done"
    assert workflows.liste_publique()["historique"][0]["ok"] is True


def film_chemin(chemin):
    return {"path": chemin, "watched": None, "rating": None, "genres": [], "adult": False}


def test_creation_par_l_api_lance_le_releve_en_fond(moteur):
    """Régression : sans boucle d'événements dans la route, le relevé ne pouvait pas démarrer (erreur 500)."""
    import httpx

    moteur["films"] = [film("Base (2000).mkv", vu=True)]

    async def scenario():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            reponse = await client.post("/api/workflows", json=regle(inclure_deja_vus=True,
                                        actions=[{"type": "copier_disque", "destination": "/media/disque1"}]))
            assert reponse.status_code == 201, reponse.text
            for _ in range(100):  # le relevé tourne en fond : on attend qu'il ait fait son travail
                await asyncio.sleep(0.02)
                if moteur["lancements"]:
                    break
            return reponse.json()

    run(scenario())
    assert len(moteur["lancements"]) == 1, "le relevé en fond doit traiter le film déjà vu"


def test_copie_longue_ne_bloque_ni_la_detection_ni_une_seconde_execution(moteur):
    """Une copie qui attend sa plage de transfert ne bloque pas la surveillance, et une seule exécution tourne par règle."""
    taches = []

    class TacheLente:
        events = []

        def __init__(self, status):
            self.status = status

    async def lancer(action, chemins):
        # La première copie attend ; les suivantes sont déjà terminées.
        tache = TacheLente("pending" if not taches else "done")
        taches.append((tache, list(chemins)))
        return tache

    async def attendre(tache):
        while tache.status != "done":
            await asyncio.sleep(0.01)
        return 0

    async def lister():
        return [dict(f) for f in moteur["films"]]

    workflows.configurer(lister=lister, lancer=lancer, attendre=attendre)
    moteur["films"] = [film("A (2001).mkv")]
    r = workflows.creer(regle())
    run(workflows.premier_releve(r["id"]))

    async def scenario():
        moteur["films"][0]["watched"] = True
        rule = workflows.trouver(r["id"])
        await workflows.traiter(rule, moteur["films"], forcer=False)  # lance la copie de A, qui attend
        await asyncio.sleep(0.05)
        moteur["films"].append(film("B (2002).mkv", vu=True))
        await asyncio.wait_for(workflows.traiter(rule, moteur["films"], forcer=False), timeout=2)  # ne bloque pas
        assert len(taches) == 1, "pas de seconde exécution pendant la première"
        taches[0][0].status = "done"
        await workflows.attendre_taches()
        await workflows.traiter(rule, moteur["films"], forcer=False)  # B part maintenant
        await workflows.attendre_taches()

    run(scenario())
    assert [c for _t, c in taches] == [["/sources/films/A (2001).mkv"], ["/sources/films/B (2002).mkv"]]
    assert workflows.trouver(r["id"])["etat"]["file_attente"] == []
