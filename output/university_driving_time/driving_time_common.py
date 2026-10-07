"""Shared input preparation, OSRM requests, and output helpers.

Both driving-time entry points use this module. Importing it does not start
OSRM, download road data, or run the calculation. Durations remain in seconds.
"""

import math
import os

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

KEYS = ["STATE", "PUMA"]
COLLEGE = ["nearest_college_lon", "nearest_college_lat"]
RESULTS = ["driving_time_seconds", "driving_time_status", "origin_snap_m",
           "destination_snap_m", "osrm_data_version"]


def normalize_keys(df):
    """Standardize merge keys as two-digit state and five-digit PUMA strings."""
    out = df.copy()
    for col, width in [("STATE", 2), ("PUMA", 5)]:
        values = pd.to_numeric(out[col], errors="raise").astype("Int64")
        if values.isna().any():
            raise ValueError(f"Missing {col} identifiers")
        out[col] = values.astype(str).str.zfill(width)
    return out


def load_pairs(paths, shapes_dir, states=None):
    """Pair each PUMA representative point with its previously selected nearest college."""
    # GIS dependencies are needed only when rebuilding representative points.
    os.environ.setdefault("USE_PYGEOS", "0")
    import geopandas as gpd

    # Co-located colleges can have different UNITIDs: route their shared
    # coordinate once, while retaining every original UNITID in person files.
    pairs = pd.concat(
        [normalize_keys(pd.read_parquet(p, columns=KEYS + COLLEGE)) for p in paths],
        ignore_index=True,
    ).drop_duplicates()
    if states:
        pairs = pairs[pairs["STATE"].isin(states)].copy()
    if pairs.empty:
        raise ValueError("No PUMA–college pairs match the requested states/input files.")
    if pairs.duplicated(KEYS).any():
        raise ValueError("Multiple nearest-college coordinates for one PUMA; resolve ties first.")
    files = sorted(shapes_dir.glob("**/tl_2025_*_puma20.shp"))
    if states:
        files = [p for p in files if p.stem.split("_")[2] in states]
    if not files:
        raise FileNotFoundError(f"No PUMA shapefiles under {shapes_dir}")
    # Convert each state's boundary data to longitude/latitude (EPSG:4326).
    frames = []
    for path in files:
        try:
            frames.append(gpd.read_file(path, engine="pyogrio").to_crs(4326))
        except Exception as exc:
            raise RuntimeError(f"Could not read/transform {path}. Check the geometry "
                               "and the active environment's PROJ installation.") from exc
    shapes = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=4326)
    shapes = normalize_keys(shapes.rename(columns={"STATEFP20": "STATE", "PUMACE20": "PUMA"}))
    # Unlike a centroid, this origin is guaranteed to lie inside the polygon.
    points = shapes.geometry.representative_point()
    shapes["puma_rep_lon"] = points.x
    shapes["puma_rep_lat"] = points.y
    pairs = pairs.merge(shapes[KEYS + ["puma_rep_lon", "puma_rep_lat"]],
                        on=KEYS, how="left", validate="one_to_one")
    return pairs.sort_values(KEYS).reset_index(drop=True)


def make_session():
    """Reuse HTTP connections and retry transient connection or server failures."""
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"])
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def driving_time(session, base_url, row, timeout):
    """Return driving seconds and query status for one representative-point-to-college pair."""
    result = dict.fromkeys(RESULTS)
    coords = [row.puma_rep_lon, row.puma_rep_lat,
              row.nearest_college_lon, row.nearest_college_lat]
    if any(pd.isna(v) or not math.isfinite(float(v)) for v in coords):
        result["driving_time_status"] = "missing_coordinates"
        return result
    if any(abs(float(v)) > bound for v, bound in zip(coords, [180, 90, 180, 90])):
        result["driving_time_status"] = "invalid_coordinates"
        return result
    # The API expects longitude,latitude; point 0 is the origin, point 1 the destination.
    lon1, lat1, lon2, lat2 = coords
    url = f"{base_url.rstrip('/')}/table/v1/driving/{lon1},{lat1};{lon2},{lat2}"
    response = session.get(url, params={"sources": "0", "destinations": "1",
                                       "annotations": "duration"}, timeout=timeout)
    if response.status_code == 403:
        raise RuntimeError(
            f"HTTP 403 from {base_url}. Verify that this address serves OSRM. "
            "On macOS, port 5000 may belong to Control Center. Start OSRM on "
            "another host port (for example Docker -p 127.0.0.1:5050:5000) "
            "and pass --osrm-url http://localhost:5050."
        )
    # OSRM returns HTTP 400 for coordinates that cannot snap to the road network.
    if response.status_code not in (200, 400):
        response.raise_for_status()
    body = response.json()
    code = body.get("code")
    if code in {"NoSegment", "NoTable", "NoRoute"}:
        result["driving_time_status"] = code
        return result
    if code != "Ok":
        raise RuntimeError(f"OSRM error: {body}")
    response.raise_for_status()
    # One source and one destination produce a 1x1 matrix; keep the duration in seconds.
    # Treat null (no route) as missing, without substituting zero or a straight-line estimate.
    seconds = body["durations"][0][0]
    if seconds is not None and (not math.isfinite(seconds) or seconds < 0):
        raise ValueError(f"Invalid OSRM duration: {seconds}")
    # snap_m measures the offset from the input coordinate to the snapped road point in meters.
    result.update(driving_time_seconds=seconds,
                  driving_time_status="ok" if seconds is not None else "no_route",
                  origin_snap_m=body["sources"][0]["distance"],
                  destination_snap_m=body["destinations"][0]["distance"],
                  osrm_data_version=body.get("data_version"))
    return result


def save_table(df, path):
    """Replace the output only after writing a temporary file to avoid partial saves."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp.parquet")
    df.to_parquet(temp, index=False)
    temp.replace(path)


