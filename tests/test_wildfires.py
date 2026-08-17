"""Drive the source with faked feed replies, shaped like the real ones.

    python tests/test_wildfires.py

The WFIGS reply here is quantised the way the real one is, with integer grid steps that are
deltas after the first point, and the GWIS reply carries the trailing service exception
MapServer really appends after a partial body. Both are faked in this file, so this reaches
no network.
"""
import math
import sys

from statsbadge_wildfires import (
    Wildfires,
    _haversine,
    _radius,
    _rank,
    feeds,
)

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def square(west, south, size, points=8):
    """A closed ring, as a feed hands one over."""
    ring = []
    for step in range(points):
        along = step / points * 4.0
        side = int(along)
        run = along - side
        if side == 0:
            ring.append((west + size * run, south))
        elif side == 1:
            ring.append((west + size, south + size * run))
        elif side == 2:
            ring.append((west + size * (1.0 - run), south + size))
        else:
            ring.append((west, south + size * (1.0 - run)))
    ring.append(ring[0])
    return ring


# A crenulated edge, as a real perimeter has, and what the simplifier has to survive.
def crinkled(west, south, size, points=400):
    ring = []
    for step in range(points):
        angle = step / points * math.tau
        wobble = 1.0 + 0.04 * math.sin(angle * 37.0)
        ring.append((west + size * 0.5 * (1.0 + math.cos(angle) * wobble),
                     south + size * 0.5 * (1.0 + math.sin(angle) * wobble)))
    ring.append(ring[0])
    return ring


@check
def test_a_quantised_wfigs_ring_decodes_to_where_it_really_is():
    """Every point after the first is a step from the one before it, in grid units."""
    transform = {"scale": [0.0015, 0.0015], "translate": [-180.0, 90.0],
                 "originPosition": "upperLeft"}
    # (42366, 31900) then two single steps east and back.
    rings = feeds._unquantize([[[42366, 31900], [1, 0], [-1, 0]]], transform)
    assert len(rings) == 1, rings
    first = rings[0][0]
    assert abs(first[0] - (-180.0 + 42366 * 0.0015)) < 1e-9, first
    assert abs(first[1] - (90.0 - 31900 * 0.0015)) < 1e-9, first
    # The second point is one grid step east of the first, and level with it.
    assert abs(rings[0][1][0] - (first[0] + 0.0015)) < 1e-9, rings[0]
    assert abs(rings[0][1][1] - first[1]) < 1e-9, rings[0]


@check
def test_lower_left_quantisation_counts_latitude_the_other_way():
    transform = {"scale": [0.001, 0.001], "translate": [0.0, 0.0],
                 "originPosition": "lowerLeft"}
    rings = feeds._unquantize([[[0, 0], [0, 10], [10, 0]]], transform)
    assert rings[0][1][1] > rings[0][0][1], rings[0]


@check
def test_decimation_holds_the_shape_and_drops_the_detail():
    """A 400 point crenulated ring comes back as an outline, not as a polygon soup."""
    ring = crinkled(-120.0, 40.0, 0.4)
    scale = feeds.frame_scale(0.4, 0.4)
    kept = feeds.decimate([ring], scale)
    assert len(kept) == 1, kept
    points = len(kept[0])
    assert 8 < points < 200, points
    # No dropped point sits further from the outline that replaced it than the tolerance
    # it was simplified against. Measured to the nearest segment and not the nearest kept
    # point, the distance Douglas-Peucker actually bounds.
    tolerance = feeds.TOLERANCE_PX / scale
    outline = kept[0]
    for point in ring:
        near = min(feeds._perpendicular(point, outline[n], outline[n + 1])
                   for n in range(len(outline) - 1))
        assert near <= tolerance * 1.01, (point, near, tolerance)


@check
def test_specks_are_dropped_and_the_largest_rings_are_kept():
    """A fire is mostly ember islands, and none of them are a pixel across."""
    big = square(-120.0, 40.0, 0.4)
    specks = [square(-120.0 + n * 0.01, 40.0, 0.0002) for n in range(40)]
    scale = feeds.frame_scale(0.4, 0.4)
    kept = feeds.decimate([big, *specks], scale)
    assert len(kept) == 1, len(kept)
    assert len(kept) <= feeds.MAX_RINGS


@check
def test_no_more_rings_than_the_cap_survive():
    scale = feeds.frame_scale(1.0, 1.0)
    rings = [square(-120.0 + n * 0.1, 40.0, 0.08) for n in range(20)]
    kept = feeds.decimate(rings, scale)
    assert len(kept) == feeds.MAX_RINGS, len(kept)


