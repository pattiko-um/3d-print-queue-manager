# 3D Print Queue Manager

Allows users to manage 3D print requests in a shared queue. Users can create a ticket, associate .STL files with the ticket, receive estimates for print duration and filament use based on standard configurations, and manage ticket statuses.

Built with Python, with a Flask/SQLite backend and vanilla JS/CSS/HTML frontend.

## Setup

### Prerequisites
- Python 3.8+
- Dependencies: Flask, python-dotenv (install via `pip install -r requirements.txt`)

### Environment Configuration
Create a `.env` file in the project root with:

```bash
PRINT_ROOT_DIR="/path/to/print/directory"
PRUSA_SLICER_PATH="/path/to/PrusaSlicer.exe"  # or .app on macOS
```

### Database Initialization

The database uses SQLite with a migration system. Migrations live in `/db/migrations/` as SQL files.

**First run:**
```bash
python app.py
```

The app automatically creates `/db/printqueue.db` and applies all unapplied migrations from `/db/migrations/`.

**Reset database:**
```bash
rm db/printqueue.db
python app.py
```

### Creating a Database Migration

1. Create a new SQL file in `/db/migrations/` with the naming pattern `NNNN_description.sql` (e.g., `0002_add_priority_field.sql`).
2. Write your SQL changes in the file:

```sql
-- 0002_add_priority_field.sql
-- Add priority field to tickets if not present

BEGIN TRANSACTION;

ALTER TABLE tickets ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;

COMMIT;
```

3. On next app startup, the migration runner automatically applies all unapplied migrations and records them in the `migrations` table.

**Important:** Each migration file is applied exactly once. If you need to undo a migration, create a new migration that reverses it.

## Running

```bash
python app.py
```

Server runs at `http://localhost:5000`

## Project Structure

```
.
├── app.py                 # Flask app
├── index.html             # Frontend entry
├── requirements.txt       # Python dependencies
├── db/
│   ├── init_db.py         # Migration runner
│   ├── printqueue.db      # SQLite database (created on first run)
│   └── migrations/        # SQL migration files
│       └── 0001_initial.sql
├── prusa_configs/
│   └── default.ini        # PrusaSlicer config
├── static/
│   ├── app.js
│   └── style.css
├── stl_files/             # Drop .stl/.3mf/.stp files here to import
└── README.md
```

## Features

- **Ticket Management:** Create, edit, delete, and move tickets across statuses.
- **Print File Analysis:** Automatically estimates print time, filament use, and detects issues via PrusaSlicer.
- **Quantity Tracking:** Track multiple copies per print and completion progress.
- **Directory Scanning:** Import new tickets from a shared print directory structure.
- **Config Export:** PrusaSlicer configs can be loaded when opening prints.

Work in progress.