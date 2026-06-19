#!/usr/bin/env python3
"""
Vantrue / Dashcam MP4 GPS Track Extractor

Web interface to select MP4 files by directory + glob pattern, preview individual
GPS tracks, combine them in filename-number order and export one GPX file.

Requirements:
  sudo apt install exiftool        # Debian/Raspberry Pi OS/Ubuntu
  pip install fastapi uvicorn

Run:
  python3 vantrue_gps_track_web.py
  open http://127.0.0.1:8000
"""

from __future__ import annotations

import html
import json
import math
import queue
import re
import subprocess
import sys
import threading
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic import Field

try:
    import tkinter as tk
    from tkinter import filedialog
except ImportError:  # pragma: no cover - tkinter is optional on some systems
    tk = None
    filedialog = None

from fastapi import FastAPI
from fastapi import File
from fastapi import HTTPException
from fastapi import Query
from fastapi.staticfiles import StaticFiles
from fastapi import UploadFile
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI(title="Vantrue GPS Track Extractor")

CACHE: dict[str, dict[str, Any]] = {}
DEFAULT_SOURCE_DIR = "/Volumes/NO NAME/Normal"
DEFAULT_TARGET_DIR = ""
JOB_LOCK = threading.Lock()
JOBS: dict[str, dict[str, Any]] = {}
HTML_TEMPLATE_PATH = Path(__file__).with_name("templates") / "index.html"
STATIC_DIR = Path(__file__).with_name("static")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@dataclass
class FolderDialogRequest:
    prompt: str
    initialdir: str
    result: str = ""
    done: threading.Event = field(default_factory=threading.Event)


class FolderDialogBroker:
    def __init__(self) -> None:
        self._requests: queue.Queue[FolderDialogRequest] = queue.Queue()
        self._root: Any = None

    def attach_root(self, root: Any) -> None:
        self._root = root

    def request_folder(self, prompt: str, initialdir: str = "") -> str:
        if tk is None or filedialog is None:
            return ""

        request = FolderDialogRequest(prompt=prompt, initialdir=initialdir)
        self._requests.put(request)
        request.done.wait()
        return request.result

    def pump(self) -> None:
        if self._root is None:
            return

        try:
            request = self._requests.get_nowait()
        except queue.Empty:
            self._root.after(100, self.pump)
            return

        folder = filedialog.askdirectory(title=request.prompt, initialdir=request.initialdir or None)
        request.result = folder or ""
        request.done.set()
        self._root.after(0, self.pump)


DIALOG_BROKER = FolderDialogBroker()


@dataclass
class TrackPoint:
    lat: float
    lon: float
    ele: float | None = None
    time: str | None = None
    speed_kmh: float | None = None
    source_file: str | None = None


@dataclass
class StationWaypoint:
    day: str
    lat: float
    lon: float
    name: str


class WriteStartRequest(BaseModel):
    directory: str
    pattern: str = "*_N_A.MP4"
    outdir: str = ""
    outname: str = "track.gpx"
    stations: list[StationWaypoint] = Field(default_factory=list)


def numeric_sort_key(path: Path) -> tuple:
    """Sort by all integer groups in filename, then by complete name."""
    nums = [int(x) for x in re.findall(r"\d+", path.name)]
    return (*nums, path.name.lower()) if nums else (math.inf, path.name.lower())


def scan_files(directory: str, pattern: str) -> list[Path]:
    base = Path(directory).expanduser().resolve()
    if not base.is_dir():
        raise HTTPException(status_code=400, detail=f"Quellverzeichnis existiert nicht: {base}")
    files = sorted(base.rglob(pattern), key=numeric_sort_key)
    return [p for p in files if p.is_file() and p.suffix.lower() == ".mp4"]


def run_exiftool(mp4: Path) -> list[dict[str, Any]]:
    cmd = ["exiftool", "-ee", "-j", "-n", str(mp4)]
    try:
        process = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("ExifTool wurde nicht gefunden. Installiere es mit: sudo apt install exiftool") from exc

    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or f"ExifTool Fehler bei {mp4.name}")

    try:
        data = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ExifTool lieferte kein gültiges JSON für {mp4.name}") from exc

    return data if isinstance(data, list) else []


