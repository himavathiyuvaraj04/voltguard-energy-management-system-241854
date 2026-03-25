import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request
from flask_cors import CORS


def _get_sqlite_db_path() -> str:
    """
    Resolve the SQLite database path.

    Notes:
    - Uses SQLITE_DB environment variable if present (provided by sqlite_database container in the platform).
    - Falls back to local ./data/voltguard.db for dev/demo convenience.
    """
    return os.getenv("SQLITE_DB", os.path.join(os.path.dirname(__file__), "data", "voltguard.db"))


def _connect(db_path: str) -> sqlite3.Connection:
    """Create a SQLite connection with row_factory=sqlite3.Row."""
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    # Keep FK on in case we expand schema later.
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _init_db(db_path: str) -> None:
    """Initialize required tables if they don't exist."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    with _connect(db_path) as conn:
        # Minimal daily meter data used by baseline/anomaly computation.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meter_daily_consumption (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer TEXT NOT NULL,
                site TEXT NOT NULL,
                date TEXT NOT NULL, -- ISO yyyy-mm-dd
                kwh REAL NOT NULL,
                UNIQUE(customer, site, date)
            );
            """
        )

        # Alerts table per requirements.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer TEXT NOT NULL,
                site TEXT NOT NULL,
                date TEXT NOT NULL, -- ISO yyyy-mm-dd
                deviation_pct REAL NOT NULL, -- (actual - baseline) / baseline * 100
                status TEXT NOT NULL CHECK(status IN ('read', 'unread')) DEFAULT 'unread',
                created_at TEXT NOT NULL
            );
            """
        )

        # Helpful index for listing/querying alerts.
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_alerts_customer_site_date
            ON alerts(customer, site, date);
            """
        )


def _parse_iso_date(value: str) -> datetime:
    """Parse yyyy-mm-dd to datetime (naive)."""
    return datetime.strptime(value, "%Y-%m-%d")


def _to_iso_date(d: datetime) -> str:
    """Format datetime to yyyy-mm-dd."""
    return d.strftime("%Y-%m-%d")


def _compute_rolling_4w_baseline(
    conn: sqlite3.Connection,
    customer: str,
    site: str,
    date_iso: str,
) -> Optional[float]:
    """
    Compute rolling 4-week baseline average for a given date.

    Rolling window definition:
    - Baseline = average kWh of the prior 28 days *excluding* the current date.
    - If there is no historical data in the window, returns None.
    """
    target = _parse_iso_date(date_iso)
    # Prior 28 days (4 weeks).
    from datetime import timedelta

    start_iso = _to_iso_date(target - timedelta(days=28))
    end_iso = _to_iso_date(target - timedelta(days=1))

    row = conn.execute(
        """
        SELECT AVG(kwh) AS avg_kwh
        FROM meter_daily_consumption
        WHERE customer = ?
          AND site = ?
          AND date >= ?
          AND date <= ?;
        """,
        (customer, site, start_iso, end_iso),
    ).fetchone()

    if not row:
        return None
    avg_kwh = row["avg_kwh"]
    if avg_kwh is None:
        return None
    return float(avg_kwh)


def _get_series(
    conn: sqlite3.Connection,
    customer: str,
    site: str,
    limit: int,
) -> List[Dict[str, Any]]:
    """
    Return the most recent `limit` daily consumption rows for a site.

    Each returned record includes:
    - date
    - actual
    - baseline (rolling prior-28-day avg)
    - anomaly (bool) where anomaly = actual > baseline * 1.2 (when baseline exists)
    """
    # Oldest-to-newest ordering is easier for charting; query newest then reverse.
    rows = conn.execute(
        """
        SELECT date, kwh
        FROM meter_daily_consumption
        WHERE customer = ?
          AND site = ?
        ORDER BY date DESC
        LIMIT ?;
        """,
        (customer, site, limit),
    ).fetchall()

    # Reverse to ascending by date.
    rows = list(reversed(rows))

    series: List[Dict[str, Any]] = []
    for r in rows:
        date_iso = r["date"]
        actual = float(r["kwh"])
        baseline = _compute_rolling_4w_baseline(conn, customer, site, date_iso)
        anomaly = False
        if baseline is not None and baseline > 0:
            anomaly = actual > baseline * 1.2

        series.append(
            {
                "date": date_iso,
                "actual": actual,
                "baseline": baseline,
                "anomaly": anomaly,
            }
        )
    return series


