import os
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS

# PUBLIC_INTERFACE
def create_app():
    """Create and configure the Flask application.

    Endpoints added:
    - POST /api/upload-csv : multipart/form-data with 'file' CSV
    - GET  /api/analytics  : returns basic analytics payload
    - GET  /api/alerts     : returns list of alerts

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
        methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )

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
        """
        if "file" not in request.files:
            return jsonify({"error": "Missing file field 'file'."}), 400

        f = request.files["file"]
        if not f or not f.filename:
            return jsonify({"error": "No file selected."}), 400

        filename = f.filename.lower()
        if not filename.endswith(".csv"):
            return jsonify({"error": "Only .csv uploads are supported."}), 400

        # Keep it simple: just read bytes to validate the upload works.
        # In a real implementation, you would parse/store/process the meter data.
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
        """Return basic analytics data.

        Keep it simple: returns a small payload suitable for UI verification.
        """
        # Stub data (replace with real computation later).
        return jsonify(
            {
                "kpis": {
                    "total_sites": 1,
                    "total_meters": 1,
                    "avg_daily_kwh": 1234.5,
                    "anomalies_last_7_days": 2,
                },
                "series": [
                    {"date": "2026-03-19", "kwh": 1200},
                    {"date": "2026-03-20", "kwh": 1300},
                    {"date": "2026-03-21", "kwh": 1100},
                    {"date": "2026-03-22", "kwh": 1400},
                    {"date": "2026-03-23", "kwh": 1500},
                    {"date": "2026-03-24", "kwh": 1250},
                    {"date": "2026-03-25", "kwh": 1350},
                ],
            }
        )

    @app.get("/api/alerts")
    def alerts():
        """Return current alerts/anomalies.

        Keep it simple: returns a small list of alert objects.
        """
        return jsonify(
            {
                "alerts": [
                    {
                        "id": "alert_001",
                        "severity": "high",
                        "title": "Consumption spike detected",
                        "site": "Site A",
                        "meter_id": "MTR-001",
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "status": "unread",
                    },
                    {
                        "id": "alert_002",
                        "severity": "medium",
                        "title": "Baseline drift",
                        "site": "Site A",
                        "meter_id": "MTR-001",
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "status": "unread",
                    },
                ]
            }
        )

    return app


app = create_app()

if __name__ == "__main__":
    # Default to port 3001 to match the running container metadata.
    port = int(os.getenv("PORT", "3001"))
    app.run(host="0.0.0.0", port=port, debug=True)
