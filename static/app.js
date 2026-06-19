const files = Array.isArray(window.APP_FILES) ? window.APP_FILES : [];

const map = L.map('map').setView([51.0, 8.0], 6);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors',
}).addTo(map);

let layer = null;
let stationLayer = null;
let lastDrawnPoints = [];
let lastDrawnStations = [];
let lastDrawnLabel = '';

const STATION_NAME_PREFIX = 'gpsTrackStationName:';

function clamp01(value) {
    return Math.max(0, Math.min(1, value));
}

function colorLerp(start, end, amount) {
    const t = clamp01(amount);
    const r = Math.round(start[0] + (end[0] - start[0]) * t);
    const g = Math.round(start[1] + (end[1] - start[1]) * t);
    const b = Math.round(start[2] + (end[2] - start[2]) * t);
    return `rgb(${r}, ${g}, ${b})`;
}

function formatNum(value, digits = 1) {
    if (typeof value !== 'number' || !Number.isFinite(value)) return 'n/a';
    return value.toFixed(digits);
}

function gradientLegend(startColor, endColor) {
    return `<span style="display:inline-block; width:100%; height:10px; border:1px solid #777; border-radius:3px; background: linear-gradient(to right, ${startColor}, ${endColor});"></span>`;
}

function toRad(deg) {
    return (deg * Math.PI) / 180;
}

function segmentDistanceKm(point1, point2) {
    const radiusKm = 6371.0088;
    const lat1 = toRad(point1.lat);
    const lat2 = toRad(point2.lat);
    const dLat = toRad(point2.lat - point1.lat);
    const dLon = toRad(point2.lon - point1.lon);
    const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
    return 2 * radiusKm * Math.asin(Math.sqrt(a));
}

function dayKey(point) {
    if (point && typeof point.time === 'string' && point.time.length >= 10) {
        return point.time.slice(0, 10);
    }
    if (point && typeof point.source_file === 'string') {
        const match = point.source_file.match(/^(\d{4})(\d{2})(\d{2})_/);
        if (match) return `${match[1]}-${match[2]}-${match[3]}`;
    }
    return 'unbekannt';
}

function buildDistanceStats(points) {
    let totalKm = 0;
    const perDay = {};

    if (!Array.isArray(points) || points.length < 2) {
        return { totalKm, perDay };
    }

    for (let index = 0; index < points.length - 1; index += 1) {
        const point1 = points[index];
        const point2 = points[index + 1];
        if (!point1 || !point2) continue;
        if (typeof point1.lat !== 'number' || typeof point1.lon !== 'number') continue;
        if (typeof point2.lat !== 'number' || typeof point2.lon !== 'number') continue;
        if (!Number.isFinite(point1.lat) || !Number.isFinite(point1.lon)) continue;
        if (!Number.isFinite(point2.lat) || !Number.isFinite(point2.lon)) continue;

        const km = segmentDistanceKm(point1, point2);
        totalKm += km;
        const key = dayKey(point1);
        perDay[key] = (perDay[key] || 0) + km;
    }

    return { totalKm, perDay };
}

function buildWorkdayColorMap(points) {
    const palette = [
        '#e63946', '#457b9d', '#2a9d8f', '#f4a261', '#264653',
        '#8d99ae', '#06d6a0', '#ef476f', '#118ab2', '#8338ec',
        '#3a86ff', '#ff006e', '#ffbe0b', '#fb5607',
    ];
    const days = [];
    for (const point of points) {
        const key = dayKey(point);
        if (!days.includes(key)) days.push(key);
    }
    days.sort();

    const colorMap = {};
    for (let index = 0; index < days.length; index += 1) {
        colorMap[days[index]] = palette[index % palette.length];
    }
    return colorMap;
}

function getNumericRange(points, fieldName) {
    const values = [];
    for (const point of points) {
        const value = point ? point[fieldName] : null;
        if (typeof value === 'number' && Number.isFinite(value)) values.push(value);
    }
    if (values.length === 0) return null;
    return { min: Math.min(...values), max: Math.max(...values) };
}

