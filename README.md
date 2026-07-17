# Starlink Monitor v2

A lightweight, self-hosted web dashboard and telemetry collector designed to monitor Starlink connection quality, satellite tracking, and dish hardware performance in real-time. Optimized for Unraid and Docker environments.

---

## 📊 Features

### Real-Time Dashboard
* **Live Telemetry:** 2-second sampling rate for instant updates on **Ping Drop (%)**, **Latency (ms)**, and **Live Throughput (Mbit/s)**.
* **Sky View Obstruction Map:** Visualizes the dish's field of view (North-oriented) mapping clear vs. obstructed satellite sectors.
* **Dish Hardware Telemetry:** Tracks physical orientation (Azimuth & Elevation), GPS status, SNR metrics, and hardware-level alarms.
* **Outage Statistics:** Calculates 24-hour uptime, historical peaks, and tracks average obstruction durations/intervals.

### Data & Architecture
* **gRPC Collector:** Highly efficient Python background worker utilizing Starlink's native gRPC interface.
* **Optimized Storage:** SQLite database pre-configured with **WAL (Write-Ahead Logging)** mode for high-frequency time-series logging without performance degradation.
* **Data Management Panel:** Built-in retention controls to easily purge or filter logs (older than 7, 30, or 90 days) directly from the UI, paired with raw CSV data export functionality.
* **Clean Design:** Developer-centric interface styled with high-contrast charts (Chart.js) and clean typography using *JetBrains Mono*.

---

## 🛠️ Tech Stack

* **Backend / Collector:** Python 3, `grpcio`, `deep-translator` (localization)
* **Frontend:** HTML5, CSS3, JavaScript (ES6), Chart.js
* **Database:** SQLite 3 (WAL Mode)
* **Deployment:** Docker & `docker-compose`

---

## 🚀 Getting Started

### Prerequisites
* Network access to your Starlink Dish (`192.168.100.1:52001`)
* Docker and Docker Compose installed (or Unraid Docker management)

### Installation & Setup

1. **Clone the repository:**
   git clone https://github.com/BenGhosti/StarlinkMonitor.git
   cd StarlinkMonitor

2. **Configure Environment Variables:**
   Copy the example environment file and adjust it to your setup:
   cp example.env .env

3. **Deploy with Docker Compose:**
   docker-compose up -d

The dashboard will now be accessible via your configured port (e.g., `http://localhost:8080`).

---

## 📁 Project Structure

```text
StarlinkMonitor/
├── collector/          # Python background worker (gRPC polling)
├── frontend/           # Web server and dashboard UI (index.html, JS, CSS)
├── scripts/            # Maintenance, translation, and 2FA automation scripts
├── data/               # Persistent SQLite database storage (local & untracked)
├── archive/            # Local backup/ZIP folder (untracked)
├── docker-compose.yml  # Multi-container orchestration
└── README.md           # Project documentation

---

## 🔒 License & Safety

* **Data Isolation:** All database entries (`data/`), `.env` configurations, and legacy backup `.zip` files are strictly excluded from git tracking to prevent accidental data leaks.