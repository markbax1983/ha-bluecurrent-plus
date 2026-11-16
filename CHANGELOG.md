# Changelog

Alle noemenswaardige wijzigingen per versie. Het versienummer staat in
`custom_components/blue_current_plus/manifest.json`. Bij een push naar `main` met
een nieuw versienummer maakt GitHub Actions automatisch de tag `vX.Y.Z` en een
GitHub Release aan, met de tekst van die versie uit dit bestand.

## [0.1.8] - 2026-11-16

### Toegevoegd
- Sensoren **Geladen deze maand** en **Vergoeding deze maand**, met het totaal en aantal sessies van de vorige maand als attributen. Een sessie telt mee in de maand waarin hij begon. Bewaard over herstarts heen.

### Opgelost
- Iconen van sensoren die in 0.1.4 verdwenen zijn uit `icons.json` gehaald.

## [0.1.7] - 2026-11-16

### Verwijderd
- Sensor *Tarief*: dubbel met het number *Vergoedingstarief*. Wordt bij het opstarten uit het entiteitenregister gehaald. Het attribuut `start_price_ex_vat` staat nu op het number.
- Opruimcode voor sensoren die in 0.1.4 verdwenen.

### Gewijzigd
- Apparaatnaam en de optie *Zonder laadpas* volgen de taal van Home Assistant (Nederlands of Engels) in plaats van altijd Nederlands.
- Portal-token wordt weggelaten uit diagnostics.
- Testdata bevat geen echte laadpaal- en pasgegevens meer.
- README herschreven, met een sectie over beperkingen.

## [0.1.6] - 2026-11-16

### Toegevoegd
- Eigen icoon in `brand/`; wordt vanaf Home Assistant 2026.3 getoond bij de integratie en in HACS.
- `CHANGELOG.md` en automatische tags/releases via GitHub Actions.

## [0.1.5] - 2026-11-16

### Toegevoegd
- Number **Vergoedingstarief** (€/kWh excl. btw): uitlezen via de websocket, aanpassen via dezelfde REST-call als my.bluecurrent.nl (`editreimbursementtariff`). Grens 0,05–1,50 €/kWh.
- Options flow met een optioneel portal-token, voor als Blue Current het sessie-token weigert. Een geweigerde tarief-call start geen herauthenticatie.

## [0.1.4] - 2026-09-24

### Gewijzigd
- Duidelijkere namen: Laadstroom/Laadvermogen/Netspanning fase n, Maximale laadstroom, Laadpaal status.
- Netwaarden heten nu *Gemelde netstroom* (diagnostisch, standaard uit): het is wat de paal binnenkrijgt.
- Tarief met 2 decimalen.
- Sessiekosten en sessiewaarde samengevoegd tot **Sessie vergoeding**: live schatting tijdens het laden, het bedrag van Blue Current na afloop.

### Toegevoegd
- Teller **Totale vergoeding** over afgeronde sessies.

### Verwijderd
- Sensor *Sessie einde*; verouderde entiteiten worden uit het register gehaald.

## [0.1.3] - 2026-09-24

### Opgelost
- Sessie-energie en -vergoeding blijven staan na loskoppelen (de backend zet `actual_kwh` dan op 0).
- Geen nep-waarde van 32 A meer vóór de eerste statusupdate.

## [0.1.2] - 2026-09-24

### Gewijzigd
- Tarief gebruikt de prijs excl. btw, de waarde die je op my.bluecurrent.nl instelt.

## [0.1.1] - 2026-09-24

### Opgelost
- Laatst bekende sessiestart/-einde blijven staan (live pushes sturen `null`).
- Bij loskoppelen wordt de status één keer opgevraagd voor de echte eindtijd.
- Event *sessie afgerond* alleen voor sessies die tijdens het draaien zijn gezien.

### Toegevoegd
- Standaard laadpas = de voorkeurspas van de laadpaal.
- Sensoren tarief en sessiewaarde (kWh × tarief).
- Kan naast de originele Blue Current-integratie draaien.

## [0.1.0] - 2026-09-24

### Toegevoegd
- Eerste versie: eigen websocket-client, correcte vermogens- en energiesensoren, max laadstroom (number) via overrides, laadpas-select, reconnect met backoff en diagnostics.