function buildProfileContext(points, profile) {
    return {
        workdayColorMap: profile === 'workday' ? buildWorkdayColorMap(points) : null,
        elevationRange: profile === 'elevation' ? getNumericRange(points, 'ele') : null,
        speedRange: profile === 'speed' ? getNumericRange(points, 'speed_kmh') : null,
    };
}

function segmentColor(point, profile, context) {
    if (profile === 'workday') {
        return context.workdayColorMap?.[dayKey(point)] || '#666666';
    }
    if (profile === 'elevation') {
        const value = typeof point?.ele === 'number' ? point.ele : null;
        if (value === null || !context.elevationRange) return '#808080';
        const span = context.elevationRange.max - context.elevationRange.min;
        const t = span <= 0 ? 0.5 : (value - context.elevationRange.min) / span;
        return colorLerp([32, 170, 32], [220, 20, 60], t);
    }
    if (profile === 'speed') {
        const value = typeof point?.speed_kmh === 'number' ? point.speed_kmh : null;
        if (value === null || !context.speedRange) return '#808080';
        const span = context.speedRange.max - context.speedRange.min;
        const t = span <= 0 ? 0.5 : (value - context.speedRange.min) / span;
        return colorLerp([220, 20, 60], [30, 90, 220], t);
    }
    return '#1565c0';
}

function clearLegend(message) {
    const titleEl = document.getElementById('legendTitle');
    const contentEl = document.getElementById('legendContent');
    if (!titleEl || !contentEl) return;
    titleEl.textContent = 'Legende';
    contentEl.textContent = message || 'Kein Track geladen.';
}

function updateLegend(profile, context) {
    const titleEl = document.getElementById('legendTitle');
    const contentEl = document.getElementById('legendContent');
    if (!titleEl || !contentEl) return;

    if (profile === 'elevation') {
        titleEl.textContent = 'Profil 1: Hoehe';
        if (!context.elevationRange) {
            contentEl.textContent = 'Keine Hoehendaten verfuegbar.';
            return;
        }
        contentEl.innerHTML = [
            'Niedrig (gruen) -> Hoch (rot)',
            gradientLegend('rgb(32,170,32)', 'rgb(220,20,60)'),
            `Min: ${formatNum(context.elevationRange.min, 1)} m`,
            `Max: ${formatNum(context.elevationRange.max, 1)} m`,
        ].join('<br>');
        return;
    }

    if (profile === 'speed') {
        titleEl.textContent = 'Profil 3: Geschwindigkeit';
        if (!context.speedRange) {
            contentEl.textContent = 'Keine Geschwindigkeitsdaten verfuegbar.';
            return;
        }
        contentEl.innerHTML = [
            'Langsam (rot) -> Schnell (blau)',
            gradientLegend('rgb(220,20,60)', 'rgb(30,90,220)'),
            `Min: ${formatNum(context.speedRange.min, 1)} km/h`,
            `Max: ${formatNum(context.speedRange.max, 1)} km/h`,
        ].join('<br>');
        return;
    }

    if (profile === 'workday') {
        titleEl.textContent = 'Profil 2: Wochentag';
        const workdayMap = context.workdayColorMap || {};
        const days = Object.keys(workdayMap).sort();
        if (days.length === 0) {
            contentEl.textContent = 'Keine Tagesdaten verfuegbar.';
            return;
        }
        contentEl.innerHTML = days
            .map(day => `<span class="legendSwatch" style="background:${workdayMap[day]}"></span>${day}`)
            .join('<br>');
        return;
    }

    clearLegend('Kein Farbprofil aktiv.');
}

