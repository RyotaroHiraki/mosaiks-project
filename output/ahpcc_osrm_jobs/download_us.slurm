#!/bin/bash
#SBATCH --job-name=osrm-download
#SBATCH --partition=cloud72
#SBATCH --qos=cloud
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --time=06:00:00
#SBATCH --export=HOME,USER,LOGNAME,TERM
#SBATCH --output=download-%j.log

set -euo pipefail
cd "${SLURM_SUBMIT_DIR:?Submit from capstone_osrm}"
mkdir -p roads
cd roads
# Lock the download directory so simultaneous submissions cannot share a partial file.
exec 9>.download.lock
flock -n 9 || { echo 'Another download is running'; exit 1; }
name=us-261004.osm.pbf
url="https://download.geofabrik.de/north-america/$name"
curl --fail --location --retry 5 --connect-timeout 30 --max-time 18000 \
  "$url.md5" -o "$name.md5.tmp"
mv "$name.md5.tmp" "$name.md5"
if [ ! -f "$name" ]; then
  curl --fail --location --retry 5 --connect-timeout 30 --max-time 18000 \
    --continue-at - "$url" -o "$name.part"
  expected=$(awk '{print $1}' "$name.md5")
  actual=$(md5sum "$name.part" | awk '{print $1}')
  [ "$actual" = "$expected" ] || { echo 'Checksum mismatch; retain partial file for investigation'; exit 1; }
  mv "$name.part" "$name"
fi
md5sum --check "$name.md5"
printf '%s\n' "$url" > source-url.txt
sha256sum "$name" > "$name.sha256"
echo 'Road download verified. Ready for OSRM preprocessing.'
