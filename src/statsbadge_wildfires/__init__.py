"""Wildfires near a location, for the badge to draw as outlines on a map.

Where the quakes map crosses an ocean to reach the next event, this one stays put. A page is
set to a location and a radius, then shows what is burning inside it. That bound is what
makes the set worth ranking: ranked globally, the biggest fires on Earth are Sahel grassland
burns month in and month out.

The location is the badge's, out of `Source.location`, so a badge that already has a clock
needs nothing configured here. A page can override it, as a clock page can, so one badge
holds fires near home and fires near somewhere being watched.

Perimeters come from WFIGS for the United States and Copernicus GWIS for Europe, Africa and
western Asia. Australia and East Asia have no perimeter feed here, so a location there draws
the map with nothing in range.

The outlines travel, where the quakes map draws the firmware's coastlines and sends only
coordinates. A fire has a shape, the shape is the subject, and it differs every time.
`feeds.py` keeps that affordable.
"""

import math
import os
import threading
import time

from statsbadge.sources.base import PollingSource

from . import feeds

HERE = os.path.dirname(os.path.abspath(__file__))

# How often the feeds are asked. WFIGS republishes as infrared flights land, a few times a
# day per fire, and GWIS on the satellite pass. Faster asks a public service for an answer
# that has not moved.
INTERVAL = 900.0
# A failure waits this long instead of the whole interval, a tethered laptop dropping its
# connection being over in seconds.
RETRY_AFTER = 120.0

# The last good set per target, kept so a badge switched on before the network is up has
# something to draw.
STORED = "fires"

# How far out the fetch box is drawn, over the radius asked for. A fire whose centre is
# just outside the radius can still be the nearest thing burning.
BOX_MARGIN = 1.15
KM_PER_DEGREE = 111.195
# The furthest a box is ever drawn. Past this the feeds are being asked for a continent,
# and the page is no longer about anywhere.
MAX_RADIUS_KM = 5000.0


