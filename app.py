import streamlit as st
import json
import os
from pathlib import Path
from typing import Optional
import folium
from streamlit_folium import st_folium
import generator
from shapely.geometry import shape, mapping, Point, LineString, Polygon

# Simple Streamlit UI to generate rows using [`generator.py:1`](generator.py:1)
st.set_page_config(page_title="Rows GeoJSON Generator", layout="wide")

# session state keys
if "output_fc" not in st.session_state:
    st.session_state["output_fc"] = None
if "last_error" not in st.session_state:
    st.session_state["last_error"] = None

EXAMPLE_PATH = Path("Example/rows.geojson")
EXAMPLE_TURN = Path("Example/destination.geojson")


def load_geojson_from_upload(upload) -> Optional[dict]:
    """Parse an uploaded Streamlit file into a GeoJSON dict.

    This is more robust across different UploadedFile implementations:
    - prefer UploadedFile.getvalue() when available
    - handle bytes or str payloads
    """
    if upload is None:
        return None
    try:
        # UploadedFile supports getvalue() in Streamlit; fall back to read()
        if hasattr(upload, "getvalue"):
            raw = upload.getvalue()
        else:
            raw = upload.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)
    except Exception:
        st.error("Could not parse uploaded GeoJSON.")
        return None


def load_example_if_missing(fc_upload):
    if fc_upload is not None:
        return load_geojson_from_upload(fc_upload)
    if EXAMPLE_PATH.exists():
        try:
            return json.loads(EXAMPLE_PATH.read_text())
        except Exception:
            st.error("Failed reading Example/rows.geojson")
            return None
    return None


def find_area_and_ab(fc: dict):
    """Return (area_feature, ab_feature) or (None, None)"""
    if not fc or "features" not in fc:
        return None, None
    area = None
    ab = None
    for feat in fc["features"]:
        geom_type = feat.get("geometry", {}).get("type", "")
        if geom_type == "Polygon" and area is None:
            area = feat
        if geom_type == "LineString" and ab is None:
            ab = feat
    return area, ab


def add_geojson_to_map(m, fc, layer_name="layer", style=None):
    folium.GeoJson(fc, name=layer_name, style_function=lambda x: style or {}).add_to(m)


def render_preview_map(area_feat, ab_feat, output_fc):
    # determine center
    center = [0, 0]
    try:
        if area_feat:
            geom = shape(area_feat["geometry"])
            c = geom.centroid
            center = [c.y, c.x]
        elif ab_feat:
            geom = shape(ab_feat["geometry"])
            c = geom.centroid
            center = [c.y, c.x]
    except Exception:
        center = [0, 0]

    m = folium.Map(location=center, zoom_start=17, tiles="OpenStreetMap")

    if area_feat:
        add_geojson_to_map(m, {"type": "FeatureCollection", "features": [area_feat]}, "Area",
                           style={"color": "#3388ff", "weight": 2, "fillColor": "#3388ff", "fillOpacity": 0.1})
    if ab_feat:
        add_geojson_to_map(m, {"type": "FeatureCollection", "features": [ab_feat]}, "AB Line",
                           style={"color": "#000000", "weight": 3})

    if output_fc:
        # separate layers for rows and destinations
        rows = []
        dests = []
        turns = []
        for feat in output_fc.get("features", []):
            ptype = feat.get("properties", {}).get("type", "")
            geom_type = feat.get("geometry", {}).get("type", "")
            # NetworkPath can be either a row or a turn - distinguish by geometry type
            if ptype == "NetworkPath":
                if geom_type == "LineString":
                    # Check if it's a simple 2-point line (likely a row) or complex (likely a turn)
                    coords = feat.get("geometry", {}).get("coordinates", [])
                    if len(coords) <= 3:
                        rows.append(feat)
                    else:
                        turns.append(feat)
                else:
                    turns.append(feat)
            elif ptype == "NetworkDestination":
                dests.append(feat)
            elif ptype == "RowPath":  # Legacy support
                rows.append(feat)
            elif ptype == "DestinationPoint":  # Legacy support
                dests.append(feat)
            else:
                # TurnAttachment or other
                turns.append(feat)
        if rows:
            add_geojson_to_map(m, {"type": "FeatureCollection", "features": rows}, "Rows",
                               style={"color": "#ff7f00", "weight": 2})
        if dests:
            for d in dests:
                geom = d.get("geometry")
                coords = geom.get("coordinates")
                folium.CircleMarker(location=[coords[1], coords[0]], radius=4, color="#1f78b4",
                                    fill=True, fill_color="#1f78b4", popup=d.get("properties", {}).get("name", "")).add_to(m)
        if turns:
            add_geojson_to_map(m, {"type": "FeatureCollection", "features": turns}, "Turns",
                               style={"color": "#2ca02c", "weight": 2})

    folium.LayerControl().add_to(m)
    return m


