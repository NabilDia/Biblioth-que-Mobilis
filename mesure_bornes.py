"""Compare séquentiel, threads et asyncio : python mesure_bornes.py [adresse] [répétitions]"""

import asyncio
import json
import os
import statistics
import sys
import time
from typing import Any
from urllib.parse import urlsplit

from mobilis.bornes import (
    SIMULTANES,
    STATUTS_A_REESSAYER,
    ClientBornes,
    ErreurBornes,
    ErreurTemporaire,
    attente,
    photographier,
)

ADRESSE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
REPETITIONS = int(sys.argv[2]) if len(sys.argv) > 2 else 3
CLE = os.environ.get("MOBILIS_CLE", "demo-mobilis-2026")


def sequentiel() -> int:
    client = ClientBornes(ADRESSE, CLE)
    recus = 0
    for code in client.codes():
        try:
            client.etat(code)
            recus += 1
        except ErreurBornes:
            pass
    return recus


def threads() -> int:
    return len(photographier(ClientBornes(ADRESSE, CLE)).etats)


async def _get(chemin: str, places: asyncio.Semaphore, tentatives: int = 4) -> Any:
    # shortcut: HTTP minimal, sans TLS, pour ne pas ajouter de dépendance à cette mesure ;
    # en production, prendre une bibliothèque asynchrone.
    adresse = urlsplit(ADRESSE)
    for tentative in range(1, tentatives + 1):
        async with places:
            lecteur, redacteur = await asyncio.open_connection(adresse.hostname, adresse.port)
            redacteur.write(
                f"GET {chemin} HTTP/1.0\r\nHost: {adresse.netloc}\r\nX-Api-Key: {CLE}\r\n\r\n".encode()
            )
            brut = await lecteur.read()
            redacteur.close()
            await redacteur.wait_closed()
        entete, _, corps = brut.partition(b"\r\n\r\n")
        statut = int(entete.split()[1])
        if statut not in STATUTS_A_REESSAYER:
            break
        if tentative < tentatives:
            delai = 1.0 if b"Retry-After: 1" in entete else None
            await asyncio.sleep(attente(ErreurTemporaire(f"HTTP {statut}", delai), tentative))
    if statut != 200:
        raise ErreurBornes(f"HTTP {statut}")
    return json.loads(corps)


async def _photo_asyncio() -> int:
    places = asyncio.Semaphore(SIMULTANES)
    taches = []
    page: int | None = 1
    while page is not None:
        reponse = await _get(f"/stations?page={page}", places)
        taches += [
            asyncio.create_task(_get(f"/stations/{code}", places)) for code in reponse["stations"]
        ]
        page = reponse["page_suivante"]
    resultats = await asyncio.gather(*taches, return_exceptions=True)
    return sum(not isinstance(resultat, BaseException) for resultat in resultats)


def avec_asyncio() -> int:
    return asyncio.run(_photo_asyncio())


for nom, strategie in (
    ("séquentiel", sequentiel),
    ("threads", threads),
    ("asyncio", avec_asyncio),
):
    durees = []
    for _ in range(REPETITIONS):
        depart = time.perf_counter()
        recus = strategie()
        durees.append(time.perf_counter() - depart)
    print(
        f"{nom:11} médiane {statistics.median(durees):5.1f} s, "
        f"max {max(durees):5.1f} s, {recus} états reçus",
        flush=True,
    )
