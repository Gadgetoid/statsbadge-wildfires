"""Fetch real fires and write the frames the badge shot script draws.

    python tools/capture_frames.py

Two locations, picked for what they exercise. Boise sits in the middle of the US fire
season, so WFIGS answers with named incidents, containment and large outlines. Sheffield has
nothing in range, so the fallback and GWIS burnt area are drawn instead.

Reaches both feeds, so it needs a network. The result is committed, letting the shot script
run without one.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from statsbadge_wildfires import Wildfires  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# (key, location, radius km). The keys are what `shoot_firemap.py` iterates.
PLACES = (
    ("boise", "Boise, US", 600),
    ("sheffield", "Sheffield, GB", 2000),
)


def main():
    captured = {}
    for key, place, radius in PLACES:
        source = Wildfires({"min_area": 100, "count": 10})
        source.home = {"place": place}
        source.pages([{"id": "fires", "radius": radius}])
        source._refresh()
        if source.last_fault:
            print(f"{key}: {source.last_fault}")
        frame = {}
        source.sample(frame, 1.0)
        captured[key] = frame["wildfires"]
        page = frame["wildfires"]["pages"].get("fires")
        if page is None:
            print(f"{key}: nothing to draw")
            continue
        outlined = sum(1 for fire in page["fires"] if fire.get("outline"))
        print(f"{key}: {page['count']} in range, {len(page['fires'])} shown, "
              f"{outlined} with outlines, beyond={page['beyond']}")
    path = os.path.join(HERE, "sample_frames.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(captured, handle)
    print(f"wrote {path}, {os.path.getsize(path)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
