"""The two perimeter feeds, and getting a fire down to what a badge can draw.

WFIGS covers the United States and GWIS covers Europe, Africa and western Asia. Both are
asked, and whichever answers for a location wins. Australia and East Asia have no perimeter
feed here yet, so `fetch` returns whatever it got and an empty answer passes.

Both are asked for a box around the location, so a 500km radius downloads a box.

Raw perimeters are too detailed to send: the twenty largest WFIGS fires are 15MB of GeoJSON
across 426,000 points, flown by infrared with every ember island a ring. The feeds
generalise server-side, and what arrives is culled and simplified again against the pixels
it will be drawn at.
"""

import json
import math
import urllib.parse
import urllib.request

# One acre in hectares. WFIGS reports acres, GWIS hectares, and the badge is given
# hectares.
ACRE_HA = 0.404686

TIMEOUT = 20.0

WFIGS = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services"
         "/WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query")
GWIS = "https://maps.effis.emergency.copernicus.eu/gwis"

# Degrees WFIGS generalises to, about 44m. At 0.0015 a 2700 hectare fire ran out of detail
# at a seventh of the screen. Only the fires that will be drawn are asked for this fine.
OFFSET = 0.0004
# Fields for the coarse pass. No geometry, so a few KB however many are burning.
SCOUT_FIELDS = ("OBJECTID", "poly_GISAcres", "attr_InitialLatitude",
                "attr_InitialLongitude")

# How many rings of one fire are kept, largest first. A fire is drawn 200px across at most,
# where its fortieth island is a speck on a speck.
MAX_RINGS = 6
# Rings smaller than this across, in pixels at the framing the fire is drawn at, are
# dropped. Of 935 rings across twenty fires, 836 were five points or fewer.
SPECK_PX = 3.0
# How far a point may move, in pixels at that same framing, when the outline is simplified.
TOLERANCE_PX = 1.0

# The badge's map band, measuring the pixels above. Held here: the host has no `look`.
MAP_W = 320
MAP_H = 156
# Latitude is drawn taller than longitude, matching the badge's worldmap.
ASPECT = 1.3
# How much of the band a fire fills, leaving the outline off the edges.
FRAME = 0.85


def fetch(south, west, north, east, min_ha, home=None, want=10):
    """Every perimeter either feed has in a box, as fire records.

    `home` is (latitude, longitude) and `want` how many the page will draw. WFIGS is asked
    in two passes, so only those fires cost geometry: the box can hold two hundred, and the
    nearest is often not among the biggest.

    A feed that fails raises. An empty answer passes: most boxes are not on fire.
    """
    fires = _wfigs(south, west, north, east, min_ha, home, want)
    fires += _gwis(south, west, north, east, min_ha)
    return fires


def _envelope(south, west, north, east):
    return {"xmin": west, "ymin": south, "xmax": east, "ymax": north,
            "spatialReference": {"wkid": 4326}}


def _wfigs(south, west, north, east, min_ha, home, want):
    """Current US interagency perimeters in a box, nearest first.

    Everything the box holds comes back. The nearest few carry an outline; the rest carry
    only where they are and how big, counted by "fires in range" at a line of JSON each.
    Fetching geometry for two hundred fires to draw ten costs minutes, and counting ten
    because ten were fetched reports the wrong number.
    """
    envelope = _envelope(south, west, north, east)
    scouted = _wfigs_scout(envelope, min_ha, home)
    if not scouted:
        return []
    detailed = _wfigs_shapes(envelope, [row[0] for row in scouted[:_headroom(want)]])
    return detailed + [_stub(row) for row in scouted[_headroom(want):]]


def _headroom(want):
    """How many are worth an outline, over what the page draws.

    A fire whose geometry has been withdrawn comes back with none, and would otherwise
    shorten the set the page cycles through.
    """
    return max(want * 2, want + 5)


def _stub(row):
    """A scouted fire with no outline: enough to count, rank and point at."""
    _oid, latitude, longitude, acres = row
    return {
        "name": "",
        "area": round((acres or 0.0) * ACRE_HA),
        "lon": round(longitude, 4),
        "lat": round(latitude, 4),
        "span": [0.0, 0.0],
        "at": None,
    }


