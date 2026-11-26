import gzip
import tracemalloc
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mobilis.trajets import (
    Trajet,
    TypeVelo,
    compter_flux,
    convertir,
    exploitables,
    lire_trajets,
    profil_horaire,
    sans_doublons,
    vides_le_matin,
)

ENTETE = "trajet_id,abonne_id,velo_id,type_velo,station_depart,debut,station_arrivee,fin,distance_m"
NORMALE = "T1,A1,V1,mecanique,MB-1,2026-08-01T00:04:23+02:00,MB-2,2026-08-01T00:42:17+02:00,6681"


def lire(dossier: Path, *lignes: str) -> list[Trajet]:
    chemin = dossier / "trajets.csv.gz"
    chemin.write_bytes(gzip.compress("\n".join([ENTETE, *lignes]).encode()))
    return list(lire_trajets(chemin))


def test_lit_un_trajet(tmp_path: Path) -> None:
    assert lire(tmp_path, NORMALE) == [
        Trajet(
            trajet_id="T1",
            abonne_id="A1",
            velo_id="V1",
            type_velo=TypeVelo.MECANIQUE,
            station_depart="MB-1",
            debut=datetime(2026, 7, 31, 22, 4, 23, tzinfo=UTC),
            station_arrivee="MB-2",
            fin=datetime(2026, 7, 31, 22, 42, 17, tzinfo=UTC),
            distance_m=6681,
        )
    ]


@pytest.mark.parametrize(
    "texte", ["2026-08-01T00:42:17+02:00", "2026-07-31T22:42:17Z", "01/08/2026 00:42:17"]
)
def test_trois_formats_un_seul_instant(tmp_path: Path, texte: str) -> None:
    [trajet] = lire(tmp_path, f"T1,,V1,electrique,MB-1,{texte},MB-2,{texte},10")
    assert trajet.debut == trajet.fin == datetime(2026, 7, 31, 22, 42, 17, tzinfo=UTC)
    assert trajet.debut.tzinfo is UTC


def test_changement_d_heure(tmp_path: Path) -> None:
    """Exemple E6 de la spécification : 30 minutes malgré le recul de l'horloge."""
    [trajet] = lire(
        tmp_path,
        "T1,A1,V1,mecanique,MB-1,2026-10-25T02:40:00+02:00,MB-2,2026-10-25T02:10:00+01:00,10",
    )
    assert trajet.fin is not None
    assert trajet.fin - trajet.debut == timedelta(minutes=30)


def test_champs_vides(tmp_path: Path) -> None:
    [trajet] = lire(tmp_path, "T1,,V1,mecanique,MB-1,2026-08-01T00:04:23+02:00,,,")
    assert trajet.abonne_id is None
    assert trajet.station_arrivee is None
    assert trajet.fin is None
    assert trajet.distance_m is None


@pytest.mark.parametrize(
    "mauvaise",
    [
        "T2,A1,V1,mecanique,MB-1,2026-08-01T00:04:23+02:00",
        NORMALE + ",en trop",
        "T2,A1,V1,trottinette,MB-1,2026-08-01T00:04:23+02:00,,,",
        "T2,A1,V1,mecanique,MB-1,hier,,,",
        ",A1,V1,mecanique,MB-1,2026-08-01T00:04:23+02:00,,,",
    ],
)
def test_ligne_illisible_signalee(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, mauvaise: str
) -> None:
    assert [t.trajet_id for t in lire(tmp_path, mauvaise, NORMALE)] == ["T1"]
    assert "ligne 2 ignorée" in caplog.text


