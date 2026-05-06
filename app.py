"""
PrintQueue — Local 3D Print Ticket Manager
Flask + SQLite backend
"""

import os
import json
import sqlite3
import shutil
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
DB_PATH = BASE_DIR / "printqueue.db"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
PRINT_ROOT_DIR = get_env_path('PRINT_ROOT_DIR')
PRUSA_SLICER_PATH = str(get_env_path('PRUSA_SLICER_PATH')) if os.getenv('PRUSA_SLICER_PATH') else None

PRINT_ROOT_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=str(STATIC_DIR), template_folder=str(TEMPLATES_DIR))

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


def init_db():
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tickets (
            id                  INTEGER PRIMARY KEY,
            title               TEXT NOT NULL,
            requester           TEXT DEFAULT '',
            username            TEXT DEFAULT '',
            external_ticket_id  INTEGER,
            ticket_url          TEXT,
            notes               TEXT DEFAULT '',
            status              TEXT NOT NULL DEFAULT 'received'
                                CHECK(status IN ('received','awaiting_input','queued','in_process','complete','delivered')),
            priority            INTEGER NOT NULL DEFAULT 0,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS prints (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id           INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
            filename            TEXT NOT NULL,
            filepath            TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'to_do'
                                CHECK(status IN ('to_do','awaiting_input','queued','printing','complete')),
            -- STL geometry
            size_x_mm           REAL,
            size_y_mm           REAL,
            size_z_mm           REAL,
            volume_mm3          REAL,
            triangle_count      INTEGER,
            has_overhangs       INTEGER DEFAULT 0,
            overhang_area_mm2   REAL,
            support_vol_mm3     REAL,
            -- Estimates
            layer_count         INTEGER,
            filament_length_m   REAL,
            filament_mass_g     REAL,
            time_minutes        REAL,
            time_formatted      TEXT,
            -- Config snapshot (JSON)
            config_json         TEXT,
            -- Issues (JSON array)
            issues_json         TEXT,
            -- Errors
            parse_error         TEXT,
            -- Meta
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL
        );
    """)
    db.commit()
    db.close()




# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def row_to_dict(row):
    return dict(row) if row else None


def ticket_with_prints(db, ticket_id):
    ticket = row_to_dict(db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone())
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
    ticket["print_count"] = len(prints)
    ticket["total_time_minutes"] = sum(p.get("time_minutes") or 0 for p in prints)
    ticket["total_filament_g"] = sum(p.get("filament_mass_g") or 0 for p in prints)
    return ticket


def scan_and_analyze_stl(filepath_str):
    """Import estimator lazily so the server starts even if numpy is missing."""
    try:
        ext = Path(filepath_str).suffix.lower()
        if ext == ".3mf":
            from three_mf_estimator import analyze_3mf_with_prusaslicer
            return analyze_3mf_with_prusaslicer(filepath_str, PRUSA_SLICER_PATH, str(BASE_DIR / "config.ini"))

        from stl_estimator import analyze_stl_with_prusaslicer, analyze_stl
        if ext == ".stl":
            return analyze_stl_with_prusaslicer(filepath_str, PRUSA_SLICER_PATH)
        if ext == ".stp":
            from three_mf_estimator import analyze_3mf_with_prusaslicer
            return analyze_3mf_with_prusaslicer(filepath_str, PRUSA_SLICER_PATH, str(BASE_DIR / "config.ini"))

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
        t = row_to_dict(row)
        prints = db.execute(
            "SELECT id, filename, status, time_minutes, filament_mass_g, issues_json, created_at FROM prints WHERE ticket_id=? ORDER BY created_at DESC",
            (t["id"],)
        ).fetchall()
        t["prints"] = [
            {
                "id": p["id"],
                "filename": p["filename"],
                "status": p["status"],
            }
            for p in prints
        ]
        t["print_count"] = len(prints)
        t["remaining_prints"] = sum(1 for p in prints if p["status"] != "complete")
        t["remaining_time_minutes"] = sum((p["time_minutes"] or 0) for p in prints if p["status"] != "complete")
        t["remaining_filament_g"] = sum((p["filament_mass_g"] or 0) for p in prints if p["status"] != "complete")
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
    Scan PRINT_ROOT_DIR and create/link tickets with prints.
    Returns a streaming summary of created/updated tickets.
    """
    scanned = scan_print_root_directory()

    def emit(obj):
        return json.dumps(obj) + "\n"

    def generate():
        db = get_db()
        result = {
            "created_tickets": 0,
            "updated_tickets": 0,
            "added_prints": 0,
            "errors": [],
            "tickets": [],
            "files_processed": [],
        }

        yield emit({"type": "start", "message": "Scanning directories..."})

        for item in scanned:
            ticket_id = item["ticket_id"]
            username = item["username"]
            stl_files = item["stl_files"]
            ts = now_iso()

            yield emit({"type": "ticket", "ticket_id": ticket_id})

            existing = db.execute(
                "SELECT id, requester FROM tickets WHERE external_ticket_id=?",
                (ticket_id,)
            ).fetchone()

            if existing:
                ticket_row_id = existing["id"]
                result["updated_tickets"] += 1
                if not existing["requester"] and username:
                    db.execute(
                        "UPDATE tickets SET requester=?, username=?, updated_at=? WHERE id=?",
                        (username, username, ts, ticket_row_id)
                    )
                    db.commit()
            else:
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

                existing_print = db.execute(
                    "SELECT id FROM prints WHERE ticket_id=? AND filepath=?",
                    (ticket_row_id, stl_path)
                ).fetchone()

                if existing_print:
                    result["files_processed"][-1]["status"] = "skipped"
                    yield emit({"type": "file", "ticket_id": ticket_id, "filename": filename, "status": "skipped"})
                    continue

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
    allowed = {"status"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({"error": "nothing to update"}), 400
    updates["updated_at"] = now_iso()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE prints SET {set_clause} WHERE id=?", (*updates.values(), pid))
    db.commit()
    p = db.execute("SELECT * FROM prints WHERE id=?", (pid,)).fetchone()
    if not p:
        return jsonify({"error": "not found"}), 404
    return jsonify(row_to_dict(p))


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


# ---------------------------------------------------------------------------
# Stats endpoint
# ---------------------------------------------------------------------------

@app.route("/api/stats", methods=["GET"])
def stats():
    db = get_db()
    tickets = {r[0]: r[1] for r in db.execute(
        "SELECT status, COUNT(*) FROM tickets GROUP BY status").fetchall()}
    prints = {r[0]: r[1] for r in db.execute(
        "SELECT status, COUNT(*) FROM prints GROUP BY status").fetchall()}
    totals = db.execute(
        "SELECT SUM(time_minutes), SUM(filament_mass_g) FROM prints").fetchone()
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
    init_db()
    print("\n🖨️  PrintQueue running at http://localhost:5000")
    app.run(debug=True, port=5000)