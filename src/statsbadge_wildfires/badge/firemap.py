"""The badge side of the wildfires extension: what is burning near a location.

Installed into the app's `ext/` directory by `statsbadge install` and imported by the app,
at which point it registers itself.

Zoom, pan, zoom. The camera pulls out to a coastline, crosses at that height with a cursor
on the site it is heading for, then closes in until the fire fills the band. A pan at the
zoom a fire is drawn at is a smear.

`world.geo.json` has a median segment of about 79km, so the close view of a basemap is one
segment wider than the screen. The coastlines wash out on the way in, leaving the burnt area
and the scale bar.
"""

import math
import time

import draw
import look
import pages
import worldmap

BAND_H = 40
MAP_TOP = look.BODY_TOP
MAP_H = look.BODY_H - BAND_H
BAND_TOP = MAP_TOP + MAP_H

ASPECT = worldmap.ASPECT

OUT_MS = 900
PAN_MS = 800
IN_MS = 1300
TRAVEL_MS = OUT_MS + PAN_MS + IN_MS

# Pixels per degree the pull-out aims for, about 90 degrees across the band. At 9 the
# coastline drew but held too little of the world to place anywhere.
CONTEXT_SCALE = 3.5

# Longest one segment of the feed's geometry may be drawn, in pixels. Held to each fire's
# `detail`, so a triangle stays small where a fifty-segment outline fills the band.
MAX_DETAIL_PX = 8.0
# A backstop under `detail`, which is a median and can come back finer than the geometry
# around it.
SCALE_CEILING = 12000.0
# A fire with no outline is a point, and framing a point fills the band with nothing.
NO_OUTLINE_SCALE = 120.0

# Whole below the first, gone above the second. The wash between them is one rectangle over
# the drawn land: `land(theme, alpha)` keys its pen cache on alpha and holds two keys, so
# fading through it rebuilds 24 pens a frame.
LAND_FADE_FROM = 35.0
LAND_UNTIL = 160.0

# The burnt area is filled. Stroking a custom path of a few hundred points asked the
# rasteriser for 1.7MB and wedged the badge; filling is what worldmap does 288 times a frame.
FILL_ALPHA = 190
DOT_PX_LOW = 1.5
DOT_PX_HIGH = 4.0
# Log scaled: fires run from a hundred hectares to a quarter of a million, and a linear
# ramp spends itself on the largest.
AREA_LOW = 100.0
AREA_HIGH = 250000.0

RING_MS = 2200
RINGS = 3
RING_PX = 9.0
RING_MIN_PX = 1.5
CURSOR_ARM = 6
# How far clear of the fire's dot the arms start.
CURSOR_GAP = 3
# Closer than this to the cursor, the location marker sits under the crosshair. Squared,
# since the firmware's math has no hypot and the comparison does not need the root.
HOME_CLEAR_PX2 = 10.0 * 10.0

# Metres, since a fire filling the band is often a couple of kilometres across.
BAR_STEPS = (100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000,
             500000, 1000000, 2000000)
BAR_W = 86
BAR_H = 4
BAR_PAD = 8

# Keyed by page id, so two fire pages hold a place each.
_state = {}


def _area_fraction(hectares):
    """Where a fire sits on the ramp, by how big it is."""
    if not hectares or hectares <= 0:
        return 0.0
    span = math.log(AREA_HIGH / AREA_LOW)
    return max(0.0, min(1.0, math.log(max(hectares, AREA_LOW) / AREA_LOW) / span))


def _dot_px(hectares):
    return DOT_PX_LOW + (DOT_PX_HIGH - DOT_PX_LOW) * _area_fraction(hectares)


def _page_state(page):
    key = (page or {}).get("id") or "wildfires"
    state = _state.get(key)
    if state is None:
        now = time.ticks_ms()
        state = _state[key] = {
            "index": 0, "held": now, "view": None,
            # Where the camera left from and when, which is the whole of the travel: the
            # legs are worked out against these, not accumulated frame by frame.
            "moved": now, "from": None,
            # Built once and re-aimed after that. The key is cheap to compare and moves
            # when the feed replaces the fire.
            "shapes": (), "shapes_for": None,
        }
    return state


def _depart(state, view, now):
    """Note where the camera is leaving from, on the frame a new fire is picked."""
    state["moved"] = now
    state["from"] = (view.lon, view.lat, view.scale)


def _view(state, home, radius_km):
    """The page's camera, opened on the whole radius the first time it is asked for.

    Never closer than the pull-out, so a page opens on a map and travels out of one rather
    than starting inside a flat wash.
    """
    view = state["view"]
    if view is None:
        span = max(0.2, radius_km / 111.195)
        scale = min(look.W * 0.42 / span, MAP_H * 0.42 / (span * ASPECT), CONTEXT_SCALE)
        view = state["view"] = worldmap.View(MAP_TOP, MAP_H, lon=home[1], lat=home[0],
                                             scale=scale)
    return view


