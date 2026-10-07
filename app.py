from __future__ import annotations

import logging
import os
from pathlib import Path

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from jobs import Job, JobManager, default_runner
from scraper import InvalidUsername, ScraperConfig, clean_username


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        )


def create_app(job_manager: JobManager | None = None) -> Flask:
    _configure_logging()
    app = Flask(__name__)
    app.json.sort_keys = False

    if job_manager is None:
        job_manager = JobManager(
            default_runner(ScraperConfig.from_env()),
            output_dir=Path(os.environ.get("OUTPUT_DIR", "output")),
            max_workers=int(os.environ.get("IG_MAX_CONCURRENCY", "2")),
        )
    app.extensions["jobs"] = job_manager

    def get_job_or_404(job_id: str) -> Job:
        job = job_manager.get(job_id)
        if job is None:
            abort(404)
        return job

    def job_payload(job: Job, include_result: bool = False) -> dict:
        data = job.to_dict(include_result=include_result)
        data["download_url"] = url_for("download", filename=job.filename) if job.filename else None
        return data

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.post("/jobs")
    def create_job():
        if request.is_json:
            payload = request.get_json(silent=True)
            raw = payload.get("username", "") if isinstance(payload, dict) else ""
        else:
            raw = request.form.get("username", "")

        try:
            username = clean_username(raw)
        except InvalidUsername as exc:
            if request.is_json:
                return jsonify(error=str(exc)), 400
            return render_template("index.html", error=str(exc), username=raw), 400

        job = job_manager.submit(username)
        if request.is_json:
            location = url_for("job_status", job_id=job.id)
            return jsonify(job_payload(job)), 202, {"Location": location}
        return redirect(url_for("job_page", job_id=job.id), code=303)

    @app.get("/jobs/<job_id>")
    def job_page(job_id: str):
        return render_template("job.html", job=get_job_or_404(job_id))

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id: str):
        return jsonify(job_payload(get_job_or_404(job_id), include_result=True))

    @app.get("/download/<path:filename>")
    def download(filename: str):
        return send_from_directory(job_manager.output_dir, filename, as_attachment=True)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app


if __name__ == "__main__":
    create_app().run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
