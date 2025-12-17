# Rows GeoJSON Generator

Minimal Streamlit app to generate field "rows" paths from an input GeoJSON area and AB line.

Files of interest:
- [`Example/rows.geojson:1`](Example/rows.geojson:1) — input FeatureCollection (polygon area + AB line)
- [`Example/destination.geojson:1`](Example/destination.geojson:1) — sample destination point

Goals
- Take an input GeoJSON (area + AB), ask user for row spacing and naming rules, and output a GeoJSON of generated paths and destination features.

Requirements (to be written to `requirements.txt` in code mode)
- streamlit
- geopandas
- shapely
- pyproj
- folium
- streamlit-folium
- fiona
- rtree

Development plan
1. Implement Streamlit frontend ([`app.py:1`](app.py:1)) with file uploads, controls, preview, copy/download.
2. Implement core generator ([`generator.py:1`](generator.py:1)) handling projection, spacing, clipping, naming, and optional turn attachment.
3. Wire preview using `folium` via `streamlit_folium`.
4. Add small unit tests and example files.

Defaults chosen
- Start label: F01 (start letter F, start number 1)
- Default destination side: A
- Allow numbering style: 01 or 1 (user-selectable)

Next steps
- I will switch to code mode to create the code files and `requirements.txt` and implement the Streamlit app. Please confirm you want me to proceed.

Contact / Notes
- Iteration will be fast: the UI will allow regenerating until satisfied.