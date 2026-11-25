# mobilis

Bibliothèque et commande qui traitent les données Mobilis : référentiel des stations, fréquentation, facturation et état des bornes.

## Installer

Python 3.11 ou plus récent. Les données ne sont pas dans ce dépôt : il faut le kit `mobilis-kit`.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export MOBILIS_DONNEES=/chemin/vers/mobilis-kit/donnees
```

## Refaire les chiffres

| Commande | Réponse |
|---|---|
| `mobilis stations` | Stations exploitables et rapport d'anomalies |
| `mobilis frequentation` | Profil horaire, stations qui se vident le matin |
| `mobilis facturation` | Recettes par mois et par formule, effet de la dernière grille |
| `mobilis etat` | Stations vides ou pleines, écarts avec le référentiel |

`mobilis etat` interroge l'API des bornes. Dans un autre terminal, depuis `mobilis-kit` :

```bash
python3 outils/serveur_bornes.py
```

Puis, avec la clé donnée dans le README du kit :

```bash
export MOBILIS_CLE=...
mobilis etat
```

Les trois premières commandes donnent toujours les mêmes chiffres. `etat` change à chaque appel.

## Mesures

```bash
python mesure_memoire.py "$MOBILIS_DONNEES"/trajets_2026-08_2026-10.csv.gz
python mesure_bornes.py
```

## Vérifier le code

```bash
ruff check . && ruff format --check . && mypy && pytest
```

Les tests n'ont besoin ni des données ni du serveur.