def _wfigs_scout(envelope, min_ha, home):
    """Which fires are worth the geometry: attributes only, so a few KB whatever is burning.

    Ordered by distance from home where there is one. Ordering by size instead is what
    drops the fire at the end of the road in favour of one four states away.
    """
    query = urllib.parse.urlencode({
        "where": f"poly_GISAcres > {min_ha / ACRE_HA:.3f}",
        "geometry": json.dumps(envelope),
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": ",".join(SCOUT_FIELDS),
        "returnGeometry": "false",
        "outSR": 4326,
        "f": "json",
    })
    payload = _json(f"{WFIGS}?{query}")
    if payload.get("error"):
        raise OSError(f"WFIGS: {payload['error'].get('message', 'refused')}")
    found = []
    for feature in payload.get("features") or ():
        attributes = feature.get("attributes") or {}
        oid = attributes.get("OBJECTID")
        latitude = attributes.get("attr_InitialLatitude")
        longitude = attributes.get("attr_InitialLongitude")
        # A fire with no reported origin cannot be placed until the second pass.
        if oid is None or latitude is None or longitude is None:
            continue
        found.append((oid, latitude, longitude, attributes.get("poly_GISAcres") or 0.0))
    if home is not None:
        # The reported point of origin, which is enough to sort by and is not what the
        # badge is given: the real centre comes from the geometry in the second pass.
        found.sort(key=lambda row: _near(home, row))
    else:
        found.sort(key=lambda row: -row[3])
    return found


def _near(home, row):
    """How far a scouted fire is, roughly. Only its order matters here.

    Flat and squared: wrong over a continent, right over the few degrees separating the
    nearest fire from the next. The badge is given great-circle distances, measured against
    the real centre once the geometry arrives.
    """
    latitude, longitude = row[1], row[2]
    return ((latitude - home[0]) ** 2
            + ((longitude - home[1]) * math.cos(math.radians(home[0]))) ** 2)


def _wfigs_shapes(envelope, oids):
    """The geometry of named fires, generalised and quantised.

    Quantisation returns integer grid steps, already deltas after the first point. That is
    both the smaller answer and the encoding the badge draws from, so nothing re-encodes.
    """
    quantization = {"mode": "view", "originPosition": "upperLeft", "tolerance": OFFSET,
                    "extent": envelope}
    query = urllib.parse.urlencode({
        "objectIds": ",".join(str(oid) for oid in oids),
        "outFields": ",".join((
            "poly_IncidentName", "poly_GISAcres", "attr_PercentContained",
            "attr_FireDiscoveryDateTime", "attr_IncidentName")),
        "returnGeometry": "true",
        "outSR": 4326,
        "maxAllowableOffset": OFFSET,
        "quantizationParameters": json.dumps(quantization),
        "f": "json",
    })
    payload = _json(f"{WFIGS}?{query}")
    if payload.get("error"):
        raise OSError(f"WFIGS: {payload['error'].get('message', 'refused')}")
    transform = payload.get("transform") or {}
    fires = []
    for feature in payload.get("features") or ():
        attributes = feature.get("attributes") or {}
        rings = _unquantize((feature.get("geometry") or {}).get("rings") or (), transform)
        acres = attributes.get("poly_GISAcres")
        if not rings or acres is None:
            continue
        name = (attributes.get("poly_IncidentName")
                or attributes.get("attr_IncidentName") or "").strip()
        fires.append(_record(
            name=name, hectares=float(acres) * ACRE_HA, rings=rings,
            contained=attributes.get("attr_PercentContained"),
            # Milliseconds, and null on a fire nobody has filed a discovery time for.
            at=_seconds(attributes.get("attr_FireDiscoveryDateTime")),
            floor=OFFSET))
    return fires


def _gwis(south, west, north, east, min_ha):
    """Copernicus near-real-time burnt area in a box.

    MapServer appends a service exception *after* a partial body when a feature trips it,
    so the JSON is cut at the header that starts it. Left alone that reads as a parse
    error on a body that was fine up to there.
    """
    query = urllib.parse.urlencode({
        "SERVICE": "WFS", "VERSION": "1.0.0", "REQUEST": "GetFeature",
        "TYPENAME": "nrt.ba.poly.month", "OUTPUTFORMAT": "geojson",
        "BBOX": f"{west},{south},{east},{north}",
    })
    body = _text(f"{GWIS}?{query}")
    cut = body.find("Content-Type:")
    if cut > 0:
        body = body[:cut]
    payload = json.loads(body)
    fires = []
    for feature in payload.get("features") or ():
        properties = feature.get("properties") or {}
        rings = _rings_of(feature.get("geometry") or {})
        try:
            hectares = float(properties.get("area") or 0)
        except (TypeError, ValueError):
            continue
        if not rings or hectares < min_ha:
            continue
        # GWIS files no incident name. `Wildfires._name` fills one in from the nearest town.
        fires.append(_record(name="", hectares=hectares, rings=rings, contained=None,
                             at=_stamp(properties.get("initialdate"))))
    return fires