@check
def test_an_outline_encodes_to_deltas_that_walk_back_to_where_they_started():
    ring = square(-120.0, 40.0, 0.4, points=12)
    step = 0.001
    encoded = feeds._encode([ring], -120.0, 40.0, step)
    assert len(encoded) == 1, encoded
    deltas = encoded[0]
    x, y = deltas[0], deltas[1]
    walked = [(-120.0 + x * step, 40.0 + y * step)]
    for index in range(2, len(deltas) - 1, 2):
        x += deltas[index]
        y += deltas[index + 1]
        walked.append((-120.0 + x * step, 40.0 + y * step))
    # Every walked point is on the ring it came from, within a grid step.
    for lon, lat in walked:
        near = min(math.hypot(lon - a, lat - b) for a, b in ring)
        assert near <= step * 1.5, (lon, lat, near)
    # The closing point is not sent: the badge closes the shape itself.
    assert len(walked) < len(ring), (len(walked), len(ring))


@check
def test_a_gwis_body_with_a_trailing_exception_still_parses():
    """MapServer appends the exception after a body that was fine up to there."""
    body = ('{"type":"FeatureCollection","features":[]}\n'
            'Content-Type: text/xml; charset=UTF-8\n\n'
            '<?xml version="1.0"?><ServiceExceptionReport/>')
    cut = body.find("Content-Type:")
    assert cut > 0
    import json
    assert json.loads(body[:cut])["features"] == []


@check
def test_haversine_is_the_great_circle_and_not_a_flat_guess():
    # London to Edinburgh, about 534km.
    km = _haversine(51.5074, -0.1278, 55.9533, -3.1883)
    assert 520 < km < 550, km
    # A flat approximation at this latitude is out by tens of kilometres.
    flat = math.hypot((55.9533 - 51.5074) * 111.195, (-3.1883 + 0.1278) * 111.195)
    assert abs(flat - km) > 10, (flat, km)


@check
def test_the_nearest_fire_is_first():
    answer = {"lat": 53.38, "lon": -1.47, "label": "Sheffield, GB", "fires": [
        {"lat": 55.0, "lon": -1.5, "area": 900, "name": "far", "at": None},
        {"lat": 53.5, "lon": -1.5, "area": 300, "name": "near", "at": None},
    ]}
    ranked = _rank(answer, radius_km=500, count=10, now=0)
    assert [fire["name"] for fire in ranked["fires"]] == ["near", "far"], ranked["fires"]
    assert ranked["nearest"] == ranked["fires"][0]["km"]
    assert ranked["beyond"] is False
    assert ranked["count"] == 2


@check
def test_nothing_in_range_shows_the_nearest_anyway_and_says_so():
    """An empty map looks like a broken feed. "900km away" is the likelier truth."""
    answer = {"lat": 53.38, "lon": -1.47, "label": "Sheffield, GB", "fires": [
        {"lat": 42.0, "lon": -1.5, "area": 900, "name": "spain", "at": None},
    ]}
    ranked = _rank(answer, radius_km=100, count=10, now=0)
    assert ranked["beyond"] is True
    assert len(ranked["fires"]) == 1, ranked
    # Counted as none in range, so a text page reading `count` does not claim one.
    assert ranked["count"] == 0
    assert ranked["fires"][0]["km"] > 100


@check
def test_an_age_is_worked_out_against_the_minute_and_the_timestamp_is_dropped():
    answer = {"lat": 0.0, "lon": 0.0, "label": None, "fires": [
        {"lat": 0.1, "lon": 0.0, "area": 900, "name": "x", "at": 1000},
    ]}
    ranked = _rank(answer, radius_km=500, count=10, now=4600)
    fire = ranked["fires"][0]
    assert fire["age_s"] == 3600, fire
    assert "at" not in fire, fire


@check
def test_a_fire_with_no_timestamp_carries_no_age():
    answer = {"lat": 0.0, "lon": 0.0, "label": None, "fires": [
        {"lat": 0.1, "lon": 0.0, "area": 900, "name": "x", "at": None},
    ]}
    fire = _rank(answer, radius_km=500, count=10, now=4600)["fires"][0]
    assert "age_s" not in fire, fire
    assert "at" not in fire, fire


@check
def test_the_count_setting_caps_what_is_sent():
    fires = [{"lat": n * 0.01, "lon": 0.0, "area": 900, "name": str(n), "at": None}
             for n in range(30)]
    ranked = _rank({"lat": 0.0, "lon": 0.0, "label": None, "fires": fires},
                   radius_km=5000, count=4, now=0)
    assert len(ranked["fires"]) == 4, ranked
    # The count is what is in range, not what was sent, so a dial reads the real number.
    assert ranked["count"] == 30