function updateDistanceTable(points, label, profile, context) {
    const titleEl = document.getElementById('distanceTitle');
    const summaryEl = document.getElementById('distanceSummary');
    const tableEl = document.getElementById('distanceTable');
    const bodyEl = document.getElementById('distanceTbody');
    if (!titleEl || !summaryEl || !tableEl || !bodyEl) return;

    if (!Array.isArray(points) || points.length === 0) {
        titleEl.textContent = 'Strecke';
        summaryEl.textContent = 'Kein Track geladen.';
        tableEl.style.display = 'none';
        bodyEl.innerHTML = '';
        return;
    }

    const stats = buildDistanceStats(points);
    const days = Object.keys(stats.perDay).sort();
    const workdayMap = profile === 'workday' ? context.workdayColorMap : null;

    titleEl.textContent = `Strecke: ${label || 'Track'}`;
    summaryEl.textContent = `Gesamt: ${stats.totalKm.toFixed(3)} km`;

    if (days.length === 0) {
        tableEl.style.display = 'none';
        bodyEl.innerHTML = '';
        return;
    }

    bodyEl.innerHTML = days.map(day => {
        const color = workdayMap ? (workdayMap[day] || '#666666') : null;
        const colorCell = color
            ? `<span class="tableSwatch" title="${color}" style="background:${color}"></span>`
            : '<span style="color:#777;">-</span>';
        return `<tr><td>${colorCell}</td><td>${day}</td><td class="num">${stats.perDay[day].toFixed(3)}</td></tr>`;
    }).join('');
    tableEl.style.display = '';
}

function updateStationPanelSummary(text) {
    const summaryEl = document.getElementById('stationSummary');
    if (summaryEl) summaryEl.textContent = text;
}

function stationStorageKey(day, lat, lon) {
    return `${STATION_NAME_PREFIX}${day}:${lat.toFixed(5)}:${lon.toFixed(5)}`;
}

function loadStationName(day, lat, lon) {
    try {
        return localStorage.getItem(stationStorageKey(day, lat, lon)) || '';
    } catch {
        return '';
    }
}

function saveStationName(day, lat, lon, name) {
    try {
        const key = stationStorageKey(day, lat, lon);
        if (name && name.trim()) {
            localStorage.setItem(key, name.trim());
        } else {
            localStorage.removeItem(key);
        }
    } catch (err) {
        setInfo('Station konnte nicht gespeichert werden: ' + String(err));
    }
}

function stationLabelForDisplay(station) {
    const fallback = station?.day ? `Ruhepunkt ${station.day}` : 'Ruhepunkt';
    return typeof station?.name === 'string' && station.name.trim() ? station.name.trim() : fallback;
}

function normalizeStationEntry(station) {
    if (!station || typeof station !== 'object') return null;
    const point = station.point || station;
    if (typeof point?.lat !== 'number' || typeof point?.lon !== 'number') return null;
    const day = typeof station.day === 'string' && station.day.trim() ? station.day.trim() : dayKey(point);
    const name = typeof station.name === 'string' ? station.name.trim() : '';
    return { day, point, name };
}

function collectDailyStations(points) {
    const stations = [];
    if (!Array.isArray(points) || points.length === 0) return stations;

    let currentDay = null;
    let lastPointOfDay = null;

    for (const point of points) {
        if (!point || typeof point.lat !== 'number' || typeof point.lon !== 'number') continue;
        const day = dayKey(point);
        if (currentDay !== null && day !== currentDay && lastPointOfDay) {
            stations.push({ day: currentDay, point: lastPointOfDay });
        }
        currentDay = day;
        lastPointOfDay = point;
    }

    if (currentDay !== null && lastPointOfDay) {
        stations.push({ day: currentDay, point: lastPointOfDay });
    }

    return stations;
}

function deriveStations(points, stationData) {
    if (Array.isArray(stationData) && stationData.length > 0) {
        return stationData.map(normalizeStationEntry).filter(Boolean);
    }
    return collectDailyStations(points).map(station => ({
        day: station.day,
        point: station.point,
        name: loadStationName(station.day, station.point.lat, station.point.lon),
    }));
}

function collectStationsForExport(points) {
    return collectDailyStations(points).map(station => ({
        day: station.day,
        lat: station.point.lat,
        lon: station.point.lon,
        name: loadStationName(station.day, station.point.lat, station.point.lon) || `Ruhepunkt ${station.day}`,
    }));
}

function importStationNames(stations) {
    if (!Array.isArray(stations)) return;
    for (const station of stations) {
        if (!station || typeof station !== 'object') continue;
        if (typeof station.lat !== 'number' || typeof station.lon !== 'number') continue;
        const day = typeof station.day === 'string' && station.day.trim() ? station.day.trim() : dayKey(station);
        const name = typeof station.name === 'string' ? station.name.trim() : '';
        if (name) saveStationName(day, station.lat, station.lon, name);
    }
}