def run_exiftool_gps_series(mp4: Path) -> dict[str, list[str]]:
    """Read repeated GPS tags as series values.

    ExifTool JSON output can collapse duplicate keys (GPSLatitude/GPSLongitude)
    into one value. This text-mode query keeps all repeated samples.
    """
    cmd = [
        "exiftool",
        "-ee3",
        "-n",
        "-a",
        "-GPSLatitude",
        "-GPSLongitude",
        "-GPSAltitude",
        "-GPSSpeed",
        "-GPSDateTime",
        "-SampleTime",
        str(mp4),
    ]
    try:
        process = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("ExifTool wurde nicht gefunden. Installiere es mit: sudo apt install exiftool") from exc

    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or f"ExifTool Fehler bei {mp4.name}")

    series = {
        "lat": [],
        "lon": [],
        "ele": [],
        "speed": [],
        "gps_time": [],
        "sample_time": [],
    }

    for line in process.stdout.splitlines():
        match = re.match(r"^([^:]+?)\s*:\s*(.*)$", line.strip())
        if not match:
            continue
        tag = match.group(1).strip().lower()
        value = match.group(2).strip()
        if not value:
            continue

        if tag == "gps latitude":
            series["lat"].append(value)
        elif tag == "gps longitude":
            series["lon"].append(value)
        elif tag == "gps altitude":
            series["ele"].append(value)
        elif tag == "gps speed":
            series["speed"].append(value)
        elif tag == "gps date/time":
            series["gps_time"].append(value)
        elif tag == "sample time":
            series["sample_time"].append(value)

    return series


