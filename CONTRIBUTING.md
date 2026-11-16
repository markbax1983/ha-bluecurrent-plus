# Meewerken aan Blue Current Plus

Fijn dat je wilt helpen. Hieronder staat hoe je de code test en hoe een nieuwe versie wordt uitgebracht.

## Een probleem melden

Open een [issue](https://github.com/markbax1983/ha-bluecurrent-plus/issues) en voeg de *diagnostics* van de integratie toe (Instellingen → Apparaten & diensten → Blue Current Plus → ⋮ → Diagnostische gegevens downloaden). Tokens en pasnummers zijn daarin weggelaten. Vermeld ook het model van je laadpaal.

## Testen

```bash
pip install -r requirements_test.txt
pytest -q        # tests tegen een gesimuleerde Blue Current-backend
ruff check .     # code-controle
ruff format .    # opmaak
```

Dezelfde controles draaien automatisch op GitHub bij elke push en pull request (workflow *Validate*), samen met de checks van HACS en hassfest.

Gebruik in tests geen echte gegevens: geen echte laadpaalnummers, pas-UID's, EMP-nummers of namen.

## Nieuwe versie uitbrengen

1. Hoog `version` op in `custom_components/blue_current_plus/manifest.json`.
2. Zet bovenaan [CHANGELOG.md](CHANGELOG.md) een sectie `## [X.Y.Z] - datum` met de wijzigingen.
3. Push naar `main`. De workflow *Release* draait de tests en maakt daarna tag `vX.Y.Z` en een GitHub Release met de tekst uit de changelog. HACS ziet de nieuwe versie dan als update.

Staat de versie niet in de changelog, dan faalt de workflow en komt er geen release.

## Icoon

Het icoon staat in `custom_components/blue_current_plus/brand/` (`icon.png` 256×256 en `icon@2x.png` 512×512); de bron is `assets/icon.svg`. Het is een eigen ontwerp, niet het logo van Blue Current.