def _record(name, hectares, rings, contained, at, floor=None):
    """One fire, framed and decimated to what it will be drawn at.

    `floor` is the resolution the feed generalised to, where that is known. `detail` is how
    fine this outline really is, and the badge caps its zoom on it. Framed to fill the band,
    44m detail is worth 6000 pixels per degree against 600 for 400m. Past either, the badge
    draws the feed's grid.
    """
    lons = [lon for ring in rings for lon, _ in ring]
    lats = [lat for ring in rings for _, lat in ring]
    west, east = min(lons), max(lons)
    south, north = min(lats), max(lats)
    scale = frame_scale(east - west, north - south)
    kept = decimate(rings, scale)
    record = {
        "name": name,
        "area": round(hectares),
        "lon": round((west + east) * 0.5, 4),
        "lat": round((south + north) * 0.5, 4),
        # Degrees across and tall, the two the badge frames against. Both, since a
        # fire is rarely square: framing an east-west one against its longer side leaves it
        # at half the size it could be drawn.
        "span": [round(east - west, 4), round(north - south, 4)],
        "at": at,
    }
    if contained is not None:
        record["contained"] = round(float(contained))
    if kept:
        # A pixel at the framing this fire is drawn at, so the encoding costs no detail.
        step = 1.0 / scale
        record["outline"] = _encode(kept, west, south, step)
        record["origin"] = [round(west, 4), round(south, 4)]
        record["step"] = round(step, 7)
        record["detail"] = round(max(floor or 0.0, _resolution(rings), step), 6)
    return record