def find_value(record: dict[str, Any], names: tuple[str, ...]) -> Any:
    lower = {k.lower(): v for k, v in record.items()}
    for name in names:
        if name in record:
            return record[name]
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def parse_time(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    text = re.sub(r"^(\d{4}):(\d{2}):(\d{2})", r"\1-\2-\3", text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return str(value)


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_speed_kmh(value: Any) -> float | None:
    return to_float(value)


def extract_track_with_stats(mp4: Path) -> tuple[list[TrackPoint], dict[str, Any]]:
    cache_key = f"v2:{mp4.resolve()}"
    if cache_key in CACHE:
        cached = CACHE[cache_key]
        return [TrackPoint(**point) for point in cached["points"]], dict(cached["stats"])

    points: list[TrackPoint] = []
    raw_gps_records = 0
    duplicate_points_removed = 0
    parsed_from_series = False

    series = run_exiftool_gps_series(mp4)
    sample_count = min(len(series["lat"]), len(series["lon"]))
    if sample_count > 0:
        parsed_from_series = True
        raw_gps_records = sample_count
        for index in range(sample_count):
            lat = to_float(series["lat"][index])
            lon = to_float(series["lon"][index])
            if lat is None or lon is None:
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue

            ele = to_float(series["ele"][index]) if index < len(series["ele"]) else None
            speed = normalize_speed_kmh(series["speed"][index]) if index < len(series["speed"]) else None
            time_raw = None
            if index < len(series["gps_time"]):
                time_raw = series["gps_time"][index]
            elif index < len(series["sample_time"]):
                time_raw = series["sample_time"][index]

            if points and abs(points[-1].lat - lat) < 1e-10 and abs(points[-1].lon - lon) < 1e-10:
                duplicate_points_removed += 1
                continue

            points.append(
                TrackPoint(
                    lat=lat,
                    lon=lon,
                    ele=ele,
                    time=parse_time(time_raw),
                    speed_kmh=speed,
                    source_file=mp4.name,
                )
            )

    if not parsed_from_series:
        data = run_exiftool(mp4)
        for record in data:
            if not isinstance(record, dict):
                continue

            lat = to_float(find_value(record, ("GPSLatitude", "LocationLatitude", "Latitude")))
            lon = to_float(find_value(record, ("GPSLongitude", "LocationLongitude", "Longitude")))
            if lat is None or lon is None:
                continue
            raw_gps_records += 1
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue

            ele = to_float(find_value(record, ("GPSAltitude", "Altitude")))
            time_value = find_value(record, ("GPSDateTime", "DateTimeOriginal", "CreateDate", "SampleTime"))
            speed = normalize_speed_kmh(find_value(record, ("GPSSpeed", "GPSVelocity", "Speed")))

            if points and abs(points[-1].lat - lat) < 1e-10 and abs(points[-1].lon - lon) < 1e-10:
                duplicate_points_removed += 1
                continue

            points.append(
                TrackPoint(
                    lat=lat,
                    lon=lon,
                    ele=ele,
                    time=parse_time(time_value),
                    speed_kmh=speed,
                    source_file=mp4.name,
                )
            )

    stats = {
        "file": mp4.name,
        "raw_gps_records": raw_gps_records,
        "valid_points": len(points),
        "ignored_points": max(raw_gps_records - len(points), 0),
        "duplicate_points_removed": duplicate_points_removed,
    }
    CACHE[cache_key] = {"points": [asdict(point) for point in points], "stats": stats}
    return points, stats


def extract_track(mp4: Path) -> list[TrackPoint]:
    points, _stats = extract_track_with_stats(mp4)
    return points


def extract_many(files: list[Path]) -> list[TrackPoint]:
    all_points: list[TrackPoint] = []
    for file_path in files:
        all_points.extend(extract_track(file_path))
    return all_points


def extract_many_with_stats(files: list[Path]) -> tuple[list[TrackPoint], list[dict[str, Any]]]:
    all_points: list[TrackPoint] = []
    per_file_stats: list[dict[str, Any]] = []
    for file_path in files:
        points, stats = extract_track_with_stats(file_path)
        all_points.extend(points)
        per_file_stats.append(stats)
    return all_points, per_file_stats


def start_write_job(directory: str, pattern: str, outdir: str, outname: str, stations: list[StationWaypoint] | None = None) -> dict[str, Any]:
    files = scan_files(directory, pattern)
    if not files:
        raise HTTPException(status_code=400, detail="Keine passenden MP4-Dateien gefunden.")

    normalized_outdir = outdir or directory
    normalized_outname = outname if outname.lower().endswith(".gpx") else f"{outname}.gpx"
    out_file = Path(normalized_outdir).expanduser().resolve() / normalized_outname

    job_id = str(uuid.uuid4())
    with JOB_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "state": "queued",
            "directory": directory,
            "pattern": pattern,
            "outdir": normalized_outdir,
            "outname": normalized_outname,
            "out_file": str(out_file),
            "total_files": len(files),
            "processed_files": 0,
            "current_file": "",
            "points": 0,
            "message": "Wartet auf Start ...",
            "error": "",
        }

    def worker() -> None:
        all_points: list[TrackPoint] = []
        try:
            with JOB_LOCK:
                JOBS[job_id]["state"] = "running"
                JOBS[job_id]["message"] = "Extraktion gestartet ..."

            for index, file_path in enumerate(files, start=1):
                with JOB_LOCK:
                    JOBS[job_id]["current_file"] = file_path.name
                    JOBS[job_id]["message"] = f"Verarbeite {file_path.name} ({index}/{len(files)}) ..."

                points, _stats = extract_track_with_stats(file_path)
                all_points.extend(points)

                with JOB_LOCK:
                    JOBS[job_id]["processed_files"] = index
                    JOBS[job_id]["points"] = len(all_points)

            write_gpx(all_points, out_file, out_file.stem, stations or [])

            with JOB_LOCK:
                JOBS[job_id]["state"] = "done"
                JOBS[job_id]["message"] = (
                    f"GPX geschrieben: {out_file} mit {len(all_points)} GPS-Punkten aus {len(files)} Dateien."
                )
        except RuntimeError as exc:
            with JOB_LOCK:
                JOBS[job_id]["state"] = "error"
                JOBS[job_id]["error"] = str(exc)
                JOBS[job_id]["message"] = f"Fehler: {exc}"
        except Exception as exc:  # pragma: no cover
            with JOB_LOCK:
                JOBS[job_id]["state"] = "error"
                JOBS[job_id]["error"] = str(exc)
                JOBS[job_id]["message"] = f"Unerwarteter Fehler: {exc}"

    threading.Thread(target=worker, daemon=True).start()
    with JOB_LOCK:
        return dict(JOBS[job_id])


def start_combined_job(paths: list[Path]) -> dict[str, Any]:
    if not paths:
        raise HTTPException(status_code=400, detail="Keine Dateien ausgewählt.")

    job_id = str(uuid.uuid4())
    with JOB_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "job_type": "combined",
            "state": "queued",
            "total_files": len(paths),
            "processed_files": 0,
            "current_file": "",
            "points": 0,
            "ignored_points": 0,
            "message": "Wartet auf Start ...",
            "error": "",
            "points_data": None,
            "per_file_stats": None,
        }

    def worker() -> None:
        all_points: list[TrackPoint] = []
        per_file_stats: list[dict[str, Any]] = []
        ignored_total = 0
        try:
            with JOB_LOCK:
                JOBS[job_id]["state"] = "running"
                JOBS[job_id]["message"] = "Gesamttrack-Extraktion gestartet ..."

            for index, file_path in enumerate(paths, start=1):
                with JOB_LOCK:
                    JOBS[job_id]["current_file"] = file_path.name
                    JOBS[job_id]["message"] = f"Verarbeite {file_path.name} ({index}/{len(paths)}) ..."

                points, stats = extract_track_with_stats(file_path)
                all_points.extend(points)
                per_file_stats.append(stats)
                ignored_total += int(stats.get("ignored_points", 0))

                with JOB_LOCK:
                    JOBS[job_id]["processed_files"] = index
                    JOBS[job_id]["points"] = len(all_points)
                    JOBS[job_id]["ignored_points"] = ignored_total

            with JOB_LOCK:
                JOBS[job_id]["state"] = "done"
                JOBS[job_id]["points_data"] = [asdict(point) for point in all_points]
                JOBS[job_id]["per_file_stats"] = per_file_stats
                JOBS[job_id]["message"] = (
                    f"Gesamttrack fertig: {len(all_points)} GPS-Punkte, ignoriert: {ignored_total}, Dateien: {len(paths)}."
                )
        except RuntimeError as exc:
            with JOB_LOCK:
                JOBS[job_id]["state"] = "error"
                JOBS[job_id]["error"] = str(exc)
                JOBS[job_id]["message"] = f"Fehler: {exc}"
        except Exception as exc:  # pragma: no cover
            with JOB_LOCK:
                JOBS[job_id]["state"] = "error"
                JOBS[job_id]["error"] = str(exc)
                JOBS[job_id]["message"] = f"Unerwarteter Fehler: {exc}"

    threading.Thread(target=worker, daemon=True).start()
    with JOB_LOCK:
        return dict(JOBS[job_id])