function focusStationOnMap(lat, lon) {
    if (typeof lat !== 'number' || typeof lon !== 'number') return;
    map.setView([lat, lon], Math.max(map.getZoom(), 16));
}

function redrawCurrentTrack() {
    if (Array.isArray(lastDrawnPoints) && lastDrawnPoints.length > 0) {
        draw(lastDrawnPoints, lastDrawnLabel || 'Track', lastDrawnStations || []);
    }
}

function updateStationName(station, inputEl) {
    if (!station || !inputEl) return;
    const nextValue = inputEl.value.trim();
    saveStationName(station.day, station.lat, station.lon, nextValue);
    redrawCurrentTrack();
    setInfo(nextValue ? `Station gespeichert: ${nextValue}` : `Station fuer ${station.day} entfernt.`);
}

function deleteStationName(station, inputEl) {
    if (!station) return;
    saveStationName(station.day, station.lat, station.lon, '');
    if (inputEl) inputEl.value = stationLabelForDisplay({ day: station.day, name: '' });
    redrawCurrentTrack();
    setInfo(`Station fuer ${station.day} geloescht.`);
}

function renderStationPanel(points, stationData) {
    const listEl = document.getElementById('stationList');
    if (!listEl) return;

    if (!Array.isArray(points) || points.length === 0) {
        updateStationPanelSummary('Keine Stationen geladen.');
        listEl.innerHTML = '<div class="stationEmpty">Kein Track geladen.</div>';
        return;
    }

    const stations = deriveStations(points, stationData);
    updateStationPanelSummary(`${stations.length} Station${stations.length === 1 ? '' : 'en'} gefunden.`);

    if (stations.length === 0) {
        listEl.innerHTML = '<div class="stationEmpty">Keine Tagesstationen verfuegbar.</div>';
        return;
    }

    listEl.innerHTML = '';
    for (const station of stations) {
        const itemEl = document.createElement('div');
        itemEl.className = 'stationItem';

        const headerEl = document.createElement('div');
        headerEl.className = 'stationHeader';

        const dayEl = document.createElement('div');
        dayEl.className = 'stationDay';
        dayEl.textContent = station.day;

        const coordsEl = document.createElement('div');
        coordsEl.className = 'stationCoords';
        coordsEl.textContent = `${station.point.lat.toFixed(5)}, ${station.point.lon.toFixed(5)}`;

        headerEl.appendChild(dayEl);
        headerEl.appendChild(coordsEl);

        const rowEl = document.createElement('div');
        rowEl.className = 'stationRow';

        const inputEl = document.createElement('input');
        inputEl.className = 'stationInput';
        inputEl.type = 'text';
        inputEl.value = stationLabelForDisplay(station);
        inputEl.setAttribute('aria-label', `Name der Station am ${station.day}`);

        const actionsEl = document.createElement('div');
        actionsEl.className = 'stationActions';

        const focusBtn = document.createElement('button');
        focusBtn.type = 'button';
        focusBtn.textContent = 'Karte';
        focusBtn.addEventListener('click', () => focusStationOnMap(station.point.lat, station.point.lon));

        const saveBtn = document.createElement('button');
        saveBtn.type = 'button';
        saveBtn.textContent = 'Speichern';
        saveBtn.addEventListener('click', () => updateStationName({ day: station.day, lat: station.point.lat, lon: station.point.lon }, inputEl));

        const deleteBtn = document.createElement('button');
        deleteBtn.type = 'button';
        deleteBtn.textContent = 'Loeschen';
        deleteBtn.addEventListener('click', () => deleteStationName({ day: station.day, lat: station.point.lat, lon: station.point.lon }, inputEl));

        inputEl.addEventListener('keydown', event => {
            if (event.key === 'Enter') {
                event.preventDefault();
                updateStationName({ day: station.day, lat: station.point.lat, lon: station.point.lon }, inputEl);
            }
        });

        actionsEl.appendChild(focusBtn);
        actionsEl.appendChild(saveBtn);
        actionsEl.appendChild(deleteBtn);

        rowEl.appendChild(inputEl);
        rowEl.appendChild(actionsEl);

        itemEl.appendChild(headerEl);
        itemEl.appendChild(rowEl);
        listEl.appendChild(itemEl);
    }
}

