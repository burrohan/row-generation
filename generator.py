import json
import math
from typing import Optional, List, Tuple
from shapely.geometry import shape, mapping, LineString, Point, Polygon, GeometryCollection
from shapely.ops import transform, snap, split
from shapely.affinity import rotate, translate
import pyproj
import uuid

# Global tolerance for geometric operations (in meters for projected CRS)
SNAP_TOLERANCE = 0.01  # 1cm tolerance for snapping operations


def _get_utm_crs(lon, lat):
    """Get the appropriate UTM CRS for a given lon/lat coordinate."""
    utm_zone = int((lon + 180) / 6) + 1
    hemisphere = 'north' if lat >= 0 else 'south'
    return f"EPSG:{32600 + utm_zone if hemisphere == 'north' else 32700 + utm_zone}"


def _to_utm(geom, lon, lat):
    """Project geometry from WGS84 to appropriate UTM zone for accurate metric calculations."""
    utm_crs = _get_utm_crs(lon, lat)
    project = pyproj.Transformer.from_crs("EPSG:4326", utm_crs, always_xy=True).transform
    return transform(project, geom)


def _from_utm(geom, lon, lat):
    """Project geometry from UTM back to WGS84."""
    utm_crs = _get_utm_crs(lon, lat)
    project = pyproj.Transformer.from_crs(utm_crs, "EPSG:4326", always_xy=True).transform
    return transform(project, geom)


def _rotate(geom, angle_deg, origin=(0, 0)):
    return rotate(geom, angle_deg, origin=origin, use_radians=False)


def _snap_point_to_line(point: Point, line: LineString, tolerance: float = SNAP_TOLERANCE) -> Point:
    """Snap a point to the nearest location on a line if within tolerance."""
    # Project point onto line
    distance_along = line.project(point)
    snapped = line.interpolate(distance_along)
    
    # Only snap if within tolerance
    if point.distance(snapped) <= tolerance:
        return snapped
    return point


def _split_line_at_point(line: LineString, point: Point, tolerance: float = SNAP_TOLERANCE) -> List[LineString]:
    """Split a line at a point, with tolerance-based snapping."""
    # Snap point to line first
    snapped_point = _snap_point_to_line(point, line, tolerance)
    
    # Check if point is at start or end (within tolerance)
    if snapped_point.distance(Point(line.coords[0])) <= tolerance:
        return [line]
    if snapped_point.distance(Point(line.coords[-1])) <= tolerance:
        return [line]
    
    # Split the line
    try:
        result = split(line, snapped_point.buffer(tolerance))
        if result.geom_type == 'MultiLineString':
            return list(result.geoms)
        elif result.geom_type == 'LineString':
            return [result]
        else:
            return [line]
    except:
        return [line]


def _label_sequence(start_letter: str, start_num: int, index: int, zero_pad: bool, keep_start_letter: bool = True):
    """
    Generate a single label.
    If keep_start_letter is True the letter portion stays constant (e.g., A01, A02, A03).
    Otherwise the letter cycles with index (A01, B02, C03...).
    """
    if keep_start_letter:
        letter = start_letter.upper()
    else:
        letter_ord = ord(start_letter.upper())
        letter = chr(((letter_ord - 65) + index) % 26 + 65)
    num = start_num + index
    if zero_pad:
        num_s = f"{num:02d}"
    else:
        num_s = str(num)
    return f"{letter}{num_s}"


def _attach_custom_turn(template_geom, template_anchor, dest_point, angle_deg: float = 0.0,
                        flip_horizontal: bool = False, flip_vertical: bool = False):
    """
    Attach a custom-turn geometry template to a destination point, using a provided anchor point
    from the template (both provided in the same metric CRS, EPSG:3857).

    Steps:
    - Translate the template so that the template_anchor is at the origin.
    - Apply horizontal/vertical flips if requested.
    - Rotate the template around the origin by `angle_deg`.
    - Translate the rotated template so its anchor sits at `dest_point`.

    This ignores the template's original world location and uses its shape + provided anchor.
    """
    # center template around the anchor (so anchor moves to origin)
    centered = translate(template_geom, xoff=-template_anchor.x, yoff=-template_anchor.y)
    
    # apply flips if requested (scale by -1 on the respective axis)
    flipped = centered
    if flip_horizontal:
        from shapely.affinity import scale
        flipped = scale(flipped, xfact=-1, yfact=1, origin=(0, 0))
    if flip_vertical:
        from shapely.affinity import scale
        flipped = scale(flipped, xfact=1, yfact=-1, origin=(0, 0))
    
    # rotate around origin to align with row direction
    rotated = rotate(flipped, angle_deg, origin=(0, 0), use_radians=False)
    # translate so anchor (now at origin) is moved to dest_point coordinates
    attached = translate(rotated, xoff=dest_point.x, yoff=dest_point.y)
    return attached