def write_gpx(points: list[TrackPoint], out_file: Path, track_name: str, stations: list[StationWaypoint] | None = None) -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)

    gpx = ET.Element(
        "gpx",
        {
            "version": "1.1",
            "creator": "vantrue_gps_track_web.py",
            "xmlns": "http://www.topografix.com/GPX/1/1",
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:schemaLocation": "http://www.topografix.com/GPX/1/1 http://www.topografix.com/GPX/1/1/gpx.xsd",
        },
    )
    track = ET.SubElement(gpx, "trk")
    ET.SubElement(track, "name").text = track_name
    segment = ET.SubElement(track, "trkseg")

    for point in points:
        trackpoint = ET.SubElement(segment, "trkpt", {"lat": f"{point.lat:.8f}", "lon": f"{point.lon:.8f}"})
        if point.ele is not None:
            ET.SubElement(trackpoint, "ele").text = f"{point.ele:.2f}"
        if point.time:
            ET.SubElement(trackpoint, "time").text = point.time
        if point.speed_kmh is not None or point.source_file:
            ext = ET.SubElement(trackpoint, "extensions")
            if point.speed_kmh is not None:
                ET.SubElement(ext, "speed_kmh").text = f"{point.speed_kmh:.3f}"
            if point.source_file:
                ET.SubElement(ext, "source_file").text = point.source_file

    for station in stations or []:
        waypoint = ET.SubElement(gpx, "wpt", {"lat": f"{station.lat:.8f}", "lon": f"{station.lon:.8f}"})
        ET.SubElement(waypoint, "name").text = station.name
        ET.SubElement(waypoint, "desc").text = f"Ruhepunkt am {station.day}"
        ext = ET.SubElement(waypoint, "extensions")
        ET.SubElement(ext, "station_day").text = station.day

    ET.ElementTree(gpx).write(out_file, encoding="utf-8", xml_declaration=True)


