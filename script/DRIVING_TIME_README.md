# PUMA-to-college driving time

## Purpose and current status

Calculate travel time in **seconds** from each PUMA representative point to the
nearest college already selected by `02_create_distance.ipynb`. The destination
is not reselected by driving time. Origins cover the contiguous 48 states plus
Washington, D.C.; Alaska, Hawaii, and territories are excluded from the results.

As checked on **2026-10-07**, the nationwide OSRM calculation has returned
**2,447 PUMA-level results**, now copied from AHPCC to `dataset/osrm/`:

- All 2,447 rows have `driving_time_status=ok` and nonmissing durations.
- There are no duplicate `(STATE, PUMA)` keys.
- Returned identifiers and coordinates match the transferred input.
- **117 rows need review** because their origin-to-road offsets exceed 2,000 m.
  The largest origin offset is approximately 100.5 km.
- These results have **not yet been merged into the CPS person records**.

An API success is not evidence that the origin, snapped road point, or route is
appropriate. Review the flagged cases before treating the measure as final.

## Files and responsibilities

Paths below are relative to the local project root.

| File or directory | Purpose |
| --- | --- |
| `script/02_create_driving_time.py` | Query an existing OSRM server; optionally prepare coordinates and merge person records. |
| `script/driving_time_common.py` | Shared key normalization, representative-point preparation, HTTP requests, and atomic Parquet writes. |
| `output/university_driving_time/` | Transfer bundle: the two Python files, `pairs.parquet`, requirements, and instructions. |
| `output/ahpcc_osrm_jobs/download_us.slurm` | Download a dated US road extract and verify its checksum. |
| `output/ahpcc_osrm_jobs/build_and_route.slurm` | Build the road network, start OSRM, calculate durations, and flag results for review. |
| `dataset/osrm/` | Downloaded results from the completed HPC calculation. |
| `script/02_run_regional_driving_time.py` | Earlier Mac workflow that builds and checks smaller regional road networks. Not used for this HPC run. |
| `script/REGIONAL_DRIVING_TIME.md` | Documentation for that separate regional workflow. |

The Python client does not download roads or start OSRM. Those steps are handled
by the HPC job scripts. If changing the client, update its transfer copy before
submitting a new run; the transfer directory is not automatically synchronized.

## Inputs and measurement

Each prepared pair contains:

| Column | Meaning |
| --- | --- |
| `STATE` | Two-digit state FIPS string. |
| `PUMA` | Five-digit PUMA string; unique only together with `STATE`. |
| `puma_rep_lon`, `puma_rep_lat` | Origin representative point in EPSG:4326. |
| `nearest_college_lon`, `nearest_college_lat` | Previously selected college destination in EPSG:4326. |

The original coordinate preparation uses:

- `dataset/cleaned_dataset/cleaned_cps{12,13,14}_with_nearest_college.parquet`
- `dataset/sharpfiles/**/tl_2025_*_puma20.shp`, with accompanying shapefile files.

Representative points are computed after transforming polygons to EPSG:4326,
following the distance notebook. They are polygon-interior points, not
population-weighted locations or residential addresses. A polygon can include
water or remote land, so an interior point can still be far from a usable road.
The HPC run reuses the prepared coordinates rather than recomputing them.

For each pair, the client calls OSRM's Table API with source index 0 and
destination index 1. The returned 1-by-1 duration is kept in seconds. Missing
routes remain missing; no zero or straight-line substitute is inserted.

## Completed AHPCC run

The setup used SingularityCE 3.9.7 on a CentOS 7 compute node:

- OSRM v26.9.0, container image originally pinned to
  `ghcr.io/project-osrm/osrm-backend@sha256:8a1b1bc938412f15f9b5b32d794c4ec6bf4a85dfbbabfa0a014b70b187edb53b`.
- Geofabrik extract `us-261004.osm.pbf`, a dated US snapshot. The network includes
  US regions outside the study scope, but queried origins are mainland-only.
- Car profile `/opt/car.lua`; MLD preprocessing:
  `osrm-extract` → `osrm-partition` → `osrm-customize`.
- Python 3.11 container plus a project virtual environment containing pandas,
  pyarrow, and requests. No host Anaconda module is needed.
- Download job **1329566**; build-and-route job **1329567**.
- Build job requested one exclusive `comp72` node, 32 CPUs, all allocatable node
  memory (`--mem=0`), and a 72-hour limit. This is a limit, not a runtime estimate.

The HPC working directory is `~/capstone_osrm/`, physically under
`/scrfs/storage/rhiraki/home/capstone_osrm/`. The job records input hashes,
OSRM version, installed Python packages, source URL/checksums, and settings under
`runs/1329567/`, alongside the network and server log. Those provenance files
are not included in the five result files currently copied to `dataset/osrm/`;
copy them separately to retain a local reproducibility record.

Heavy preparation and Python execution belong on allocated compute nodes, not
login nodes. The scripts export only `HOME,USER,LOGNAME,TERM` and initialize
compute-node modules to avoid importing the Rocky9 login-node library environment.

### Submitting a new run

These commands run on the HPC login node after both job scripts, containers,
virtual environment, and transfer bundle have been prepared in `capstone_osrm`.
They submit a **new calculation**; they are not needed to inspect existing results.

