# Blue Current Plus

Een slimmere Home Assistant-integratie voor Blue Current-laadpalen. Het is een vervanger voor de standaard [`blue_current`](https://github.com/bluecurrent/ha-bluecurrent)-integratie, met een eigen websocket-client in plaats van de `bluecurrent-api`-library.

## Waarom?

| Probleem in de standaard integratie | Blue Current Plus |
|---|---|
| `total_kw` rekent `gem. I × gem. U × 1,732` met fasespanning. Bij 3×16 A geeft dat ~6,4 kW in plaats van ~11 kW. | **Laadvermogen** = Σ (U × I) per fase, plus het vermogen per fase |
| Geen energiesensor voor het Energie-dashboard (`actual_kwh` begint elke sessie op 0) | **Totaal geladen**: oplopende kWh-teller (`total_increasing`), bewaard over herstarts heen |
| Laadpassen worden nooit opgehaald, de RFID-UID moet je zelf zoeken | **Laadpas**-select met naam en EMP-id; de knop **Start laden** gebruikt de gekozen pas |
| Max laadstroom alleen via een service; faalt stil door `KeyError: 'data'` | **Max laadstroom**-slider (number), met debounce: een sleepbeweging kost één request |
| Bij de request-limiet de hele dag `unavailable`, zonder uitleg | Request-teller, oplopende backoff, en een repair-melding met oorzaak |
| Onbekende berichten van de server worden weggegooid | Die komen in debug-log en diagnostics terecht, zodat we kunnen ontdekken wat de API nog meer kan |
| `resterende stroom` = limiet − *gemiddelde* fase | Limiet − *zwaarst belaste* fase |
| Sessie start/einde worden overschreven door de `null`-waarden in live pushes | De laatst bekende tijden blijven staan; bij loskoppelen wordt de echte eindtijd één keer opgevraagd |
| Sessiekosten staan tijdens het laden op €0 en komen pas bij het einde | **Sessie vergoeding**: live kWh × tarief (excl. btw) tijdens het laden, het bedrag van Blue Current zelf zodra de sessie klaar is (attribuut `final`). **Totale vergoeding** telt alle afgeronde sessies op |

## Entiteiten (per laadpaal)

- **Sensoren:** laadvermogen, laadvermogen/laadstroom per fase, netspanning per fase, actieve fases, totaal geladen, sessie energie/vergoeding/start/duur, totale vergoeding, tarief, laadpaal status, voertuigstatus, maximale laadstroom, resterende stroom, requests vandaag, offline sinds
- **Diagnostisch, standaard uit:** gemelde netstroom per fase + max. Dat zijn de waarden die de paal van zijn meteringang krijgt, niet per se je echte netstroom
- **Binary sensors:** laden, auto aangesloten, stroomlimiet actief, cloudverbinding
- **Bediening:** max laadstroom (number), vergoedingstarief (number, € per kWh excl. btw), laadpas (select), start/stop laden, reset, herstart, plug & charge, alleen gekoppelde laadpassen, laadpaal blokkeren

## Events

- `blue_current_plus_session_finished`: bij elke afgeronde sessie, met `evse_id`, `start`, `stop`, `energy_kwh`, `reimbursement`, `reimbursement_final` en `duration_min`. Alleen voor sessies die tijdens het draaien zijn gezien, dus niet voor een oude sessie bij het opstarten. Handig voor factuurcontrole of ERE-registratie.
- `blue_current_plus_command_result`: het resultaat van start/stop/reset/herstart.

## Installatie

1. HACS → Integraties → ⋮ → *Aangepaste repositories* → `https://github.com/markbax1983/ha-bluecurrent-plus` (type: Integratie)
2. Installeer **Blue Current Plus** en herstart Home Assistant.
3. Tip: schakel de originele Blue Current-integratie uit zodra alles werkt. Ze kunnen naast elkaar draaien, maar ze verbruiken dezelfde dagelijkse request-limiet.
4. Instellingen → Apparaten & diensten → Integratie toevoegen → *Blue Current Plus*, en vul je API-token in.

## Tip: vergoeding per maand

Maak in HA een *Meterhulp* (utility meter) op **Totale vergoeding** en/of **Totaal geladen**, met een maandelijkse cyclus. Die bedragen kun je rechtstreeks naast de creditfactuur leggen.

## Hoe werkt de max laadstroom?

Een waarde onder de `max_usage` van de paal maakt (of wijzigt) een *user override* voor de hele dag, elke dag (00:00–23:59, MA–ZO). Terugzetten naar het maximum verwijdert de override. Wijzigingen worden 5 seconden gebundeld, zodat automations of een slider de dagelijkse request-limiet niet opmaken.

## Vergoedingstarief aanpassen

Het number **Vergoedingstarief** toont het kWh-tarief excl. btw dat op my.bluecurrent.nl staat (attribuut `price_in_vat` = incl. btw) en past het aan met dezelfde REST-call als de portal (`POST api.bluecurrent.nl/app/bc_api/api/v2.0/editreimbursementtariff`). Grens 0,05–1,50 €/kWh tegen typefouten. Werkt ook als de paal zelf offline is. In een automation: `number.set_value`.

Standaard gebruikt dit hetzelfde sessie-token als de websocket. Weigert Blue Current dat, dan krijg je een foutmelding; vul dan via **Configureren** een *portal-token* in (my.bluecurrent.nl → DevTools → Netwerk → een `api.bluecurrent.nl`-verzoek → de waarde na `Authorization: Token `). Een geweigerd tarief-verzoek start géén herauthenticatie van de integratie.

> Let op: dit is geen officieel gedocumenteerde API; Blue Current kan hem zonder aankondiging wijzigen. Het tarief bepaalt je creditfactuur, controleer na een wijziging in de portal.

## Ontwikkeling

```bash
pip install -r requirements_test.txt
pytest -q      # draait tegen een gesimuleerde Blue Current-backend
ruff check .
```