def parse_gpx_points_and_stations(gpx_bytes: bytes, source_name: str = "loaded.gpx") -> tuple[list[TrackPoint], list[dict[str, Any]]]:
    try:
        root = ET.fromstring(gpx_bytes)
    except ET.ParseError as exc:
        raise HTTPException(status_code=400, detail="Ungültige GPX-Datei (XML konnte nicht gelesen werden).") from exc

    ns_uri = ""
    if root.tag.startswith("{") and "}" in root.tag:
        ns_uri = root.tag[1:].split("}", 1)[0]

    def qn(tag_name: str) -> str:
        return f"{{{ns_uri}}}{tag_name}" if ns_uri else tag_name

    points: list[TrackPoint] = []
    stations: list[dict[str, Any]] = []

    def append_points(point_elements: list[ET.Element]) -> None:
        for point_el in point_elements:
            lat = to_float(point_el.attrib.get("lat"))
            lon = to_float(point_el.attrib.get("lon"))
            if lat is None or lon is None:
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue

            ele = None
            time_val = None
            speed = None
            source_file = source_name

            ele_el = point_el.find(qn("ele"))
            if ele_el is not None and ele_el.text is not None:
                ele = to_float(ele_el.text)

            time_el = point_el.find(qn("time"))
            if time_el is not None and time_el.text is not None:
                time_val = parse_time(time_el.text)

            ext_el = point_el.find(qn("extensions"))
            if ext_el is not None:
                for child in list(ext_el):
                    local_tag = child.tag
                    if local_tag.startswith("{") and "}" in local_tag:
                        local_tag = local_tag.split("}", 1)[1]
                    if local_tag == "speed_kmh" and child.text is not None:
                        speed = to_float(child.text)
                    elif local_tag == "source_file" and child.text is not None and child.text.strip():
                        source_file = child.text.strip()

            points.append(
                TrackPoint(
                    lat=lat,
                    lon=lon,
                    ele=ele,
                    time=time_val,
                    speed_kmh=speed,
                    source_file=source_file,
                )
            )

    track_points: list[ET.Element] = []
    for track in root.findall(f".//{qn('trk')}"):
        for segment in track.findall(qn("trkseg")):
            track_points.extend(segment.findall(qn("trkpt")))
    append_points(track_points)

    # Some GPX exporters store navigable paths as routes instead of tracks.
    if not points:
        route_points: list[ET.Element] = []
        for route in root.findall(f".//{qn('rte')}"):
            route_points.extend(route.findall(qn("rtept")))
        append_points(route_points)

    waypoint_elements = root.findall(f".//{qn('wpt')}")

    if not points:
        append_points(waypoint_elements)

    for waypoint in waypoint_elements:
        lat = to_float(waypoint.attrib.get("lat"))
        lon = to_float(waypoint.attrib.get("lon"))
        if lat is None or lon is None:
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue

        name = f"Ruhepunkt {source_name}"
        day = "unbekannt"

        name_el = waypoint.find(qn("name"))
        if name_el is not None and name_el.text is not None and name_el.text.strip():
            name = name_el.text.strip()

        desc_el = waypoint.find(qn("desc"))
        if desc_el is not None and desc_el.text:
            match = re.search(r"am\s+(\d{4}-\d{2}-\d{2})", desc_el.text)
            if match:
                day = match.group(1)

        ext_el = waypoint.find(qn("extensions"))
        if ext_el is not None:
            for child in list(ext_el):
                local_tag = child.tag
                if local_tag.startswith("{") and "}" in local_tag:
                    local_tag = local_tag.split("}", 1)[1]
                if local_tag == "station_day" and child.text is not None and child.text.strip():
                    day = child.text.strip()

        stations.append({"day": day, "lat": lat, "lon": lon, "name": name})

    return points, stations


def html_page(
    directory: str = "",
    pattern: str = "*_N_A.MP4",
    outdir: str = "",
    outname: str = "track.gpx",
    files: list[Path] | None = None,
    message: str = "",
) -> str:
    file_options = ""
    if files:
        for file_path in files:
            file_options += f'<option value="{html.escape(str(file_path))}">{html.escape(file_path.name)}</option>'

    files_json = json.dumps([str(file_path) for file_path in files] if files else [])
    template = HTML_TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template
        .replace("{{", "{")
        .replace("}}", "}")
        .replace("__MESSAGE_BLOCK__", f'<div class="msg">{html.escape(message)}</div>' if message else "")
        .replace("__DIRECTORY__", html.escape(directory))
        .replace("__PATTERN__", html.escape(pattern))
        .replace("__OUTDIR__", html.escape(outdir))
        .replace("__OUTNAME__", html.escape(outname))
        .replace("__FILE_OPTIONS__", file_options)
        .replace("__FILES_JSON__", files_json)
    )


