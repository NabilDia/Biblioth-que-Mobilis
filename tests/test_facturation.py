import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from mobilis.facturation import (
    Abonnement,
    Facture,
    Forfait,
    Formule,
    Motif,
    Signalement,
    Tarification,
    Tarifs,
    TarifTranches,
    facturer,
    lire_abonnements,
    lire_tarifs,
    recettes,
)
from mobilis.trajets import Trajet, TypeVelo

MECANIQUE, ELECTRIQUE = TypeVelo.MECANIQUE, TypeVelo.ELECTRIQUE


def _tranches(
    incluses: int, tranche: int, prix: str, electrique: str, deblocage: str, plafond: str | None
) -> TarifTranches:
    return TarifTranches(
        minutes_incluses=timedelta(minutes=incluses),
        tranche=timedelta(minutes=tranche),
        prix_tranche=Decimal(prix),
        supplement_electrique_tranche=Decimal(electrique),
        frais_deblocage=Decimal(deblocage),
        plafond_jour=None if plafond is None else Decimal(plafond),
    )


TARIFS = Tarifs(
    penalite=Decimal("150.00"),
    seuil_non_restitution=timedelta(hours=24),
    grilles=(
        (
            date(2025, 1, 1),
            {
                Formule.ANNUEL: _tranches(30, 30, "1.00", "0.50", "0.00", "12.00"),
                Formule.MENSUEL: _tranches(30, 30, "1.00", "0.50", "0.00", "15.00"),
                Formule.SOLIDAIRE: _tranches(45, 30, "0.50", "0.25", "0.00", "5.00"),
                Formule.OCCASIONNEL: _tranches(0, 15, "0.60", "0.40", "1.00", None),
            },
        ),
        (
            date(2026, 10, 1),
            {
                Formule.ANNUEL: _tranches(30, 30, "1.20", "0.60", "0.00", "14.00"),
                Formule.MENSUEL: _tranches(30, 30, "1.20", "0.60", "0.00", "18.00"),
                Formule.SOLIDAIRE: _tranches(45, 30, "0.50", "0.25", "0.00", "5.00"),
                Formule.OCCASIONNEL: _tranches(0, 15, "0.75", "0.50", "1.20", None),
            },
        ),
    ),
)
ABONNEMENTS = {
    "ANNUEL": [Abonnement(Formule.ANNUEL, date(2025, 1, 1), None)],
    "MENSUEL": [Abonnement(Formule.MENSUEL, date(2025, 1, 1), None)],
    "CHANGE": [
        Abonnement(Formule.ANNUEL, date(2026, 9, 1), date(2026, 9, 10)),
        Abonnement(Formule.SOLIDAIRE, date(2026, 9, 11), date(2026, 9, 12)),
    ],
}


def trajet(
    debut: str,
    fin: str | None,
    abonne: str | None = None,
    velo: TypeVelo = MECANIQUE,
    ident: str = "T1",
) -> Trajet:
    return Trajet(
        trajet_id=ident,
        abonne_id=abonne,
        velo_id="V1",
        type_velo=velo,
        station_depart="MB-1",
        debut=datetime.fromisoformat(debut).astimezone(UTC),
        station_arrivee="MB-2",
        fin=None if fin is None else datetime.fromisoformat(fin).astimezone(UTC),
        distance_m=None,
    )


def duree(
    minutes: float, jour: str = "2026-09-10", abonne: str | None = None, velo: TypeVelo = MECANIQUE
) -> Trajet:
    depart = trajet(f"{jour}T10:00:00+02:00", None, abonne, velo)
    return replace(depart, fin=depart.debut + timedelta(minutes=minutes))


def factures(*trajets: Trajet) -> list[Facture]:
    numerotes = [replace(t, trajet_id=f"T{i}") for i, t in enumerate(trajets)]
    return list(facturer(numerotes, TARIFS, ABONNEMENTS, {"MB-1", "MB-2"}))


def facture(un_trajet: Trajet) -> Facture:
    [resultat] = factures(un_trajet)
    return resultat


