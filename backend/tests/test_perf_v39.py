"""Performances (version 39) : les marques adultes sont lues une fois par liste, pas une fois par élément."""
import pytest

from app import adult_media


def _count_reads(monkeypatch):
    reads = {"n": 0}
    real = adult_media.marked_paths

    def counting():
        reads["n"] += 1
        return real()

    monkeypatch.setattr(adult_media, "marked_paths", counting)
    return reads


@pytest.fixture
def items():
    return [{"original_path": f"/srv/film{i}.mkv", "adult": False} for i in range(500)]


def test_filter_visible_lit_les_marques_une_seule_fois(env, monkeypatch, items):
    reads = _count_reads(monkeypatch)
    assert len(adult_media.filter_visible(items, False)) == 500
    assert reads["n"] == 1, "une seule lecture de library-adult.json pour toute la liste"


def test_session_ouverte_ne_lit_pas_les_marques(env, monkeypatch, items):
    reads = _count_reads(monkeypatch)
    assert len(adult_media.filter_visible(items, True)) == 500
    assert reads["n"] == 0


def test_film_marque_reste_masque_verrouille(env, items):
    adult_media.set_marked([items[3]["original_path"]], True)
    visible = adult_media.filter_visible(items, False)
    assert len(visible) == 499
    assert items[3] not in visible
    assert len(adult_media.filter_visible(items, True)) == 500


def test_is_adult_item_accepte_des_marques_deja_lues(env, monkeypatch):
    adult_media.set_marked(["/srv/x.mkv"], True)
    reads = _count_reads(monkeypatch)
    marks = adult_media.marked_paths()  # une lecture, faite par l'appelant
    assert reads["n"] == 1
    assert adult_media.is_adult_item({"original_path": "/srv/x.mkv"}, marks)
    assert not adult_media.is_adult_item({"original_path": "/srv/y.mkv"}, marks)
    assert reads["n"] == 1, "avec les marques passées en argument, aucune nouvelle lecture"
