"""
PrintQueue — Local 3D Print Ticket Manager
Flask + SQLite backend
"""

import os
import json
import sqlite3
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, g, Response, stream_with_context
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def get_env_path(name):
    raw = os.getenv(name, "") or ""
    raw = raw.strip()
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        raw = raw[1:-1].strip()
    raw = os.path.expanduser(os.path.expandvars(raw))
    raw = os.path.normpath(raw)
    return Path(raw)

BASE_DIR = Path(__file__).parent
# Database directory and path
DB_DIR = BASE_DIR / "db"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "printqueue.db"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
PRINT_ROOT_DIR = get_env_path('PRINT_ROOT_DIR')
PRUSA_SLICER_PATH = str(get_env_path('PRUSA_SLICER_PATH')) if os.getenv('PRUSA_SLICER_PATH') else None

PRINT_ROOT_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=str(STATIC_DIR), template_folder=str(TEMPLATES_DIR))

# Use the migration-based initializer in db/init_db.py
from db.init_db import init_db

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def row_to_dict(row):
    return dict(row) if row else None


def normalize_ticket_status(ticket):
    if not ticket:
        return ticket
    if ticket.get("status") in ("delivered", "archived"):
        ticket["status"] = "closed"
    if ticket.get("status") == "closed" and not ticket.get("closed_at"):
        ticket["closed_at"] = ticket.get("updated_at")
    if not ticket.get("closed_at") and ticket.get("archived_at"):
        ticket["closed_at"] = ticket.get("archived_at")
    return ticket


def ticket_with_prints(db, ticket_id):
    ticket = normalize_ticket_status(row_to_dict(db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()))
    if not ticket:
        return None
    prints = [row_to_dict(r) for r in db.execute(
        "SELECT * FROM prints WHERE ticket_id=? ORDER BY id", (ticket_id,)).fetchall()]
    for p in prints:
        if p.get("config_json"):
            p["config"] = json.loads(p["config_json"])
        if p.get("issues_json"):
            p["issues"] = json.loads(p["issues_json"])
        p.pop("config_json", None)
        p.pop("issues_json", None)
    
    ticket["prints"] = prints
    ticket["print_count"] = sum((p.get("quantity", 1) or 1) for p in prints)
    
    # Calculate totals accounting for quantities
    total_time = 0
    total_filament = 0
    remaining_copies = 0
    remaining_time = 0
    remaining_filament = 0
    
    for p in prints:
        qty = p.get("quantity", 1) or 1
        qty_completed = p.get("quantity_completed", 0) or 0
        time = p.get("time_minutes") or 0
        filament = p.get("filament_mass_g") or 0
        status = p.get("status", "to_do")
        
        # Total is based on all copies in this print
        total_time += time * qty
        total_filament += filament * qty
        
        # Remaining depends on status
        if status == "complete":
            # All copies are complete
            pass
        else:
            # Count remaining copies (not yet completed)
            remaining_copies += qty - qty_completed
            remaining_time += time * (qty - qty_completed)
            remaining_filament += filament * (qty - qty_completed)
    
    ticket["total_time_minutes"] = total_time
    ticket["total_filament_g"] = total_filament
    ticket["remaining_prints"] = remaining_copies
    ticket["remaining_time_minutes"] = remaining_time
    ticket["remaining_filament_g"] = remaining_filament
    return ticket


def scan_and_analyze_stl(filepath_str):
    """Import estimator lazily so the server starts even if numpy is missing."""
    try:
        ext = Path(filepath_str).suffix.lower()
        if ext == ".3mf":
            from three_mf_estimator import analyze_3mf_with_prusaslicer
            return analyze_3mf_with_prusaslicer(filepath_str, PRUSA_SLICER_PATH, str(BASE_DIR / "prusa_configs" / "default.ini"))

        from stl_estimator import analyze_stl_with_prusaslicer, analyze_stl
        if ext == ".stl":
            return analyze_stl_with_prusaslicer(filepath_str, PRUSA_SLICER_PATH)
        if ext == ".stp":
            from three_mf_estimator import analyze_3mf_with_prusaslicer
            return analyze_3mf_with_prusaslicer(filepath_str, PRUSA_SLICER_PATH, str(BASE_DIR / "prusa_configs" / "default.ini"))

        return {"error": f"Unsupported file type: {ext}"}
    except ImportError as e:
        return {"error": f"estimator unavailable: {e}"}