@pytest.mark.parametrize(
    ("trajets", "attendu"),
    [
        pytest.param([duree(25, abonne="ANNUEL")], "0.00", id="E1"),
        pytest.param([duree(25, abonne="ANNUEL", velo=ELECTRIQUE)], "0.50", id="E2"),
        pytest.param([duree(61, abonne="MENSUEL")], "2.00", id="E3"),
        pytest.param([duree(20, "2026-10-05", velo=ELECTRIQUE)], "3.70", id="E4"),
        pytest.param([duree(180, "2026-08-12", "ANNUEL")] * 3, "12.00", id="E5"),
        pytest.param(
            [trajet("2026-10-25T02:40:00+02:00", "2026-10-25T02:10:00+01:00", "ANNUEL")],
            "0.00",
            id="E6",
        ),
    ],
)
def test_exemples_de_la_direction(trajets: list[Trajet], attendu: str) -> None:
    assert sum(f.montant for f in factures(*trajets)) == Decimal(attendu)
    assert all(f.motif is Motif.USAGE for f in factures(*trajets))


@pytest.mark.parametrize(
    ("debut", "attendu"),
    [
        ("2026-09-30T21:59:59+00:00", "1.60"),  # 30 septembre 23:59:59 à Paris
        ("2026-09-30T22:00:00+00:00", "1.95"),  # 1er octobre 00:00:00 à Paris
    ],
)
def test_r1_grille_a_la_date_locale(debut: str, attendu: str) -> None:
    depart = trajet(debut, None)
    assert facture(replace(depart, fin=depart.debut + timedelta(minutes=10))).montant == Decimal(
        attendu
    )


@pytest.mark.parametrize(
    ("jour", "formule"),
    [
        ("2026-08-31", Formule.OCCASIONNEL),
        ("2026-09-01", Formule.ANNUEL),
        ("2026-09-10", Formule.ANNUEL),
        ("2026-09-11", Formule.SOLIDAIRE),
        ("2026-09-12", Formule.SOLIDAIRE),
        ("2026-09-13", Formule.OCCASIONNEL),
    ],
)
def test_r2_r3_formule_active(jour: str, formule: Formule) -> None:
    resultat = facture(duree(10, jour, "CHANGE"))
    assert resultat.formule is formule
    assert resultat.signalements == ()


def test_r3_introuvable_signale() -> None:
    resultat = facture(duree(10, abonne="INCONNU"))
    assert resultat.formule is Formule.OCCASIONNEL
    assert resultat.signalements == (Signalement.ABONNE_INTROUVABLE,)


@pytest.mark.parametrize(("minutes", "attendu"), [(30, "0"), (30 + 1 / 60, "1"), (60, "1")])
def test_r4_tranche_entamee_des_la_premiere_seconde(minutes: float, attendu: str) -> None:
    assert facture(duree(minutes, abonne="ANNUEL")).montant == Decimal(attendu)


def test_r6_plafond_par_jour_et_par_abonne() -> None:
    long = duree(600, abonne="ANNUEL")
    resultats = factures(
        long, long, replace(long, abonne_id="MENSUEL"), duree(600, "2026-09-11", "ANNUEL")
    )
    assert [f.montant for f in resultats] == [Decimal(m) for m in ("12", "0", "15", "12")]


def test_r6_pas_de_plafond_occasionnel() -> None:
    assert [f.montant for f in factures(duree(600), duree(600))] == [Decimal("25.00")] * 2


def test_r6_trajet_tres_en_retard_reste_plafonne() -> None:
    """Trois trajets de 3 h le 1er septembre, le dernier reçu après trente autres journées."""
    autres_jours = [duree(60, f"2026-09-{n:02d}", "ANNUEL") for n in range(2, 31)]
    trois_heures = duree(180, "2026-09-01", "ANNUEL")
    resultats = factures(trois_heures, trois_heures, *autres_jours, trois_heures)
    premier_jour = [f.montant for f in resultats if f.jour == date(2026, 9, 1)]
    assert premier_jour == [Decimal(m) for m in ("5", "5", "2")]  # 12 € au plus


def test_r7_arrondi_moitie_vers_le_haut() -> None:
    [(debut, formules), *_] = TARIFS.grilles
    tarif = replace(formules[Formule.OCCASIONNEL], frais_deblocage=Decimal("1.005"))
    tarifs = replace(TARIFS, grilles=((debut, {**formules, Formule.OCCASIONNEL: tarif}),))
    [resultat] = facturer([duree(10)], tarifs, {}, {"MB-1", "MB-2"})
    assert resultat.montant == Decimal("1.61")  # 1,605 ; l'arrondi bancaire donnerait 1,60
    assert isinstance(resultat.montant, Decimal)