def choose_folder(prompt: str, initialdir: str = "") -> str:
    return DIALOG_BROKER.request_folder(prompt, initialdir)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return html_page(DEFAULT_SOURCE_DIR, outdir=DEFAULT_TARGET_DIR)


@app.get("/scan", response_class=HTMLResponse)
def scan(directory: str, pattern: str = "*_N_A.MP4", outdir: str = "", outname: str = "track.gpx") -> str:
    global DEFAULT_SOURCE_DIR, DEFAULT_TARGET_DIR

    if not directory:
        directory = choose_folder("MP4-Quellordner auswählen", DEFAULT_SOURCE_DIR or str(Path.cwd()))
    if not directory:
        return html_page(DEFAULT_SOURCE_DIR, pattern, DEFAULT_TARGET_DIR, outname, message="Kein Quellordner ausgewählt.")

    DEFAULT_SOURCE_DIR = directory
    if outdir:
        DEFAULT_TARGET_DIR = outdir

    files = scan_files(directory, pattern)
    return html_page(directory, pattern, outdir, outname, files, f"{len(files)} MP4-Dateien gefunden.")


@app.get("/api/track")
def api_track(file: str = Query(...)) -> JSONResponse:
    mp4 = Path(file).expanduser().resolve()
    if not mp4.is_file():
        raise HTTPException(status_code=404, detail="Datei nicht gefunden")
    try:
        points, stats = extract_track_with_stats(mp4)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse({"file": str(mp4), "points": [asdict(point) for point in points], "stats": stats})


@app.get("/api/combined")
def api_combined(files: str = Query(...)) -> JSONResponse:
    paths = parse_file_paths(files)
    try:
        points, per_file_stats = extract_many_with_stats(paths)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse({"points": [asdict(point) for point in points], "per_file_stats": per_file_stats})


@app.post("/api/load-gpx")
async def api_load_gpx(file: UploadFile = File(...)) -> JSONResponse:
    filename = file.filename or "loaded.gpx"
    if not filename.lower().endswith(".gpx"):
        raise HTTPException(status_code=400, detail="Bitte eine GPX-Datei hochladen (.gpx).")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Die GPX-Datei ist leer.")

    points, stations = parse_gpx_points_and_stations(content, source_name=Path(filename).name)
    if not points:
        raise HTTPException(status_code=400, detail="Keine Trackpunkte in der GPX-Datei gefunden.")

    return JSONResponse(
        {
            "label": Path(filename).name,
            "points": [asdict(point) for point in points],
            "stations": stations,
            "stats": {
                "file": Path(filename).name,
                "valid_points": len(points),
            },
        }
    )


def parse_file_paths(files: str) -> list[Path]:
    try:
        file_list = json.loads(files)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Ungültige Dateiliste") from exc

    if not isinstance(file_list, list):
        raise HTTPException(status_code=400, detail="Ungültige Dateiliste")

    paths = [Path(file_path).expanduser().resolve() for file_path in file_list]
    for path in paths:
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"Datei nicht gefunden: {path}")
    return paths


@app.get("/api/choose-source-folder")
def api_choose_source_folder() -> JSONResponse:
    global DEFAULT_SOURCE_DIR

    folder = choose_folder("MP4-Quellordner auswählen", DEFAULT_SOURCE_DIR or str(Path.cwd()))
    if folder:
        DEFAULT_SOURCE_DIR = folder
    return JSONResponse({"folder": folder})


@app.get("/api/choose-target-folder")
def api_choose_target_folder() -> JSONResponse:
    global DEFAULT_TARGET_DIR

    folder = choose_folder("Zielordner für GPX auswählen", DEFAULT_TARGET_DIR or DEFAULT_SOURCE_DIR or str(Path.cwd()))
    if folder:
        DEFAULT_TARGET_DIR = folder
    return JSONResponse({"folder": folder})


