# i3X REST-Server mit OPC-UA Backend  
## **Anforderungsdokument (Lastenheft)**  
Version: 1.0  
Autor: —  

---

# 1. Ziel des Systems
Das System soll einen vollständig i3X‑konformen REST‑Server bereitstellen, der Daten aus einem OPC‑UA‑Server über eine standardisierte i3X‑API verfügbar macht.  
Der Server fungiert als:

- **i3X‑Provider** (REST‑API nach außen)  
- **OPC‑UA‑Client** (Datenquelle nach innen)

Ziel ist es, OPC‑UA‑Daten in ein i3X‑Kontextmodell zu überführen und über die i3X‑APIs **/model**, **/data** und **/action** zugänglich zu machen.

---

# 2. Systemübersicht
Das System besteht aus folgenden Komponenten:

- **REST‑Server (Python, asyncio)**  
- **OPC‑UA‑Client (async)**  
- **Model‑Builder** (OPC UA → i3X)  
- **Mapping‑Engine**  
- **Caching‑Layer (optional)**  
- **Subscription‑Manager (optional)**  

---

# 3. Funktionale Anforderungen

## 3.1 i3X‑API‑Implementierung

### 3.1.1 /model‑API
Der Server muss folgende Endpunkte implementieren:

| Endpoint | Beschreibung |
|---------|--------------|
| `GET /model` | Liefert das gesamte i3X‑Kontextmodell |
| `GET /model/{id}` | Liefert ein einzelnes Modellobjekt |
| `GET /model/{id}/children` | Liefert die Kindknoten eines Modells |

**Anforderungen:**
- Modell wird aus OPC‑UA‑Nodes generiert  
- IDs müssen stabil sein  
- i3X‑Konformität (Assets, Properties, Actions, EventSources)  
- Modell muss aktualisierbar sein  

---

### 3.1.2 /data‑API
Der Server muss folgende Endpunkte implementieren:

| Endpoint | Beschreibung |
|---------|--------------|
| `GET /data/{propertyId}` | Liest aktuellen Wert einer OPC‑UA‑Variable |
| `POST /data/query` | Batch‑Reads |
| Optional: `GET /data/history/{propertyId}` | Historische Werte |

**Anforderungen:**
- Property‑IDs müssen eindeutig auf OPC‑UA‑NodeIds gemappt werden  
- Datentyp‑Konvertierung OPC UA → JSON  
- Fehler müssen i3X‑konform zurückgegeben werden  
- Batch‑Reads müssen effizient sein  

---

### 3.1.3 /action‑API
| Endpoint | Beschreibung |
|---------|--------------|
| `POST /action/{actionId}/invoke` | Führt OPC‑UA‑Methode aus |

**Anforderungen:**
- Methodenparameter automatisch erkennen  
- Rückgabewerte korrekt serialisieren  
- Fehlerbehandlung gemäß i3X  

---

## 3.2 OPC‑UA‑Client‑Funktionalität

### 3.2.1 Browsing
- Vollständiges Browsen des OPC‑UA‑Baums  
- Lesen von NodeClass, DisplayName, BrowseName, DataType, References  
- Aktualisierung on‑demand oder zyklisch  

### 3.2.2 Lesen
- Unterstützung von `read_value()`  
- Konvertierung in JSON‑kompatible Typen  
- Fehlerbehandlung (BadStatus, Timeout)  

### 3.2.3 Schreiben (optional)
- Unterstützung von `write_value()`  
- Konfigurierbare Schreibrechte  

### 3.2.4 Methodenaufrufe
- Unterstützung von `call_method()`  
- Automatische Parametertyp‑Erkennung  

### 3.2.5 Subscriptions (optional)
- Unterstützung von OPC‑UA‑Subscriptions  
- Generierung von i3X‑Events  

---

# 4. Nicht‑funktionale Anforderungen

## 4.1 Performance
- Antwortzeit für einfache Reads: < 100 ms  
- Modellgenerierung: < 5 Sekunden bei 10.000 Nodes  

## 4.2 Skalierbarkeit
- Mehrere parallele Clients  
- Optional: mehrere OPC‑UA‑Server  
- Optional: Docker / Docker-Compose 

## 4.3 Sicherheit
- TLS für REST  
- OPC‑UA SecurityModes (Sign, Sign&Encrypt)  
- Optional: use OPC UA Client User Authentification
- Optional: Rollenmodell  

## 4.4 Fehlerbehandlung
- i3X‑konforme Fehlermeldungen  
- Logging aller Fehler  
- Automatische Wiederverbindung  

## 4.5 Konfigurierbarkeit
- OPC‑UA‑Endpoint  
- SecurityMode  
- Modell‑Refresh‑Intervall  
- Logging‑Level  
- Caching‑Strategien  

---

# 5. Mapping‑Spezifikation OPC UA → i3X

| OPC UA | i3X |
|--------|------|
| Object | Asset |
| Variable | Property |
| Method | Action |
| EventNotifier | EventSource |
| NodeId | id |
| BrowseName | name |
| DataType | type |
| Hierarchy | children |

---

# 6. Technische Architektur

## 6.1 Komponenten
- FastAPI / Starlette / Quart  
- asyncua (OPC‑UA‑Client)  
- Pydantic für i3X‑Schemas  
- asyncio für Concurrency  

## 6.2 Modulstruktur

i3x_server/
├── main.py
├── api/
│    ├── model.py
│    ├── data.py
│    └── action.py
├── opcua/
│    ├── client.py
│    ├── browser.py
│    └── subscriptions.py
├── model/
│    ├── builder.py
│    └── mapper.py
├── schemas/
└── config/


---

# 7. Testanforderungen

## 7.1 Unit‑Tests
- Modellgenerierung  
- Datentyp‑Konvertierung  
- Fehlerbehandlung  

## 7.2 Integrationstests
- Verbindung zu realem OPC‑UA‑Server  
- Lesen/Schreiben  
- Methodenaufrufe  

## 7.3 API‑Tests
- i3X‑Konformität  
- Lasttests  

---

# 8. Lieferumfang
- Vollständiger Python‑Code  
- OpenAPI‑Dokumentation  
- Beispiel‑Konfiguration  
- Beispiel‑OPC‑UA‑Mapping  
- Optional: Dockerfile  

---

# 9. Akzeptanzkriterien
- i3X‑API vollständig implementiert  
- Modell korrekt aus OPC‑UA generiert  
- Reads funktionieren zuverlässig  
- Methodenaufrufe funktionieren  
- System stabil unter Last  
- Dokumentation vollständig  

---
