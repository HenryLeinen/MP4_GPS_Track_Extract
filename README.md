# Vantrue GPS Track Extractor

A local web application that extracts GPS data from dashcam MP4 recordings, visualises tracks on an interactive map, and exports them as GPX files with named rest stops.

---

## Purpose

Vantrue (and many other dashcam models) embed GPS telemetry directly in the MP4 video file using the ExifTool-readable metadata stream. This tool reads that metadata, assembles the recorded coordinates into ordered tracks, and lets you:

- **Preview** a track from a single clip on an interactive map.
- **Combine** all clips in a folder into one continuous journey track.
- **Colour-code** the route by day, elevation, or speed.
- **Mark rest stops** — the last GPS point of each calendar day is shown as a named station marker.
- **Export** the combined track as a standard GPX file, including any named stations as waypoints.
- **Import** a previously exported GPX file and view it instantly.

---

## Abilities

| Feature | Description |
|---|---|
| Single-file track preview | Select any scanned MP4 from the list and draw its GPS path. |
| Combined track | Merge all scanned files into one route, sorted numerically by filename. |
| Colour profiles | Colour the route by workday, elevation gradient, or speed gradient. |
| Distance table | Per-day distance breakdown shown alongside the map. |
| Daily rest stops | Toggle station markers for the last recorded point of each day. |
| Station naming | Click a marker (or use the Stations panel) to give each stop a custom name. |
| Name persistence | Station names survive page reloads via `localStorage` and round-trip through GPX waypoints on export/import. |
| GPX export | Writes a valid GPX 1.1 file with track points, optional elevation, speed, and station waypoints. |
| GPX import | Loads any GPX file — supports `<trk>` track points, `<rte>` route points, and `<wpt>` waypoint-only files. |
| Folder chooser | Native OS folder picker dialog (requires `tkinter`). |
| LAN mode | Pass `--lan` at startup to bind to `0.0.0.0:8000` for access from other devices on the network. |

---

## Dependencies

### System

| Dependency | Install | Notes |
|---|---|---|
| **Python 3.11+** | `brew install python` / system package | Required. |
| **ExifTool** | `brew install exiftool` (macOS) · `sudo apt install libimage-exiftool-perl` (Debian/Ubuntu) | Extracts GPS metadata from MP4 files. Must be on `PATH`. |
| **tkinter** | Included with most Python distributions | Required for the native folder picker dialog. Without it the app still runs but "Ordner wählen" buttons are disabled. |

### Python packages

| Package | Version used | Purpose |
|---|---|---|
| `fastapi` | 0.136+ | HTTP server and API routing. |
| `uvicorn` | 0.49+ | ASGI server that runs FastAPI. |
| `pydantic` | (installed with FastAPI) | Request body validation. |

### Browser (CDN, no install required)

| Library | Source | Purpose |
|---|---|---|
| **Leaflet 1.9.4** | `unpkg.com` | Interactive map and track rendering. |
| **OpenStreetMap tiles** | `tile.openstreetmap.org` | Map tile background. |

---

## Project structure

```
MP4_GPS_Track_Extract/
├── main.py              # FastAPI backend — GPS extraction, GPX I/O, API routes
├── templates/
│   └── index.html       # HTML layout with placeholder tokens
├── static/
│   ├── app.js           # All browser-side map and UI logic
│   └── app.css          # Stylesheet
└── README.md
```

---

## Setup

```bash
# 1. Clone or copy the project folder
cd MP4_GPS_Track_Extract

# 2. Create a virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate          # macOS / Linux
# .venv\Scripts\activate.bat       # Windows

# 3. Install Python dependencies
pip install fastapi uvicorn

# 4. Install ExifTool
#    macOS:
brew install exiftool
#    Debian / Ubuntu / Raspberry Pi OS:
sudo apt install libimage-exiftool-perl
```

---

## Usage

### Start the server

```bash
# Local access only (default)
python3 main.py

# LAN access — accessible from other devices on the same network
python3 main.py --lan
```

Then open **http://127.0.0.1:8000** in a browser.

---

### Scan and preview MP4 files

1. Enter the path to your MP4 folder in **MP4-Quellordner**, or click **Ordner wählen** for a native dialog.
2. Optionally adjust the **Dateimuster** (file glob, default `*_N_A.MP4`). The scan is recursive — subfolders are included.
3. Click **Dateien scannen**. Matching MP4 files appear in the list.
4. Select a file and click **Ausgewählten Track zeigen** to preview its route, or click **Gesamttrack erzeugen** to combine all files into one journey.

### Export a GPX file

1. Fill in **GPX-Zielordner** (output folder) and **GPX-Dateiname** (output filename, default `track.gpx`).
2. Click **GPX aus MP4 schreiben**. A progress bar shows extraction status. When done, the file is written to the specified folder and includes any named stations as GPX waypoints.

### Import an existing GPX file

Click the **GPX-Datei laden** file picker and select a `.gpx` file. The track renders immediately on the map and any named station waypoints are restored.

### Name rest stops

1. Toggle **Tagesstationen zeigen** to show station markers on the map.
2. Click a marker to enter a name via a prompt, or use the **Stationen** panel on the right to rename, save, delete, or jump to a station.

### Map controls

| Control | Action |
|---|---|
| **Route maximieren** | Fit the current track to the map viewport. |
| **Vollbild** | Enter or exit fullscreen. |
| **Farbdarstellung** | Switch colour profile: standard, elevation, workday, or speed. |