def _resolution(rings):
    """How fine the geometry really is, as the median gap between its points.

    WFIGS reports what it generalised to. GWIS polygons arrive already reduced at source
    with no such figure, so this measures one. A fire drawn past the detail it has is the
    feed's grid blown up.
    """
    gaps = []
    for ring in rings:
        for (from_lon, from_lat), (to_lon, to_lat) in zip(ring, ring[1:], strict=False):
            gap = math.hypot(to_lon - from_lon, to_lat - from_lat)
            if gap > 0.0:
                gaps.append(gap)
    if not gaps:
        return 0.0
    gaps.sort()
    return gaps[len(gaps) // 2]


def frame_scale(span_lon, span_lat):
    """Pixels per degree that fits a fire in the map band."""
    return min(MAP_W * FRAME / max(span_lon, 1e-4),
               MAP_H * FRAME / max(span_lat * ASPECT, 1e-4))


def decimate(rings, scale):
    """Drop the specks, keep the largest few, simplify what is left.

    Against `scale`, so the tolerance is a pixel at the zoom the fire is drawn at. A 90km
    fire and a 3km one are both drawn 200px across. A fixed tolerance in degrees is
    invisible on one, ruinous on the other.
    """
    speck = SPECK_PX / scale
    tolerance = TOLERANCE_PX / scale
    big = [ring for ring in rings if _span(ring) >= speck]
    big.sort(key=_span, reverse=True)
    kept = []
    for ring in big[:MAX_RINGS]:
        simplified = simplify(ring, tolerance)
        if len(simplified) >= 4:
            kept.append(simplified)
    return kept


def simplify(points, tolerance):
    """Ramer-Douglas-Peucker, iterative so a 60,000 point ring cannot blow the stack."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        worst = 0.0
        worst_at = None
        for index in range(first + 1, last):
            away = _perpendicular(points[index], points[first], points[last])
            if away > worst:
                worst, worst_at = away, index
        if worst_at is not None and worst > tolerance:
            keep[worst_at] = True
            stack.append((first, worst_at))
            stack.append((worst_at, last))
    return [point for point, kept in zip(points, keep, strict=True) if kept]


def _perpendicular(point, start, end):
    dx, dy = end[0] - start[0], end[1] - start[1]
    if dx == 0.0 and dy == 0.0:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    along = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / (dx * dx + dy * dy)
    along = max(0.0, min(1.0, along))
    return math.hypot(point[0] - (start[0] + along * dx),
                      point[1] - (start[1] + along * dy))


def _encode(rings, origin_lon, origin_lat, step):
    """Rings as integer grid steps from a corner, delta encoded.

    Deltas because they are small: a point next to the last one is a byte or two of JSON
    where an absolute coordinate is six. The badge walks them back up as it builds the
    shape, which it has to do anyway.
    """
    encoded = []
    for ring in rings:
        grid = [(round((lon - origin_lon) / step), round((lat - origin_lat) / step))
                for lon, lat in ring]
        # The closing point repeats the first and the badge closes the shape itself.
        if len(grid) > 1 and grid[0] == grid[-1]:
            grid.pop()
        thinned = [grid[0]]
        for point in grid[1:]:
            if point != thinned[-1]:
                thinned.append(point)
        if len(thinned) < 3:
            continue
        deltas = [thinned[0][0], thinned[0][1]]
        # Ragged: each point against the one before it, so the last has no pair.
        for (was_x, was_y), (x, y) in zip(thinned, thinned[1:], strict=False):
            deltas.append(x - was_x)
            deltas.append(y - was_y)
        encoded.append(deltas)
    return encoded


def _unquantize(rings, transform):
    """Esri quantised rings to lon/lat, walking the deltas back up.

    Every point after the first is a step from the one before it, in whole grid units.
    """
    scale = transform.get("scale") or (1.0, 1.0)
    translate = transform.get("translate") or (0.0, 0.0)
    up = transform.get("originPosition") != "lowerLeft"
    out = []
    for ring in rings:
        x = y = 0
        points = []
        for index, step in enumerate(ring):
            if len(step) < 2:
                continue
            if index == 0:
                x, y = step[0], step[1]
            else:
                x, y = x + step[0], y + step[1]
            lon = translate[0] + x * scale[0]
            lat = translate[1] - y * scale[1] if up else translate[1] + y * scale[1]
            points.append((lon, lat))
        if len(points) >= 3:
            out.append(points)
    return out


def _rings_of(geometry):
    """Every ring of a GeoJSON polygon or multipolygon, holes included.

    A hole is drawn like any other ring. At this size the shape is the subject.
    """
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates") or ()
    if kind == "Polygon":
        rings = list(coordinates)
    elif kind == "MultiPolygon":
        rings = [ring for polygon in coordinates for ring in polygon]
    else:
        return []
    return [[(point[0], point[1]) for point in ring if len(point) >= 2]
            for ring in rings if len(ring) >= 3]


def _span(ring):
    """How far across a ring is, in degrees, on its longer side."""
    lons = [lon for lon, _ in ring]
    lats = [lat for _, lat in ring]
    return max(max(lons) - min(lons), max(lats) - min(lats))


def _seconds(milliseconds):
    """An Esri timestamp in seconds, or None where the field is empty."""
    if milliseconds is None:
        return None
    try:
        return int(milliseconds) // 1000
    except (TypeError, ValueError):
        return None


def _stamp(text):
    """A GWIS `YYYY-MM-DD HH:MM:SS` timestamp in seconds, or None.

    Read as UTC: the feed publishes UTC and the badge ages against it.
    """
    if not text:
        return None
    try:
        parts = str(text).replace("T", " ").split(" ")
        year, month, day = (int(value) for value in parts[0].split("-"))
        hour, minute, second = 0, 0, 0
        if len(parts) > 1:
            clock = parts[1].split(":")
            hour = int(clock[0])
            minute = int(clock[1]) if len(clock) > 1 else 0
            second = int(float(clock[2])) if len(clock) > 2 else 0
    except (TypeError, ValueError, IndexError):
        return None
    # calendar.timegm without the import: days since the epoch, by civil date.
    era_year = year - (month <= 2)
    era = (era_year if era_year >= 0 else era_year - 399) // 400
    year_of_era = era_year - era * 400
    day_of_year = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    day_of_era = year_of_era * 365 + year_of_era // 4 - year_of_era // 100 + day_of_year
    days = era * 146097 + day_of_era - 719468
    return days * 86400 + hour * 3600 + minute * 60 + second


def _json(url):
    return json.loads(_text(url))


def _text(url):
    with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
        return response.read().decode("utf-8", "replace")