st.title("🌾 Row Path Generator")

col1, col2 = st.columns([1, 2])

with col1:
    st.markdown("### 📥 Inputs")
    
    tab1, tab2 = st.tabs(["Required", "Turns"])
    
    with tab1:
        pasted_line = st.text_area("Line", height=50, key="pasted_line",
                                   placeholder='Paste LineString GeoJSON')
        
        pasted_shape = st.text_area("Area", height=50, key="pasted_shape",
                                    placeholder='Paste Polygon GeoJSON')
        
        use_example = st.checkbox("Use examples", value=False)
    
    with tab2:
        st.caption("Turn A (optional)")
        pasted_turn = st.text_area("Turn A", height=45, key="pasted_turn",
                                   placeholder='Paste turn GeoJSON or leave empty', label_visibility="collapsed")
        
        st.caption("Turn B (optional)")
        pasted_turn2 = st.text_area("Turn B", height=45, key="pasted_turn2",
                                   placeholder='Paste turn GeoJSON or leave empty', label_visibility="collapsed")
        
        # Show controls if either turn is provided
        if pasted_turn or pasted_turn2:
            st.markdown("**Turn Controls:**")
            
            col_a, col_b = st.columns(2)
            with col_a:
                st.caption("A End")
                turn_at_a = st.checkbox("Attach", value=bool(pasted_turn), key="turn_at_a")
                if turn_at_a:
                    rotation_a = st.number_input("Rotate°", -180, 180, 0, 15, key="rot_a")
                    flip_a_h = st.checkbox("↔️", value=False, key="flip_a_h")
                    flip_a_v = st.checkbox("↕️", value=False, key="flip_a_v")
                else:
                    rotation_a = 0
                    flip_a_h = flip_a_v = False
            
            with col_b:
                st.caption("B End")
                turn_at_b = st.checkbox("Attach", value=bool(pasted_turn2), key="turn_at_b")
                if turn_at_b:
                    rotation_b = st.number_input("Rotate°", -180, 180, 0, 15, key="rot_b")
                    flip_b_h = st.checkbox("↔️", value=False, key="flip_b_h")
                    flip_b_v = st.checkbox("↕️", value=False, key="flip_b_v")
                else:
                    rotation_b = 0
                    flip_b_h = flip_b_v = False
        else:
            turn_at_a = turn_at_b = False
            rotation_a = rotation_b = 0
            flip_a_h = flip_a_v = flip_b_h = flip_b_v = False

    st.markdown("### ⚙️ Settings")
    
    spacing_m = st.number_input("Spacing (m)", min_value=0.5, value=6.0, step=0.5, format="%.2f")
    
    col_a, col_b = st.columns(2)
    with col_a:
        start_letter = st.text_input("Letter", value="S", max_chars=1)
    with col_b:
        start_num = st.number_input("Number", min_value=0, value=1, step=1)
    
    dest_side = st.radio("Destination", options=["A", "B"], index=0, horizontal=True)
    
    with st.expander("Advanced", expanded=False):
        zero_pad = st.checkbox("Zero-pad", value=True)
        dual_zone = st.checkbox("Dual labels", value=False)

    generate_btn = st.button("✨ Generate", type="primary", use_container_width=True)