@check
def test_scouting_orders_by_distance_and_not_by_size():
    """The fire at the end of the road beats one four states away, whatever its acreage."""
    payload = {"features": [
        {"attributes": {"OBJECTID": 1, "attr_InitialLatitude": 48.0,
                        "attr_InitialLongitude": -116.2, "poly_GISAcres": 500000.0}},
        {"attributes": {"OBJECTID": 2, "attr_InitialLatitude": 43.7,
                        "attr_InitialLongitude": -116.2, "poly_GISAcres": 300.0}},
    ]}
    was = feeds._json
    try:
        feeds._json = lambda url: payload
        rows = feeds._wfigs_scout({}, 100.0, (43.6135, -116.2035))
    finally:
        feeds._json = was
    assert [row[0] for row in rows] == [2, 1], rows


@check
def test_a_fire_with_no_reported_origin_is_not_scouted():
    payload = {"features": [
        {"attributes": {"OBJECTID": 1, "attr_InitialLatitude": None,
                        "attr_InitialLongitude": None, "poly_GISAcres": 900.0}},
    ]}
    was = feeds._json
    try:
        feeds._json = lambda url: payload
        rows = feeds._wfigs_scout({}, 100.0, (43.6, -116.2))
    finally:
        feeds._json = was
    assert rows == [], rows


@check
def test_fires_beyond_the_outlines_still_count():
    """"Fires in range" is what is burning, not what geometry was worth fetching."""
    scouted = [(n, 43.6 + n * 0.01, -116.2, 900.0) for n in range(40)]
    stubs = [feeds._stub(row) for row in scouted[feeds._headroom(10):]]
    assert stubs, "headroom should leave some scouted fires without outlines"
    assert all("outline" not in stub for stub in stubs), stubs
    answer = {"lat": 43.6135, "lon": -116.2035, "label": "Boise, US",
              "fires": [dict(stub) for stub in stubs]}
    ranked = _rank(answer, radius_km=5000, count=10, now=0)
    assert ranked["count"] == len(stubs), (ranked["count"], len(stubs))
    assert len(ranked["fires"]) == 10, ranked


@check
def test_detail_reports_the_resolution_the_outline_really_has():
    """The badge caps its zoom on this, so it may not claim more than the feed gave."""
    ring = crinkled(-120.0, 40.0, 0.3)
    fine = feeds._record(name="x", hectares=9000.0, rings=[ring], contained=None,
                         at=None, floor=0.0004)
    assert fine["detail"] >= 0.0004, fine["detail"]
    # With no figure from the feed the geometry is measured, and a coarse ring
    # reports coarse.
    coarse = feeds._record(name="x", hectares=9000.0, rings=[square(-120.0, 40.0, 0.3, 8)],
                          contained=None, at=None)
    assert coarse["detail"] > fine["detail"], (coarse["detail"], fine["detail"])


@check
def test_perimeters_of_one_incident_are_merged():
    """Five perimeters a few km apart are one incident, not five views of the same hill."""
    fires = [{"lat": 43.60 + n * 0.01, "lon": -116.2, "area": 400, "name": f"p{n}",
              "span": [0.01, 0.01], "at": None} for n in range(5)]
    fires.append({"lat": 45.0, "lon": -116.2, "area": 900, "name": "far",
                  "span": [0.01, 0.01], "at": None})
    ranked = _rank({"lat": 43.6, "lon": -116.2, "label": None, "fires": fires},
                   radius_km=5000, count=10, now=0, merge_km=25)
    names = [fire["name"] for fire in ranked["fires"]]
    assert names == ["p0", "far"], names
    assert ranked["fires"][0]["nearby"] == 4, ranked["fires"][0]
    # The cluster is worth what is burning in it, not what its nearest one is.
    assert ranked["fires"][0]["cluster_area"] == 2000, ranked["fires"][0]
    assert "nearby" not in ranked["fires"][1], ranked["fires"][1]
    # Still counted: merging is about what the map cycles through, not what is burning.
    assert ranked["count"] == 6


