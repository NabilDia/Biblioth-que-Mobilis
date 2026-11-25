import time
from datetime import UTC, date, datetime

import pytest
from conftest import ADRESSE, CODES, FausseApi

from mobilis import bornes
from mobilis.bornes import (
    ClientBornes,
    ErreurBornes,
    ErreurTemporaire,
    EtatStation,
    Photo,
    comparer,
    photographier,
    reessayer,
)
from mobilis.stations import Station, Statut


@pytest.fixture
def pauses(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    liste: list[float] = []
    monkeypatch.setattr("mobilis.bornes.time.sleep", liste.append)
    return liste


def test_reessayer_respecte_retry_after(pauses: list[float]) -> None:
    reponses = iter([ErreurTemporaire("HTTP 429", 1.0), ErreurTemporaire("HTTP 503"), "ok"])

    @reessayer()
    def appel() -> str:
        reponse = next(reponses)
        if isinstance(reponse, ErreurTemporaire):
            raise reponse
        return str(reponse)

    assert appel() == "ok"
    assert pauses[0] == 1.0
    assert 0.2 <= pauses[1] <= 0.6  # deuxième tentative : 0,4 s à 50 % près


def test_reessayer_abandonne(pauses: list[float]) -> None:
    appels = 0

    @reessayer(tentatives=3)
    def appel() -> None:
        nonlocal appels
        appels += 1
        raise ErreurTemporaire("HTTP 504")

    with pytest.raises(ErreurTemporaire):
        appel()
    assert (appels, len(pauses)) == (3, 2)


def test_reessayer_ignore_les_erreurs_definitives(pauses: list[float]) -> None:
    @reessayer()
    def appel() -> None:
        raise ErreurBornes("HTTP 404")

    with pytest.raises(ErreurBornes):
        appel()
    assert pauses == []


def test_retry_after_demesure_est_borne() -> None:
    assert bornes.attente(ErreurTemporaire("HTTP 429", 3600), 1) == bornes.ATTENTE_MAX


@pytest.mark.parametrize("statut", [429, 503, 504])
def test_client_reessaie(api: FausseApi, monkeypatch: pytest.MonkeyPatch, statut: int) -> None:
    api.erreurs = {"/stations/MB-0001": [statut]}
    vues: list[ErreurTemporaire] = []

    def sans_attendre(erreur: ErreurTemporaire, tentative: int) -> float:
        vues.append(erreur)
        return 0.0

    monkeypatch.setattr(bornes, "attente", sans_attendre)
    assert ClientBornes(ADRESSE, "cle").etat("MB-0001").velos == 5
    assert [e.retry_after for e in vues] == [1.0 if statut == 429 else None]


def test_mauvaise_cle(api: FausseApi) -> None:
    with pytest.raises(ErreurBornes, match="HTTP 401"):
        photographier(ClientBornes(ADRESSE, "mauvaise"))


def test_photographier_toutes_les_pages_sans_depasser_8_appels(
    api: FausseApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    api.lenteur = 0.02
    api.erreurs = {"/stations/MB-0007": [504] * 10}
    monkeypatch.setattr(bornes, "attente", lambda erreur, tentative: 0.0)
    photo = photographier(ClientBornes(ADRESSE, "cle"))
    assert photo.codes == set(CODES)
    assert photo.injoignables == {"MB-0007": "HTTP 504"}
    assert 1 < api.maximum <= 8


def test_photographier_respecte_la_limite(api: FausseApi) -> None:
    api.lenteur = 0.3  # la liste prend 0,6 s ; tous les états, au moins 1,5 s
    depart = time.monotonic()
    photo = photographier(ClientBornes(ADRESSE, "cle"), limite=1.0)
    assert time.monotonic() - depart < 1.3
    assert photo.codes == set(CODES)
    assert "délai dépassé" in photo.injoignables.values()


def test_liste_trop_lente(api: FausseApi) -> None:
    api.lenteur = 0.3
    with pytest.raises(ErreurBornes, match="liste des stations"):
        photographier(ClientBornes(ADRESSE, "cle"), limite=0.1)


def etat(code: str, velos: int, places: int) -> EtatStation:
    return EtatStation(code, velos, 0, places, datetime(2026, 11, 24, tzinfo=UTC))


def test_vides_et_pleines() -> None:
    photo = Photo(
        etats={c: etat(c, *v) for c, v in {"A": (0, 5), "B": (5, 0), "C": (2, 3)}.items()}
    )
    assert (photo.vides, photo.pleines) == (["A"], ["B"])


def test_comparer() -> None:
    def station(code: str, statut: Statut, capacite: int | None = 10) -> Station:
        return Station(code, "", "", capacite, None, None, statut, date(2020, 1, 1))

    stations = {
        s.code: s
        for s in (
            station("OK", Statut.EN_SERVICE),
            station("AUTRE_CAPACITE", Statut.EN_SERVICE),
            station("SANS_CAPACITE", Statut.EN_SERVICE, None),
            station("ABSENTE", Statut.EN_SERVICE),
            station("FERMEE", Statut.FERMEE),
            station("FERMEE_VUE", Statut.FERMEE),
            station("HS", Statut.HORS_SERVICE),
            station("MUETTE", Statut.EN_SERVICE),
        )
    }
    vus = ("OK", "SANS_CAPACITE", "FERMEE_VUE", "HS", "NOUVELLE")
    photo = Photo(
        etats={**{c: etat(c, 4, 6) for c in vus}, "AUTRE_CAPACITE": etat("AUTRE_CAPACITE", 4, 8)},
        injoignables={"MUETTE": "HTTP 504"},
    )
    ecarts = comparer(photo, stations)
    assert ecarts.inconnues == ["NOUVELLE"]
    assert ecarts.absentes == ["ABSENTE"]
    assert ecarts.fermees_presentes == ["FERMEE_VUE"]
    assert ecarts.hors_service_qui_repondent == ["HS"]
    assert ecarts.capacites == {"AUTRE_CAPACITE": (10, 12)}