def _ease(fraction):
    """Smoothstep, so each leg of the travel starts and stops gently."""
    fraction = max(0.0, min(1.0, fraction))
    return fraction * fraction * (3.0 - 2.0 * fraction)


def _zoom(from_scale, to_scale, fraction):
    """Interpolate a zoom the way a zoom is read: by ratio, not by difference.

    Linearly, most of a pull-out from six thousand to nine happens in the first few frames
    and the rest crawls.
    """
    return math.exp(math.log(from_scale)
                    + (math.log(to_scale) - math.log(from_scale)) * fraction)


def _travel(view, state, fire, now):
    """Pull out until there is a map, cross, then close in on the fire.

    Driven by the clock, so all three legs happen in the same order however far apart two
    fires are.
    """
    started = state["moved"]
    from_lon, from_lat, from_scale = state["from"]
    target = _fill_scale(fire)
    # Never "pull out" to something closer in than where either end already is.
    context = min(CONTEXT_SCALE, from_scale, target)
    elapsed = time.ticks_diff(now, started)

    if elapsed >= TRAVEL_MS:
        view.jump_to(fire["lon"], fire["lat"], target)
        return 1.0
    if elapsed < OUT_MS:
        view.jump_to(from_lon, from_lat,
                     _zoom(from_scale, context, _ease(elapsed / OUT_MS)))
        return 0.0
    if elapsed < OUT_MS + PAN_MS:
        across = _ease((elapsed - OUT_MS) / PAN_MS)
        view.jump_to(from_lon + worldmap.shortest(fire["lon"] - from_lon) * across,
                     from_lat + (fire["lat"] - from_lat) * across,
                     context)
        return 0.0
    closing = _ease((elapsed - OUT_MS - PAN_MS) / IN_MS)
    view.jump_to(fire["lon"], fire["lat"], _zoom(context, target, closing))
    return closing


def _fill_scale(fire):
    """Pixels per degree that fits this fire in the band, held to the detail it has.

    The cap is the feed's resolution, so a small fire with a fine outline fills the band
    where one with a coarse outline stays small.
    """
    span = fire.get("span") or (0.05, 0.05)
    fits = min(look.W * 0.85 / max(span[0], 1e-4),
               MAP_H * 0.85 / max(span[1] * ASPECT, 1e-4))
    detail = fire.get("detail")
    if not fire.get("outline"):
        return min(fits, NO_OUTLINE_SCALE)
    if detail:
        fits = min(fits, MAX_DETAIL_PX / detail)
    return min(SCALE_CEILING, fits)


def _shapes(state, fire):
    """The active fire's outline as shapes in map degrees, built once per fire.

    Points are (lon, -lat) like the world map's, so one mat3 both scales and places them
    and the rings are only re-aimed after this.
    """
    key = (fire["lon"], fire["lat"], fire.get("area"))
    if state["shapes_for"] == key:
        return state["shapes"]
    origin = fire.get("origin")
    outline = fire.get("outline")
    step = fire.get("step")
    built = []
    if origin and outline and step:
        for deltas in outline:
            if len(deltas) < 6:
                continue
            x, y = deltas[0], deltas[1]
            path = [vec2(origin[0] + x * step, -(origin[1] + y * step))]
            for index in range(2, len(deltas) - 1, 2):
                x += deltas[index]
                y += deltas[index + 1]
                path.append(vec2(origin[0] + x * step, -(origin[1] + y * step)))
            if len(path) >= 3:
                built.append(shape.custom(path))
    state["shapes"] = tuple(built)
    state["shapes_for"] = key
    return state["shapes"]


def _placed(view):
    """The transform that puts map degrees on the screen, for this frame."""
    base_y = view.lat * view.scale * ASPECT + view.mid[1]
    return mat3().translate(-view.lon * view.scale + view.mid[0],
                            base_y).scale(view.scale, view.scale * ASPECT)


def _dots(theme, view, fires):
    """Every fire in range as a dot: what is burning, and where.

    The active one included. Marking it by leaving its dot out and putting a cursor there
    instead leaves the crosshair over a gap of its own making. The cursor frames a dot.
    """
    was = screen.clip
    screen.clip = view.box
    screen.pen = theme.dim
    for fire in fires:
        x, y = view.at(fire["lon"], fire["lat"])
        if not view.holds(x, y):
            continue
        screen.shape(shape.circle(vec2(x, y), _dot_px(fire.get("area"))))
    screen.clip = was


