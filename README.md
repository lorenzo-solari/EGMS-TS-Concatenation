The EGMS Time Series Concatenation plugin for QGIS merges two consecutive European Ground Motion Service (EGMS) displacement time-series periods into a single, continuous dataset. It is designed for analysts working with the Copernicus EGMS product who need to extend their analysis beyond a single observation window.

Key capabilities:
1.	Concatenates Period 1 and Period 2 EGMS CSV files into one output CSV.
2.	Retains only the Measurement Points (MP) common to both periods.
3.	Removes MPs where the pixel/line grid coordinates differ between periods.
4.	Estimates and applies a per-point displacement offset to harmonise the two series at their overlap.
5.	Optionally clips the output to a user-defined area of interest (AOI) polygon.
6.	Computes a mean linear velocity (mm/yr) over the full concatenated series.
7.	Automatically loads the result as a point layer in the QGIS map canvas.

Requirements 
1.	QGIS 3.28 or later.
2.	Python packages: pandas, geopandas, numpy (these are included in the standard QGIS Python environment or can be installed via OSGeo4W / conda).
3.	Input data: EGMS CSV files (one per observation period) in ETRS89-LAEA (EPSG:3035).
4.	Optional: An AOI polygon in Shapefile (.shp) format, in any CRS (reprojection is automatic).

Installation
1.	Download the plugin ZIP EGMS_TS_Concatenation.zip.
2.	In QGIS, open Plugins → Manage and Install Plugins → Install from ZIP.
3.	Browse to the downloaded ZIP file and click Install Plugin.
4.	Ensure the plugin is enabled (checkbox ticked) in the Installed tab.
5.	A new toolbar button and a menu entry under Plugins → EGMS Time Series Concatenation will appear.
