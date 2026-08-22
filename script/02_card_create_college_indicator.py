from pathlib import Path

import geopandas as gpd
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
IPEDS_PATH = ROOT / "dataset/raw_dataset/ipeds2024.csv"
SHP_DIR = ROOT / "dataset/sharpfiles"
CLEAN_DIR = ROOT / "dataset/cleaned_dataset"
PUMA_OUT = ROOT / "dataset/puma_college_indicator.parquet"

TITLE_IV_COL = "HD2024.Postsecondary and Title IV institution indicator"
LON_COL = "HD2024.Longitude location of institution"
LAT_COL = "HD2024.Latitude location of institution"


def pick_col(df, candidates):
    lower_map = {c.lower(): c for c in df.columns}
    for c in candidates:
        if c.lower() in lower_map:
            return lower_map[c.lower()]
    raise KeyError(f"Column not found. candidates={candidates}")


def load_puma_shapes():
    shp_files = sorted(SHP_DIR.glob("**/tl_2025_*_puma20.shp"))
    if not shp_files:
        raise FileNotFoundError(f"No PUMA shapefiles found under {SHP_DIR}")

    gdfs = [gpd.read_file(path, engine="pyogrio") for path in shp_files]
    puma = gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True), crs=gdfs[0].crs)
    puma = puma.to_crs("EPSG:4326").copy()

    state_col = pick_col(puma, ["STATEFP20", "STATEFP", "STATE"])
    puma_col = pick_col(puma, ["PUMACE20", "PUMA", "PUMACE"])

    puma["STATE"] = puma[state_col].astype(str).str.zfill(2)
    puma["PUMA"] = puma[puma_col].astype(str).str.zfill(5)
    puma["STATE_PUMA"] = puma["STATE"] + "_" + puma["PUMA"]

    return puma[["STATE", "PUMA", "STATE_PUMA", "geometry"]]


def load_title_iv_colleges():
    ipeds = pd.read_csv(IPEDS_PATH)
    ipeds.columns = ipeds.columns.str.strip()

    colleges = ipeds.copy()
    if TITLE_IV_COL in colleges.columns:
        colleges = colleges[
            colleges[TITLE_IV_COL].astype(str).str.contains("Title IV", case=False, na=False)
        ].copy()

    colleges["lon"] = pd.to_numeric(colleges[LON_COL], errors="coerce")
    colleges["lat"] = pd.to_numeric(colleges[LAT_COL], errors="coerce")
    colleges = colleges.dropna(subset=["lon", "lat"]).copy()

    return gpd.GeoDataFrame(
        colleges[["unitid", "institution name", "lon", "lat"]],
        geometry=gpd.points_from_xy(colleges["lon"], colleges["lat"]),
        crs="EPSG:4326",
    )


def build_puma_college_indicator():
    puma = load_puma_shapes()
    colleges = load_title_iv_colleges()

    joined = gpd.sjoin(
        colleges,
        puma[["STATE", "PUMA", "STATE_PUMA", "geometry"]],
        how="inner",
        predicate="intersects",
    )

    counts = (
        joined.groupby(["STATE", "PUMA", "STATE_PUMA"])
        .size()
        .rename("college_count_in_puma")
        .reset_index()
    )

    indicator = puma[["STATE", "PUMA", "STATE_PUMA"]].merge(
        counts,
        on=["STATE", "PUMA", "STATE_PUMA"],
        how="left",
    )
    indicator["college_count_in_puma"] = (
        indicator["college_count_in_puma"].fillna(0).astype("int64")
    )
    indicator["has_college_in_puma"] = (indicator["college_count_in_puma"] > 0).astype("int64")

    return indicator.sort_values(["STATE", "PUMA"]).reset_index(drop=True)


def attach_indicator(df, indicator):
    out = df.copy()
    out["STATE"] = out["STATE"].astype(str).str.zfill(2)
    out["PUMA"] = out["PUMA"].astype(str).str.zfill(5)

    drop_cols = [c for c in ["college_count_in_puma", "has_college_in_puma"] if c in out.columns]
    if drop_cols:
        out = out.drop(columns=drop_cols)

    out = out.merge(
        indicator[["STATE", "PUMA", "college_count_in_puma", "has_college_in_puma"]],
        on=["STATE", "PUMA"],
        how="left",
    )
    out["college_count_in_puma"] = out["college_count_in_puma"].fillna(0).astype("int64")
    out["has_college_in_puma"] = out["has_college_in_puma"].fillna(0).astype("int64")
    return out


def main():
    indicator = build_puma_college_indicator()
    indicator.to_parquet(PUMA_OUT, index=False)

    input_files = []
    for year in ["12", "13", "14"]:
        input_files.extend(
            [
                CLEAN_DIR / f"cleaned_cps{year}.parquet",
                CLEAN_DIR / f"cleaned_cps{year}_with_nearest_college.parquet",
                CLEAN_DIR / f"cleaned_cps{year}_with_nearest_college_imgfeat.parquet",
            ]
        )

    for in_path in input_files:
        if not in_path.exists():
            continue
        out_path = in_path.with_name(f"{in_path.stem}_with_college_indicator.parquet")
        df = pd.read_parquet(in_path)
        attach_indicator(df, indicator).to_parquet(out_path, index=False)
        print(f"saved: {out_path.relative_to(ROOT)}")

    share = indicator["has_college_in_puma"].mean()
    print(f"saved: {PUMA_OUT.relative_to(ROOT)}")
    print(f"PUMAs: {len(indicator):,}")
    print(f"PUMAs with at least one Title IV college: {indicator['has_college_in_puma'].sum():,}")
    print(f"share with college: {share:.3f}")


if __name__ == "__main__":
    main()