class Wildfires(PollingSource):
    name = "wildfires"
    label = "Wildfires"
    provides = ("wildfires",)

    # Scalars only. The fires themselves go in `pages`, which no field picker could offer
    # without putting a row of Python in a text page.
    #
    # Declared slow: the feeds are asked every fifteen minutes where the badge polls
    # every second, and `age_s` is drawn to the minute, so the set travels on a change.
    groups = {"wildfires": {"label": "Wildfires", "slow": True, "fields": {
        "count": {"label": "Fires in range"},
        "biggest": {"label": "Largest fire", "unit": "ha"},
        "nearest": {"label": "Nearest fire", "unit": "km"},
    }}}

    badge_module = os.path.join(HERE, "badge", "firemap.py")

    settings = (
        {"key": "min_area", "label": "Smallest fire", "type": "number", "default": 100,
         "min": 1, "unit": "hectares",
         "hint": "Below about 100 hectares the feeds fill up with burns that were out "
                 "before anyone noticed them"},
        {"key": "count", "label": "How many", "type": "number", "default": 10, "min": 1,
         "max": 20,
         "hint": "How many fires the map cycles through, nearest first"},
        {"key": "merge", "label": "Merge fires within", "type": "number", "default": 25,
         "min": 0, "max": 500, "unit": "km",
         "hint": "One incident is often several perimeters a few kilometres apart, and "
                 "the map cycling through all of them is the same view five times. Zero "
                 "shows every one"},
    )

    badge_page = {
        "kind": "firemap",
        "title": "Wildfires",
        "fields": [],
        "slots": {},
    }

    # A page location overrides the badge's, so one badge can watch two places. Left empty
    # they fall back to the badge-wide location, as a badge with a clock already has.
    page_settings = (
        {"key": "radius", "label": "Within", "type": "number", "default": 500, "min": 10,
         "max": int(MAX_RADIUS_KM), "unit": "km",
         "hint": "How far from the location to look. Wide enough to find something, "
                 "narrow enough that what it finds is nearby"},
        {"key": "hold", "label": "Each fire", "type": "number", "default": 7, "min": 1,
         "unit": "seconds",
         "hint": "How long the map holds on one before travelling to the next"},
        {"key": "place", "label": "Place", "type": "text",
         "hint": "A town, and a country after a comma to settle which one. Empty uses "
                 "the badge's location"},
        {"key": "latitude", "label": "Latitude", "type": "number", "min": -90, "max": 90,
         "hint": "Overrides the place where both are set"},
        {"key": "longitude", "label": "Longitude", "type": "number", "min": -180,
         "max": 180, "hint": "Overrides the place where both are set"},
    )

    @classmethod
    def available(cls, _config=None):
        return True

    def __init__(self, config):
        super().__init__(config)
        # What the fetcher last brought back, keyed by target. Read while sampling and
        # replaced on the fetcher's thread, so both go through the lock.
        self._fires = {}
        # One entry per page: where it is looking and how far. Rebuilt by `pages`, read by
        # the fetcher.
        self._targets = {}
        self._lock = threading.Lock()
        self._next = 0.0
        self._read_settings()

    def start(self):
        """Restore the stored fires, then fetch on a thread."""
        with self._lock:
            self._fires = dict(self.store.get(STORED) or {})
        super().start()

    def pages(self, instances):
        """Take the configured pages, and fetch for anywhere new.

        A page's location is resolved on the fetcher's thread and not here: a place name
        is a geocode, a geocode is a request, and this runs on the collector's thread as
        the config is saved.
        """
        targets = {}
        for page in instances:
            page_id = page.get("id")
            if page_id:
                targets[page_id] = {
                    "place": page.get("place"),
                    "latitude": page.get("latitude"),
                    "longitude": page.get("longitude"),
                    "radius": _radius(page.get("radius")),
                }
        with self._lock:
            changed = targets != self._targets
            self._targets = targets
        if changed:
            self._next = 0.0
            self.wake()

    def configure(self, settings):
        """Take settings while running, and fetch again rather than waiting out the interval.

        A size typed in the browser changes which fires are in the set, so it should show
        up on the badge and not in a quarter of an hour.
        """
        super().configure(settings)
        self._read_settings()
        self._next = 0.0
        self.wake()

    def _read_settings(self):
        self.min_area = float(self.config["min_area"])
        self.count = int(self.config["count"])
        self.merge = float(self.config["merge"])

    def sample(self, frame, dt):
        """What the fetcher brought back, ranked by distance and aged.

        Prompt: the ranking is arithmetic over at most a few dozen fires, and every fetch
        and every geocode happened on the other thread.
        """
        with self._lock:
            found = {key: dict(value) for key, value in self._fires.items()}
            targets = dict(self._targets)
        # Aged against the minute just gone rather than this instant. The badge draws no
        # age finer than a minute, and this group is declared slow, so what matters is how
        # often the set *changes*. Rounding each age to its own minute moves the revision
        # ten times a minute; moving the clock moves all of them together, once.
        now = int(time.time()) // 60 * 60
        pages = {}
        for page_id, target in targets.items():
            answer = found.get(page_id)
            if answer is None:
                continue
            pages[page_id] = _rank(answer, target["radius"], self.count, now, self.merge)
        frame["wildfires"] = {
            "pages": pages,
            "count": max((page["count"] for page in pages.values()), default=0),
            "biggest": max((page["biggest"] for page in pages.values()
                            if page["biggest"] is not None), default=None),
            "nearest": min((page["nearest"] for page in pages.values()
                            if page["nearest"] is not None), default=None),
        }

    def poll(self):
        if time.monotonic() < self._next:
            return
        with self._lock:
            targets = dict(self._targets)
        if not targets:
            self._next = time.monotonic() + INTERVAL
            return
        found = {}
        failed = False
        # One fetch per distinct place and radius, so two pages watching one town cost one
        # request between them.
        boxes = {}
        for page_id, target in targets.items():
            try:
                located = self.location(target)
                if located is None:
                    self.note_ok(page_id)
                    continue
                latitude, longitude, label = located
                key = (round(latitude, 3), round(longitude, 3), target["radius"])
                if key not in boxes:
                    fires = self._box(latitude, longitude, target["radius"])
                    boxes[key] = {"fires": self._name(fires), "lat": latitude,
                                  "lon": longitude, "label": label}
                found[page_id] = boxes[key]
                self.note_ok(page_id)
            except Exception as exc:  # noqa: BLE001
                failed = True
                self.note_fault(exc, key=page_id)
        if found:
            with self._lock:
                self._fires = found
            # Kept for the next launch, not as a cache for this one.
            self.store.set(STORED, found)
        self._next = time.monotonic() + (RETRY_AFTER if failed else INTERVAL)

    def _name(self, fires):
        """Give the burnt areas the feed did not name the nearest town to each.

        Named here on the fetcher's thread rather than in `_rank`, which runs on the
        collector's as every sample is composed: the table is 34,000 settlements and the
        answer only changes when a fetch brings back different fires.

        A table that will not read leaves the names empty, and the badge draws its own
        fallback. Nothing here is worth failing a fetch that worked.
        """
        try:
            for fire in fires:
                if not (fire.get("name") or "").strip():
                    found = self.geocode.nearest(fire["lat"], fire["lon"])
                    if found:
                        fire["name"] = (f"{found['km']} km {found['bearing']} of "
                                        f"{found['name']}" if found["km"] else found["name"])
        except Exception as exc:
            self.note_fault(exc)
        return fires

    def _box(self, latitude, longitude, radius_km):
        """Every perimeter within a box around a location.

        The box is drawn in degrees, which is why it is wider in longitude the further from
        the equator it is: a degree of longitude is a kilometre and a half at Svalbard.
        """
        reach = radius_km * BOX_MARGIN
        span_lat = reach / KM_PER_DEGREE
        # Held off the poles, where a degree of longitude is nothing and this would ask for
        # the whole parallel.
        cosine = max(0.05, math.cos(math.radians(latitude)))
        span_lon = min(180.0, span_lat / cosine)
        return feeds.fetch(south=max(-90.0, latitude - span_lat),
                           west=max(-180.0, longitude - span_lon),
                           north=min(90.0, latitude + span_lat),
                           east=min(180.0, longitude + span_lon),
                           min_ha=self.min_area,
                           home=(latitude, longitude),
                           want=self.count)


