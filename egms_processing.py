import pandas as pd
import geopandas as gpd
import numpy as np
from datetime import datetime
import csv
import threading

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TS2_SUFFIX  = "_2"
DECIMALS    = 2
x_field     = "easting"
y_field     = "northing"
pixel_field = "pixel"
line_field  = "line"
crs         = "EPSG:3035"


def parse_date(s):
    if len(str(s)) == 8 and str(s).isdigit():
        try:
            return datetime.strptime(str(s), "%Y%m%d")
        except Exception:
            return None
    return None


def _compute(csv_period1, csv_period2, output_csv,
             clip_shapefile, on_progress, on_done, on_error):
    """
    Pure-Python worker that runs entirely in a background thread.
    No QGIS objects are created here. Results are handed back via
    callbacks that the dialog marshals onto the Qt main thread.
    """
    try:
        # 1. Load CSVs
        on_progress(5, "Loading CSVs...")
        df_p1 = pd.read_csv(csv_period1)
        df_p2 = pd.read_csv(csv_period2)

        gdf_p1 = gpd.GeoDataFrame(
            df_p1,
            geometry=gpd.points_from_xy(df_p1[x_field], df_p1[y_field]),
            crs=crs
        )
        gdf_p2 = gpd.GeoDataFrame(
            df_p2,
            geometry=gpd.points_from_xy(df_p2[x_field], df_p2[y_field]),
            crs=crs
        )

        # 2. Clip to AOI
        if clip_shapefile:
            on_progress(15, "Clipping to AOI...")
            clip_layer   = gpd.read_file(clip_shapefile)
            clip_polygon = clip_layer.unary_union
            gdf_p1 = gdf_p1[gdf_p1.geometry.within(clip_polygon)]
            gdf_p2 = gdf_p2[gdf_p2.geometry.within(clip_polygon)]

        # 3. Keep common PIDs
        on_progress(25, "Matching PIDs...")
        common_pids = set(gdf_p1["pid"]).intersection(gdf_p2["pid"])
        gdf_p1 = gdf_p1[gdf_p1["pid"].isin(common_pids)]
        gdf_p2 = gdf_p2[gdf_p2["pid"].isin(common_pids)]

        # 4. Rename Period-2 columns
        on_progress(30, "Renaming Period 2 columns...")
        df_p2_renamed = gdf_p2.drop(columns="geometry").copy()
        date_cols_p2  = [c for c in df_p2_renamed.columns if parse_date(c)]
        df_p2_renamed.rename(
            columns={c: c + TS2_SUFFIX for c in date_cols_p2},
            inplace=True
        )
        df_p2_renamed.rename(columns={
            x_field:     x_field     + TS2_SUFFIX,
            y_field:     y_field     + TS2_SUFFIX,
            pixel_field: pixel_field + TS2_SUFFIX,
            line_field:  line_field  + TS2_SUFFIX,
        }, inplace=True)

        # 5. Merge
        on_progress(40, "Merging periods...")
        df_merged = pd.merge(
            gdf_p1.drop(columns="geometry"),
            df_p2_renamed,
            on="pid",
            how="inner"
        )

        # 6. Pixel / line consistency check
        on_progress(50, "Validating pixel/line consistency...")
        mask = (
            (df_merged[pixel_field] == df_merged[pixel_field + TS2_SUFFIX]) &
            (df_merged[line_field]  == df_merged[line_field  + TS2_SUFFIX])
        )
        df_merged = df_merged[mask]

        # 7. Coordinate arrays
        n        = len(df_merged)
        east     = df_merged[x_field     + TS2_SUFFIX].to_numpy(dtype=float)
        north    = df_merged[y_field     + TS2_SUFFIX].to_numpy(dtype=float)
        pid_vals = df_merged["pid"].to_numpy()

        # 8. Detect date columns
        on_progress(55, "Detecting date columns...")
        p1_dates      = [f for f in df_p1.columns if parse_date(f)]
        p2_dates      = [f for f in df_p2.columns if parse_date(f)]
        overlap_dates = sorted(set(p1_dates) & set(p2_dates))
        only_ts1      = sorted(d for d in p1_dates if d not in overlap_dates)
        only_ts2      = sorted(d for d in p2_dates if d not in overlap_dates)
        all_epochs    = only_ts1 + sorted(overlap_dates) + only_ts2

        # 9. Build matrices
        on_progress(60, "Building displacement matrices...")
        P1  = df_merged[only_ts1].to_numpy(dtype=float)              if only_ts1      else np.empty((n, 0))
        OV1 = df_merged[overlap_dates].to_numpy(dtype=float)         if overlap_dates else np.empty((n, 0))
        OV2 = df_merged[[d + TS2_SUFFIX for d in overlap_dates]].to_numpy(dtype=float) \
                                                                      if overlap_dates else np.empty((n, 0))
        P2  = df_merged[[d + TS2_SUFFIX for d in only_ts2]].to_numpy(dtype=float) \
                                                                      if only_ts2      else np.empty((n, 0))

        # 10. Fusion + offset_mm
        on_progress(70, "Fusing time series...")
        if overlap_dates:
            valid = ~np.isnan(OV1) & ~np.isnan(OV2)
            shift = np.nanmedian(
                np.where(valid, OV2 - OV1, np.nan),
                axis=1, keepdims=True
            )
            shift = np.where(np.isnan(shift), 0, shift)
        else:
            shift = np.zeros((n, 1))

        # offset_mm: per-point median offset used to align Period 1 onto Period 2
        offset_mm = shift.ravel()

        fused     = np.concatenate([P1 + shift, OV2, P2], axis=1)
        first_idx = np.argmax(~np.isnan(fused), axis=1)
        first_val = fused[np.arange(n), first_idx]
        fused    -= first_val[:, None]

        # 11. Velocity
        on_progress(80, "Computing velocities...")
        t0   = datetime.strptime(all_epochs[0], "%Y%m%d")
        days = np.array([
            (datetime.strptime(d, "%Y%m%d") - t0).days
            for d in all_epochs
        ])
        X   = np.vstack([days, np.ones_like(days)]).T
        vel = np.full(n, np.nan)
        for i in range(n):
            m = ~np.isnan(fused[i])
            if m.sum() >= 2:
                coeff  = np.linalg.lstsq(X[m], fused[i, m], rcond=None)[0]
                vel[i] = coeff[0] * 365.25

        # 12. Write CSV
        on_progress(90, "Writing output CSV...")
        header = ["pid", x_field, y_field,
                  "mean_velocity_mmyr", "offset_mm"] + all_epochs

        with open(output_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            for i in range(n):
                row = [
                    pid_vals[i],
                    round(east[i],       DECIMALS),
                    round(north[i],      DECIMALS),
                    "" if np.isnan(vel[i])       else round(vel[i],       DECIMALS),
                    "" if np.isnan(offset_mm[i]) else round(offset_mm[i], DECIMALS),
                ]
                row.extend(
                    "" if np.isnan(v) else round(v, DECIMALS)
                    for v in fused[i]
                )
                writer.writerow(row)

        on_progress(100, "Done.")
        on_done(output_csv)

    except Exception as exc:
        on_error(exc)


def run_concat(csv_period1, csv_period2, output_csv,
               clip_shapefile=None, iface=None, layer_name="EGMS_Fused_TS",
               on_progress=None, on_done=None, on_error=None):
    """
    Launch all processing in a daemon thread so QGIS stays fully responsive.

    Callbacks (all invoked from the background thread – the dialog marshals
    them onto the Qt main thread via Qt signals):
        on_progress(pct: int, msg: str)
        on_done(output_csv: str)
        on_error(exc: Exception)
    """
    on_progress = on_progress or (lambda pct, msg: None)
    on_done     = on_done     or (lambda path: None)
    on_error    = on_error    or (lambda exc:  None)

    t = threading.Thread(
        target=_compute,
        args=(csv_period1, csv_period2, output_csv,
              clip_shapefile, on_progress, on_done, on_error),
        daemon=True
    )
    t.start()
    return t
