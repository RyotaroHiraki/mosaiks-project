"""Driving seconds from each PUMA representative point to its existing nearest college.

Run after 02_create_distance.ipynb, using an OSRM server prepared with the car
profile and OSM coverage for the study area (default: http://localhost:5050):

    python script/02_create_driving_time.py --osrm-url http://localhost:5050

Dependencies: pandas, geopandas, pyogrio, pyarrow, requests.
The destination is the distance-nearest college already selected by the notebook;
it is not reselected by driving time. Representative points are calculated in
EPSG:4326, exactly as in that notebook. No seconds-to-hours conversion is applied.
OSRM setup/data preparation is separate from this client script.
For Docker, publish port 5050 on the host to port 5000 in the OSRM container
(-p 127.0.0.1:5050:5000). macOS Control Center may already occupy host port 5000.
API: https://project-osrm.org/docs/v5.24.0/api/#table-service
"""

import argparse
import math
import os
from pathlib import Path

import pandas as pd
import requests

from driving_time_common import (
    KEYS, COLLEGE, RESULTS, driving_time, load_pairs, make_session,
    normalize_keys, save_table,
)

ROOT = Path(__file__).resolve().parents[1]
CONTIGUOUS_STATES = set("01 04 05 06 08 09 10 11 12 13 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 44 45 46 47 48 49 50 51 53 54 55 56".split())


def read_prepared_pairs(path, states=None):
    """Load validated coordinates without CPS files or GIS dependencies."""
    coords = ["puma_rep_lon", "puma_rep_lat"] + COLLEGE
    pairs = normalize_keys(pd.read_parquet(path, columns=KEYS + coords))
    if states:
        pairs = pairs[pairs.STATE.isin(states)].copy()
    if pairs.empty or pairs.duplicated(KEYS).any():
        raise ValueError("Prepared pairs must be nonempty and unique by STATE/PUMA")
    for column, bound in zip(coords, [180, 90, 180, 90]):
        pairs[column] = pd.to_numeric(pairs[column], errors="raise")
        valid = pairs[column].map(lambda value: pd.notna(value) and math.isfinite(value) and abs(value) <= bound)
        if not valid.all():
            raise ValueError(f"Missing or invalid coordinates in {column}")
    return pairs.sort_values(KEYS).reset_index(drop=True)


def main():
    """Prepare pairs, query OSRM, save PUMA results, and merge them into person records."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--osrm-url", default=os.environ.get("OSRM_URL", "http://localhost:5050"))
    parser.add_argument("--input-files", nargs="+", type=Path,
                        default=[ROOT / f"dataset/cleaned_dataset/cleaned_cps{y}_with_nearest_college.parquet"
                                 for y in (12, 13, 14)])
    parser.add_argument("--shapes-dir", type=Path, default=ROOT / "dataset/sharpfiles")
    parser.add_argument("--pairs-file", type=Path,
                        help="Use prepared coordinates; skip CPS/shapefile reads and person-level merges")
    parser.add_argument("--contiguous-only", action="store_true",
                        help="Restrict origins and person outputs to the contiguous 48 states plus D.C.")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--states", nargs="+", help="State FIPS codes, e.g. --states 01 for Alabama")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--prepare-only", action="store_true", help="Save coordinate pairs without API calls")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.states:
        if any(not s.isascii() or not s.isdigit() or not 1 <= len(s) <= 2 for s in args.states):
            parser.error("--states expects one- or two-digit state FIPS codes")
        args.states = sorted({s.zfill(2) for s in args.states})
    if args.contiguous_only:
        if args.states and not set(args.states) <= CONTIGUOUS_STATES:
            parser.error("--states includes states outside the contiguous 48 states plus D.C.")
        args.states = args.states or sorted(CONTIGUOUS_STATES)
    if args.output_dir is None:
        args.output_dir = ROOT / "dataset/cleaned_dataset"
        # Keep state-only trial outputs separate from nationwide results.
        if args.contiguous_only and set(args.states) == CONTIGUOUS_STATES:
            args.output_dir /= "contiguous_us"
        elif args.states:
            args.output_dir /= "states_" + "_".join(args.states)

    # 1. Deduplicate coordinate pairs across annual files and save them.
    # With --prepare-only, stop here without connecting to OSRM.
    pairs = (read_prepared_pairs(args.pairs_file, args.states) if args.pairs_file
             else load_pairs(args.input_files, args.shapes_dir, args.states))
    pair_path = args.output_dir / "puma_nearest_college_driving_pairs.parquet"
    if args.pairs_file and args.pairs_file.resolve() in {
        pair_path.resolve(), (args.output_dir / "puma_nearest_college_driving_time.parquet").resolve()
    }:
        parser.error("Choose an output directory that does not overwrite --pairs-file")
    save_table(pairs, pair_path)
    print(f"Prepared {len(pairs):,} PUMA–college pairs: {pair_path}", flush=True)
    if args.prepare_only:
        return

    # 2. Query once per PUMA rather than per person to avoid duplicate API requests.
    records = []
    table_path = args.output_dir / "puma_nearest_college_driving_time.parquet"
    with make_session() as session:
        for i, row in enumerate(pairs.itertuples(index=False), 1):
            try:
                result = driving_time(session, args.osrm_url, row, args.timeout)
            except (requests.RequestException, ValueError, KeyError, IndexError, RuntimeError) as exc:
                raise RuntimeError(f"Failed at STATE={row.STATE}, PUMA={row.PUMA}. "
                                   "Check OSRM server, car profile and map coverage. "
                                   f"Completed checkpoint rows: {len(records) // 100 * 100}. "
                                   f"Details: {exc}") from exc
            records.append({**row._asdict(), **result, "osrm_url": args.osrm_url})
            # Save progress every 100 pairs; automatic resumption is not implemented.
            if i % 100 == 0:
                save_table(pd.DataFrame(records), table_path)
                print(f"Processed {i:,}/{len(pairs):,}", flush=True)
    table = pd.DataFrame(records)
    table["driving_time_seconds"] = pd.to_numeric(table["driving_time_seconds"], errors="raise")
    save_table(table, table_path)
    if args.pairs_file:
        print(f"Saved: {table_path}")
        print(table["driving_time_status"].value_counts(dropna=False).to_string())
        return
    # 3. Merge on state + PUMA; many_to_one validation prevents row multiplication.
    # Preserve each person's college ID and save under a new filename.
    for path in args.input_files:
        df = normalize_keys(pd.read_parquet(path))
        if args.states:
            df = df[df["STATE"].isin(args.states)].copy()
        df = df.drop(columns=RESULTS, errors="ignore")
        out = df.merge(table[KEYS + RESULTS], on=KEYS, how="left", validate="many_to_one")
        output = args.output_dir / f"{path.stem}_with_driving_time.parquet"
        if output.resolve() in {p.resolve() for p in args.input_files}:
            raise ValueError(f"Output would overwrite input: {output}")
        save_table(out, output)
        print(f"Saved: {output}")
    print(table["driving_time_status"].value_counts(dropna=False).to_string())


if __name__ == "__main__":
    main()