def _persist_alert_if_anomaly(
    conn: sqlite3.Connection,
    customer: str,
    site: str,
    date_iso: str,
    actual: float,
    baseline: Optional[float],
    anomaly: bool,
) -> Optional[int]:
    """
    Persist anomaly as an alert row if anomaly is True.

    De-duplication:
    - If an alert already exists for (customer, site, date), do not insert a duplicate.
    """
    if not anomaly:
        return None
    if baseline is None or baseline <= 0:
        return None

    deviation_pct = ((actual - baseline) / baseline) * 100.0

    existing = conn.execute(
        """
        SELECT id
        FROM alerts
        WHERE customer = ?
          AND site = ?
          AND date = ?;
        """,
        (customer, site, date_iso),
    ).fetchone()
    if existing:
        return int(existing["id"])

    created_at = datetime.utcnow().isoformat() + "Z"
    cur = conn.execute(
        """
        INSERT INTO alerts (customer, site, date, deviation_pct, status, created_at)
        VALUES (?, ?, ?, ?, 'unread', ?);
        """,
        (customer, site, date_iso, deviation_pct, created_at),
    )
    return int(cur.lastrowid)


def _seed_demo_data_if_empty(conn: sqlite3.Connection) -> None:
    """
    Seed a small set of demo data when DB is empty.

    This ensures the UI has data without requiring uploads in this task.
    """
    row = conn.execute("SELECT COUNT(1) AS c FROM meter_daily_consumption;").fetchone()
    if row and int(row["c"]) > 0:
        return

    # Generate 42 days of data so rolling baseline (28 days) is meaningful.
    from datetime import timedelta

    customer = "Default Customer"
    site = "Site A"

    start = datetime.utcnow().date() - timedelta(days=41)
    for i in range(42):
        d = start + timedelta(days=i)
        date_iso = d.strftime("%Y-%m-%d")
        # Base load around 1200 with some small variation; add a spike near the end.
        kwh = 1200.0 + ((i % 7) - 3) * 30.0
        if i in (39,):  # spike day
            kwh = 1700.0
        conn.execute(
            """
            INSERT OR IGNORE INTO meter_daily_consumption (customer, site, date, kwh)
            VALUES (?, ?, ?, ?);
            """,
            (customer, site, date_iso, float(kwh)),
        )