def _rank(answer, radius_km, count, now, merge_km=0.0):
    """The fires a page shows: inside the radius, nearest first, one per cluster.

    Where nothing is in range the nearest few are shown anyway, marked as beyond it. An
    empty map looks like a broken feed, where "nearest fire, 900km away" carries the far
    more likely reading: nothing near you is on fire.
    """
    home_lat, home_lon = answer["lat"], answer["lon"]
    measured = []
    for fire in answer.get("fires") or ():
        fire = dict(fire)
        fire["km"] = round(_haversine(home_lat, home_lon, fire["lat"], fire["lon"]))
        if fire.get("at") is not None:
            fire["age_s"] = max(0, now - fire.pop("at"))
        else:
            fire.pop("at", None)
        measured.append(fire)
    measured.sort(key=lambda fire: fire["km"])
    inside = [fire for fire in measured if fire["km"] <= radius_km]
    beyond = not inside
    shown = _cluster(inside or measured, merge_km)[:count]
    return {
        "fires": shown,
        "home": [round(home_lat, 4), round(home_lon, 4)],
        "label": answer.get("label"),
        "radius": radius_km,
        "beyond": beyond,
        "count": len(inside),
        "biggest": max((fire["area"] for fire in shown), default=None),
        "nearest": shown[0]["km"] if shown else None,
    }


def _cluster(fires, merge_km):
    """One fire per cluster, keeping the nearest of each.

    A large incident is filed as several perimeters a few kilometres apart, so a page
    cycling through all of them holds the same view five times and calls it five fires. The
    nearest of a cluster stands for it and carries the count it stands for.

    `fires` arrives sorted by distance, so the first of each cluster is the nearest.
    """
    if merge_km <= 0.0:
        return list(fires)
    kept = []
    for fire in fires:
        for already in kept:
            # Against the larger fire's own extent as well as the setting: two perimeters
            # 20km apart are one incident when the incident is 40km across.
            reach = max(merge_km, _span_km(already) * 0.5, _span_km(fire) * 0.5)
            if _haversine(already["lat"], already["lon"],
                          fire["lat"], fire["lon"]) <= reach:
                already["nearby"] = already.get("nearby", 0) + 1
                # The cluster is worth what is burning in it, not what its nearest is.
                already["cluster_area"] = (already.get("cluster_area", already["area"])
                                           + fire["area"])
                break
        else:
            kept.append(fire)
    return kept


def _span_km(fire):
    """How far across a fire is, from the degrees the badge frames it by."""
    span = fire.get("span") or (0.0, 0.0)
    return max(span[0] * math.cos(math.radians(fire["lat"])), span[1]) * KM_PER_DEGREE


def _haversine(from_lat, from_lon, to_lat, to_lon):
    """Kilometres between two points on the great circle.

    Not the flat approximation: a 2000km radius at fifty degrees north is out by enough to
    put a fire on the wrong side of the edge.
    """
    radius = 6371.0
    phi_from, phi_to = math.radians(from_lat), math.radians(to_lat)
    delta_phi = math.radians(to_lat - from_lat)
    delta_lambda = math.radians(to_lon - from_lon)
    inner = (math.sin(delta_phi * 0.5) ** 2
             + math.cos(phi_from) * math.cos(phi_to) * math.sin(delta_lambda * 0.5) ** 2)
    return 2.0 * radius * math.asin(min(1.0, math.sqrt(inner)))


def _radius(value):
    try:
        return max(10.0, min(MAX_RADIUS_KM, float(value or 500)))
    except (TypeError, ValueError):
        return 500.0