function renderDailyStations(points, stationData) {
    if (stationLayer) {
        map.removeLayer(stationLayer);
        stationLayer = null;
    }

    const toggle = document.getElementById('showStationsToggle');
    if (!toggle?.checked || !Array.isArray(points) || points.length === 0) {
        return;
    }

    const stations = deriveStations(points, stationData);
    if (stations.length === 0) return;

    stationLayer = L.layerGroup().addTo(map);

    for (const station of stations) {
        const point = station.point;
        const savedName = loadStationName(station.day, point.lat, point.lon);
        const importedName = typeof station.name === 'string' ? station.name.trim() : '';
        const defaultLabel = `Ruhepunkt ${station.day}`;
        const displayName = savedName || importedName || defaultLabel;

        const marker = L.circleMarker([point.lat, point.lon], {
            radius: 8,
            color: '#8a4b00',
            weight: 2,
            fillColor: '#f4a261',
            fillOpacity: 0.95,
        }).addTo(stationLayer);

        marker.bindTooltip(displayName, {
            permanent: true,
            direction: 'top',
            offset: [0, -10],
            className: 'stationLabel',
        });

        marker.bindPopup(`<b>${displayName}</b><br>Tag: ${station.day}<br>Klicken, um den Namen zu aendern.`);
        marker.on('click', () => {
            const currentValue = loadStationName(station.day, point.lat, point.lon) || displayName;
            const nextValue = window.prompt(`Name fuer die Station am ${station.day} eingeben:`, currentValue);
            if (nextValue === null) return;

            const trimmed = nextValue.trim();
            saveStationName(station.day, point.lat, point.lon, trimmed);
            marker.setPopupContent(`<b>${trimmed || defaultLabel}</b><br>Tag: ${station.day}<br>Klicken, um den Namen zu aendern.`);
            marker.setTooltipContent(trimmed || defaultLabel);
            redrawCurrentTrack();
        });
    }
}

function setInfo(text) {
    const infoEl = document.getElementById('info');
    if (infoEl) infoEl.textContent = text;
}

async function fetchTextOrDetail(response) {
    const text = await response.text();
    try {
        const payload = JSON.parse(text);
        if (payload && typeof payload.detail === 'string') return payload.detail;
    } catch {
        // ignore JSON parse failures
    }
    return text;
}

function buildStatsLines(data) {
    const detailLines = [];
    if (data.stats) {
        const stats = data.stats;
        const parts = [
            `Datei ${stats.file}`,
            stats.raw_gps_records != null ? `Roh=${stats.raw_gps_records}` : null,
            stats.valid_points != null ? `Gueltig=${stats.valid_points}` : null,
            stats.ignored_points != null ? `Ignoriert=${stats.ignored_points}` : null,
            stats.duplicate_points_removed != null ? `Duplikate entfernt=${stats.duplicate_points_removed}` : null,
        ].filter(Boolean);
        detailLines.push(parts.join(' - '));
    }
    if (Array.isArray(data.per_file_stats)) {
        for (const stats of data.per_file_stats) {
            const parts = [
                `Datei ${stats.file}`,
                stats.raw_gps_records != null ? `Roh=${stats.raw_gps_records}` : null,
                stats.valid_points != null ? `Gueltig=${stats.valid_points}` : null,
                stats.ignored_points != null ? `Ignoriert=${stats.ignored_points}` : null,
                stats.duplicate_points_removed != null ? `Duplikate entfernt=${stats.duplicate_points_removed}` : null,
            ].filter(Boolean);
            detailLines.push(parts.join(' - '));
        }
    }
    return detailLines;
}

