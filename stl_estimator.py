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
  - Supports:           Everywhere (adds ~25% filament + time overhead)
  - Filament density:   1.24 g/cm³ (PLA)

Estimation model (v2):
  Filament is derived from actual mesh volume and surface area, NOT bounding box.
  - Shell volume   = surface_area × wall_thickness  (exact surface area from triangles)
  - Infill volume  = mesh_volume × infill_density   (exact volume from divergence theorem)
  - Top/btm volume = bbox XY footprint × top_bottom_layers × layer_height × 2
  Time is derived from total path length (filament volume → path length) split by
  perimeter fraction vs infill fraction, weighted by their respective speeds.
"""

import struct
import math
import os
import subprocess
import tempfile
import re


LAYER_HEIGHT = 0.2          # mm
NOZZLE_DIAMETER = 0.4       # mm
EXTRUSION_WIDTH = 0.45      # mm  (Prusa default = nozzle * 1.125)
INFILL_DENSITY = 0.15       # 15%
PERIMETERS = 2
TOP_BOTTOM_LAYERS = 4
PRINT_SPEED_INFILL = 200    # mm/s
PRINT_SPEED_PERIMETER = 45  # mm/s
TRAVEL_OVERHEAD = 1.20      # 20% time overhead for travels, retracts, etc.
SUPPORT_OVERHEAD = 1.25     # supports everywhere add ~25% filament + time
FILAMENT_DIAMETER = 1.75    # mm
FILAMENT_DENSITY = 1.24     # g/cm³ PLA
FILAMENT_COST_PER_KG = 20   # USD, rough default


OVERHANG_THRESHOLD = -0.707   # cos(135°) — face normals below this Z are overhangs
                               # equivalent to PrusaSlicer's default 45° threshold
SUPPORT_DENSITY    = 0.15     # support infill density (15% — Prusa default)
SUPPORT_Z_DISTANCE = 0.2      # mm gap between support top and part (one layer)

# Bed size for validation (Prusa MK4)
BED_SIZE_X = 250  # mm
BED_SIZE_Y = 220  # mm
BED_SIZE_Z = 270  # mm

# Issue thresholds
MIN_VOLUME_CM3 = 1.0  # Unreasonably small if volume < 1 cm³
MIN_DIMENSION_MM = 5.0  # Unreasonably small if any dimension < 5mm


def parse_stl(filepath):
    """
    Parse STL (binary or ASCII).
    Returns list of (normal, v0, v1, v2) tuples where each is a 3-tuple of floats.
    For binary STLs the stored normals are used directly.
    For ASCII STLs normals are computed from the vertex cross-product.
    """
    with open(filepath, "rb") as f:
        f.read(80)  # header
        try:
            count = struct.unpack("<I", f.read(4))[0]
            expected_size = 80 + 4 + count * 50
            actual_size = os.path.getsize(filepath)
            if abs(actual_size - expected_size) < 100:
                return _parse_binary(f, count)
        except Exception:
            pass
    return _parse_ascii(filepath)


def _parse_binary(f, count):
    """Read binary STL triangles. Each record: 12 bytes normal + 36 bytes verts + 2 bytes attr."""
    triangles = []
    for _ in range(count):
        data = f.read(50)
        if len(data) < 50:
            break
        vals = struct.unpack("<12fH", data)
        normal = vals[0:3]          # stored face normal (may be zero — validated below)
        v0, v1, v2 = vals[3:6], vals[6:9], vals[9:12]

        # Some exporters write zero normals; compute from vertices in that case.
        if normal[0] == 0.0 and normal[1] == 0.0 and normal[2] == 0.0:
            normal = _compute_normal(v0, v1, v2)

        triangles.append((normal, v0, v1, v2))
    return triangles


def _parse_ascii(filepath):
    """Read ASCII STL. Normals are computed from vertices (stored normals often unreliable)."""
    triangles = []
    verts = []
    with open(filepath, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("vertex"):
                parts = line.split()
                verts.append(tuple(float(x) for x in parts[1:4]))
                if len(verts) == 3:
                    normal = _compute_normal(verts[0], verts[1], verts[2])
                    triangles.append((normal, verts[0], verts[1], verts[2]))
                    verts = []
    return triangles


def _compute_normal(v0, v1, v2):
    """Return unit face normal via cross product of two edges. Returns (0,0,0) for degenerate."""
    e1 = (v1[0]-v0[0], v1[1]-v0[1], v1[2]-v0[2])
    e2 = (v2[0]-v0[0], v2[1]-v0[1], v2[2]-v0[2])
    cx = e1[1]*e2[2] - e1[2]*e2[1]
    cy = e1[2]*e2[0] - e1[0]*e2[2]
    cz = e1[0]*e2[1] - e1[1]*e2[0]
    length = math.sqrt(cx*cx + cy*cy + cz*cz)
    if length == 0:
        return (0.0, 0.0, 0.0)
    return (cx/length, cy/length, cz/length)


def compute_geometry(triangles):
    """
    Return bounding box, exact mesh volume (divergence theorem),
    exact surface area (sum of triangle areas), and overhang metrics.

    Overhang detection: a face is an overhang if its normal has a Z component
    below OVERHANG_THRESHOLD (default -0.707, i.e. > 45° past horizontal).
    We accumulate:
      - overhang_area_mm2  : total XY-projected area of overhanging faces
      - overhang_z_*       : Z range of overhang faces (to estimate support height)
    """
    if not triangles:
        return None

    # Pre-compute z_min for bed-contact face filtering
    min_z = min(v[2] for _, v0, v1, v2 in triangles for v in (v0, v1, v2))

    min_x = min_y = float("inf")
    max_x = max_y = max_z = float("-inf")
    signed_volume  = 0.0
    surface_area   = 0.0
    overhang_area  = 0.0          # XY-projected area of overhang faces
    overhang_z_min = float("inf")
    overhang_z_max = float("-inf")

    for normal, v0, v1, v2 in triangles:
        for v in (v0, v1, v2):
            min_x = min(min_x, v[0]); max_x = max(max_x, v[0])
            min_y = min(min_y, v[1]); max_y = max(max_y, v[1])
            max_z = max(max_z, v[2])

        # Signed volume via divergence theorem
        signed_volume += (
            v0[0] * (v1[1] * v2[2] - v2[1] * v1[2])
            - v1[0] * (v0[1] * v2[2] - v2[1] * v0[2])
            + v2[0] * (v0[1] * v1[2] - v1[1] * v0[2])
        ) / 6.0

        # Triangle area via cross product magnitude
        e1 = (v1[0]-v0[0], v1[1]-v0[1], v1[2]-v0[2])
        e2 = (v2[0]-v0[0], v2[1]-v0[1], v2[2]-v0[2])
        cx = e1[1]*e2[2] - e1[2]*e2[1]
        cy = e1[2]*e2[0] - e1[0]*e2[2]
        cz = e1[0]*e2[1] - e1[1]*e2[0]
        tri_area = math.sqrt(cx*cx + cy*cy + cz*cz) / 2.0
        surface_area += tri_area

        # Overhang: face normal Z < threshold AND face isn't resting on the bed.
        # Faces whose centroid is within one layer height of the model's z_min
        # are bed-contact faces — they sit on the print surface and need no support.
        nz = normal[2]
        if nz < OVERHANG_THRESHOLD:
            face_z = (v0[2] + v1[2] + v2[2]) / 3.0
            if face_z > min_z + LAYER_HEIGHT:   # not a bed-contact face
                overhang_area += tri_area * abs(nz)
                overhang_z_min = min(overhang_z_min, face_z)
                overhang_z_max = max(overhang_z_max, face_z)

    volume_mm3 = abs(signed_volume)
    size_x = max_x - min_x
    size_y = max_y - min_y
    size_z = max_z - min_z

    has_overhangs = overhang_area > 0
    return {
        "volume_mm3":          volume_mm3,
        "surface_area_mm2":    surface_area,
        "size_x":              round(size_x, 2),
        "size_y":              round(size_y, 2),
        "size_z":              round(size_z, 2),
        "bbox_xy_area_mm2":    size_x * size_y,
        # Overhang / support fields
        "has_overhangs":       has_overhangs,
        "overhang_area_mm2":   round(overhang_area, 2),
        "overhang_z_min":      round(overhang_z_min, 2) if has_overhangs else None,
        "overhang_z_max":      round(overhang_z_max, 2) if has_overhangs else None,
    }


def estimate_print(geo, with_supports=True):
    """
    Volume + surface-area based estimation (v3).

    Filament breakdown:
      shell_vol    = lateral_surface_area × wall_thickness
      infill_vol   = (mesh_volume - shell_vol) × infill_density
      topbtm_vol   = bbox XY footprint × top_bottom_layers × layer_height × 2 faces
      support_vol  = overhang_area × avg_support_height × support_density
                     (geometry-derived; zero if no overhangs detected)

    Time: shell/infill/support paths weighted by their speeds.
      Perimeters: 0.50× nominal (always accel-limited).
      Infill: scales with sqrt(xy_area) toward nominal (longer moves on bigger parts).
      Supports: printed at infill speed, single-wall, low density.
    """
    if not geo:
        return None

    z_height   = geo["size_z"]
    mesh_vol   = geo["volume_mm3"]
    surf_area  = geo["surface_area_mm2"]
    xy_area    = geo["bbox_xy_area_mm2"]

    layer_count = max(1, math.ceil(z_height / LAYER_HEIGHT))

    # ---- Part filament volumes -------------------------------------------
    wall_thickness = PERIMETERS * EXTRUSION_WIDTH
    lateral_sa     = max(surf_area * 0.30, surf_area - 2 * xy_area)
    shell_vol      = lateral_sa * wall_thickness
    interior_vol   = max(0.0, mesh_vol - shell_vol)
    infill_vol     = interior_vol * INFILL_DENSITY
    topbtm_vol     = xy_area * (TOP_BOTTOM_LAYERS * LAYER_HEIGHT) * 2

    part_vol = shell_vol + infill_vol + topbtm_vol

    # ---- Support volume (geometry-derived) -------------------------------
    support_vol  = 0.0
    support_path = 0.0

    if with_supports and geo.get("has_overhangs"):
        overhang_area = geo["overhang_area_mm2"]   # XY-projected overhang area
        z_min = geo["overhang_z_min"]
        z_max = geo["overhang_z_max"]

        # Average support column height: from bed (z=0) up to the midpoint of
        # the overhang Z range. This approximates "supports everywhere" — columns
        # grow from the bed to wherever the overhang sits.
        # We use the lower bound of the overhang Z range as the ceiling height,
        # because supports must reach the lowest overhanging face.
        avg_support_height = max(LAYER_HEIGHT, z_min)

        # Support volume = overhang footprint × height × density
        # (supports are open lattice at SUPPORT_DENSITY, not solid)
        support_vol = overhang_area * avg_support_height * SUPPORT_DENSITY

        # Support interface layers (2 solid layers at top of each support column)
        # printed at full density — adds a small but real amount of material
        interface_vol = overhang_area * (2 * LAYER_HEIGHT)
        support_vol += interface_vol

        # Support path length (single-wall perimeter + infill at support density)
        extrusion_xs = EXTRUSION_WIDTH * LAYER_HEIGHT
        support_path = support_vol / extrusion_xs

    total_filament_vol = part_vol + support_vol

    # ---- Filament length + mass ------------------------------------------
    extrusion_xs       = EXTRUSION_WIDTH * LAYER_HEIGHT
    filament_radius    = FILAMENT_DIAMETER / 2.0
    filament_length_mm = total_filament_vol / (math.pi * filament_radius ** 2)
    filament_length_m  = filament_length_mm / 1000.0
    filament_mass_g    = (total_filament_vol / 1000.0) * FILAMENT_DENSITY

    # ---- Time estimate ---------------------------------------------------
    shell_path  = shell_vol  / extrusion_xs
    infill_path = (infill_vol + topbtm_vol) / extrusion_xs

    eff_perim  = PRINT_SPEED_PERIMETER * 0.50
    char_len   = math.sqrt(xy_area)
    eff_infill = min(PRINT_SPEED_INFILL, char_len * 2.5)

    # Supports print at infill speed (no perimeter wall on support columns)
    time_s = (
        shell_path  / eff_perim  +
        infill_path / eff_infill +
        support_path / eff_infill
    ) * 1.15   # 15% misc overhead

    time_minutes = time_s / 60.0

    return {
        "layer_count":        layer_count,
        "filament_length_m":  round(filament_length_m, 2),
        "filament_mass_g":    round(filament_mass_g, 1),
        "time_minutes":       round(time_minutes, 1),
        "time_formatted":     _format_time(time_minutes),
        "support_vol_mm3":    round(support_vol, 1),
        "has_overhangs":      geo.get("has_overhangs", False),
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

        # Detect issues
        issues = []
        
        if geo["size_x"] > BED_SIZE_X or geo["size_y"] > BED_SIZE_Y or geo["size_z"] > BED_SIZE_Z:
            issues.append("Too large for print bed")
        volume_cm3 = geo["volume_mm3"] / 1000.0
        if volume_cm3 < MIN_VOLUME_CM3:
            issues.append("Unreasonably small volume")
        if geo["size_x"] < MIN_DIMENSION_MM or geo["size_y"] < MIN_DIMENSION_MM or geo["size_z"] < MIN_DIMENSION_MM:
            issues.append("Unreasonably small dimensions")

        return {
            "triangle_count":     len(triangles),
            "size_x_mm":          geo["size_x"],
            "size_y_mm":          geo["size_y"],
            "size_z_mm":          geo["size_z"],
            "volume_mm3":         round(geo["volume_mm3"], 2),
            "surface_area_mm2":   round(geo["surface_area_mm2"], 2),
            # Overhang / support
            "has_overhangs":      geo["has_overhangs"],
            "overhang_area_mm2":  geo["overhang_area_mm2"],
            "support_vol_mm3":    est["support_vol_mm3"],
            # Estimates
            "layer_count":        est["layer_count"],
            "filament_length_m":  est["filament_length_m"],
            "filament_mass_g":    est["filament_mass_g"],
            "time_minutes":       est["time_minutes"],
            "time_formatted":     est["time_formatted"],
            # Issues
            "issues":             issues,
            # Config snapshot
            "config": {
                "layer_height_mm":    LAYER_HEIGHT,
                "nozzle_diameter_mm": NOZZLE_DIAMETER,
                "infill_pct":         int(INFILL_DENSITY * 100),
                "supports":           "everywhere (geometry-detected)" if geo["has_overhangs"] else "none needed",
                "perimeters":         PERIMETERS,
                "filament":           "PLA 1.75mm",
            },
        }
    except Exception as e:
        return {"error": str(e)}


def analyze_stl_with_prusaslicer(filepath, prusaslicer_path="prusaslicer", config_path="./prusa_configs/default.ini"):
    print("here")
    """
    Use PrusaSlicer CLI to generate G-code and extract accurate estimates.
    Falls back to geometry-based estimation if PrusaSlicer fails.
    """
    try:
        # First, get geometry for basic info
        triangles = parse_stl(filepath)
        if not triangles:
            return {"error": "No geometry found in STL"}
        geo = compute_geometry(triangles)

        # Try PrusaSlicer
        with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False) as tmp:
            gcode_path = tmp.name
        cmd = [prusaslicer_path, "--export-gcode", "--output", gcode_path, filepath]
        if config_path:
            cmd = [prusaslicer_path, "--load", config_path, "--export-gcode", "--output", gcode_path, filepath]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if result.returncode != 0:
            return analyze_stl(filepath)

        estimates = parse_gcode_estimates(gcode_path)
        os.unlink(gcode_path)

        if not estimates:
            return analyze_stl(filepath)

        return {
            "triangle_count":     len(triangles),
            "size_x_mm":          geo["size_x"],
            "size_y_mm":          geo["size_y"],
            "size_z_mm":          geo["size_z"],
            "volume_mm3":         round(geo["volume_mm3"], 2),
            "surface_area_mm2":   round(geo["surface_area_mm2"], 2),
            "has_overhangs":      geo["has_overhangs"],
            "overhang_area_mm2":  geo["overhang_area_mm2"],
            "layer_count":        estimates.get("layer_count"),
            "filament_length_m":  estimates.get("filament_length_m"),
            "filament_mass_g":    estimates.get("filament_mass_g"),
            "time_minutes":       estimates.get("time_minutes"),
            "time_formatted":     estimates.get("time_formatted"),
            "issues":             [],  # PrusaSlicer doesn't provide geometry validation
            "config": {
                "method":             "PrusaSlicer G-code generation",
                "layer_height_mm":    LAYER_HEIGHT,
                "nozzle_diameter_mm": NOZZLE_DIAMETER,
                "infill_pct":         int(INFILL_DENSITY * 100),
                "supports":           "PrusaSlicer automatic",
                "perimeters":         PERIMETERS,
                "filament":           "PLA 1.75mm",
            },
        }
    except Exception:
        return analyze_stl(filepath)


def parse_gcode_estimates(gcode_path):
    """
    Parse PrusaSlicer G-code for time and filament estimates.
    Returns dict with time_minutes, filament_length_m, filament_mass_g, etc.
    """
    with open(gcode_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    estimates = {}

    # Time: ; estimated printing time (normal mode) = 1h 23m 45s
    # or: estimated printing time (normal mode)=6h 15m 42s
    time_match = re.search(r"(?:;\s*)?estimated printing time \(normal mode\)\s*=\s*(.+)", content)
    if time_match:
        time_str = time_match.group(1).strip()
        minutes = parse_time_string(time_str)
        if minutes:
            estimates["time_minutes"] = round(minutes, 1)
            estimates["time_formatted"] = time_str

    # Filament length: ; filament used [mm] = 1234.56
    # or: filament used [mm]=51877.73
    length_match = re.search(r"(?:;\s*)?filament used \[mm\]\s*=\s*([\d.]+)", content)
    if length_match:
        length_mm = float(length_match.group(1))
        estimates["filament_length_m"] = round(length_mm / 1000, 2)

    # Filament mass: ; filament used [g] = 15.2
    # or: filament used [g]=154.73
    mass_match = re.search(r"(?:;\s*)?filament used \[g\]\s*=\s*([\d.]+)", content)
    if mass_match:
        estimates["filament_mass_g"] = round(float(mass_match.group(1)), 1)

    # Layer count: ; total layers count = 123
    layer_match = re.search(r"(?:;\s*)?total layers count\s*=\s*(\d+)", content)
    if layer_match:
        estimates["layer_count"] = int(layer_match.group(1))

    return estimates if estimates else None


def parse_time_string(time_str):
    """Parse '1h 23m 45s' into minutes."""
    hours = 0
    minutes = 0
    seconds = 0
    
    parts = time_str.split()
    for i, part in enumerate(parts):
        if part.endswith('h'):
            hours = int(part[:-1])
        elif part.endswith('m'):
            minutes = int(part[:-1])
        elif part.endswith('s'):
            seconds = int(part[:-1])
    
    return hours * 60 + minutes + seconds / 60


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 2:
        print("Usage: python stl_estimator.py file.stl")
        sys.exit(1)
    result = analyze_stl(sys.argv[1])
    print(json.dumps(result, indent=2))