@app.post("/api/write/start")
def api_write_start(request: WriteStartRequest) -> JSONResponse:
    global DEFAULT_SOURCE_DIR, DEFAULT_TARGET_DIR

    directory = request.directory
    pattern = request.pattern
    outdir = request.outdir
    outname = request.outname
    if not directory:
        raise HTTPException(status_code=400, detail="Kein Quellordner angegeben")

    DEFAULT_SOURCE_DIR = directory
    if outdir:
        DEFAULT_TARGET_DIR = outdir

    job = start_write_job(directory, pattern, outdir, outname, request.stations)
    return JSONResponse({"job_id": job["job_id"], "total_files": job["total_files"], "out_file": job["out_file"]})


@app.post("/api/combined/start")
def api_combined_start(files: str = Query(...)) -> JSONResponse:
    paths = parse_file_paths(files)
    job = start_combined_job(paths)
    return JSONResponse({"job_id": job["job_id"], "total_files": job["total_files"]})


@app.get("/api/combined/status")
def api_combined_status(job_id: str = Query(...)) -> JSONResponse:
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job nicht gefunden")

        status = {
            "job_id": job.get("job_id"),
            "state": job.get("state"),
            "total_files": job.get("total_files", 0),
            "processed_files": job.get("processed_files", 0),
            "current_file": job.get("current_file", ""),
            "points": job.get("points", 0),
            "ignored_points": job.get("ignored_points", 0),
            "message": job.get("message", ""),
            "error": job.get("error", ""),
        }

        if job.get("state") == "done":
            status["points_data"] = job.get("points_data", [])
            status["per_file_stats"] = job.get("per_file_stats", [])

        return JSONResponse(status)


@app.get("/api/write/status")
def api_write_status(job_id: str = Query(...)) -> JSONResponse:
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job nicht gefunden")
        return JSONResponse(dict(job))


@app.post("/extract", response_class=HTMLResponse)
async def extract(directory: str = Query(None), pattern: str = Query("*_N_A.MP4"), outdir: str = Query(""), outname: str = Query("track.gpx")) -> str:
    return html_page(message="Bitte nutze den Button nach dem Suchen oder rufe /write?... auf. Siehe Code-Hinweis unten.")


@app.get("/write", response_class=HTMLResponse)
def write(directory: str, pattern: str = "*_N_A.MP4", outdir: str = "", outname: str = "track.gpx") -> str:
    global DEFAULT_SOURCE_DIR, DEFAULT_TARGET_DIR

    if not directory:
        directory = choose_folder("MP4-Quellordner auswählen", DEFAULT_SOURCE_DIR or str(Path.cwd()))
    if not directory:
        return html_page(DEFAULT_SOURCE_DIR, pattern, DEFAULT_TARGET_DIR, outname, message="Kein Quellordner ausgewählt.")

    DEFAULT_SOURCE_DIR = directory

    if not outdir:
        outdir = choose_folder("Zielordner für GPX auswählen", DEFAULT_TARGET_DIR or directory)
    if not outdir:
        return html_page(directory, pattern, DEFAULT_TARGET_DIR, outname, message="Kein Zielordner ausgewählt.")

    DEFAULT_TARGET_DIR = outdir

    files = scan_files(directory, pattern)
    if not files:
        return html_page(directory, pattern, outdir, outname, [], "Keine passenden MP4-Dateien gefunden.")

    if not outname.lower().endswith(".gpx"):
        outname += ".gpx"
    out_file = Path(outdir).expanduser().resolve() / outname

    try:
        points = extract_many(files)
        write_gpx(points, out_file, out_file.stem)
    except RuntimeError as exc:
        return html_page(directory, pattern, outdir, outname, files, f"Fehler: {exc}")

    return html_page(directory, pattern, outdir, outname, files, f"GPX geschrieben: {out_file} mit {len(points)} GPS-Punkten aus {len(files)} Dateien.")


if __name__ == "__main__":
    host = "0.0.0.0" if "--lan" in sys.argv else "127.0.0.1"

    config = uvicorn.Config(app, host=host, port=8000, log_level="info")
    server = uvicorn.Server(config)

    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    if tk is None or filedialog is None:
        server_thread.join()
    else:
        root = tk.Tk()
        root.withdraw()
        root.protocol("WM_DELETE_WINDOW", lambda: (setattr(server, "should_exit", True), root.destroy()))
        DIALOG_BROKER.attach_root(root)
        root.after(100, DIALOG_BROKER.pump)
        try:
            root.mainloop()
        finally:
            server.should_exit = True