with col2:
    st.markdown("### 🗺️ Map Preview")
    # Load inputs separately (priority: pasted > uploaded > example if enabled)
    line_fc = None
    shape_fc = None
    turn_fc = None

    # Load AB Line (pasted or uploaded or example)
    if pasted_line and pasted_line.strip():
        try:
            parsed = json.loads(pasted_line)
            # Allow either a Feature or a FeatureCollection
            if parsed.get("type") == "Feature":
                line_fc = parsed
            elif parsed.get("type") == "FeatureCollection":
                # pick first LineString feature
                for f in parsed.get("features", []):
                    if f.get("geometry", {}).get("type") == "LineString":
                        line_fc = f
                        break
            else:
                # if raw geometry passed
                if parsed.get("type") == "LineString":
                    line_fc = {"type": "Feature", "geometry": parsed, "properties": {}}
        except Exception as e:
            st.error(f"Could not parse Line GeoJSON: {str(e)}")
            line_fc = None
    elif uploaded_line is not None:
        line_fc = load_geojson_from_upload(uploaded_line)
    elif use_example and EXAMPLE_PATH.exists():
        try:
            ex = json.loads(EXAMPLE_PATH.read_text())
            for f in ex.get("features", []):
                if f.get("geometry", {}).get("type") == "LineString":
                    line_fc = f
                    break
        except Exception:
            line_fc = None

    # Load Shape/Area (pasted or example)
    shape_fc = None
    if pasted_shape and pasted_shape.strip():
        try:
            parsed = json.loads(pasted_shape)
            if parsed.get("type") == "Feature":
                shape_fc = parsed
            elif parsed.get("type") == "FeatureCollection":
                for f in parsed.get("features", []):
                    if f.get("geometry", {}).get("type") == "Polygon":
                        shape_fc = f
                        break
            else:
                if parsed.get("type") == "Polygon":
                    shape_fc = {"type": "Feature", "geometry": parsed, "properties": {}}
        except Exception as e:
            st.error(f"Could not parse Area GeoJSON: {str(e)}")
            shape_fc = None
    elif use_example and EXAMPLE_PATH.exists():
        try:
            ex = json.loads(EXAMPLE_PATH.read_text())
            for f in ex.get("features", []):
                if f.get("geometry", {}).get("type") == "Polygon":
                    shape_fc = f
                    break
        except Exception:
            shape_fc = None

    # Load Primary Turn (pasted or example)
    turn_fc = None
    if pasted_turn and pasted_turn.strip():
        try:
            turn_fc = json.loads(pasted_turn)
        except Exception as e:
            st.error(f"Could not parse Turn A GeoJSON: {str(e)}")
            turn_fc = None
    elif use_example and EXAMPLE_TURN.exists():
        try:
            turn_fc = json.loads(EXAMPLE_TURN.read_text())
        except Exception:
            turn_fc = None
    
    # Load Secondary Turn (pasted only)
    turn2_fc = None
    if pasted_turn2 and pasted_turn2.strip():
        try:
            turn2_fc = json.loads(pasted_turn2)
        except Exception as e:
            st.error(f"Could not parse Turn B GeoJSON: {str(e)}")
            turn2_fc = None

    area_feat = shape_fc
    ab_feat = line_fc

    if not area_feat or not ab_feat:
        st.info("📋 Please upload both a reference line and field area to get started, or enable 'Use example files'")

    # persist output across reruns using session_state
    output_fc = st.session_state.get("output_fc", None)

    # generate when button pressed
    if generate_btn:
        # use the separated inputs (line_fc / shape_fc) rather than an aggregated `fc`
        if not (area_feat and ab_feat):
            st.error("Please provide both an AB Line and a Shape/Area before generating.")
        else:
            try:
                result = generator.generate_rows_geojson(
                    area_feature=area_feat,
                    ab_feature=ab_feat,
                    spacing_m=spacing_m,
                    start_letter=start_letter or "S",
                    start_num=int(start_num),
                    zero_pad=bool(zero_pad),
                    dual_zone=bool(dual_zone),
                    dest_side=dest_side,
                    custom_turn_geojson=turn_fc,
                    keep_start_letter=True,
                    attach_turns_both_ends=bool(turn_at_a or turn_at_b),
                    flip_start_horizontal=flip_a_h,
                    flip_start_vertical=flip_a_v,
                    flip_end_horizontal=flip_b_h,
                    flip_end_vertical=flip_b_v,
                    secondary_turn_geojson=turn2_fc,
                    turn_side_a="A" if turn_at_a else "None",
                    turn_side_b="B" if turn_at_b else "None",
                    rotation_offset_a=rotation_a,
                    rotation_offset_b=rotation_b,
                )
                st.session_state["output_fc"] = result
                st.session_state["last_error"] = None
                output_fc = result
                st.success("✅ Rows generated successfully!")
            except Exception as e:
                st.session_state["output_fc"] = None
                st.session_state["last_error"] = str(e)
                st.exception(e)
                output_fc = None

    # Always render preview with any generated output (or None)
    preview_map = render_preview_map(area_feat, ab_feat, output_fc)
    st_folium(preview_map, width=900, height=600)

    if output_fc:
        st.markdown("### 💾 Export Results")
        
        # Export NetworkPath and NetworkDestination features (exclude area)
        filtered_features = [
            f for f in output_fc.get("features", [])
            if f.get("properties", {}).get("type") in ("NetworkPath", "NetworkDestination", "RowPath", "DestinationPoint", "TurnAttachment")
        ]
        filtered_fc = {"type": "FeatureCollection", "features": filtered_features}
        out_text = json.dumps(filtered_fc, indent=2)

        col_dl, col_copy = st.columns(2)
        with col_dl:
            st.download_button("📥 Download", data=out_text, file_name="rows_output.geojson",
                             mime="application/json", use_container_width=True)
        with col_copy:
            # embed JSON safely into the copy script using json.dumps (proper escaping)
            escaped_json = json.dumps(out_text)
            copy_button_html = (
                '<div style=\"margin-top:0px;\">\\n'
                '<button id=\"copy-btn\" style=\"width:100%;height:38px;padding:0.25rem 0.75rem;background:#ff4b4b;color:white;border:none;border-radius:0.5rem;cursor:pointer;font-size:1rem;font-weight:400;line-height:1.6;\">📋 Copy</button>\\n'
                '<div id=\"toast\" style=\"display:none;position:fixed;top:20px;right:20px;background:#21c354;color:white;padding:12px 24px;border-radius:8px;box-shadow:0 4px 6px rgba(0,0,0,0.1);z-index:9999;font-size:14px;\">✅ Copied to clipboard!</div>\\n'
                '</div>\\n'
                '<script>\\n'
                'const text = ' + escaped_json + ';\\n'
                'const btn = document.getElementById(\"copy-btn\");\\n'
                'const toast = document.getElementById(\"toast\");\\n'
                'btn.addEventListener(\"click\", async () => {\\n'
                '  try {\\n'
                '    await navigator.clipboard.writeText(text);\\n'
                '    toast.style.display = \"block\";\\n'
                '    setTimeout(()=>{toast.style.display=\"none\";},2000);\\n'
                '  } catch(e) {\\n'
                '    alert(\"Copy failed: \" + e);\\n'
                '  }\\n'
                '});\\n'
                '</script>'
            )
            st.components.v1.html(copy_button_html, height=38)

        with st.expander("📄 View Raw GeoJSON", expanded=False):
            st.text_area("GeoJSON Output", value=out_text, height=300, label_visibility="collapsed")