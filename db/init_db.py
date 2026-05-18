from pathlib import Path
import sqlite3
from datetime import datetime


def init_db(db_path, base_dir=None):
    """Initialize the SQLite database and run any unapplied SQL migrations.

    Args:
        db_path (str or Path): Path to the SQLite database file.
        base_dir (str or Path): Project base directory (optional, unused currently).
    """
    db_path = Path(db_path)
    db_dir = db_path.parent
    db_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()

    # Ensure migrations table exists
    cur.execute("""
        CREATE TABLE IF NOT EXISTS migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            applied_at TEXT NOT NULL
        )
    """)
    conn.commit()

    # Discover migrations
    migrations_dir = db_dir / "migrations"
    if not migrations_dir.exists():
        # Nothing to run
        conn.close()
        return

    sql_files = sorted([p for p in migrations_dir.iterdir() if p.suffix == '.sql'])
    for sql_file in sql_files:
        name = sql_file.name
        # Check if applied
        cur.execute("SELECT 1 FROM migrations WHERE name=?", (name,))
        if cur.fetchone():
            continue
        # Apply migration
        sql = sql_file.read_text(encoding='utf-8')
        try:
            conn.executescript(sql)
            cur.execute("INSERT INTO migrations (name, applied_at) VALUES (?,?)", (name, datetime.utcnow().isoformat() + 'Z'))
            conn.commit()
            print(f"Applied migration: {name}")
        except Exception as e:
            conn.rollback()
            conn.close()
            raise

    conn.close()
