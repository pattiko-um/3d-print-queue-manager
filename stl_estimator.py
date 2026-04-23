"""
STL Estimator — PrusaSlicer-style print estimation
Parses binary/ASCII STL files, computes bounding box + volume,
then estimates print time and filament using default Prusa MK4 settings.

Default config assumptions (MK4 / 0.4mm nozzle / PLA):
  - Layer height:       0.2 mm
  - Nozzle diameter:    0.4 mm
  - Infill density:     15%
  - Perimeters:         2
  - Top/bottom layers:  4
  - Print speed:        200 mm/s (internal), 45 mm/s (perimeters)
  - Filament diameter:  1.75 mm
  - Supports:           Everywhere (adds ~15% volume overhead)
  - Filament density:   1.24 g/cm³ (PLA)
"""

import struct
import math
import os


LAYER_HEIGHT = 0.2          # mm
NOZZLE_DIAMETER = 0.4       # mm
EXTRUSION_WIDTH = 0.45      # mm  (Prusa default = nozzle * 1.125)
INFILL_DENSITY = 0.15       # 15%
PERIMETERS = 2
TOP_BOTTOM_LAYERS = 4
PRINT_SPEED_INFILL = 200    # mm/s
PRINT_SPEED_PERIMETER = 45  # mm/s
TRAVEL_OVERHEAD = 1.25      # 25% time overhead for travels, retracts, etc.
SUPPORT_VOLUME_FACTOR = 1.15  # supports add ~15% filament
FILAMENT_DIAMETER = 1.75    # mm
FILAMENT_DENSITY = 1.24     # g/cm³ PLA
FILAMENT_COST_PER_KG = 20   # USD, rough default


def parse_stl(filepath):
    """Parse STL (binary or ASCII), return list of triangles as (v0, v1, v2) tuples."""
    with open(filepath, "rb") as f:
        header = f.read(80)
        try:
            count = struct.unpack("<I", f.read(4))[0]
            # Sanity check: binary STLs have exactly 50 bytes per triangle
            expected_size = 80 + 4 + count * 50
            actual_size = os.path.getsize(filepath)
            if abs(actual_size - expected_size) < 100:
                return _parse_binary(f, count)
        except Exception:
            pass

    # Fall back to ASCII
    return _parse_ascii(filepath)


def _parse_binary(f, count):
    triangles = []
    for _ in range(count):
        data = f.read(50)
        if len(data) < 50:
            break
        vals = struct.unpack("<12fH", data)
        # vals[0:3] = normal, vals[3:6] = v0, vals[6:9] = v1, vals[9:12] = v2
        v0 = vals[3:6]
        v1 = vals[6:9]
        v2 = vals[9:12]
        triangles.append((v0, v1, v2))
    return triangles


