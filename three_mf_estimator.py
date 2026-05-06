import os
import re
import subprocess
import tempfile


def analyze_3mf_with_prusaslicer(filepath, prusaslicer_path="prusaslicer", config_path="./config.ini"):
    """Use PrusaSlicer CLI to estimate a 3MF model and return a flat result dict."""
    try:
        info = _parse_model_info(filepath, prusaslicer_path)
        if not info:
            return {"error": "Unable to read 3MF metadata from PrusaSlicer"}

        estimates = _export_gcode_and_parse(filepath, prusaslicer_path, config_path)
        if not estimates:
            return {"error": "PrusaSlicer did not generate estimates"}

        return {
            "triangle_count":     info.get("number_of_facets"),
            "size_x_mm":          _round_maybe(info.get("size_x")),
            "size_y_mm":          _round_maybe(info.get("size_y")),
            "size_z_mm":          _round_maybe(info.get("size_z")),
            "volume_mm3":         _round_maybe(info.get("volume")),
            "surface_area_mm2":   None,
            "has_overhangs":      False,
            "overhang_area_mm2":  None,
            "support_vol_mm3":    None,
            "layer_count":        estimates.get("layer_count"),
            "filament_length_m":  estimates.get("filament_length_m"),
            "filament_mass_g":    estimates.get("filament_mass_g"),
            "time_minutes":       estimates.get("time_minutes"),
            "time_formatted":     estimates.get("time_formatted"),
            "issues":             [],
            "config": {
                "method":             "PrusaSlicer G-code generation",
                "supports":           "PrusaSlicer automatic",
            },
        }
    except Exception as exc:
        return {"error": str(exc)}


def _parse_model_info(filepath, prusaslicer_path):
    if not os.path.isfile(filepath):
        return None

    cmd = [prusaslicer_path, "--info", filepath]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return None

    info = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, value = [part.strip() for part in line.split("=", 1)]
        if key in {"size_x", "size_y", "size_z", "volume"}:
            try:
                info[key] = float(value)
            except ValueError:
                continue
        elif key == "number_of_facets":
            try:
                info[key] = int(value)
            except ValueError:
                continue
    return info if info else None


def _export_gcode_and_parse(filepath, prusaslicer_path, config_path):
    with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False) as tmp_gcode:
        gcode_path = tmp_gcode.name

    temp_config_path = None
    try:
        cmd = [prusaslicer_path]
        if config_path and os.path.isfile(config_path):
            temp_config_path = _create_temp_config_override(config_path)
            cmd += ["--load", config_path, "--load", temp_config_path]
        elif config_path:
            temp_config_path = _create_temp_config_override(None)
            cmd += ["--load", temp_config_path]
        else:
            cmd += ["--load", temp_config_path] if temp_config_path else []

        bed_center = _bed_center_from_config(config_path)
        if bed_center:
            cmd += ["--center", f"{bed_center[0]},{bed_center[1]}"]
        cmd += ["--export-gcode", "--output", gcode_path, filepath]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return None
        if not os.path.isfile(gcode_path) or os.path.getsize(gcode_path) == 0:
            return None

        return parse_gcode_estimates(gcode_path)
    finally:
        if temp_config_path and os.path.isfile(temp_config_path):
            os.unlink(temp_config_path)
        if os.path.isfile(gcode_path):
            os.unlink(gcode_path)


def _create_temp_config_override(source_config_path):
    overrides = {
        "binary_gcode": "0",
        "gcode_comments": "1",
        "gcode_flavor": "marlin2",
    }
    lines = []
    if source_config_path and os.path.isfile(source_config_path):
        with open(source_config_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    lines.append(line)
                    continue
                key = stripped.split("=", 1)[0].strip()
                if key in overrides:
                    lines.append(f"{key} = {overrides[key]}\n")
                    overrides.pop(key)
                else:
                    lines.append(line)
    if overrides:
        for key, value in overrides.items():
            lines.append(f"{key} = {value}\n")

    temp = tempfile.NamedTemporaryFile(suffix=".ini", delete=False, mode="w", encoding="utf-8")
    temp.writelines(lines)
    temp.close()
    return temp.name


def _bed_center_from_config(config_path):
    if not config_path or not os.path.isfile(config_path):
        return None

    with open(config_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if not line.strip().startswith("bed_shape"):
                continue
            _, value = line.split("=", 1)
            value = value.strip()
            points = [p.strip() for p in value.split(",") if p.strip()]
            xs = []
            ys = []
            for point in points:
                match = re.match(r"([0-9.+-]+)x([0-9.+-]+)", point)
                if match:
                    xs.append(float(match.group(1)))
                    ys.append(float(match.group(2)))
            if xs and ys:
                return ((max(xs) + min(xs)) / 2.0, (max(ys) + min(ys)) / 2.0)
    return None


def parse_gcode_estimates(gcode_path):
    with open(gcode_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    estimates = {}
    time_match = re.search(r"(?:;\s*)?estimated printing time \(normal mode\)\s*=\s*(.+)", content)
    if time_match:
        time_str = time_match.group(1).strip()
        minutes = parse_time_string(time_str)
        if minutes is not None:
            estimates["time_minutes"] = round(minutes, 1)
            estimates["time_formatted"] = time_str

    length_match = re.search(r"(?:;\s*)?filament used \[mm\]\s*=\s*([\d.]+)", content)
    if length_match:
        length_mm = float(length_match.group(1))
        estimates["filament_length_m"] = round(length_mm / 1000, 2)

    mass_match = re.search(r"(?:;\s*)?filament used \[g\]\s*=\s*([\d.]+)", content)
    if mass_match:
        estimates["filament_mass_g"] = round(float(mass_match.group(1)), 1)

    layer_match = re.search(r"(?:;\s*)?total layers count\s*=\s*(\d+)", content)
    if layer_match:
        estimates["layer_count"] = int(layer_match.group(1))

    return estimates if estimates else None


def parse_time_string(time_str):
    hours = 0
    minutes = 0
    seconds = 0
    parts = time_str.split()
    for part in parts:
        if part.endswith("h"):
            hours = int(part[:-1])
        elif part.endswith("m"):
            minutes = int(part[:-1])
        elif part.endswith("s"):
            seconds = int(part[:-1])
    return hours * 60 + minutes + seconds / 60


def _round_maybe(value):
    if value is None:
        return None
    try:
        return round(value, 2)
    except Exception:
        return value
