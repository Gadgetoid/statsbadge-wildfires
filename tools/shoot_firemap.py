"""Draw the fire map on a badge, at several points in the camera's travel, and dump each.

The app's modules are only importable from a statsbadge checkout: what `install` puts on the
badge is precompiled onto the FAT partition, off the MicroPython path. So this runs with that
checkout mounted, staged into its `build/` alongside the page and the captured frames.
`build/` is ignored there, and `tools/stage_shots.sh` does the copying.

    python tools/capture_frames.py           # refresh tools/sample_frames.json
    tools/stage_shots.sh /dev/cu.usbmodem101 ../statsbadge

Runs against the badge's real firmware, real fonts and real picovector, the only check on
whether the page draws. It bypasses the paired host, so a badge serving somebody else's
layout can still shoot this.

The camera is stepped by hand rather than watched, since a dump is one frame: pulled out
with the coastlines and every fire as a dot, mid-travel, and closed in on the outline with
the scale bar.
"""

import json
import os
import sys
import time

sys.path.insert(0, "/remote/src/statsbadge/badge_app")
# Staged beside this script: the page under test, and the frames to draw.
sys.path.insert(0, "/remote/build")

import draw
import look
import pages as pages_module

# The page registers itself on import, exactly as the app loads it.
import firemap

for directory in ("/remote/build", "/remote/build/shots"):
    try:
        os.mkdir(directory)
    except OSError:
        pass

badge.mode(HIRES | VSYNC)
screen.antialias = image.X4
badge.default_clear = None
BUTTON_HOME.irq(None)

draw.prepare()

with open("/remote/build/sample_frames.json") as handle:
    SAMPLES = json.load(handle)

theme = look.get(look.DEFAULT)
PAGE = {"id": "fires", "kind": "firemap", "title": "Wildfires", "hold": 999}


def shoot(name, reading, settle_ms, fire_index=0):
    """Let the camera ease for a while, then dump whatever it is looking at."""
    firemap._state.clear()
    frame = {"wildfires": reading}
    # Rendered repeatedly so the camera's easing actually runs: it moves against elapsed
    # milliseconds, so one frame lands on the opening view whatever the settle asks for.
    started = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), started) < settle_ms:
        state = firemap._state.get("fires")
        if state is not None:
            state["index"] = fire_index
            state["held"] = time.ticks_ms()
        pages_module.render(PAGE, frame, {}, theme, 0, 1, "shot")
    badge.update()
    with open("/remote/build/shots/live_{}.raw".format(name), "wb") as handle:
        handle.write(screen.raw)
    view = firemap._state["fires"]["view"]
    print("  {:<22} scale {:7.1f} px/deg  at {:.3f},{:.3f}".format(
        name, view.scale, view.lon, view.lat))


# Where in the travel each shot is taken. The camera pulls out for 900ms, crosses for 800
# and closes in for 1300, so these land one to a leg with the fade partway through the last.
PHASES = (("out", 700), ("pan", 1400), ("fade", 2400), ("close", 4200))
# Which fire of the set, for a second span and a second scale bar. Every entry here is
# another shot of every location.
FIRES = (0,)

# A frame is 320x240x4, so every shot is 307KB back over the serial mount, which is the whole
# runtime. Trim PHASES and FIRES to what is being looked at. The full grid takes minutes, and
# killing it partway leaves the badge needing the reset button.
print("worldmap parse...")
while not firemap.worldmap.ready():
    pass

for key in ("boise", "sheffield"):
    reading = SAMPLES[key]
    page = reading["pages"]["fires"]
    print("{}: {} fires, {} in range, beyond={}".format(
        key, len(page["fires"]), page["count"], page["beyond"]))
    for index in FIRES:
        suffix = "" if index == 0 else "_{}".format(index)
        for phase, settle in PHASES:
            shoot("{}_{}{}".format(key, phase, suffix), reading, settle,
                  fire_index=index)

print("\nwrote {} shots to build/shots".format(len(PHASES) * len(FIRES) * 2))