@pytest.mark.parametrize(
    ("cas", "motif", "montant"),
    [
        (replace(duree(1.9), station_arrivee="MB-1"), Motif.FANTOME, "0"),
        (replace(duree(2), station_arrivee="MB-1"), Motif.USAGE, "1.60"),
        (duree(1.9), Motif.USAGE, "1.60"),
        (replace(duree(1), fin=None), Motif.PENALITE, "150"),
        (replace(duree(1), station_arrivee=None), Motif.PENALITE, "150"),
        (duree(24 * 60), Motif.USAGE, "58.60"),
        (duree(24 * 60 + 1), Motif.PENALITE, "150"),
        (duree(-1), Motif.INCOHERENT, "0"),
        (replace(duree(-1), station_arrivee="MB-1"), Motif.INCOHERENT, "0"),
    ],
)
def test_r8_r9_r10_cas_particuliers(cas: Trajet, motif: Motif, montant: str) -> None:
    resultat = facture(cas)
    assert (resultat.motif, resultat.montant) == (motif, Decimal(montant))


def test_r9_penalite_hors_plafond() -> None:
    long = duree(600, abonne="ANNUEL")
    resultats = factures(long, replace(long, fin=None), long)
    assert [f.montant for f in resultats] == [Decimal(m) for m in ("12", "150", "0")]


def test_r11_doublon_facture_une_fois() -> None:
    assert len(list(facturer([duree(10)] * 3, TARIFS, {}, {"MB-1", "MB-2"}))) == 1


@pytest.mark.parametrize("champ", ["station_depart", "station_arrivee"])
def test_r12_station_inconnue_facturee_et_signalee(champ: str) -> None:
    depart = "MB-9" if champ == "station_depart" else "MB-1"
    arrivee = "MB-9" if champ == "station_arrivee" else "MB-2"
    resultat = facture(replace(duree(10), station_depart=depart, station_arrivee=arrivee))
    assert resultat.montant == Decimal("1.60")
    assert resultat.signalements == (Signalement.STATION_INCONNUE,)


def test_deux_implementations_interchangeables() -> None:
    tarifications: list[Tarification] = [
        Forfait(Decimal("150.00")),
        TARIFS.tarif(Formule.ANNUEL, date(2026, 9, 10)),
    ]
    assert [t.prix(duree(61)) for t in tarifications] == [Decimal("150.00"), Decimal("2.00")]


def test_lire_tarifs(tmp_path: Path) -> None:
    valeurs = {
        "minutes_incluses": 30,
        "tranche_minutes": 30,
        "prix_tranche": 1.1,
        "supplement_electrique_tranche": "0.50",
        "frais_deblocage": "0.00",
        "plafond_jour": None,
    }
    chemin = tmp_path / "tarifs.json"
    chemin.write_text(
        json.dumps(
            {
                "penalite_non_restitution": "150.00",
                "seuil_non_restitution_heures": 24,
                "grilles": [{"en_vigueur_du": "2025-01-01", "formules": {"annuel": valeurs}}],
            }
        )
    )
    tarifs = lire_tarifs(chemin)
    assert tarifs.penalite == Decimal("150.00")
    assert tarifs.seuil_non_restitution == timedelta(hours=24)
    assert tarifs.tarif(Formule.ANNUEL, date(2026, 1, 1)) == _tranches(
        30, 30, "1.1", "0.50", "0.00", None
    )


def test_lire_abonnements(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    chemin = tmp_path / "abonnes.jsonl"
    chemin.write_text(
        '{"id": "A1", "formule": " ANNUEL", "debut": "2025-01-01", "fin": "2025-12-31"}\n'
        "\n"
        '{"id": "A2", "formu\n'
        "null\n"
        '{"id": "A3", "formule": "vip", "debut": "2025-01-01", "fin": null}\n'
        '{"id": "A1", "formule": "Mensuel", "debut": "2026-01-01", "fin": null}\n'
    )
    assert lire_abonnements(chemin) == {
        "A1": [
            Abonnement(Formule.ANNUEL, date(2025, 1, 1), date(2025, 12, 31)),
            Abonnement(Formule.MENSUEL, date(2026, 1, 1), None),
        ]
    }
    assert [f"ligne {n} ignorée" in caplog.text for n in (2, 3, 4, 5)] == [False, True, True, True]


def test_recettes() -> None:
    totaux = recettes(factures(duree(10), duree(10), duree(10, "2026-10-01")))
    assert {cle: (c.nombre, c.montant) for cle, c in totaux.items()} == {
        ("2026-09", Formule.OCCASIONNEL, Motif.USAGE): (2, Decimal("3.20")),
        ("2026-10", Formule.OCCASIONNEL, Motif.USAGE): (1, Decimal("1.95")),
    }