# PUBLIC_INTERFACE
def create_app():
    """Create and configure the Flask application.

    Endpoints added/maintained:
    - POST /api/upload-csv : multipart/form-data with 'file' CSV (still basic validation)
    - GET  /api/analytics  : now returns analytics series with actual/baseline/anomaly fields
    - GET  /api/alerts     : now returns persisted anomalies from SQLite `alerts` table

    SQLite:
    - Uses SQLITE_DB env var if provided; otherwise uses ./data/voltguard.db.

    CORS:
    - Enabled for /api/* so the React frontend (localhost:3000) can call the backend.
    - Configure allowed origins via CORS_ALLOW_ORIGINS (comma-separated) if needed.
    """
    app = Flask(__name__)

    allow_origins_env = os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:3000")
    allow_origins = [o.strip() for o in allow_origins_env.split(",") if o.strip()]

    # Enable CORS for API routes.
    CORS(
        app,
        resources={r"/api/*": {"origins": allow_origins}},
        supports_credentials=False,
        methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )

    db_path = _get_sqlite_db_path()
    _init_db(db_path)

    @app.get("/")
    def health_check():
        return jsonify({"status": "ok", "service": "flask_backend"})

    @app.post("/api/upload-csv")
    def upload_csv():
        """Upload a CSV file for ingestion/processing.

        Expects:
          multipart/form-data with a `file` field containing a CSV file.

        Returns:
          JSON with a simple acknowledgement.

        Notes:
          This task does not require full CSV ingestion; anomaly detection is computed
          from records in SQLite (meter_daily_consumption).
        """
        if "file" not in request.files:
            return jsonify({"error": "Missing file field 'file'."}), 400

        f = request.files["file"]
        if not f or not f.filename:
            return jsonify({"error": "No file selected."}), 400

        filename = f.filename.lower()
        if not filename.endswith(".csv"):
            return jsonify({"error": "Only .csv uploads are supported."}), 400

        data = f.read() or b""
        size_bytes = len(data)

        return jsonify(
            {
                "message": "CSV uploaded.",
                "filename": f.filename,
                "size_bytes": size_bytes,
                "received_at": datetime.utcnow().isoformat() + "Z",
            }
        )

    @app.get("/api/analytics")
    def analytics():
        """Return analytics data including anomaly detection.

        Query params (optional):
        - customer: customer identifier/name (default: "Default Customer")
        - site: site identifier/name (default: "Site A")
        - limit: number of days to return (default: 30)

        Response fields in series:
        - date: yyyy-mm-dd
        - actual: numeric kWh consumption
        - baseline: rolling 4-week average baseline (avg of prior 28 days) or null
        - anomaly: boolean; True if actual > baseline * 1.2 (when baseline exists)

        Side-effects:
        - If an anomaly is detected, it is persisted into SQLite `alerts` table
          with deviation_pct and status='unread'.
        """
        customer = request.args.get("customer", "Default Customer").strip() or "Default Customer"
        site = request.args.get("site", "Site A").strip() or "Site A"
        try:
            limit = int(request.args.get("limit", "30"))
        except ValueError:
            return jsonify({"error": "limit must be an integer"}), 400
        limit = max(1, min(limit, 365))

        with _connect(db_path) as conn:
            _seed_demo_data_if_empty(conn)

            series = _get_series(conn, customer, site, limit)

            # Persist any anomalies found in the returned window.
            for point in series:
                _persist_alert_if_anomaly(
                    conn=conn,
                    customer=customer,
                    site=site,
                    date_iso=point["date"],
                    actual=float(point["actual"]),
                    baseline=point["baseline"],
                    anomaly=bool(point["anomaly"]),
                )

            # KPI computation
            anomalies_last_7_days = sum(1 for p in series[-7:] if p.get("anomaly"))

            # avg_daily_kwh based on actuals in returned series
            actuals = [float(p["actual"]) for p in series if p.get("actual") is not None]
            avg_daily_kwh = (sum(actuals) / len(actuals)) if actuals else 0.0

        return jsonify(
            {
                "kpis": {
                    "total_sites": 1,
                    "total_meters": 1,
                    "avg_daily_kwh": round(avg_daily_kwh, 2),
                    "anomalies_last_7_days": int(anomalies_last_7_days),
                },
                "context": {"customer": customer, "site": site},
                "series": series,
            }
        )

    @app.get("/api/alerts")
    def alerts():
        """Return current alerts/anomalies from SQLite.

        Query params (optional):
        - customer: filter by customer
        - site: filter by site
        - status: filter by 'read' or 'unread'
        - limit: max alerts to return (default: 100)

        Returns:
          { "alerts": [ {id, customer, site, date, deviation_pct, status, created_at} ] }
        """
        customer = request.args.get("customer")
        site = request.args.get("site")
        status = request.args.get("status")
        try:
            limit = int(request.args.get("limit", "100"))
        except ValueError:
            return jsonify({"error": "limit must be an integer"}), 400
        limit = max(1, min(limit, 500))

        where: List[str] = []
        params: List[Any] = []

        if customer:
            where.append("customer = ?")
            params.append(customer)
        if site:
            where.append("site = ?")
            params.append(site)
        if status:
            if status not in ("read", "unread"):
                return jsonify({"error": "status must be 'read' or 'unread'"}), 400
            where.append("status = ?")
            params.append(status)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)

        with _connect(db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT id, customer, site, date, deviation_pct, status, created_at
                FROM alerts
                {where_sql}
                ORDER BY date DESC, id DESC
                LIMIT ?;
                """,
                tuple(params),
            ).fetchall()

        return jsonify(
            {
                "alerts": [
                    {
                        "id": int(r["id"]),
                        "customer": r["customer"],
                        "site": r["site"],
                        "date": r["date"],
                        "deviation_pct": float(r["deviation_pct"]),
                        "status": r["status"],
                        "created_at": r["created_at"],
                    }
                    for r in rows
                ]
            }
        )

    @app.patch("/api/alerts/<int:alert_id>/mark-read")
    def mark_alert_read(alert_id: int):
        """Mark a specific alert as read.

        Path params:
        - alert_id: numeric alert ID

        Returns:
          { "alert": {id, status} } on success

        Errors:
          - 404 if the alert does not exist
        """
        with _connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT id, status
                FROM alerts
                WHERE id = ?;
                """,
                (alert_id,),
            ).fetchone()

            if not row:
                return jsonify({"error": "Alert not found.", "id": alert_id}), 404

            # Idempotent update.
            conn.execute(
                """
                UPDATE alerts
                SET status = 'read'
                WHERE id = ?;
                """,
                (alert_id,),
            )

            updated = conn.execute(
                """
                SELECT id, status
                FROM alerts
                WHERE id = ?;
                """,
                (alert_id,),
            ).fetchone()

        return jsonify({"alert": {"id": int(updated["id"]), "status": updated["status"]}})

    return app


app = create_app()

if __name__ == "__main__":
    # Default to port 3001 to match the running container metadata.
    port = int(os.getenv("PORT", "3001"))
    app.run(host="0.0.0.0", port=port, debug=True)