function draw(points, label, stations) {
    const normalizedPoints = Array.isArray(points) ? points : [];
    const normalizedStations = Array.isArray(stations) ? stations : [];

    lastDrawnPoints = normalizedPoints;
    lastDrawnLabel = label || 'Track';
    lastDrawnStations = normalizedStations;

    if (layer) {
        map.removeLayer(layer);
        layer = null;
    }
    if (stationLayer) {
        map.removeLayer(stationLayer);
        stationLayer = null;
    }

    renderStationPanel(normalizedPoints, normalizedStations);

    const latlngs = normalizedPoints
        .filter(point => typeof point?.lat === 'number' && typeof point?.lon === 'number')
        .map(point => [point.lat, point.lon]);

    if (latlngs.length === 0) {
        setInfo(`${label}: keine GPS-Punkte gefunden.`);
        clearLegend('Keine Track-Daten verfuegbar.');
        updateDistanceTable([], label, null, null);
        return;
    }

    const profile = document.getElementById('colorProfileSelect')?.value || 'default';
    const context = buildProfileContext(normalizedPoints, profile);
    updateDistanceTable(normalizedPoints, label, profile, context);
    updateLegend(profile, context);

    layer = L.layerGroup().addTo(map);

    if (latlngs.length === 1) {
        L.circleMarker(latlngs[0], { radius: 5, color: '#1565c0', fillOpacity: 0.9 })
            .addTo(layer)
            .bindPopup('Einzelpunkt');
        map.setView(latlngs[0], 16);
        renderDailyStations(normalizedPoints, normalizedStations);
        return;
    }

    for (let index = 0; index < normalizedPoints.length - 1; index += 1) {
        const point1 = normalizedPoints[index];
        const point2 = normalizedPoints[index + 1];
        if (typeof point1?.lat !== 'number' || typeof point1?.lon !== 'number') continue;
        if (typeof point2?.lat !== 'number' || typeof point2?.lon !== 'number') continue;

        L.polyline([[point1.lat, point1.lon], [point2.lat, point2.lon]], {
            color: segmentColor(point1, profile, context),
            weight: 4,
            opacity: 0.9,
            lineCap: 'round',
        }).addTo(layer);
    }

    L.marker(latlngs[0]).addTo(layer).bindPopup('Start');
    L.marker(latlngs[latlngs.length - 1]).addTo(layer).bindPopup('Ende');
    map.fitBounds(L.latLngBounds(latlngs), { padding: [20, 20] });
    renderDailyStations(normalizedPoints, normalizedStations);
}

async function fetchTrack(url, label, sourceLabel) {
    const displayName = sourceLabel || label;
    setInfo(`Extrahiere GPS-Daten aus ${displayName} ...`);

    const response = await fetch(url);
    if (!response.ok) {
        setInfo(`Fehler bei ${displayName}: ${await fetchTextOrDetail(response)}`);
        return;
    }

    const data = await response.json();
    if (Array.isArray(data.stations)) {
        importStationNames(data.stations);
    }
    draw(data.points || [], label, data.stations || []);

    const detailLines = buildStatsLines(data);
    const mainLine = `Fertig: ${displayName} - ${(data.points || []).length} GPS-Punkte`;
    setInfo(detailLines.length ? `${mainLine}\n${detailLines.join('\n')}` : mainLine);
}

async function chooseFolder(endpoint, inputId, statusLabel) {
    setInfo(`${statusLabel} ...`);
    const response = await fetch(endpoint);
    if (!response.ok) {
        setInfo(`Fehler: ${await fetchTextOrDetail(response)}`);
        return;
    }
    const data = await response.json();
    if (data.folder) {
        document.getElementById(inputId).value = data.folder;
        setInfo(`${statusLabel}: ${data.folder}`);
    } else {
        setInfo(`${statusLabel}: kein Ordner ausgewaehlt.`);
    }
}

function chooseSourceFolder() {
    return chooseFolder('/api/choose-source-folder', 'directoryInput', 'MP4-Ordner auswaehlen');
}

function chooseTargetFolder() {
    return chooseFolder('/api/choose-target-folder', 'outdirInput', 'GPX-Ordner auswaehlen');
}

