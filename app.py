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
from flask import Flask, request, jsonify, send_from_directory, g

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
STL_DIR = BASE_DIR / "stl_files"          # shared directory for STL files
DB_PATH = BASE_DIR / "printqueue.db"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

STL_DIR.mkdir(exist_ok=True)

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
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            requester   TEXT DEFAULT '',
            notes       TEXT DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'todo'
                        CHECK(status IN ('todo','in_progress','done')),
            priority    INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS prints (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id           INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
            filename            TEXT NOT NULL,
            filepath            TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'todo'
                                CHECK(status IN ('todo','in_progress','printed')),
            -- STL geometry
            size_x_mm           REAL,
            size_y_mm           REAL,
            size_z_mm           REAL,
            volume_mm3          REAL,
            triangle_count      INTEGER,
            -- Estimates
            layer_count         INTEGER,
            filament_length_m   REAL,
            filament_mass_g     REAL,
            time_minutes        REAL,
            time_formatted      TEXT,
            -- Config snapshot (JSON)
            config_json         TEXT,
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
        p.pop("config_json", None)
    ticket["prints"] = prints
    ticket["print_count"] = len(prints)
    ticket["total_time_minutes"] = sum(p.get("time_minutes") or 0 for p in prints)
    ticket["total_filament_g"] = sum(p.get("filament_mass_g") or 0 for p in prints)
    return ticket


def scan_and_analyze_stl(filepath_str):
    """Import estimator lazily so the server starts even if numpy is missing."""
    try:
        from stl_estimator import analyze_stl_with_prusaslicer, analyze_stl
        # import shutil

        # Check if PrusaSlicer is available in PATH or via the default macOS app bundle.
        prusaslicer_path = "/Applications/Original Prusa Drivers/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"

        if prusaslicer_path:
            return analyze_stl_with_prusaslicer(filepath_str, prusaslicer_path)
        return analyze_stl(filepath_str)
    except ImportError as e:
        return {"error": f"stl_estimator unavailable: {e}"}


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
            "SELECT id, status, time_minutes, filament_mass_g FROM prints WHERE ticket_id=?",
            (t["id"],)
        ).fetchall()
        t["print_count"] = len(prints)
        t["total_time_minutes"] = sum(p["time_minutes"] or 0 for p in prints)
        t["total_filament_g"] = sum(p["filament_mass_g"] or 0 for p in prints)
        tickets.append(t)
    return jsonify(tickets)


@app.route("/api/tickets", methods=["POST"])
def create_ticket():
    data = request.json or {}
    if not data.get("title"):
        return jsonify({"error": "title is required"}), 400
    ts = now_iso()
    db = get_db()
    cur = db.execute(
        "INSERT INTO tickets (title, requester, notes, status, priority, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (data["title"], data.get("requester", ""), data.get("notes", ""),
         data.get("status", "todo"), data.get("priority", 0), ts, ts)
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

@app.route("/api/stl-files", methods=["GET"])
def list_stl_files():
    """List all .stl files in the shared STL_DIR."""
    files = []
    for f in sorted(STL_DIR.iterdir()):
        if f.suffix.lower() == ".stl":
            files.append({
                "filename": f.name,
                "filepath": str(f),
                "size_bytes": f.stat().st_size,
            })
    return jsonify(files)


# ---------------------------------------------------------------------------
# Routes — Prints
# ---------------------------------------------------------------------------

@app.route("/api/tickets/<int:tid>/prints", methods=["POST"])
def add_print(tid):
    """
    Add a print to a ticket by referencing an STL filename already in stl_files/.
    Body: { "filename": "part.stl" }
    """
    db = get_db()
    ticket = db.execute("SELECT id FROM tickets WHERE id=?", (tid,)).fetchone()
    if not ticket:
        return jsonify({"error": "ticket not found"}), 404

    data = request.json or {}
    filename = data.get("filename", "").strip()
    if not filename:
        return jsonify({"error": "filename is required"}), 400

    filepath = STL_DIR / filename
    if not filepath.exists():
        return jsonify({"error": f"file not found in stl_files/: {filename}"}), 404
    # Analyze STL
    result = scan_and_analyze_stl(str(filepath))
    error = result.get("error")
    ts = now_iso()

    cur = db.execute(
        """INSERT INTO prints
            (ticket_id, filename, filepath, status,
             size_x_mm, size_y_mm, size_z_mm, volume_mm3, triangle_count,
             layer_count, filament_length_m, filament_mass_g,
             time_minutes, time_formatted, config_json, parse_error,
             created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            tid, filename, str(filepath), "todo",
            result.get("size_x_mm"), result.get("size_y_mm"), result.get("size_z_mm"),
            result.get("volume_mm3"), result.get("triangle_count"),
            result.get("layer_count"), result.get("filament_length_m"),
            result.get("filament_mass_g"), result.get("time_minutes"),
            result.get("time_formatted"),
            json.dumps(result.get("config")) if result.get("config") else None,
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
        time_minutes=?, time_formatted=?, config_json=?, parse_error=?, updated_at=?
        WHERE id=?""",
        (
            result.get("size_x_mm"), result.get("size_y_mm"), result.get("size_z_mm"),
            result.get("volume_mm3"), result.get("triangle_count"),
            result.get("layer_count"), result.get("filament_length_m"),
            result.get("filament_mass_g"), result.get("time_minutes"),
            result.get("time_formatted"),
            json.dumps(result.get("config")) if result.get("config") else None,
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
    print(f"📁  STL files directory: {STL_DIR.resolve()}\n")
    app.run(debug=True, port=5000)