def build_ticket_url(ticket_id):
    """Build TeamDynamix URL for a ticket."""
    return f"https://teamdynamix.umich.edu/TDNext/Apps/46/Tickets/TicketDet.aspx?TicketID={ticket_id}"


def scan_print_root_directory():
    """
    Scan PRINT_ROOT_DIR for subdirectories matching pattern: <ticket_id> - <username>
    Returns list of dicts with ticket_id, username, and model files.
    """
    import re
    results = []

    if not PRINT_ROOT_DIR.exists():
        return results
    
    for subdir in PRINT_ROOT_DIR.iterdir():
        if not subdir.is_dir():
            continue
        
        # Parse directory name: "<ticket_id> - <username>"
        # More flexible pattern to handle varying spacing
        match = re.match(r"^(\d+)\s*-\s*(.+)$", subdir.name.strip())

        if not match:
            continue
        
        ticket_id = int(match.group(1))
        username = match.group(2).strip()
        
        # Find all STL, STP, and 3MF files in this subdirectory
        stl_files = sorted([f for f in subdir.glob("*") if f.is_file() and f.suffix.lower() in ('.stl', '.stp', '.3mf')])
        
        if stl_files:
            results.append({
                "ticket_id": ticket_id,
                "username": username,
                "directory": str(subdir),
                "stl_files": [str(f) for f in stl_files],
            })
    
    return results


# ---------------------------------------------------------------------------
# Routes — Tickets
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(str(BASE_DIR), "index.html")


@app.route("/api/tickets", methods=["GET"])
def list_tickets():
    db = get_db()
    rows = db.execute("SELECT * FROM tickets ORDER BY priority DESC, id DESC").fetchall()
    tickets = []
    for row in rows:
        t = normalize_ticket_status(row_to_dict(row))
        prints = db.execute(
            "SELECT id, filename, status, time_minutes, filament_mass_g, quantity, quantity_completed, issues_json, created_at FROM prints WHERE ticket_id=? ORDER BY created_at DESC",
            (t["id"],)
        ).fetchall()
        t["prints"] = [
            {
                "id": p["id"],
                "filename": p["filename"],
                "status": p["status"],
                "quantity": p["quantity"],
                "quantity_completed": p["quantity_completed"],
            }
            for p in prints
        ]
        t["print_count"] = sum((p["quantity"] or 1) for p in prints)
        
        # Calculate remaining accounting for quantities
        remaining_prints = 0
        remaining_time = 0
        remaining_filament = 0
        for p in prints:
            qty = p["quantity"] or 1
            qty_completed = p["quantity_completed"] or 0
            time = p["time_minutes"] or 0
            filament = p["filament_mass_g"] or 0
            status = p["status"]
            
            if status != "complete":
                remaining_copies = qty - qty_completed
                remaining_prints += remaining_copies
                remaining_time += time * remaining_copies
                remaining_filament += filament * remaining_copies
        
        t["remaining_prints"] = remaining_prints
        t["remaining_time_minutes"] = remaining_time
        t["remaining_filament_g"] = remaining_filament
        
        issues = set()
        for p in prints:
            if p["issues_json"]:
                try:
                    parsed = json.loads(p["issues_json"])
                    if isinstance(parsed, list):
                        issues.update(str(item) for item in parsed if item)
                except json.JSONDecodeError:
                    issues.add(str(p["issues_json"]))
        t["issues"] = sorted(issues)
        tickets.append(t)
    return jsonify(tickets)


@app.route("/api/tickets", methods=["POST"])
def create_ticket():
    data = request.json or {}
    if not data.get("title"):
        return jsonify({"error": "title is required"}), 400
    ts = now_iso()
    db = get_db()
    
    # If external_ticket_id is provided, use it; otherwise use auto-increment
    external_ticket_id = data.get("external_ticket_id")
    ticket_url = build_ticket_url(external_ticket_id) if external_ticket_id else None
    
    cur = db.execute(
        "INSERT INTO tickets (title, requester, username, external_ticket_id, ticket_url, notes, status, priority, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (data["title"], data.get("requester", ""), data.get("username", ""),
         external_ticket_id, ticket_url, data.get("notes", ""),
         data.get("status", "received"), data.get("priority", 0), ts, ts)
    )
    db.commit()
    return jsonify(ticket_with_prints(db, cur.lastrowid)), 201