def _parse_ascii(filepath):
    triangles = []
    verts = []
    with open(filepath, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("vertex"):
                parts = line.split()
                verts.append(tuple(float(x) for x in parts[1:4]))
                if len(verts) == 3:
                    triangles.append((verts[0], verts[1], verts[2]))
                    verts = []
    return triangles


def compute_geometry(triangles):
    """Return bounding box and signed volume using divergence theorem."""
    if not triangles:
        return None

    min_x = min_y = min_z = float("inf")
    max_x = max_y = max_z = float("-inf")
    signed_volume = 0.0

    for v0, v1, v2 in triangles:
        for v in (v0, v1, v2):
            min_x = min(min_x, v[0])
            min_y = min(min_y, v[1])
            min_z = min(min_z, v[2])
            max_x = max(max_x, v[0])
            max_y = max(max_y, v[1])
            max_z = max(max_z, v[2])

        # Signed volume contribution of this triangle
        signed_volume += (
            v0[0] * (v1[1] * v2[2] - v2[1] * v1[2])
            - v1[0] * (v0[1] * v2[2] - v2[1] * v0[2])
            + v2[0] * (v0[1] * v1[2] - v1[1] * v0[2])
        ) / 6.0

    volume_mm3 = abs(signed_volume)
    size_x = max_x - min_x
    size_y = max_y - min_y
    size_z = max_z - min_z

    return {
        "volume_mm3": volume_mm3,
        "size_x": round(size_x, 2),
        "size_y": round(size_y, 2),
        "size_z": round(size_z, 2),
        "bbox_volume_mm3": size_x * size_y * size_z,
    }


def estimate_print(geo, with_supports=True):
    """
    Given geometry dict, return estimated filament (g, m) and time (minutes).
    Uses a layer-by-layer approximation model.
    """
    if not geo:
        return None

    z_height = geo["size_z"]          # mm
    layer_count = max(1, math.ceil(z_height / LAYER_HEIGHT))

    # Shell (perimeter) length per layer — approximated from cross-section
    # Cross section area ≈ bbox_volume / height
    if z_height > 0:
        cross_section_area = geo["bbox_volume_mm3"] / z_height   # mm²
    else:
        cross_section_area = geo["size_x"] * geo["size_y"]

    perimeter_per_layer = math.sqrt(cross_section_area) * 4 * PERIMETERS  # mm

    # Infill length per layer (raster pattern approximation)
    infill_length_per_layer = (cross_section_area * INFILL_DENSITY) / EXTRUSION_WIDTH

    # Top/bottom solid layers
    solid_length_per_layer = cross_section_area / EXTRUSION_WIDTH
    solid_layers = min(TOP_BOTTOM_LAYERS * 2, layer_count)
    normal_layers = max(0, layer_count - solid_layers)

    total_perimeter_length = perimeter_per_layer * layer_count
    total_infill_length = infill_length_per_layer * normal_layers
    total_solid_length = solid_length_per_layer * solid_layers

    total_path_mm = total_perimeter_length + total_infill_length + total_solid_length

    # Support estimate: if supports everywhere, add ~15% path length for support structures
    if with_supports:
        total_path_mm *= SUPPORT_VOLUME_FACTOR

    # Filament volume extruded (cylinder cross-section of extrusion width × layer height)
    extrusion_cross_section = EXTRUSION_WIDTH * LAYER_HEIGHT  # mm²
    filament_volume_mm3 = total_path_mm * extrusion_cross_section

    # Filament length fed from spool
    filament_radius = FILAMENT_DIAMETER / 2
    filament_length_mm = filament_volume_mm3 / (math.pi * filament_radius ** 2)
    filament_length_m = filament_length_mm / 1000

    # Filament mass
    filament_mass_g = (filament_volume_mm3 / 1000) * FILAMENT_DENSITY  # cm³ * g/cm³

    # Time estimate
    perimeter_time = total_perimeter_length / PRINT_SPEED_PERIMETER        # seconds
    infill_time = (total_infill_length + total_solid_length) / PRINT_SPEED_INFILL
    raw_time_s = (perimeter_time + infill_time) * TRAVEL_OVERHEAD
    time_minutes = raw_time_s / 60

    return {
        "layer_count": layer_count,
        "filament_length_m": round(filament_length_m, 2),
        "filament_mass_g": round(filament_mass_g, 1),
        "time_minutes": round(time_minutes, 1),
        "time_formatted": _format_time(time_minutes),
    }


def _format_time(minutes):
    h = int(minutes // 60)
    m = int(minutes % 60)
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def analyze_stl(filepath):
    """Full pipeline: parse → geometry → estimate. Returns a flat result dict."""
    try:
        triangles = parse_stl(filepath)
        if not triangles:
            return {"error": "No geometry found in STL"}

        geo = compute_geometry(triangles)
        est = estimate_print(geo, with_supports=True)

        return {
            "triangle_count": len(triangles),
            "size_x_mm": geo["size_x"],
            "size_y_mm": geo["size_y"],
            "size_z_mm": geo["size_z"],
            "volume_mm3": round(geo["volume_mm3"], 2),
            "layer_count": est["layer_count"],
            "filament_length_m": est["filament_length_m"],
            "filament_mass_g": est["filament_mass_g"],
            "time_minutes": est["time_minutes"],
            "time_formatted": est["time_formatted"],
            # config snapshot stored for display
            "config": {
                "layer_height_mm": LAYER_HEIGHT,
                "nozzle_diameter_mm": NOZZLE_DIAMETER,
                "infill_pct": int(INFILL_DENSITY * 100),
                "supports": "everywhere",
                "perimeters": PERIMETERS,
                "filament": "PLA 1.75mm",
            },
        }
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 2:
        print("Usage: python stl_estimator.py file.stl")
        sys.exit(1)
    result = analyze_stl(sys.argv[1])
    print(json.dumps(result, indent=2))