function showSelectedTrack() {
    const selectEl = document.getElementById('fileSelect');
    if (!selectEl?.value) {
        setInfo('Bitte zuerst eine MP4-Datei auswaehlen.');
        return;
    }
    const fileLabel = selectEl.options[selectEl.selectedIndex]?.text || selectEl.value;
    fetchTrack('/api/track?file=' + encodeURIComponent(selectEl.value), fileLabel, fileLabel);
}

function showCombinedTrack() {
    startCombinedTrackJob();
}

function updateProgress(status) {
    const wrap = document.getElementById('progressWrap');
    const bar = document.getElementById('extractProgress');
    const textEl = document.getElementById('progressText');
    if (!wrap || !bar || !textEl) return;

    const total = Number(status.total_files || 0);
    const done = Number(status.processed_files || 0);
    const pct = total > 0 ? Math.round((done / total) * 100) : 0;

    wrap.style.display = 'block';
    bar.value = pct;

    const fileHint = status.current_file ? ` - ${status.current_file}` : '';
    const pointHint = Number(status.points || 0) > 0 ? ` - Punkte: ${status.points}` : '';
    textEl.textContent = `${pct}% (${done}/${total} Dateien)${fileHint}${pointHint}`;

    if (status.state === 'done') {
        bar.value = 100;
        textEl.textContent = status.message || 'Extraktion abgeschlossen.';
        setInfo(status.message || 'Extraktion abgeschlossen.');
    }
    if (status.state === 'error') {
        textEl.textContent = status.message || status.error || 'Fehler bei der Extraktion.';
        setInfo(status.message || status.error || 'Fehler bei der Extraktion.');
    }
}

async function pollExtractionJob(jobId) {
    const button = document.getElementById('extractBtn');
    try {
        while (true) {
            const response = await fetch('/api/write/status?job_id=' + encodeURIComponent(jobId));
            if (!response.ok) {
                setInfo('Fehler beim Statusabruf: ' + await fetchTextOrDetail(response));
                break;
            }
            const status = await response.json();
            updateProgress(status);
            if (status.state === 'done' || status.state === 'error') break;
            await new Promise(resolve => setTimeout(resolve, 500));
        }
    } finally {
        if (button) button.disabled = false;
    }
}

async function startExtractionJob() {
    const directory = document.getElementById('directoryInput')?.value.trim() || '';
    const pattern = document.getElementById('patternInput')?.value.trim() || '*_N_A.MP4';
    const outdir = document.getElementById('outdirInput')?.value.trim() || '';
    const outname = document.getElementById('outnameInput')?.value.trim() || 'track.gpx';
    const button = document.getElementById('extractBtn');

    if (!directory) {
        setInfo('Bitte zuerst ein Quellverzeichnis waehlen.');
        return;
    }

    if (button) button.disabled = true;
    updateProgress({ total_files: 1, processed_files: 0, current_file: '', points: 0, state: 'queued', message: 'Starte Extraktion ...' });
    setInfo('Starte GPX-Extraktion ...');

    const stations = collectStationsForExport(lastDrawnPoints || []);
    const response = await fetch('/api/write/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ directory, pattern, outdir, outname, stations }),
    });
    if (!response.ok) {
        if (button) button.disabled = false;
        setInfo('Fehler beim Start: ' + await fetchTextOrDetail(response));
        return;
    }

    const data = await response.json();
    pollExtractionJob(data.job_id);
}

async function pollCombinedTrackJob(jobId) {
    try {
        while (true) {
            const response = await fetch('/api/combined/status?job_id=' + encodeURIComponent(jobId));
            if (!response.ok) {
                setInfo('Fehler beim Statusabruf: ' + await fetchTextOrDetail(response));
                break;
            }
            const status = await response.json();
            updateProgress(status);
            if (status.state === 'done') {
                draw(status.points_data || [], 'Gesamttrack', status.stations || []);
                const detailLines = buildStatsLines(status);
                const mainLine = `Gesamttrack fertig - ${status.points || 0} GPS-Punkte im kompilierten GPX-Track, ignoriert: ${status.ignored_points || 0}`;
                setInfo(detailLines.length ? `${mainLine}\n${detailLines.join('\n')}` : mainLine);
                break;
            }
            if (status.state === 'error') break;
            await new Promise(resolve => setTimeout(resolve, 500));
        }
    } catch (err) {
        setInfo('Fehler bei der Gesamttrack-Extraktion: ' + String(err));
    }
}