def test_lecture_paresseuse(tmp_path: Path) -> None:
    """Le premier trajet sort alors que la fin du fichier est coupée : rien n'est préchargé."""
    chemin = tmp_path / "trajets.csv.gz"
    lignes = [NORMALE.replace("T1", f"T{i}") for i in range(50_000)]
    complet = gzip.compress("\n".join([ENTETE, *lignes]).encode())
    chemin.write_bytes(complet[: len(complet) // 2])
    trajets = lire_trajets(chemin)
    assert next(trajets).trajet_id == "T0"
    with pytest.raises(EOFError):
        list(trajets)


# Lundi 3 août 2026, 8 h à 8 h 30 à Paris.
BASE = Trajet(
    trajet_id="T1",
    abonne_id=None,
    velo_id="V1",
    type_velo=TypeVelo.MECANIQUE,
    station_depart="MB-1",
    debut=datetime(2026, 8, 3, 6, 0, tzinfo=UTC),
    station_arrivee="MB-2",
    fin=datetime(2026, 8, 3, 6, 30, tzinfo=UTC),
    distance_m=None,
)


def test_sans_doublons_meme_eloignes() -> None:
    trajets = [replace(BASE, trajet_id=i) for i in ("T1", "T1", "T2", "T1", "T3")]
    assert [t.trajet_id for t in sans_doublons(trajets)] == ["T1", "T2", "T3"]


def test_sans_doublons_apres_100_000_autres_trajets() -> None:
    autres = (replace(BASE, trajet_id=f"X{i}") for i in range(100_001))
    resultat = sans_doublons([BASE, *autres, BASE])
    assert sum(t.trajet_id == BASE.trajet_id for t in resultat) == 1


@pytest.mark.parametrize(
    ("trajet", "garde"),
    [
        (BASE, True),
        (replace(BASE, fin=None), False),
        (replace(BASE, station_arrivee=None), False),
        (replace(BASE, fin=BASE.debut - timedelta(seconds=1)), False),
        (replace(BASE, fin=BASE.debut + timedelta(hours=24)), True),
        (replace(BASE, fin=BASE.debut + timedelta(hours=24, seconds=1)), False),
        (replace(BASE, fin=BASE.debut + timedelta(seconds=119), station_arrivee="MB-1"), False),
        (replace(BASE, fin=BASE.debut + timedelta(seconds=120), station_arrivee="MB-1"), True),
        (replace(BASE, fin=BASE.debut + timedelta(seconds=119)), True),
    ],
)
def test_exploitables(trajet: Trajet, garde: bool) -> None:
    assert list(exploitables([trajet])) == ([trajet] if garde else [])


def test_profil_en_heure_de_paris_et_par_jour() -> None:
    dimanche_23h30_utc = datetime(2026, 8, 2, 23, 30, tzinfo=UTC)  # lundi 1 h 30 à Paris
    samedi = datetime(2026, 8, 1, 6, 0, tzinfo=UTC)
    flux = compter_flux(
        [
            BASE,
            replace(BASE, debut=BASE.debut + timedelta(days=1)),
            replace(BASE, debut=dimanche_23h30_utc),
            replace(BASE, debut=samedi),
        ]
    )
    semaine = profil_horaire(flux, weekend=False)
    assert (semaine[8], semaine[1], sum(semaine)) == (1.0, 0.5, 1.5)
    weekend = profil_horaire(flux, weekend=True)
    assert (weekend[8], sum(weekend)) == (1.0, 1.0)


def test_profil_sans_trajet() -> None:
    assert profil_horaire(compter_flux([]), weekend=True) == [0.0] * 24


def test_vides_le_matin() -> None:
    retour = replace(
        BASE,
        station_depart="MB-2",
        station_arrivee="MB-1",
        debut=datetime(2026, 8, 3, 16, 0, tzinfo=UTC),
        fin=datetime(2026, 8, 3, 16, 30, tzinfo=UTC),
    )
    samedi = replace(
        BASE,
        station_depart="MB-2",
        debut=BASE.debut - timedelta(days=2),
        fin=datetime(2026, 8, 1, 6, 30, tzinfo=UTC),
    )
    assert vides_le_matin(compter_flux([BASE, retour, samedi])) == {"MB-1": (-1.0, 1.0)}


def test_memoire_constante() -> None:
    """Dix fois plus de trajets ne doivent pas demander plus de mémoire."""

    def lignes(nombre: int) -> Iterator[tuple[int, dict[str, str]]]:
        cles = ENTETE.split(",")
        for i in range(nombre):
            yield i, dict(zip(cles, NORMALE.replace("T1", f"T{i:07d}").split(","), strict=True))

    def pic(nombre: int) -> int:
        tracemalloc.start()
        compter_flux(exploitables(sans_doublons(convertir(lignes(nombre)))))
        maximum = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        return maximum

    pic(1_000)  # remplit les caches de Python avant de mesurer
    assert pic(20_000) < 1.2 * pic(2_000)