def _find_user_line_in_clipped(user_line_m_rot, clipped_lines, tolerance=1.0):
    """
    Find which clipped line corresponds to the user's input AB line.
    Returns the index of the matching line, or None if not found.
    """
    user_centroid = user_line_m_rot.centroid
    for idx, line in enumerate(clipped_lines):
        line_centroid = line.centroid
        dist = user_centroid.distance(line_centroid)
        if dist < tolerance:
            return idx
    return None


def generate_rows_geojson(
    area_feature: dict,
    ab_feature: dict,
    spacing_m: float = 6.0,
    start_letter: str = "F",
    start_num: int = 1,
    zero_pad: bool = True,
    dual_zone: bool = False,
    dest_side: str = "A",
    custom_turn_geojson: Optional[dict] = None,
    keep_start_letter: bool = True,
    attach_turns_both_ends: bool = False,
    flip_start_horizontal: bool = False,
    flip_start_vertical: bool = False,
    flip_end_horizontal: bool = False,
    flip_end_vertical: bool = False,
    secondary_turn_geojson: Optional[dict] = None,
    turn_side_a: str = "A",
    turn_side_b: str = "B",
    rotation_offset_a: float = 0.0,
    rotation_offset_b: float = 0.0,
) -> dict:
    """
    Generate row paths with consistent spatial reference handling.
    
    All geometric operations are performed in EPSG:3857 (Web Mercator) for accurate
    metric-based spacing calculations. Results are transformed back to WGS84 for output.
    
    Args:
        area_feature: GeoJSON Feature (Polygon) in WGS84
        ab_feature: GeoJSON Feature (LineString) in WGS84 defining reference line A->B
        spacing_m: Row spacing in meters (applied in projected space)
        ... (other parameters as before)
    
    Returns:
        FeatureCollection with NetworkPath and NetworkDestination features in WGS84
    """
    # Parse input geometries (assumed to be in WGS84)
    area_geom = shape(area_feature["geometry"])
    ab_geom = shape(ab_feature["geometry"])

    # Get centroid for determining appropriate UTM zone
    centroid = ab_geom.centroid
    center_lon, center_lat = centroid.x, centroid.y

    # PROJECT TO METRIC CRS (UTM) for accurate metric-based operations
    area_m = _to_utm(area_geom, center_lon, center_lat)
    ab_m = _to_utm(ab_geom, center_lon, center_lat)

    # Extract A and B endpoints in metric space
    ax, ay = ab_m.coords[0]
    bx, by = ab_m.coords[-1]
    
    # Calculate rotation angle to make AB horizontal
    angle_rad = math.atan2(by - ay, bx - ax)
    angle_deg = math.degrees(angle_rad)

    # ROTATE to align AB with horizontal axis (consistent origin: point A)
    rotation_origin = (ax, ay)
    area_rot = _rotate(area_m, -angle_deg, origin=rotation_origin)
    ab_rot = _rotate(ab_m, -angle_deg, origin=rotation_origin)

    # Use AB line's Y coordinate as the reference (row index 0)
    # This ensures the user's AB line is always included as row 0
    ab_rot_coords = list(ab_rot.coords)
    # Use average of both endpoints to handle any floating-point imprecision in rotation
    reference_y = (ab_rot_coords[0][1] + ab_rot_coords[-1][1]) / 2.0
    
    # Get polygon bounds for generating parallel lines
    minx, miny, maxx, maxy = area_rot.bounds
    pad = (maxx - minx) * 2.0  # Horizontal padding for full-width lines
    
    # Calculate number of rows needed above and below reference
    rows_below = int(math.ceil((reference_y - miny) / spacing_m)) + 2
    rows_above = int(math.ceil((maxy - reference_y) / spacing_m)) + 2
    
    # Generate parallel lines at EXACT spacing intervals from reference_y
    row_lines_with_index = []
    for i in range(-rows_below, rows_above + 1):
        y_pos = reference_y + (i * spacing_m)  # Exact metric spacing
        line = LineString([(minx - pad, y_pos), (maxx + pad, y_pos)])
        row_lines_with_index.append((i, line))
    
    # Clip lines to polygon boundary and maintain row index
    clipped_rows = []
    
    for row_index, line in row_lines_with_index:
        # Special handling for row 0: use the actual AB line
        if row_index == 0:
            clipped_rows.append((row_index, ab_rot))
            continue
        
        # Clip to polygon
        intersection = line.intersection(area_rot)
        
        if intersection.is_empty:
            continue
        
        # Handle different geometry types from intersection
        if intersection.geom_type == 'LineString':
            if intersection.length > SNAP_TOLERANCE:
                clipped_rows.append((row_index, intersection))
        elif intersection.geom_type == 'MultiLineString':
            for segment in intersection.geoms:
                if segment.length > SNAP_TOLERANCE:
                    clipped_rows.append((row_index, segment))
        elif intersection.geom_type == 'GeometryCollection':
            for geom in intersection.geoms:
                if geom.geom_type == 'LineString' and geom.length > SNAP_TOLERANCE:
                    clipped_rows.append((row_index, geom))
    
    # Sort by row index for consistent ordering
    clipped_rows.sort(key=lambda x: x[0])

    # Prepare output feature lists
    features = []
    dest_features = []

    # Store A and B reference points in rotated metric space
    a_rot = Point(ab_rot_coords[0])
    b_rot = Point(ab_rot_coords[-1])

    # Helper function to prepare turn geometry
    def prepare_turn_template(turn_geojson):
        if not turn_geojson:
            return None, None
            
        if turn_geojson.get("type") == "FeatureCollection":
            geom = shape(turn_geojson["features"][0]["geometry"])
        elif turn_geojson.get("type") == "Feature":
            geom = shape(turn_geojson["geometry"])
        else:
            geom = shape(turn_geojson)

        # Determine anchor in the template: use the first coordinate (user-provided convention).
        if geom.geom_type == "Point":
            anchor = geom
        elif geom.geom_type == "LineString":
            anchor = Point(list(geom.coords)[0])
        elif geom.geom_type == "Polygon":
            anchor = Point(list(geom.exterior.coords)[0])
        else:
            # fallback to centroid if shape has no simple coordinates
            anchor = geom.centroid

        # project both template and anchor to UTM (metric CRS)
        template_m = _to_utm(geom, center_lon, center_lat)
        anchor_m = _to_utm(anchor, center_lon, center_lat)
        return template_m, anchor_m
    
    # prepare primary turn geometry
    custom_m_template, custom_anchor_m = prepare_turn_template(custom_turn_geojson)
    
    # prepare secondary turn geometry (for opposite end)
    secondary_m_template, secondary_anchor_m = prepare_turn_template(secondary_turn_geojson)

    # Generate timestamp once for all features
    import datetime
    current_time = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    
    # Process each clipped row
    for row_index, seg in clipped_rows:
        # Orient segment consistently with AB direction (left to right)
        seg_coords = list(seg.coords)
        seg_start_x = seg_coords[0][0]
        seg_end_x = seg_coords[-1][0]
        
        # If AB goes left-to-right (bx > ax), ensure segment also goes left-to-right
        if (bx > ax and seg_start_x > seg_end_x) or (bx < ax and seg_start_x < seg_end_x):
            seg = LineString(seg_coords[::-1])
            seg_coords = list(seg.coords)

        # Generate label
        if dual_zone:
            label1 = _label_sequence(start_letter, start_num, row_index, zero_pad, keep_start_letter)
            label2 = _label_sequence(start_letter, start_num, row_index + 1, zero_pad, keep_start_letter)
            label = f"{label1}/{label2}"
        else:
            label = _label_sequence(start_letter, start_num, row_index, zero_pad, keep_start_letter)

        # TRANSFORM BACK: Unrotate and project to WGS84
        seg_unrot = _rotate(seg, angle_deg, origin=rotation_origin)
        seg_wgs = _from_utm(seg_unrot, center_lon, center_lat)
        
        # Create NetworkPath feature with standardized properties
        path_feature = {
            "type": "Feature",
            "id": str(uuid.uuid4()),
            "properties": {
                "type": "NetworkPath",
                "createDate": current_time,
                "updateDate": current_time,
                "version": 1,
                "direction": "two_way",
                "speedLimit": "1.2",
                "enabled": True
            },
            "geometry": mapping(seg_wgs),
        }
        features.append(path_feature)

        # Determine destination endpoint based on proximity to A or B
        p1_rot = Point(seg_coords[0])
        p2_rot = Point(seg_coords[-1])
        dist1_to_a = p1_rot.distance(a_rot)
        dist2_to_a = p2_rot.distance(a_rot)
        
        if dest_side.upper() == "A":
            dest_pt_rot = p1_rot if dist1_to_a < dist2_to_a else p2_rot
            use_start = (dist1_to_a < dist2_to_a)
        else:
            dest_pt_rot = p1_rot if dist1_to_a > dist2_to_a else p2_rot
            use_start = (dist1_to_a > dist2_to_a)

        # CRITICAL: Use exact coordinate from the line segment for topological connection
        # Transform the chosen endpoint back to WGS84
        dest_unrot = _rotate(dest_pt_rot, angle_deg, origin=rotation_origin)
        dest_wgs_temp = _from_utm(dest_unrot, center_lon, center_lat)
        
        # Snap to exact line coordinate to ensure topology
        seg_wgs_coords = list(seg_wgs.coords)
        dest_coord = seg_wgs_coords[0] if use_start else seg_wgs_coords[-1]
        dest_wgs = Point(dest_coord)
        
        # Create NetworkDestination feature with standardized properties
        dest_feature = {
            "type": "Feature",
            "id": str(uuid.uuid4()),
            "properties": {
                "type": "NetworkDestination",
                "groupMpath": "",
                "createDate": current_time,
                "updateDate": current_time,
                "version": 1,
                "name": label,
                "groupId": ""
            },
            "geometry": mapping(dest_wgs),
        }
        dest_features.append(dest_feature)

        # Calculate row angle in unrotated metric space for turn attachment
        seg_coords_unrot = list(seg_unrot.coords)
        seg_dx = seg_coords_unrot[-1][0] - seg_coords_unrot[0][0]
        seg_dy = seg_coords_unrot[-1][1] - seg_coords_unrot[0][1]
        row_angle_deg = math.degrees(math.atan2(seg_dy, seg_dx))
        
        # Attach turn at A end if requested
        if turn_side_a.upper() == "A":
            a_template = custom_m_template if custom_m_template is not None else secondary_m_template
            a_anchor = custom_anchor_m if custom_anchor_m is not None else secondary_anchor_m
            
            if a_template is not None and a_anchor is not None:
                # Get A endpoint in rotated space
                turn_a_pt_rot = p1_rot if dist1_to_a < dist2_to_a else p2_rot
                # Transform to unrotated metric space for attachment
                turn_a_unrot = _rotate(turn_a_pt_rot, angle_deg, origin=rotation_origin)
                
                # Apply rotation offset and flips for A end
                turn_a_angle = row_angle_deg + rotation_offset_a
                
                attached_a = _attach_custom_turn(
                    a_template, a_anchor, turn_a_unrot,
                    angle_deg=turn_a_angle,
                    flip_horizontal=flip_start_horizontal,
                    flip_vertical=flip_start_vertical
                )
                
                attached_a_wgs = _from_utm(attached_a, center_lon, center_lat)
                turn_a_feature = {
                    "type": "Feature",
                    "id": str(uuid.uuid4()),
                    "properties": {
                        "type": "NetworkPath",
                        "createDate": current_time,
                        "updateDate": current_time,
                        "version": 1,
                        "direction": "two_way",
                        "speedLimit": "1.2",
                        "enabled": True
                    },
                    "geometry": mapping(attached_a_wgs),
                }
                features.append(turn_a_feature)
        
        # Attach turn at B end if requested
        if turn_side_b.upper() == "B":
            b_template = secondary_m_template if secondary_m_template is not None else custom_m_template
            b_anchor = secondary_anchor_m if secondary_anchor_m is not None else custom_anchor_m
            
            if b_template is not None and b_anchor is not None:
                # Get B endpoint in rotated space
                turn_b_pt_rot = p1_rot if dist1_to_a > dist2_to_a else p2_rot
                # Transform to unrotated metric space for attachment
                turn_b_unrot = _rotate(turn_b_pt_rot, angle_deg, origin=rotation_origin)
                
                # Apply rotation offset and flips for B end (180° base rotation)
                turn_b_angle = row_angle_deg + 180 + rotation_offset_b
                
                attached_b = _attach_custom_turn(
                    b_template, b_anchor, turn_b_unrot,
                    angle_deg=turn_b_angle,
                    flip_horizontal=flip_end_horizontal,
                    flip_vertical=flip_end_vertical
                )
                
                attached_b_wgs = _from_utm(attached_b, center_lon, center_lat)
                turn_b_feature = {
                    "type": "Feature",
                    "id": str(uuid.uuid4()),
                    "properties": {
                        "type": "NetworkPath",
                        "createDate": current_time,
                        "updateDate": current_time,
                        "version": 1,
                        "direction": "two_way",
                        "speedLimit": "1.2",
                        "enabled": True
                    },
                    "geometry": mapping(attached_b_wgs),
                }
                features.append(turn_b_feature)

    out_fc = {
        "type": "FeatureCollection",
        "features": features + dest_features,
    }
    return out_fc


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python generator.py input.geojson")
        sys.exit(1)
    inpath = sys.argv[1]
    with open(inpath, "r") as f:
        data = json.load(f)
    # expect polygon and line in same collection
    area = None
    ab = None
    for feat in data.get("features", []):
        geom_type = feat.get("geometry", {}).get("type", "")
        if geom_type == "Polygon" and area is None:
            area = feat
        if geom_type == "LineString" and ab is None:
            ab = feat
    if area is None or ab is None:
        print("Input must contain one Polygon and one LineString (AB).")
        sys.exit(1)
    out = generate_rows_geojson(area, ab, spacing_m=6.0)
    print(json.dumps(out))