def _land(theme, view):
    """The coastlines, washed out as the camera closes in past them.

    Cheaper the further in it goes, not dearer: `land` rejects a polygon on its box, and by
    the top of the fade all but one of the 288 are off screen.
    """
    if view.scale > LAND_UNTIL:
        return
    view.land(theme)
    if view.scale <= LAND_FADE_FROM:
        return
    gone = (view.scale - LAND_FADE_FROM) / (LAND_UNTIL - LAND_FADE_FROM)
    screen.pen = theme.bg.with_alpha(int(min(1.0, gone) * 255))
    screen.rectangle(view.box)


def _home(theme, view, home, marked=None):
    """Where the page is set to, so a fire has something to be near.

    Dropped where it lands on the fire the cursor is already on. A ring and a dot two pixels
    from another ring and another dot is not two markers, it is one that looks misregistered,
    and the band is saying how far apart they are anyway.
    """
    x, y = view.at(home[1], home[0])
    if not view.holds(x, y):
        return
    if marked is not None:
        away, down = x - marked[0], y - marked[1]
        if away * away + down * down < HOME_CLEAR_PX2:
            return
    was = screen.clip
    screen.clip = view.box
    screen.pen = theme.accent_b
    screen.shape(shape.circle(vec2(x, y), 3.0).stroke(1))
    screen.shape(shape.circle(vec2(x, y), 1.0))
    screen.clip = was


def _fire(theme, view, state, fire, closing):
    """The active fire: a cursor on the way in, its burnt area once the camera arrives.

    `closing` is how far through the last leg of the travel the camera is, so the cursor
    hands over to the shape instead of both being drawn at once.
    """
    pen = theme.at(_area_fraction(fire.get("area")))
    x, y = view.at(fire["lon"], fire["lat"])
    shapes = _shapes(state, fire)
    was = screen.clip
    screen.clip = view.box
    if shapes and closing > 0.45:
        alpha = int(min(1.0, (closing - 0.45) / 0.35) * FILL_ALPHA)
        transform = _placed(view)
        screen.pen = pen.with_alpha(alpha)
        for burnt in shapes:
            burnt.transform = transform
            screen.shape(burnt)
    # Held until the shape has faded up, so a fire is never unmarked.
    if closing < 0.8 and view.holds(x, y):
        _cursor(view, fire, pen, x, y, 1.0 - max(0.0, (closing - 0.45) / 0.35))
    screen.clip = was