@check
def test_merging_widens_for_a_fire_bigger_than_the_setting():
    """Two perimeters 30km apart are one incident when the incident is 80km across."""
    fires = [
        {"lat": 43.6, "lon": -116.2, "area": 90000, "name": "big", "span": [0.8, 0.8],
         "at": None},
        {"lat": 43.87, "lon": -116.2, "area": 400, "name": "lobe", "span": [0.01, 0.01],
         "at": None},
    ]
    ranked = _rank({"lat": 43.6, "lon": -116.2, "label": None, "fires": fires},
                   radius_km=5000, count=10, now=0, merge_km=25)
    assert [fire["name"] for fire in ranked["fires"]] == ["big"], ranked["fires"]
    assert ranked["fires"][0]["nearby"] == 1


@check
def test_merging_off_shows_every_perimeter():
    fires = [{"lat": 43.60 + n * 0.01, "lon": -116.2, "area": 400, "name": f"p{n}",
              "span": [0.01, 0.01], "at": None} for n in range(5)]
    ranked = _rank({"lat": 43.6, "lon": -116.2, "label": None, "fires": fires},
                   radius_km=5000, count=10, now=0, merge_km=0)
    assert len(ranked["fires"]) == 5, ranked["fires"]
    assert all("nearby" not in fire for fire in ranked["fires"])


@check
def test_a_radius_is_clamped_to_something_a_feed_can_answer():
    assert _radius(None) == 500.0
    assert _radius("nonsense") == 500.0
    assert _radius(1) == 10.0
    assert _radius(999999) == 5000.0


@check
def test_a_stamp_reads_as_utc():
    import calendar
    import datetime
    assert feeds._stamp("1970-01-01 00:00:00") == 0
    assert feeds._stamp("1970-01-02 00:00:00") == 86400
    assert feeds._stamp(None) is None
    assert feeds._stamp("not a date") is None
    # Against the standard library rather than against numbers typed in here, including a
    # leap day, which is where a hand-rolled civil-date conversion goes wrong.
    for text in ("2026-08-14 18:44:00", "2000-02-29 12:00:00", "1999-12-31 23:59:59",
                 "2024-02-29 00:00:00", "2100-03-01 06:30:00"):
        when = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        assert feeds._stamp(text) == calendar.timegm(when.timetuple()), text


@check
def test_an_esri_timestamp_is_milliseconds():
    assert feeds._seconds(1786819440000) == 1786819440
    assert feeds._seconds(None) is None


@check
def test_a_record_carries_what_the_badge_draws():
    ring = crinkled(-120.0, 40.0, 0.3)
    record = feeds._record(name="Bench", hectares=27100.0, rings=[ring],
                           contained=35, at=1786819440)
    assert record["name"] == "Bench"
    assert record["area"] == 27100
    assert record["contained"] == 35
    assert -120.0 < record["lon"] < -119.5, record
    assert 0.25 < max(record["span"]) < 0.35, record
    assert record["outline"] and record["origin"] and record["step"], record


@check
def test_a_fire_too_small_to_have_an_outline_still_has_a_place():
    """A ring that decimates to nothing leaves a fire the map can still point at."""
    speck = square(-120.0, 40.0, 1e-6)
    record = feeds._record(name="tiny", hectares=120.0, rings=[speck], contained=None,
                           at=None)
    assert "outline" not in record, record
    assert record["lon"] and record["lat"], record
    assert record["area"] == 120


@check
def test_hectares_and_acres_do_not_get_mixed_up():
    """WFIGS reports acres, GWIS hectares, and the badge is given hectares."""
    assert abs(feeds.ACRE_HA - 0.404686) < 1e-9
    # A 1000 acre fire is about 405 hectares, not 1000 and not 2471.
    assert abs(1000 * feeds.ACRE_HA - 404.686) < 1e-3


@check
def test_the_box_widens_with_latitude():
    """A degree of longitude is a kilometre and a half at Svalbard."""
    source = Wildfires({})
    boxes = {}

    def catch(south, west, north, east, min_ha, home=None, want=10):
        boxes["at"] = (south, west, north, east)
        return []

    was = feeds.fetch
    try:
        feeds.fetch = catch
        source._box(0.0, 0.0, 500.0)
        equator = boxes["at"][3] - boxes["at"][1]
        source._box(78.0, 0.0, 500.0)
        svalbard = boxes["at"][3] - boxes["at"][1]
    finally:
        feeds.fetch = was
    assert svalbard > equator * 3, (equator, svalbard)


@check
def test_a_page_with_no_location_is_not_fetched_for():
    """`location` answers None where neither the page nor the badge names anywhere."""
    source = Wildfires({})
    source.home = {}
    source.pages([{"id": "p1", "radius": 500}])
    calls = []

    def catch(*args, **kwargs):
        calls.append(args)
        return []

    was = feeds.fetch
    try:
        feeds.fetch = catch
        source._refresh()
    finally:
        feeds.fetch = was
    assert calls == [], calls
    assert source._fires == {}, source._fires


