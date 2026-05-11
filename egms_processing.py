import pandas as pd
import geopandas as gpd
import numpy as np
from datetime import datetime
import csv

from qgis.core import (
    QgsVectorLayer,
    QgsProject
)

TS2_SUFFIX = "_2"
DECIMALS = 2

x_field = "easting"
y_field = "northing"
pixel_field = "pixel"
line_field = "line"

crs = "EPSG:3035"


def parse_date(s):
    if len(s) == 8 and str(s).isdigit():
        try:
            return datetime.strptime(s, "%Y%m%d")
        except:
            return None
    return None


def run_concat(csv_period1, csv_period2, output_csv, clip_shapefile=None, iface=None, layer_name="EGMS_Fused_TS"):

    USE_POLYGON_CLIP = clip_shapefile is not None

    # ============================================================
    # LOAD CSVs
    # ============================================================

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

    # ============================================================
    # CLIP TO POLYGON
    # ============================================================

    if USE_POLYGON_CLIP:

        clip_layer = gpd.read_file(clip_shapefile)
        clip_polygon = clip_layer.unary_union

        gdf_p1 = gdf_p1[gdf_p1.geometry.within(clip_polygon)]
        gdf_p2 = gdf_p2[gdf_p2.geometry.within(clip_polygon)]

        print(f"P1 points after clip: {len(gdf_p1)}")
        print(f"P2 points after clip: {len(gdf_p2)}")

    # ============================================================
    # KEEP COMMON PIDS
    # ============================================================

    common_pids = set(gdf_p1["pid"]).intersection(gdf_p2["pid"])

    gdf_p1 = gdf_p1[gdf_p1["pid"].isin(common_pids)]
    gdf_p2 = gdf_p2[gdf_p2["pid"].isin(common_pids)]

    print("Common PID count:", len(common_pids))

    # ============================================================
    # RENAME PERIOD 2 COLUMNS
    # ============================================================

    df_p2_renamed = gdf_p2.drop(columns="geometry").copy()

    date_cols_p2 = [c for c in df_p2_renamed.columns if parse_date(c)]

    df_p2_renamed.rename(
        columns={c: c + TS2_SUFFIX for c in date_cols_p2},
        inplace=True
    )

    rename_fields = {
        x_field: x_field + TS2_SUFFIX,
        y_field: y_field + TS2_SUFFIX,
        pixel_field: pixel_field + TS2_SUFFIX,
        line_field: line_field + TS2_SUFFIX
    }

    df_p2_renamed.rename(columns=rename_fields, inplace=True)

    # ============================================================
    # MERGE PERIODS
    # ============================================================

    df_merged = pd.merge(
        gdf_p1.drop(columns="geometry"),
        df_p2_renamed,
        on="pid",
        how="inner"
    )

    print("Merged points:", len(df_merged))

    # ============================================================
    # PIXEL / LINE CHECK
    # ============================================================

    mask = (
        (df_merged[pixel_field] == df_merged[pixel_field + TS2_SUFFIX]) &
        (df_merged[line_field] == df_merged[line_field + TS2_SUFFIX])
    )

    before = len(df_merged)

    df_merged = df_merged[mask]

    after = len(df_merged)

    print("Points removed due to pixel/line mismatch:", before - after)
    print("Points kept after validation:", after)

    # ============================================================
    # COORDINATES
    # ============================================================

    east = df_merged[x_field + TS2_SUFFIX].to_numpy(dtype=float)
    north = df_merged[y_field + TS2_SUFFIX].to_numpy(dtype=float)
    pid_vals = df_merged["pid"].to_numpy()

    # ============================================================
    # DETECT DATE COLUMNS
    # ============================================================

    p1_dates = [f for f in df_p1.columns if parse_date(f)]
    p2_dates = [f for f in df_p2.columns if parse_date(f)]

    overlap_dates = sorted(set(p1_dates) & set(p2_dates))

    only_ts1 = sorted([d for d in p1_dates if d not in overlap_dates])
    only_ts2 = sorted([d for d in p2_dates if d not in overlap_dates])

    all_epochs = only_ts1 + sorted(overlap_dates) + only_ts2

    # ============================================================
    # BUILD MATRICES
    # ============================================================

    n = len(df_merged)

    P1  = df_merged[only_ts1].to_numpy(dtype=float)                          if only_ts1      else np.empty((n, 0))
    OV1 = df_merged[overlap_dates].to_numpy(dtype=float)                     if overlap_dates else np.empty((n, 0))
    OV2 = df_merged[[d + TS2_SUFFIX for d in overlap_dates]].to_numpy(dtype=float) if overlap_dates else np.empty((n, 0))
    P2  = df_merged[[d + TS2_SUFFIX for d in only_ts2]].to_numpy(dtype=float) if only_ts2     else np.empty((n, 0))

    # ============================================================
    # FUSION  —  compute per-point offset (median of OV2 - OV1)
    # ============================================================

    if overlap_dates:

        valid = ~np.isnan(OV1) & ~np.isnan(OV2)

        shift = np.nanmedian(
            np.where(valid, OV2 - OV1, np.nan),
            axis=1,
            keepdims=True
        )

        shift = np.where(np.isnan(shift), 0, shift)

    else:
        shift = np.zeros((n, 1))

    # offset_mm: the median displacement (mm) between the two periods per point
    # A positive value means period 2 is systematically higher than period 1
    # in the overlap window; negative means it is lower.
    offset_mm = shift.ravel()

    P1_shifted = P1 + shift

    fused = np.concatenate([P1_shifted, OV2, P2], axis=1)

    first_idx = np.argmax(~np.isnan(fused), axis=1)
    first_val = fused[np.arange(n), first_idx]

    fused -= first_val[:, None]

    # ============================================================
    # VELOCITY
    # ============================================================

    t0 = datetime.strptime(all_epochs[0], "%Y%m%d")

    days = np.array([
        (datetime.strptime(d, "%Y%m%d") - t0).days
        for d in all_epochs
    ])

    X = np.vstack([days, np.ones_like(days)]).T

    vel = np.full(n, np.nan)

    for i in range(n):

        row_mask = ~np.isnan(fused[i])

        if row_mask.sum() >= 2:

            coeff = np.linalg.lstsq(X[row_mask], fused[i, row_mask], rcond=None)[0]

            vel[i] = coeff[0] * 365.25

    # ============================================================
    # WRITE CSV  —  offset_mm added after mean_velocity_mmyr
    # ============================================================

    header = ["pid", x_field, y_field, "mean_velocity_mmyr", "offset_mm"] + all_epochs

    with open(output_csv, "w", newline="", encoding="utf-8") as f:

        writer = csv.writer(f)

        writer.writerow(header)

        for i in range(n):

            row = [
                pid_vals[i],
                round(east[i],  DECIMALS),
                round(north[i], DECIMALS),
                "" if np.isnan(vel[i])      else round(vel[i],      DECIMALS),
                "" if np.isnan(offset_mm[i]) else round(offset_mm[i], DECIMALS),
            ]

            row.extend(
                "" if np.isnan(v) else round(v, DECIMALS)
                for v in fused[i]
            )

            writer.writerow(row)

    print("Fusion completed.")
    print("Output CSV:", output_csv)

    # ============================================================
    # LOAD RESULT IN QGIS
    # ============================================================

    if iface:

        uri = f"file:///{output_csv}?delimiter=,&xField={x_field}&yField={y_field}&crs={crs}"

        layer = QgsVectorLayer(uri, layer_name, "delimitedtext")

        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)