# TODO i3x2ua

## Bereits erledigt

- [x] Projektgrundstruktur mit FastAPI, async OPC-UA-Client, Mapper und Model-Builder aufgebaut
- [x] i3X Model API implementiert (GET /model, GET /model/{id}, GET /model/{id}/children)
- [x] i3X Data API implementiert (GET /data/{propertyId}, POST /data/query)
- [x] i3X Action API implementiert (POST /action/{actionId}/invoke)
- [x] OPC-UA Browsing, Value-Read und Method-Call in Client-Schicht implementiert
- [x] OPC UA -> i3X Basis-Mapping (Object/Variable/Method/EventNotifier) implementiert
- [x] Startup-Model-Preload und Model-Cache umgesetzt
- [x] Browse-Prozess parallelisiert (konfigurierbare Concurrency)
- [x] Basis-Tests (Mapper und API) erstellt und lauffaehig
- [x] Test-HTTP-Client fuer Model/Children/Data-Pfade erstellt

## Teilweise erledigt

- [ ] Logging-Konzept erweitern (Basis-Logs vorhanden; strukturierte Logs/Korrelations-ID noch offen)
- [ ] Integrationstests mit realem OPC-UA-Server aufsetzen (manuell validiert, aber noch nicht als automatisierte Testsuite)

## Offene Kernfeatures (nicht optional)

- [ ] i3X-Konformitaet der Modellstruktur vollstaendig absichern (Assets, Properties, Actions, EventSources inkl. Feldsemantik)
- [ ] Modell-Refresh umsetzen (zyklisch und/oder on-demand statt nur initialer Cache)
- [ ] Datentyp-Konvertierung OPC UA -> JSON robust erweitern (komplexe Typen, Enums, ByteStrings, DateTime, LocalizedText)
- [ ] Fehlerformatierung weiter auf i3X-Schema haerten (einheitliche Codes, valide Details, konsistente HTTP-Status)
- [ ] Batch-Reads optimieren (parallelisierte Reads statt strikt sequentieller Verarbeitung)
- [ ] Methodenparameter automatisch erkennen (Input/Output Argumente aus OPC UA Method Metadata)
- [ ] Rueckgabewerte von Methodenaufrufen fuer alle relevanten Typen sauber serialisieren
- [ ] Logging-Konzept erweitern (strukturierte Logs, Fehlerkontext, Korrelations-ID)
- [ ] Automatische Wiederverbindung bei OPC-UA-Verbindungsabbruch implementieren
- [ ] API-/Konformitaetstests fuer alle Endpunkte inkl. Fehlerfaelle erweitern
- [ ] Integrationstests mit realem OPC-UA-Server aufsetzen
- [ ] Lasttests fuer Read-/Batch-Pfade und Modellaufbau ergaenzen
- [ ] OpenAPI-Dokumentation fachlich vervollstaendigen (Response-Beispiele, Fehlerbeispiele)
- [ ] Beispiel-OPC-UA-Mapping als gesonderte Artefakte bereitstellen

## Optionale Features aus Lastenheft

- [ ] GET /data/history/{propertyId} (historische Werte)
- [ ] write_value()-Unterstuetzung mit konfigurierbaren Schreibrechten
- [ ] Subscription-Manager fuer OPC-UA-Subscriptions
- [ ] Mapping von Subscriptions auf i3X-Events/EventSources
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