async function startCombinedTrackJob() {
    if (files.length === 0) {
        setInfo('Keine Dateien fuer den Gesamttrack vorhanden.');
        return;
    }

    updateProgress({ total_files: files.length, processed_files: 0, current_file: '', points: 0, ignored_points: 0, state: 'queued', message: 'Starte Gesamttrack-Extraktion ...' });
    setInfo(`Starte Gesamttrack-Extraktion fuer ${files.length} Dateien ...`);

    const query = new URLSearchParams({ files: JSON.stringify(files) }).toString();
    const response = await fetch('/api/combined/start?' + query, { method: 'POST' });
    if (!response.ok) {
        setInfo('Fehler beim Start des Gesamttracks: ' + await fetchTextOrDetail(response));
        return;
    }

    const data = await response.json();
    pollCombinedTrackJob(data.job_id);
}

async function loadSelectedGpxFile() {
    const input = document.getElementById('gpxFileInput');
    if (!input?.files || input.files.length === 0) return;

    const file = input.files[0];
    const form = new FormData();
    form.append('file', file);

    setInfo(`Lade GPX-Datei: ${file.name} ...`);
    try {
        const response = await fetch('/api/load-gpx', { method: 'POST', body: form });
        if (!response.ok) {
            setInfo(`Fehler beim Laden von ${file.name}: ${await fetchTextOrDetail(response)}`);
            return;
        }

        const data = await response.json();
        importStationNames(data.stations || []);
        draw(data.points || [], data.label || file.name, data.stations || []);

        const detailLines = buildStatsLines(data);
        const mainLine = `GPX geladen: ${data.label || file.name} - ${(data.points || []).length} GPS-Punkte`;
        setInfo(detailLines.length ? `${mainLine}\n${detailLines.join('\n')}` : mainLine);
    } catch (err) {
        setInfo('Fehler beim GPX-Upload: ' + String(err));
    } finally {
        input.value = '';
    }
}

function isMapFullscreen() {
    return document.fullscreenElement === document.getElementById('mapWrap');
}

function updateFullscreenButton() {
    const button = document.getElementById('fullscreenBtn');
    if (button) button.textContent = isMapFullscreen() ? 'Vollbild beenden' : 'Vollbild';
}

async function toggleMapFullscreen() {
    const wrap = document.getElementById('mapWrap');
    if (!wrap) return;

    try {
        if (isMapFullscreen()) {
            await document.exitFullscreen();
        } else {
            await wrap.requestFullscreen();
        }
    } catch (err) {
        setInfo('Vollbild nicht verfuegbar: ' + String(err));
    }
}

function fitCurrentTrackToMap() {
    if (!Array.isArray(lastDrawnPoints) || lastDrawnPoints.length === 0) {
        setInfo('Kein Track geladen.');
        return;
    }

    const latlngs = lastDrawnPoints
        .filter(point => typeof point?.lat === 'number' && typeof point?.lon === 'number')
        .map(point => [point.lat, point.lon]);

    if (latlngs.length === 0) {
        setInfo('Keine gueltigen GPS-Punkte vorhanden.');
        return;
    }
    if (latlngs.length === 1) {
        map.setView(latlngs[0], 16);
        return;
    }
    map.fitBounds(L.latLngBounds(latlngs), { padding: [20, 20] });
}

function toggleDailyStations() {
    redrawCurrentTrack();
}

document.addEventListener('fullscreenchange', () => {
    updateFullscreenButton();
    setTimeout(() => map.invalidateSize(), 50);
});

updateFullscreenButton();
clearLegend('Kein Track geladen.');
updateDistanceTable([], 'Track', null, null);
renderStationPanel([], []);

Object.assign(window, {
    chooseSourceFolder,
    chooseTargetFolder,
    showSelectedTrack,
    showCombinedTrack,
    startExtractionJob,
    loadSelectedGpxFile,
    redrawCurrentTrack,
    toggleDailyStations,
    fitCurrentTrackToMap,
    toggleMapFullscreen,
});
