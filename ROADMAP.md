# Starlink Monitor – Expansion Roadmap

> Vision: Vom reinen Starlink-Tool zur universellen Hybrid-WAN- & Infrastructure-Monitoring-Plattform

## Phase 1: Connectivity & Multi-WAN

- [ ] **Failover-ISP-Monitoring** – Zweiten ISP überwachen (Ping, Latenz, Durchsatz)
- [ ] **Uplink-Vergleich** – Starlink vs. Failover in einem Dashboard
- [ ] **Lokales Netzwerk-Monitoring** – Ping zu Gateway, DNS, eigener Server
- [ ] **iPerf3-Integration** – Aktive Speedtests (Baustein aus `router-diagnostic-toolkit`)

## Phase 2: Alerting & Automatisierung

- [ ] **Discord-Bot-Integration** – Threshold-basierte Alarme (z. B. Latenz > 100ms)
- [ ] **E-Mail-Benachrichtigungen** – SMTP-Config für tägliche Reports
- [ ] **Push-Benachrichtigungen** – Gotify/ntfy für Handy-Alerts
- [ ] **Webhook-Support** – Anbindung an bestehende Systeme

## Phase 3: Infrastruktur

- [ ] **Ressourcen-Monitoring** – CPU/RAM/Disk des Hosts
- [ ] **Docker-Container-Health** – Status + Logs aller Container
- [ ] **Multi-Site-Support** – Mehrere Standorte in einem Dashboard
- [ ] **Grafana-Integration** – Export der Daten für bestehende Dashboards

## Phase 4: Professionalisierung

- [ ] **Benutzerverwaltung** – Login mit Rollen (Admin/Viewer)
- [ ] **PDF-Reporting** – Automatische Wochen-/Monatsberichte
- [ ] **REST-API** – Fremdsysteme können Daten abfragen
- [ ] **Export/Import** – Konfiguration als JSON sichern/wiederherstellen

## Phase 5: Abschlussprojekt (Systemintegration)

- [ ] **Pflichtenheft** – Anforderungsanalyse & Zielsetzung
- [ ] **Projektplan** – Meilensteine, Zeitplan, Ressourcen
- [ ] **Architektur-Dokumentation** – Systemdesign, Datenflüsse, Sicherheit
- [ ] **Betriebshandbuch** – Deployment, Konfiguration, Troubleshooting
- [ ] **Präsentation** – Live-Demo + Architektur-Überblick

## Technologie-Stack (vorgeschlagen)

| Komponente | Technologie |
|------------|-------------|
| Backend | Python (bestehend) + FastAPI für REST-API |
| Datenbank | SQLite → Upgrade auf InfluxDB für Zeitreihen |
| Frontend | Chart.js (bestehend) → Erweiterung |
| Alerting | Discord.py + SMTP + Gotify |
| Container | Docker-Compose (bestehend) |
| Monitoring | iPerf3, ICMP, Docker-API, PSUtil |

## Meilensteine

```
Q3 2026 – Phase 1: Multi-WAN + Netzwerk-Monitoring
Q4 2026 – Phase 2: Alerting + Automation
Q1 2027 – Phase 3: Infrastruktur + Multi-Site
Q2 2027 – Phase 4: Professionalisierung
2027     – Phase 5: Abschlussprojekt-Einreichung
```