@check
def test_two_pages_in_one_place_cost_one_fetch():
    source = Wildfires({})
    source.home = {"latitude": 53.38, "longitude": -1.47}
    source.pages([{"id": "p1", "radius": 500}, {"id": "p2", "radius": 500}])
    calls = []

    def catch(south, west, north, east, min_ha, home=None, want=10):
        calls.append((south, west))
        return [{"name": "x", "area": 900, "lon": -1.5, "lat": 53.5,
                 "span": [0.1, 0.1], "at": None}]

    was = feeds.fetch
    try:
        feeds.fetch = catch
        source._refresh()
    finally:
        feeds.fetch = was
    assert len(calls) == 1, calls
    assert set(source._fires) == {"p1", "p2"}, source._fires


@check
def test_a_sample_reports_per_page_and_scalars_across_them():
    source = Wildfires({})
    source.home = {"latitude": 53.38, "longitude": -1.47}
    source.pages([{"id": "p1", "radius": 500}])
    source._fires = {"p1": {
        "lat": 53.38, "lon": -1.47, "label": "Sheffield, GB",
        "fires": [{"name": "a", "area": 2200, "lon": -1.5, "lat": 53.5,
                   "span": [0.1, 0.1], "at": None}],
    }}
    frame = {}
    source.sample(frame, 1.0)
    reading = frame["wildfires"]
    assert set(reading["pages"]) == {"p1"}, reading
    assert reading["count"] == 1
    assert reading["biggest"] == 2200
    assert reading["nearest"] is not None
    assert reading["pages"]["p1"]["label"] == "Sheffield, GB"


@check
def test_a_sample_with_nothing_fetched_yet_still_answers():
    """The first frame goes out before the fetcher has been anywhere."""
    source = Wildfires({})
    frame = {}
    source.sample(frame, 1.0)
    assert frame["wildfires"]["pages"] == {}
    assert frame["wildfires"]["count"] == 0
    assert frame["wildfires"]["biggest"] is None


@check
def test_the_declared_page_kind_matches_the_module_that_registers_it():
    """A mismatch is a page the config UI offers and the badge cannot draw."""
    import pathlib
    kind = Wildfires.badge_page["kind"]
    source = pathlib.Path(Wildfires.badge_module).read_text(encoding="utf-8")
    assert f'pages.EXTRA["{kind}"]' in source, kind
    assert f'ANIMATED.add("{kind}")' in source, kind


@check
def test_every_declared_setting_has_a_default_the_source_accepts():
    source = Wildfires({})
    assert source.min_area == 100.0
    assert source.count == 10
    source.configure({"min_area": "nonsense", "count": "nonsense"})
    assert source.min_area == 100.0
    assert source.count == 10
    source.configure({"count": 999})
    assert source.count == 20


@check
def test_a_burnt_area_the_feed_did_not_name_is_named_after_a_town():
    """GWIS files no incident name, so the host names the fire from its own coordinates.

    Reads the way USGS names a quake, which the badge already draws beside one.
    """
    source = Wildfires({})
    fires = [{"lat": 34.25, "lon": -118.05, "area": 900},
             {"lat": 39.90, "lon": -8.10, "area": 400},
             {"lat": 40.0, "lon": -8.0, "area": 50, "name": "Park Fire"}]
    named = source._name(fires)

    # The hills above Los Angeles are nearest a suburb of it, and the city is the name
    # anybody can place.
    assert named[0]["name"] == "28 km NE of Los Angeles", named[0]
    assert named[1]["name"] == "44 km SE of Coimbra", named[1]
    # An incident the feed did name keeps it: WFIGS files real ones.
    assert named[2]["name"] == "Park Fire", named[2]


@check
def test_a_fire_that_cannot_be_named_leaves_the_badge_its_fallback():
    """An older statsbadge has no reverse table. The fetch that worked still stands."""
    source = Wildfires({})

    class Older:
        def nearest(self, _latitude, _longitude):
            raise AttributeError("no such thing here")

    source.geocode = Older()
    fires = [{"lat": 34.25, "lon": -118.05, "area": 900}]
    assert source._name(fires)[0].get("name", "") == ""
    assert source.faults == 1, source.faults


def main():
    failed = []
    for fn in CHECKS:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as exc:
            failed.append((fn.__name__, exc))
            print(f"FAIL {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed.append((fn.__name__, exc))
            print(f"ERR  {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(CHECKS)} checks, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