@app.route("/api/tickets/<int:tid>", methods=["GET"])
def get_ticket(tid):
    ticket = ticket_with_prints(get_db(), tid)
    if not ticket:
        return jsonify({"error": "not found"}), 404
    return jsonify(ticket)


@app.route("/api/tickets/<int:tid>", methods=["PATCH"])
def update_ticket(tid):
    db = get_db()
    data = request.json or {}
    allowed = {"title", "requester", "notes", "status", "priority"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({"error": "nothing to update"}), 400
    # Accept a few frontend status aliases and map them to canonical DB values
    status_aliases = {
        "awaiting_filament": "awaiting_input",
        "in_progress": "in_process",
        "todo": "queued",
        "done": "complete",
    }
    if updates.get("status") in status_aliases:
        updates["status"] = status_aliases[updates["status"]]

    if updates.get("status") in ("delivered", "archived"):
        updates["status"] = "closed"
    if updates.get("status") == "closed":
        updates["closed_at"] = now_iso()
    elif "status" in updates:
        updates["closed_at"] = None
    updates["updated_at"] = now_iso()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE tickets SET {set_clause} WHERE id=?", (*updates.values(), tid))
    db.commit()
    ticket = ticket_with_prints(db, tid)
    if not ticket:
        return jsonify({"error": "not found"}), 404
    return jsonify(ticket)


@app.route("/api/tickets/<int:tid>", methods=["DELETE"])
def delete_ticket(tid):
    db = get_db()
    db.execute("DELETE FROM tickets WHERE id=?", (tid,))
    db.commit()
    return jsonify({"deleted": tid})


# ---------------------------------------------------------------------------
# Routes — STL directory scanning
# ---------------------------------------------------------------------------

@app.route("/api/import-from-directory", methods=["POST"])
def import_from_directory():
    """
    Scan PRINT_ROOT_DIR and create tickets for new directories only.
    Existing ticket directories are skipped so the scan does not update
    existing tickets or add prints to already-imported tickets.
    """
    scanned = scan_print_root_directory()

    def emit(obj):
        return json.dumps(obj) + "\n"

    def generate():
        db = get_db()
        result = {
            "created_tickets": 0,
            "updated_tickets": 0,
            "skipped_tickets": 0,
            "added_prints": 0,
            "errors": [],
            "tickets": [],
            "files_processed": [],
        }

        yield emit({"type": "start", "message": "Scanning for new ticket directories..."})

        for item in scanned:
            ticket_id = item["ticket_id"]
            username = item["username"]
            stl_files = item["stl_files"]
            ts = now_iso()

            yield emit({"type": "ticket", "ticket_id": ticket_id})

            existing = db.execute(
                "SELECT id FROM tickets WHERE external_ticket_id=?",
                (ticket_id,)
            ).fetchone()

            if existing:
                result["skipped_tickets"] += 1
                yield emit({"type": "ticket", "ticket_id": ticket_id, "status": "skipped"})
                continue

            title = f"TDX #{ticket_id} - {username}" if username else f"TDX #{ticket_id}"
            ticket_url = build_ticket_url(ticket_id)
            db.execute(
                "INSERT INTO tickets (id, title, requester, username, external_ticket_id, ticket_url, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (ticket_id, title, username, username, ticket_id, ticket_url, "received", ts, ts)
            )
            db.commit()
            ticket_row_id = ticket_id
            result["created_tickets"] += 1

            for stl_path in stl_files:
                filename = Path(stl_path).name
                result["files_processed"].append({"filename": filename, "status": "processing", "path": stl_path, "ticket_id": ticket_id})
                yield emit({"type": "file", "ticket_id": ticket_id, "filename": filename, "status": "processing"})

                analysis = scan_and_analyze_stl(stl_path)
                error = analysis.get("error")

                db.execute(
                    """INSERT INTO prints
                        (ticket_id, filename, filepath, status,
                         size_x_mm, size_y_mm, size_z_mm, volume_mm3, triangle_count,
                         has_overhangs, overhang_area_mm2, support_vol_mm3,
                         layer_count, filament_length_m, filament_mass_g,
                         time_minutes, time_formatted, config_json, issues_json, parse_error,
                         created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        ticket_row_id, filename, stl_path, "to_do",
                        analysis.get("size_x_mm"), analysis.get("size_y_mm"), analysis.get("size_z_mm"),
                        analysis.get("volume_mm3"), analysis.get("triangle_count"),
                        analysis.get("has_overhangs"), analysis.get("overhang_area_mm2"), analysis.get("support_vol_mm3"),
                        analysis.get("layer_count"), analysis.get("filament_length_m"),
                        analysis.get("filament_mass_g"), analysis.get("time_minutes"),
                        analysis.get("time_formatted"),
                        json.dumps(analysis.get("config")) if analysis.get("config") else None,
                        json.dumps(analysis.get("issues")) if analysis.get("issues") else None,
                        error,
                        ts, ts,
                    ),
                )
                db.commit()
                result["added_prints"] += 1
                result["files_processed"][-1]["status"] = "completed" if not error else "error"
                yield emit({"type": "file", "ticket_id": ticket_id, "filename": filename, "status": result["files_processed"][-1]["status"]})

                if error:
                    result["errors"].append({"file": filename, "error": error})

            db.execute("UPDATE tickets SET updated_at=? WHERE id=?", (ts, ticket_row_id))
            db.commit()
            result["tickets"].append(ticket_with_prints(db, ticket_row_id))

        yield emit({"type": "summary", "summary": result})

    return Response(stream_with_context(generate()), mimetype='application/x-ndjson')


# ---------------------------------------------------------------------------
# Routes — Prints
# ---------------------------------------------------------------------------

@app.route("/api/tickets/<int:tid>/prints", methods=["POST"])
def add_print(tid):
    """
    Add a print to a ticket by filepath.
    Body: { "filepath": "/Volumes/Scratch/3D Print Test/123 - user/part.stl" }
    Supports .stl, .stp, and .3mf models.
    """
    db = get_db()
    ticket = db.execute("SELECT id FROM tickets WHERE id=?", (tid,)).fetchone()
    if not ticket:
        return jsonify({"error": "ticket not found"}), 404

    data = request.json or {}
    filepath = data.get("filepath", "").strip()
    if not filepath:
        return jsonify({"error": "filepath is required"}), 400

    filepath_obj = Path(filepath)
    if not filepath_obj.exists():
        return jsonify({"error": f"file not found: {filepath}"}), 404
    
    # Analyze model file
    result = scan_and_analyze_stl(filepath)
    error = result.get("error")
    ts = now_iso()
    filename = filepath_obj.name

    cur = db.execute(
        """INSERT INTO prints
            (ticket_id, filename, filepath, status,
             size_x_mm, size_y_mm, size_z_mm, volume_mm3, triangle_count,
             has_overhangs, overhang_area_mm2, support_vol_mm3,
             layer_count, filament_length_m, filament_mass_g,
             time_minutes, time_formatted, config_json, issues_json, parse_error,
             created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            tid, filename, filepath, "to_do",
            result.get("size_x_mm"), result.get("size_y_mm"), result.get("size_z_mm"),
            result.get("volume_mm3"), result.get("triangle_count"),
            result.get("has_overhangs"), result.get("overhang_area_mm2"), result.get("support_vol_mm3"),
            result.get("layer_count"), result.get("filament_length_m"),
            result.get("filament_mass_g"), result.get("time_minutes"),
            result.get("time_formatted"),
            json.dumps(result.get("config")) if result.get("config") else None,
            json.dumps(result.get("issues")) if result.get("issues") else None,
            error,
            ts, ts,
        ),
    )
    db.execute("UPDATE tickets SET updated_at=? WHERE id=?", (ts, tid))
    db.commit()
    return jsonify(ticket_with_prints(db, tid)), 201


@app.route("/api/prints/<int:pid>", methods=["PATCH"])
def update_print(pid):
    db = get_db()
    data = request.json or {}
    
    # Get current print
    p = row_to_dict(db.execute("SELECT * FROM prints WHERE id=?", (pid,)).fetchone())
    if not p:
        return jsonify({"error": "not found"}), 404
    
    new_status = data.get("status", p["status"])
    new_quantity = data.get("quantity")
    quantity_completed = data.get("quantity_completed")
    
    # Validate quantity updates
    if new_quantity is not None:
        new_quantity = int(new_quantity)
        if new_quantity < 1:
            return jsonify({"error": "quantity must be >= 1"}), 400
    
    # Validate quantity_completed updates
    if quantity_completed is not None:
        quantity_completed = int(quantity_completed)
        if quantity_completed < 0:
            return jsonify({"error": "quantity_completed must be >= 0"}), 400
        if quantity_completed > (p.get("quantity", 1) or 1):
            return jsonify({"error": "quantity_completed cannot exceed quantity"}), 400
    
    ts = now_iso()
    
    # Build updates
    updates = {}
    if new_status != p["status"]:
        updates["status"] = new_status
    if new_quantity is not None:
        updates["quantity"] = new_quantity
    if quantity_completed is not None:
        updates["quantity_completed"] = quantity_completed
    
    if not updates:
        return jsonify({"error": "nothing to update"}), 400
    
    updates["updated_at"] = ts
    set_clause = ", ".join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE prints SET {set_clause} WHERE id=?", (*updates.values(), pid))
    
    db.commit()
    
    # Return updated ticket with all prints
    p_updated = row_to_dict(db.execute("SELECT * FROM prints WHERE id=?", (pid,)).fetchone())
    if p_updated:
        ticket_id = p_updated["ticket_id"]
        return jsonify(ticket_with_prints(db, ticket_id))
    return jsonify({"error": "not found"}), 404


@app.route("/api/prints/<int:pid>/increment-completed", methods=["POST"])
def increment_completed(pid):
    db = get_db()
    
    # Get current print
    p = row_to_dict(db.execute("SELECT * FROM prints WHERE id=?", (pid,)).fetchone())
    if not p:
        return jsonify({"error": "not found"}), 404
    
    qty = p.get("quantity", 1) or 1
    qty_completed = p.get("quantity_completed", 0) or 0
    
    if qty_completed >= qty:
        return jsonify({"error": "already completed all copies"}), 400
    
    qty_completed += 1
    
    ts = now_iso()
    db.execute("UPDATE prints SET quantity_completed=?, updated_at=? WHERE id=?", (qty_completed, ts, pid))
    db.commit()
    
    ticket_id = p["ticket_id"]
    return jsonify(ticket_with_prints(db, ticket_id))


@app.route("/api/prints/<int:pid>/decrement-completed", methods=["POST"])
def decrement_completed(pid):
    db = get_db()
    
    # Get current print
    p = row_to_dict(db.execute("SELECT * FROM prints WHERE id=?", (pid,)).fetchone())
    if not p:
        return jsonify({"error": "not found"}), 404
    
    qty_completed = p.get("quantity_completed", 0) or 0
    
    if qty_completed <= 0:
        return jsonify({"error": "no completed copies to decrement"}), 400
    
    qty_completed -= 1
    
    ts = now_iso()
    db.execute("UPDATE prints SET quantity_completed=?, updated_at=? WHERE id=?", (qty_completed, ts, pid))
    db.commit()
    
    ticket_id = p["ticket_id"]
    return jsonify(ticket_with_prints(db, ticket_id))


@app.route("/api/prints/<int:pid>", methods=["DELETE"])
def delete_print(pid):
    db = get_db()
    p = db.execute("SELECT ticket_id FROM prints WHERE id=?", (pid,)).fetchone()
    if not p:
        return jsonify({"error": "not found"}), 404
    tid = p["ticket_id"]
    db.execute("DELETE FROM prints WHERE id=?", (pid,))
    db.execute("UPDATE tickets SET updated_at=? WHERE id=?", (now_iso(), tid))
    db.commit()
    return jsonify(ticket_with_prints(db, tid))


@app.route("/api/prints/<int:pid>/reanalyze", methods=["POST"])
def reanalyze_print(pid):
    """Re-run STL analysis (e.g. after file was replaced)."""
    db = get_db()
    p = row_to_dict(db.execute("SELECT * FROM prints WHERE id=?", (pid,)).fetchone())
    if not p:
        return jsonify({"error": "not found"}), 404

    result = scan_and_analyze_stl(p["filepath"])
    error = result.get("error")
    ts = now_iso()
    db.execute("""UPDATE prints SET
        size_x_mm=?, size_y_mm=?, size_z_mm=?, volume_mm3=?, triangle_count=?,
        layer_count=?, filament_length_m=?, filament_mass_g=?,
        time_minutes=?, time_formatted=?, config_json=?, issues_json=?, parse_error=?, updated_at=?
        WHERE id=?""",
        (
            result.get("size_x_mm"), result.get("size_y_mm"), result.get("size_z_mm"),
            result.get("volume_mm3"), result.get("triangle_count"),
            result.get("layer_count"), result.get("filament_length_m"),
            result.get("filament_mass_g"), result.get("time_minutes"),
            result.get("time_formatted"),
            json.dumps(result.get("config")) if result.get("config") else None,
            json.dumps(result.get("issues")) if result.get("issues") else None,
            error, ts, pid,
        ),
    )
    db.commit()
    return jsonify(ticket_with_prints(db, p["ticket_id"]))


@app.route("/api/prints/<int:pid>/open-in-prusaslicer", methods=["POST"])
def open_in_prusaslicer(pid):
    """Open the print file in PrusaSlicer."""
    db = get_db()
    p = row_to_dict(db.execute("SELECT filepath FROM prints WHERE id=?", (pid,)).fetchone())
    if not p:
        return jsonify({"error": "not found"}), 404
    
    filepath = p["filepath"]
    if not os.path.exists(filepath):
        return jsonify({"error": f"file not found: {filepath}"}), 404
    
    if not PRUSA_SLICER_PATH:
        return jsonify({"error": "PrusaSlicer path not configured"}), 500
    
    try:
        # Launch PrusaSlicer with the file; if a local default.ini config exists, load it
        config_path = str(BASE_DIR / "prusa_configs" / "default.ini")
        if os.path.exists(config_path):
            subprocess.Popen([PRUSA_SLICER_PATH, '--load', config_path, filepath])
        else:
            subprocess.Popen([PRUSA_SLICER_PATH, filepath])
        return jsonify({"message": "Opened in PrusaSlicer"})
    except Exception as e:
        return jsonify({"error": f"Failed to open PrusaSlicer: {str(e)}"}), 500


# ---------------------------------------------------------------------------
# Stats endpoint
# ---------------------------------------------------------------------------

@app.route("/api/tickets/<int:tid>/scan-for-updates", methods=["POST"])
def scan_ticket_for_updates(tid):
    """
    Scan the ticket's directory for new/updated/deleted print files.
    - New files: add as prints in "to_do" status
    - Updated files (mtime changed): rescan and move to "to_do"
    - Missing files: delete the prints
    """
    db = get_db()
    ticket = row_to_dict(db.execute("SELECT external_ticket_id FROM tickets WHERE id=?", (tid,)).fetchone())
    if not ticket:
        return jsonify({"error": "ticket not found"}), 404
    
    external_id = ticket.get("external_ticket_id")
    if not external_id:
        return jsonify({"error": "ticket has no external ID"}), 400
    
    # Find ticket directory
    import re
    ticket_dir = None
    for subdir in Path(PRINT_ROOT_DIR).iterdir():
        if not subdir.is_dir():
            continue
        match = re.match(r"^(\d+)\s*-\s*(.+)$", subdir.name.strip())
        if match and int(match.group(1)) == external_id:
            ticket_dir = subdir
            break
    
    if not ticket_dir:
        return jsonify({"error": "ticket directory not found"}), 404
    
    # List files in directory
    stl_files = sorted([f for f in ticket_dir.glob("*") if f.is_file() and f.suffix.lower() in ('.stl', '.stp', '.3mf')])
    disk_files = {f.name: f for f in stl_files}
    
    # Get existing prints
    existing_prints = db.execute(
        "SELECT id, filename, filepath, status, updated_at FROM prints WHERE ticket_id=?", (tid,)
    ).fetchall()
    existing_by_name = {dict(p)["filename"]: dict(p) for p in existing_prints}
    
    ts = now_iso()
    result = {"added": [], "updated": [], "removed": [], "errors": []}
    
    # Check for new or updated files
    for filename, filepath in disk_files.items():
        if filename in existing_by_name:
            # File exists in DB - check if it's been modified
            existing = existing_by_name[filename]
            db_mtime = existing.get("updated_at")
            
            # Check file mtime
            file_stat = filepath.stat()
            file_mtime = file_stat.st_mtime
            db_mtime_ts = datetime.fromisoformat(db_mtime.replace('Z', '+00:00')).timestamp() if db_mtime else 0
            
            if file_mtime > db_mtime_ts:
                # File has been updated - rescan it
                analysis = scan_and_analyze_stl(str(filepath))
                error = analysis.get("error")
                
                db.execute("""UPDATE prints SET
                    size_x_mm=?, size_y_mm=?, size_z_mm=?, volume_mm3=?, triangle_count=?,
                    layer_count=?, filament_length_m=?, filament_mass_g=?,
                    time_minutes=?, time_formatted=?, config_json=?, issues_json=?, parse_error=?, 
                    status=?, updated_at=?
                    WHERE id=?""",
                    (
                        analysis.get("size_x_mm"), analysis.get("size_y_mm"), analysis.get("size_z_mm"),
                        analysis.get("volume_mm3"), analysis.get("triangle_count"),
                        analysis.get("layer_count"), analysis.get("filament_length_m"),
                        analysis.get("filament_mass_g"), analysis.get("time_minutes"),
                        analysis.get("time_formatted"),
                        json.dumps(analysis.get("config")) if analysis.get("config") else None,
                        json.dumps(analysis.get("issues")) if analysis.get("issues") else None,
                        error, "to_do", ts, existing["id"],
                    ),
                )
                result["updated"].append(filename)
                if error:
                    result["errors"].append({"file": filename, "error": error})
        else:
            # New file - add as print
            analysis = scan_and_analyze_stl(str(filepath))
            error = analysis.get("error")
            
            db.execute(
                """INSERT INTO prints
                    (ticket_id, filename, filepath, status,
                     size_x_mm, size_y_mm, size_z_mm, volume_mm3, triangle_count,
                     has_overhangs, overhang_area_mm2, support_vol_mm3,
                     layer_count, filament_length_m, filament_mass_g,
                     time_minutes, time_formatted, config_json, issues_json, parse_error,
                     created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    tid, filename, str(filepath), "to_do",
                    analysis.get("size_x_mm"), analysis.get("size_y_mm"), analysis.get("size_z_mm"),
                    analysis.get("volume_mm3"), analysis.get("triangle_count"),
                    analysis.get("has_overhangs"), analysis.get("overhang_area_mm2"), analysis.get("support_vol_mm3"),
                    analysis.get("layer_count"), analysis.get("filament_length_m"),
                    analysis.get("filament_mass_g"), analysis.get("time_minutes"),
                    analysis.get("time_formatted"),
                    json.dumps(analysis.get("config")) if analysis.get("config") else None,
                    json.dumps(analysis.get("issues")) if analysis.get("issues") else None,
                    error,
                    ts, ts,
                ),
            )
            result["added"].append(filename)
            if error:
                result["errors"].append({"file": filename, "error": error})
    
    # Check for deleted files
    for filename, print_data in existing_by_name.items():
        if filename not in disk_files:
            db.execute("DELETE FROM prints WHERE id=?", (print_data["id"],))
            result["removed"].append(filename)
    
    db.commit()
    return jsonify({
        "summary": result,
        "ticket": ticket_with_prints(db, tid)
    })


@app.route("/api/stats", methods=["GET"])
def stats():
    db = get_db()
    tickets = {r[0]: r[1] for r in db.execute(
        "SELECT status, COUNT(*) FROM tickets GROUP BY status").fetchall()}
    
    # For print stats, count by status considering quantities
    print_rows = db.execute(
        "SELECT status, SUM(COALESCE(quantity, 1)) FROM prints GROUP BY status"
    ).fetchall()
    prints = {r[0]: r[1] for r in print_rows}
    
    # For totals, calculate time and filament accounting for quantities
    totals = db.execute(
        "SELECT SUM(COALESCE(time_minutes, 0) * COALESCE(quantity, 1)), "
        "       SUM(COALESCE(filament_mass_g, 0) * COALESCE(quantity, 1)) "
        "FROM prints"
    ).fetchone()
    
    return jsonify({
        "tickets": tickets,
        "prints": prints,
        "total_time_minutes": totals[0] or 0,
        "total_filament_g": totals[1] or 0,
    })


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db(DB_PATH, BASE_DIR)
    print("\n🖨️  PrintQueue running at http://localhost:5000")
    app.run(debug=True, port=5000)