```bash
cd "$HOME/capstone_osrm"
download_job=$(sbatch --parsable download_us.slurm)
echo "$download_job"
```

Only after a valid job ID is returned:

```bash
sbatch --dependency=afterok:"$download_job" \
  --kill-on-invalid-dep=yes build_and_route.slurm
squeue -u "$USER"
```

The dependent job starts after a successful download and resource allocation.
Batch jobs continue after disconnecting the Mac. Each build submission creates
its own `runs/<job-id>/` directory; it does not resume previous preprocessing.
The download script reuses a verified completed file or resumes its partial download.

For the completed run:

```bash
sacct -j 1329566,1329567 --format=JobID,State,Elapsed,ExitCode
cat "$HOME/capstone_osrm/runs/1329567/status.txt"
cat "$HOME/capstone_osrm/runs/1329567/results/summary.txt"
```

Disappearance from `squeue` alone does not mean success. The script writes
`COMPLETE` to `status.txt` only after routing, validation, and review-file output.

## Saved results on the Mac

| File in `dataset/osrm/` | Contents |
| --- | --- |
| `puma_nearest_college_driving_pairs.parquet` | Input identifiers and coordinates used in the run. |
| `puma_nearest_college_driving_time.parquet` | All pairs, durations, API status, snap offsets, and OSRM metadata. |
| `puma_driving_time_review.parquet` | Same results with a Boolean `needs_review` flag. |
| `flagged_pairs.csv` | The 117 flagged rows, for inspection. |
| `summary.txt` | Row count, review count, and API-status counts. |

The main result columns are `driving_time_seconds`, `driving_time_status`,
`origin_snap_m`, `destination_snap_m`, `osrm_data_version`, and `osrm_url`.
`osrm_data_version` may be empty when the server does not supply it; retain the
separate snapshot records. The localhost URL identifies the address used within
the compute job, not a server currently reachable from the Mac.

## Reviewing the 117 flagged pairs

The HPC script sets `needs_review=True` if any of these conditions holds:

- API status is not `ok`.
- Either snap distance is missing.
- Origin snap distance is greater than 2,000 m.
- Destination snap distance is greater than 500 m.

In the current results, all 117 flags are due to the origin-distance threshold.
These thresholds are screening rules, not automatic exclusion rules or error bounds.

Review procedure:

1. Sort `flagged_pairs.csv` by `origin_snap_m`, largest first.
2. Plot the PUMA boundary, original representative point, and college location.
   Check for water, islands, remote terrain, and unexpected polygon components.
3. Restart OSRM on an allocated HPC compute node using the existing
   `runs/1329567/us.osrm` network files. No road preprocessing is needed if those
   files remain intact. Query the flagged pairs for snapped coordinates and
   route geometry, retaining the same input coordinates and car profile.
4. Map the snapped endpoints and route alongside the original points. Distinguish
   an unsuitable representative point from road coverage or connectivity issues.
5. Record a decision and reason for each case. Keep plausible values with their
   flags; define a consistent alternative-origin method where needed, or retain
   an unmeasurable trip as missing. Do not silently replace the original results.

The current result files contain snap distances but **not snapped coordinates or
route geometry**. The map-review collection workflow is a next step, not an
implemented feature of the existing client.

Changing an origin may also change which college is nearest. Any revised origin
rule must specify whether college selection is recomputed or held fixed, and
apply that decision consistently. OSRM duration excludes travel from the original
coordinate to its snapped road point. These are modeled road-network times, not
observed traffic-dependent travel times. Routes requiring Canadian or Mexican
roads are outside the US extract's intended coverage.

## Person-level merge and standalone client usage

The completed HPC run used `--pairs-file`, which skips person-level merging.
After reviewing the results, merge the chosen PUMA-level table into the CPS
records on `(STATE, PUMA)` using a many-to-one validation. Preserve college IDs,
review flags, and original files; verify match rates and row counts. For
mainland-only outputs, filter person records to the same 48 states plus D.C.
A merge-only command for downloaded results is not implemented in this client.

To query an already running OSRM server from the project root:

```bash
python script/02_create_driving_time.py \
  --pairs-file output/university_driving_time/pairs.parquet \
  --contiguous-only \
  --output-dir dataset/osrm_new_run \
  --osrm-url http://127.0.0.1:5050
```

Coordinate-only mode needs pandas, pyarrow, and requests. Without `--pairs-file`,
the client additionally needs geopandas and pyogrio to read boundaries; it reads
the CPS inputs, prepares points, queries OSRM, and writes new person-level files.
Use `--prepare-only` to stop after coordinate preparation. Without
`--contiguous-only` or `--states`, it processes all input states.

The client checkpoints every 100 pairs but does not automatically resume.
Rerunning queries all pairs and replaces outputs in the selected output directory.
The `needs_review` files are produced by the HPC wrapper, not the standalone client.

## References

- [OSRM v26.9.0](https://github.com/Project-OSRM/osrm-backend/tree/v26.9.0)
- [Geofabrik US extracts](https://download.geofabrik.de/north-america/us.html)
- [AHPCC support wiki](https://hpcwiki.uark.edu/)
