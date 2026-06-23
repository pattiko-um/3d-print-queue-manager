import re

def parse_bgcode(filepath):
    try:
        with open(filepath, "rb") as f:
            header = f.read(65536).decode("utf-8", errors="ignore")

        result = {}

        filament_g = re.search(
            r"filament used \[g\]=([0-9.]+)",
            header
        )
        if filament_g:
            result["filament_mass_g"] = float(filament_g.group(1))

        filament_mm = re.search(
            r"filament used \[mm\]=([0-9.]+)",
            header
        )
        if filament_mm:
            result["filament_length_m"] = float(filament_mm.group(1)) / 1000

        max_z = re.search(
            r"max_layer_z=([0-9.]+)",
            header
        )
        if max_z:
            result["size_z_mm"] = float(max_z.group(1))

        time_match = re.search(
            r"estimated printing time \(normal mode\)=([^\n\r]+)",
            header
        )
        if time_match:
            time_str = time_match.group(1).strip()

            h = m = s = 0

            mh = re.search(r"(\d+)h", time_str)
            mm = re.search(r"(\d+)m", time_str)
            ms = re.search(r"(\d+)s", time_str)

            if mh:
                h = int(mh.group(1))
            if mm:
                m = int(mm.group(1))
            if ms:
                s = int(ms.group(1))

            total_minutes = h * 60 + m + round(s / 60)

            result["time_minutes"] = total_minutes
            result["time_formatted"] = time_str

        config = {}

        printer = re.search(r"printer_model=([^\n\r]+)", header)
        if printer:
            config["printer_model"] = printer.group(1).strip()

        filament = re.search(r"filament_type=([^\n\r]+)", header)
        if filament:
            config["filament_type"] = filament.group(1).strip()

        if config:
            result["config"] = config

        return result

    except Exception as e:
        return {"error": str(e)}