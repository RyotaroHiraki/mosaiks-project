#!/bin/bash
#SBATCH --job-name=osrm-us
#SBATCH --partition=comp72
#SBATCH --qos=comp
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --exclusive
#SBATCH --mem=0
#SBATCH --time=72:00:00
#SBATCH --export=HOME,USER,LOGNAME,TERM
#SBATCH --output=osrm-us-%j.log

# Initialize the compute node's own modules, not the Rocky9 login environment.
source /etc/profile
module load singularity/3.9.7
set -euo pipefail
cd "${SLURM_SUBMIT_DIR:?Submit from capstone_osrm}"
root=$(pwd -P)
run="$root/runs/$SLURM_JOB_ID"
mkdir -p "$run"
status="$run/status.txt"
echo 'checking inputs' > "$status"
server_pid=''
cleanup() {
  code=$?
  if [ -n "$server_pid" ]; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
  if [ "$code" -ne 0 ]; then
    echo "FAILED (exit $code); see osrm-us-$SLURM_JOB_ID.log" >> "$status"
  fi
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
osrm_image="$root/containers/osrm.sif"
python_image="$root/containers/python.sif"
python_bin="$root/.venv/bin/python"
osrm() { singularity exec --cleanenv --bind /scrfs:/scrfs "$osrm_image" "$@"; }
py() { singularity exec --cleanenv --bind /scrfs:/scrfs "$python_image" "$python_bin" "$@"; }

(cd roads && md5sum --check us-261004.osm.pbf.md5)
# Snapshot the small inputs and scripts so changes to the upload folder cannot
# alter a calculation already running. Preserve hashes of both runtime images.
cp university_driving_time/{pairs.parquet,02_create_driving_time.py,driving_time_common.py} "$run/"
sha256sum "$osrm_image" "$python_image" "$run/"*.py "$run/pairs.parquet" > "$run/input-sha256.txt"
cp roads/{source-url.txt,us-261004.osm.pbf.md5,us-261004.osm.pbf.sha256} "$run/"
osrm osrm-routed --version > "$run/osrm-version.txt"
py -m pip freeze > "$run/python-packages.txt"
py -c 'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import importlib; c=importlib.import_module("02_create_driving_time"); d=c.read_prepared_pairs(Path(sys.argv[1])/"pairs.parquet", c.CONTIGUOUS_STATES); assert len(d)==2447, len(d)' "$run"
# Each submission has its own network/output directory. Failed preprocessing is
# retained for diagnosis; resubmission starts a new build rather than reusing it.
cp --reflink=auto roads/us-261004.osm.pbf "$run/us.osm.pbf"
printf 'profile=/opt/car.lua\nalgorithm=MLD\nthreads=%s\n' "$SLURM_CPUS_PER_TASK" > "$run/settings.txt"
echo 'extracting roads' > "$status"
osrm osrm-extract -t "$SLURM_CPUS_PER_TASK" -p /opt/car.lua "$run/us.osm.pbf"
echo 'partitioning road network' > "$status"
osrm osrm-partition -t "$SLURM_CPUS_PER_TASK" "$run/us.osrm"
echo 'customizing road network' > "$status"
osrm osrm-customize -t "$SLURM_CPUS_PER_TASK" "$run/us.osrm"
echo 'starting OSRM' > "$status"
singularity exec --cleanenv --bind /scrfs:/scrfs "$osrm_image" \
  osrm-routed --algorithm mld --ip 127.0.0.1 --port 5050 --threads 4 \
  "$run/us.osrm" > "$run/server.log" 2>&1 &
server_pid=$!
ready=0
for attempt in $(seq 1 600); do
  kill -0 "$server_pid" 2>/dev/null || { echo 'OSRM exited; see server.log'; exit 1; }
  if curl --fail --silent --max-time 2 \
    'http://127.0.0.1:5050/nearest/v1/driving/-87.678089,34.739788' > "$run/health.json"; then
    ready=1
    break
  fi
  sleep 2
done
[ "$ready" -eq 1 ] || { echo 'OSRM startup timed out'; exit 1; }
echo 'querying 2447 PUMA-college pairs' > "$status"
py "$run/02_create_driving_time.py" --pairs-file "$run/pairs.parquet" \
  --contiguous-only --output-dir "$run/results" --osrm-url http://127.0.0.1:5050
# Flag large snap offsets separately from API success; do not call these
# full-network routes regionally stable or replace missing routes with zero.
py - "$run" <<'PY'
import sys
from pathlib import Path
import pandas as pd
run = Path(sys.argv[1])
df = pd.read_parquet(run/'results/puma_nearest_college_driving_time.parquet')
assert len(df) == 2447 and not df.duplicated(['STATE', 'PUMA']).any()
df['needs_review'] = (df.driving_time_status.ne('ok') |
    df.origin_snap_m.isna() | df.destination_snap_m.isna() |
    df.origin_snap_m.gt(2000) | df.destination_snap_m.gt(500))
df.to_parquet(run/'results/puma_driving_time_review.parquet', index=False)
df.loc[df.needs_review].to_csv(run/'results/flagged_pairs.csv', index=False)
(run/'results/summary.txt').write_text(
    f'PUMAs: {len(df)}\nNeeds review: {int(df.needs_review.sum())}\n' +
    df.driving_time_status.value_counts(dropna=False).to_string() + '\n')
print((run/'results/summary.txt').read_text())
PY
echo 'COMPLETE: review results/summary.txt and results/flagged_pairs.csv' > "$status"
echo "Results: $run/results"