def _cursor(view, fire, pen, x, y, fade):
    """A crosshair around the site, with rings leaving it.

    Where the camera is going while the fire is still a dot, and the spot on a fire the
    feed sent no outline for.

    Nothing is drawn at the centre. The dot is already there, drawn with the rest of the
    set, and the arms clear it: a marker covering its own subject leaves the camera closing
    in on an empty crosshair.
    """
    now = time.ticks_ms()
    alpha = int(max(0.0, min(1.0, fade)) * 255)
    gap = int(_dot_px(fire.get("area"))) + CURSOR_GAP
    for ring in range(RINGS):
        progress = ((now + ring * (RING_MS // RINGS)) % RING_MS) / RING_MS
        radius = gap + progress * RING_PX
        if radius < gap + RING_MIN_PX:
            continue
        # Squared, so a ring is bright where it leaves and gone well before the edge.
        screen.pen = pen.with_alpha(int((1.0 - progress) ** 2 * alpha))
        screen.shape(shape.circle(vec2(x, y), radius).stroke(1))
    screen.pen = pen.with_alpha(alpha)
    for along in (-1, 1):
        screen.rectangle(rect(int(x + along * gap), int(y), along * CURSOR_ARM, 1))
        screen.rectangle(rect(int(x), int(y + along * gap), 1, along * CURSOR_ARM))


def _scale_bar(theme, view):
    """How far across the map is, in kilometres, at whatever the camera is on.

    Once the coastlines are gone this is the only measure of size left, so it is always
    drawn.
    """
    # A degree of longitude shortens away from the equator, and the bar is horizontal.
    metres_per_degree = 111195.0 * max(0.02, math.cos(math.radians(view.lat)))
    px_per_metre = view.scale / metres_per_degree
    span = BAR_STEPS[0]
    for step in BAR_STEPS:
        if step * px_per_metre <= BAR_W:
            span = step
    width = max(8.0, span * px_per_metre)
    left = look.W - look.PAD - width
    top = BAND_TOP - BAR_PAD - BAR_H
    screen.pen = theme.ink.with_alpha(150)
    screen.rectangle(rect(int(left), top + BAR_H - 1, int(width), 1))
    screen.rectangle(rect(int(left), top, 1, BAR_H))
    screen.rectangle(rect(int(left + width) - 1, top, 1, BAR_H))
    # Clear of the bar: the label is placed by its top edge.
    draw.blit_label(_distance(span), look.SIZE_SMALL, theme.dim,
                    int(left + width), top - look.SIZE_SMALL - 2, align=2)


def _distance(metres):
    """Metres under a kilometre, kilometres over it."""
    if metres < 1000:
        return f"{metres} m"
    return f"{metres // 1000} km"


def _band(theme, target, fire, index, total, note=None):
    """The strip under the map: what it is, how big, how far and how long ago."""
    screen.pen = theme.panel
    screen.rectangle(rect(0, BAND_TOP, look.W, BAND_H))
    screen.pen = theme.accent_b
    screen.rectangle(rect(0, BAND_TOP, look.W, 1))
    if fire is None:
        if note:
            draw.blit_label(note, look.SIZE_VALUE, theme.dim, look.PAD, BAND_TOP + 12)
        return

    area = _area(fire.get("area"))
    draw.blit_label(area, look.SIZE_BIG,
                    draw.readable(theme.at(_area_fraction(fire.get("area"))), theme.panel,
                                  theme.ink),
                    look.PAD, BAND_TOP + 5)
    left = look.PAD + draw.text_width(area, look.SIZE_BIG) + 10

    counter = f"{index + 1}/{total}"
    draw.blit_label(counter, look.SIZE_SMALL, theme.dim, look.W - look.PAD, BAND_TOP + 6,
                    align=2)

    room = look.W - left - look.PAD * 2 - draw.text_width(counter, look.SIZE_SMALL)
    # The page's own location would put "Sheffield, GB" on a fire in Algeria.
    name = (fire.get("name") or "").strip() or "burnt area"
    draw.blit_label(draw.fit(name, look.SIZE_LABEL, room), look.SIZE_LABEL, theme.ink,
                    left, BAND_TOP + 5)

    detail = []
    # Before the distance, which is the cluster's nearest and not its only.
    nearby = fire.get("nearby")
    if nearby:
        detail.append(f"+{nearby} nearby")
    if fire.get("km") is not None:
        detail.append(f"{fire['km']} km away"
                      + (", beyond your radius" if target.get("beyond") else ""))
    if fire.get("contained") is not None:
        detail.append(f"{fire['contained']}% contained")
    aged = draw.ago(fire.get("age_s"))
    if aged:
        detail.append(aged)
    if detail:
        draw.blit_label(draw.fit(", ".join(detail), look.SIZE_SMALL,
                                 look.W - left - look.PAD),
                        look.SIZE_SMALL, theme.dim, left, BAND_TOP + 22)


def _area(hectares):
    """Hectares, or square kilometres once there are too many to read."""
    if not hectares:
        return "a fire"
    if hectares >= 10000:
        return f"{hectares / 100.0:,.0f} km2"
    return f"{hectares:,.0f} ha"


def render(page, frame, _history, theme):
    reading = (frame.get("wildfires") or {}).get("pages") or {}
    mine = reading.get((page or {}).get("id")) or {}
    fires = mine.get("fires") or []

    if not worldmap.ready():
        draw.blit_label("loading the map", look.SIZE_VALUE, theme.dim,
                        look.W // 2, MAP_TOP + MAP_H // 2 - 8, align=1)
        _band(theme, page, None, 0, 0)
        return

    home = mine.get("home")
    if home is None:
        draw.blit_label("set a location", look.SIZE_VALUE, theme.dim,
                        look.W // 2, MAP_TOP + MAP_H // 2 - 8, align=1)
        _band(theme, page, None, 0, 0, note="in the badge's settings, or on this page")
        return

    state = _page_state(page)
    view = _view(state, home, mine.get("radius") or 500)
    now = time.ticks_ms()
    if state["from"] is None:
        _depart(state, view, now)

    # A button on a map suggests panning and zooming, not a step to the next fire, so the
    # page moves on unprompted. The hold is time on a fire, and the travel is on top.
    hold = max(1.0, float((page or {}).get("hold") or 7))
    if fires and time.ticks_diff(now, state["held"]) > int(hold * 1000.0) + TRAVEL_MS:
        state["index"] += 1
        state["held"] = now
        _depart(state, view, now)

    active = None
    closing = 1.0
    if fires:
        # Each frame, since a shorter list would leave the index past the end.
        state["index"] %= len(fires)
        active = fires[state["index"]]
        closing = _travel(view, state, active, now)

    _land(theme, view)
    # Drawn even with the coastlines up: a few hundred kilometres of radius opens with the
    # nearest coast off screen, leaving the bar as the only measure of width.
    _scale_bar(theme, view)
    _home(theme, view, home,
          marked=view.at(active["lon"], active["lat"]) if active else None)
    if fires:
        _dots(theme, view, fires)
        _fire(theme, view, state, active, closing)
        _band(theme, mine, active, state["index"], len(fires))
    else:
        where = mine.get("label") or "here"
        _band(theme, mine, None, 0, 0, note=f"nothing burning near {where}")


pages.EXTRA["firemap"] = render
# The camera and the rings move between readings, so this page is drawn every frame.
pages.ANIMATED.add("firemap")
