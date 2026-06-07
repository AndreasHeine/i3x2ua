# TODO i3x2ua

Diese Datei enthaelt nur offene bzw. teilweise offene Arbeitspakete.

Details zu bereits umgesetzten Punkten stehen in `README.md` unter Status.

## Teilweise erledigt

- [ ] Integrationstests mit realem OPC-UA-Server aufsetzen (manuell validiert, aber noch nicht als automatisierte Testsuite)
- [ ] Strukturierte Logs und Korrelations-ID fuer `/v1`-Requests ergaenzen

## Offene Beta-Kernfeatures

- [ ] `POST /v1/objects/history` fachlich implementieren oder bewusst als nicht unterstuetzt markieren
- [ ] `GET /v1/objects/{elementId}/history` und `PUT /v1/objects/{elementId}/history` umsetzen oder finalisieren
- [ ] `PUT /v1/objects/{elementId}/value` implementieren, falls Schreibrechte im Zielsystem vorgesehen sind
- [ ] Subscription-Lifecycle unter `/v1/subscriptions/*` implementieren
- [ ] Stream-/SSE-Ausgabe fuer Subscriptions implementieren
- [ ] History- und Update-Capabilities in `GET /v1/info` an den realen Funktionsumfang koppeln
- [ ] Fehlerformatierung weiter auf das Beta-Schema haerten
- [ ] OpenAPI-Dokumentation mit Beispielen und Fehlerfaellen vervollstaendigen

## Optionale Features aus Lastenheft

- [ ] History-Schreib-/Lese-Features fuer Objekte und Werte ausbauen
- [ ] Rollenmodell fuer Zugriffssteuerung
- [ ] OPC-UA User Authentication (Client-Auth)
- [ ] Multi-Server-Unterstuetzung (mehrere OPC-UA-Backends)
- [ ] Dockerfile und optional Docker-Compose bereitstellen

## Sicherheit und Betrieb

- [ ] TLS fuer REST-API aktivieren (Zertifikatskonfiguration, sichere Defaults)
- [ ] OPC-UA SecurityModes Sign / SignAndEncrypt konfigurierbar und getestet
- [ ] Health-Endpoints (z. B. /health, /ready) fuer Betrieb/Monitoring bereitstellen
- [ ] Konfigurierbare Caching-Strategien ausbauen (TTL, Invalidation, Refresh-Strategie)

## Dokumentation und Lieferumfang

- [ ] Betriebsdokumentation erweitern (Deployment, Security, Monitoring, Troubleshooting)
- [ ] Konfigurationsbeispiele fuer Dev/Test/Prod bereitstellen
- [ ] Akzeptanzkriterien als pruefbare Checkliste in Tests abbilden
