########################
#how to use (bash/terminal)
# chmod +x setup_mosaiks_env.sh
# ./setup_mosaiks_env.sh
#########################




#!/usr/bin/env bash
set -euo pipefail

ENV_NAME="mosaiks-env"
PY_VER="3.11.8"

# conda 初期化
source "$(conda info --base)/etc/profile.d/conda.sh"

# 1) env 作成
conda create -y -n "${ENV_NAME}" python="${PY_VER}"
conda activate "${ENV_NAME}"

# 2) install
python -m pip install -U pip setuptools wheel
python -m pip install -r requirements_mosaiks.txt

# 3) Dask query-planning を環境変数で固定（KeyError回避）
#    ※ notebookでも設定するけど、念のため永続化
for rc in "${HOME}/.zshrc" "${HOME}/.bashrc"; do
  touch "$rc"
  grep -q 'DASK_DATAFRAME__QUERY_PLANNING' "$rc" || echo 'export DASK_DATAFRAME__QUERY_PLANNING=True' >> "$rc"
done

# 4) mosaiks の site-packages にパッチを当てる（あなたの差分に合わせた最小パッチ）
python - <<'PY'
import pathlib, re, mosaiks

root = pathlib.Path(mosaiks.__file__).resolve().parent
images_py = root / "fetch" / "images.py"
stacs_py  = root / "fetch" / "stacs.py"

print("mosaiks root:", root)
print("patching images:", images_py)
txt = images_py.read_text(encoding="utf-8", errors="ignore")

# --- Patch A: proj:epsg 直参照の置換（あなたが見つけた行）
txt, n = re.subn(
    r'crs\s*=\s*stac_items_not_none\[0\]\.properties\["proj:epsg"\]',
    'first = stac_items_not_none[0]\n'
    '    crs = first.properties.get("proj:epsg")\n'
    '    if crs is None:\n'
    '        code = first.properties.get("proj:code")  # e.g. "EPSG:32644"\n'
    '        if isinstance(code, str) and code.upper().startswith("EPSG:"):\n'
    '            crs = int(code.split(":")[1])\n'
    '\n'
    '    # final fallback\n'
    '    crs = crs or 3857',
    txt
)
print("Patch A replacements:", n)

# --- Patch B: least_cloudy の stackstac.stack に epsg=crs を注入（無い場合のみ）
if "elif image_composite_method == \"least_cloudy\"" in txt and "epsg=crs" not in txt:
    txt2 = re.sub(
        r'(xarray\s*=\s*stackstac\.stack\(\s*\n\s*item,\s*\n\s*assets=bands,\s*\n\s*resolution=resolution,\s*\n\s*rescale=True,\s*\n\s*dtype=dtype,\s*)',
        r'\1                epsg=crs,\n',
        txt,
        flags=re.MULTILINE
    )
    if txt2 != txt:
        txt = txt2
        print("Patch B applied: injected epsg=crs into least_cloudy")
    else:
        print("Patch B skipped: pattern not matched (file may differ)")
else:
    print("Patch B skipped: epsg=crs already present or block not found")

images_py.write_text(txt, encoding="utf-8")
print("images.py patched.")

# --- Optional Patch C: stacs.py 内の proj:epsg fallback（あなたが貼ってくれた差分に合わせる）
if stacs_py.exists():
    st = stacs_py.read_text(encoding="utf-8", errors="ignore")
    if 'properties["proj:epsg"]' in st:
        st2 = st.replace(
            'stac_crs = item.properties["proj:epsg"]',
            'stac_crs = item.properties.get("proj:epsg")\n'
            '        if stac_crs is None:\n'
            '            code = item.properties.get("proj:code")\n'
            '            if isinstance(code, str) and code.upper().startswith("EPSG:"):\n'
            '                stac_crs = int(code.split(":")[1])\n'
            '            else:\n'
            '                stac_crs = 4326'
        )
        stacs_py.write_text(st2, encoding="utf-8")
        print("stacs.py patched (proj:epsg fallback).")
    else:
        print("stacs.py patch skipped (no direct proj:epsg indexing found).")

print("✅ patching done")
PY

# 5) quick sanity check (imports)
python - <<'PY'
import mosaiks, dask, distributed
print("mosaiks:", mosaiks.__version__)
print("dask:", dask.__version__, "distributed:", distributed.__version__)
PY

echo ""
echo "✅ Done. Restart your terminal so DASK_DATAFRAME__QUERY_PLANNING is loaded."
echo "In notebooks, use image_dtype='float' for Sentinel-2 with rescale=True."
