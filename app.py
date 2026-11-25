"""Flask web application for buyer discovery, lead management and outreach."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

from assets import ensure_sample_pdf
from config import (
    DEFAULT_CITY,
    DEFAULT_COUNTRY,
    DEFAULT_PRODUCT,
    DEFAULT_STATE,
    EMAIL_VALIDATION_MODE,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GEMINI_TIMEOUT_SECONDS,
    MAX_EMAILS_PER_LEAD_PER_DAY,
    MAX_EMAILS_PER_RUN,
    SECRET_KEY,
    SAMPLE_PDF_PATH,
    UPLOAD_DIR,
)
from database import (
    classify_leads,
    connect,
    delete_leads,
    emails_to_lead_since,
    get_email_history,
    get_group_counts,
    get_lead,
    get_leads,
    get_stats,
    init_db,
    insert_lead,
    record_discovery_run,
    record_email,
    record_import_batch,
    update_lead,
    validate_leads,
)
from mail import available_attachments, gmail_status, personalize, send_email, template_defaults
from parsers import parse_lead_file
from services import (
    DISCOVERY_CANDIDATES,
    SOURCE_DEFINITIONS,
    build_manual_playbooks,
    build_search_links,
    build_search_ladder,
    now_iso,
)


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = SECRET_KEY
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
    ensure_sample_pdf()
    init_db()

    def get_db():
        if "db" not in g:
            g.db = connect()
        return g.db

    @app.teardown_appcontext
    def close_db(error=None) -> None:
        db = g.pop("db", None)
        if db is not None:
            if error is None:
                db.commit()
            else:
                db.rollback()
            db.close()

    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        return {
            "app_name": "Export Buyer Discovery",
            "gemini_configured": bool(GEMINI_API_KEY),
            "gemini_model": GEMINI_MODEL,
            "gemini_timeout": GEMINI_TIMEOUT_SECONDS,
            "gmail_state": gmail_status(),
            "validation_mode": EMAIL_VALIDATION_MODE,
            "max_emails_per_run": MAX_EMAILS_PER_RUN,
            "max_emails_per_lead_per_day": MAX_EMAILS_PER_LEAD_PER_DAY,
        }

    def filters_from_args() -> dict[str, str]:
        return {
            key: request.args.get(key, "")
            for key in ("q", "product", "country", "state", "city", "source", "buyer_type", "validation")
        }

    def selected_ids(field: str = "lead_ids") -> list[int]:
        values = request.form.getlist(field)
        if request.form.get("ids"):
            values.extend(request.form.get("ids", "").split(","))
        result: list[int] = []
        for value in values:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number not in result:
                result.append(number)
        return result

    def distinct_values(db, column: str) -> list[str]:
        allowed = {"product", "country", "state", "city", "source", "buyer_type"}
        if column not in allowed:
            return []
        rows = db.execute(
            f"SELECT DISTINCT {column} AS value FROM leads "
            f"WHERE {column} != '' ORDER BY {column} COLLATE NOCASE"
        ).fetchall()
        return [row["value"] for row in rows]

    def flash_result(status: str, label: str) -> None:
        messages = {
            "inserted": f"Added {label}.",
            "duplicate_email": f"Skipped {label}: duplicate email address.",
            "duplicate_website": f"Skipped {label}: duplicate website.",
            "duplicate_company": f"Skipped {label}: duplicate company in the same location.",
            "invalid": f"Skipped {label}: no usable contact details.",
        }
        flash(messages.get(status, f"Skipped {label}: {status}"), "warning" if status != "inserted" else "success")

    def classification_message(result: dict[str, Any]) -> str:
        sources = ", ".join(
            f"{count} {method}" for method, count in result.get("methods", {}).items()
        ) or "no leads classified"
        return (
            f"Classification complete: {result.get('business', 0)} business buyer(s), "
            f"{result.get('individual', 0)} individual buyer(s), "
            f"{result.get('uncertain', 0)} uncertain. Sources: {sources}."
        )

    @app.route("/health")
    def health():
        return jsonify({"status": "ok", "time": now_iso()})

    @app.route("/")
    def dashboard():
        db = get_db()
        return render_template(
            "dashboard.html",
            stats=get_stats(db),
            recent_leads=get_leads(db, {"validation": ""})[:8],
            sources=get_group_counts(db, "source", 6),
            buyer_types=get_group_counts(db, "buyer_type", 5),
            filters={
                "product": request.args.get("product", DEFAULT_PRODUCT),
                "country": request.args.get("country", DEFAULT_COUNTRY),
                "state": request.args.get("state", DEFAULT_STATE),
                "city": request.args.get("city", DEFAULT_CITY),
            },
        )

    @app.route("/discovery", methods=["GET"])
    def discovery():
        product = request.args.get("product", DEFAULT_PRODUCT)
        country = request.args.get("country", DEFAULT_COUNTRY)
        state = request.args.get("state", DEFAULT_STATE)
        city = request.args.get("city", DEFAULT_CITY)
        return render_template(
            "discovery.html",
            filters={"product": product, "country": country, "state": state, "city": city},
            links=build_search_links(product, country, state, city),
            ladder=build_search_ladder(product, country, state, city),
            playbooks=build_manual_playbooks(product, country, state, city),
            sources=SOURCE_DEFINITIONS,
            demo_candidate_count=len(DISCOVERY_CANDIDATES),
        )

    @app.route("/discovery/run", methods=["POST"])
    def discovery_run():
        db = get_db()
        product = request.form.get("product", DEFAULT_PRODUCT).strip()
        country = request.form.get("country", DEFAULT_COUNTRY).strip()
        state = request.form.get("state", DEFAULT_STATE).strip()
        city = request.form.get("city", DEFAULT_CITY).strip()
        selected_sources = request.form.getlist("sources")
        valid_keys = {item["key"] for item in SOURCE_DEFINITIONS}
        selected_sources = [key for key in selected_sources if key in valid_keys]
        if not selected_sources:
            selected_sources = [item["key"] for item in SOURCE_DEFINITIONS]

        links = {item["key"]: item for item in build_search_links(product, country, state, city)}
        matched = []
        for candidate in DISCOVERY_CANDIDATES:
            candidate_product = str(candidate.get("product", "")).lower()
            if product and product.lower() not in candidate_product:
                continue
            if country and str(candidate.get("country", "")).lower() != country.lower():
                continue
            if state and str(candidate.get("state", "")).lower() != state.lower():
                continue
            if city and str(candidate.get("city", "")).lower() != city.lower():
                continue
            matched.append(candidate)

        inserted = 0
        duplicates = 0
        for index, candidate in enumerate(matched):
            source_key = selected_sources[index % len(selected_sources)]
            source = links[source_key]["name"]
            status, _ = insert_lead(
                db,
                {**candidate, "source": source},
                source_url=links[source_key]["url"],
                is_demo=True,
            )
            if status == "inserted":
                inserted += 1
            else:
                duplicates += 1

        record_discovery_run(
            db, product, country, state, city, selected_sources, len(matched), inserted, duplicates
        )
        flash(
            f"Demo discovery finished: {inserted} new lead(s), {duplicates} duplicate(s) skipped. "
            "These are fictional sample prospects.",
            "success",
        )
        return redirect(
            url_for(
                "leads",
                product=product,
                country=country,
                state=state,
                city=city,
            )
        )

    @app.route("/leads", methods=["GET"])
    def leads():
        db = get_db()
        filters = filters_from_args()
        return render_template(
            "leads.html",
            leads=get_leads(db, filters),
            filters=filters,
            products=distinct_values(db, "product"),
            countries=distinct_values(db, "country"),
            states=distinct_values(db, "state"),
            cities=distinct_values(db, "city"),
            sources=distinct_values(db, "source"),
            stats=get_stats(db),
        )

    @app.route("/leads/add", methods=["POST"])
    def lead_add():
        db = get_db()
        lead = {key: request.form.get(key, "").strip() for key in (
            "contact_name", "company_name", "email", "phone", "website",
            "product", "country", "state", "city", "source", "role",
        )}
        lead["source"] = lead["source"] or "Manual entry"
        status, _ = insert_lead(db, lead)
        flash_result(status, f"lead {lead['contact_name'] or lead['company_name'] or '(unnamed)'}")
        return redirect(url_for("leads"))

    @app.route("/leads/import", methods=["POST"])
    def lead_import():
        uploaded = request.files.get("lead_file")
        if not uploaded or not uploaded.filename:
            flash("Choose a CSV or XLSX lead file first.", "error")
            return redirect(url_for("leads"))

        filename = secure_filename(uploaded.filename)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        destination = UPLOAD_DIR / f"{timestamp}_{filename}"
        uploaded.save(destination)

        rows, error = parse_lead_file(destination, filename)
        if error:
            flash(f"Upload failed: {error}", "error")
            return redirect(url_for("leads"))

        defaults = {
            key: request.form.get(key, "").strip()
            for key in ("product", "country", "state", "city", "source")
        }
        inserted = duplicates = invalid = 0
        db = get_db()
        for row in rows:
            lead = {**defaults, **row}
            lead["source"] = lead["source"] or f"File import: {filename}"
            status, _ = insert_lead(db, lead)
            if status == "inserted":
                inserted += 1
            elif status == "invalid":
                invalid += 1
            else:
                duplicates += 1
        record_import_batch(db, filename, len(rows), inserted, duplicates, invalid)
        flash(
            f"Import complete: {inserted} added, {duplicates} duplicate(s) skipped, "
            f"{invalid} row(s) without usable contact details.",
            "success",
        )
        return redirect(url_for("leads"))

    @app.route("/leads/bulk", methods=["POST"])
    def lead_bulk():
        db = get_db()
        ids = selected_ids()
        action = request.form.get("action", "")
        if not ids:
            flash("Select at least one lead.", "error")
            return redirect(url_for("leads"))

        if action == "validate":
            valid, invalid = validate_leads(db, ids)
            flash(f"Validation complete: {valid} valid, {invalid} invalid.", "success")
        elif action == "classify":
            force_demo = request.form.get("force_demo") in {"1", "on", "true", "yes"}
            counts = classify_leads(db, ids, force_demo=force_demo)
            flash(classification_message(counts), "success")
        elif action == "delete":
            count = delete_leads(db, ids)
            flash(f"Deleted {count} lead(s).", "success")
        elif action == "outreach":
            return redirect(url_for("outreach", ids=",".join(str(value) for value in ids)))
        else:
            flash("Unknown bulk action.", "error")
        return redirect(url_for("leads"))

    @app.route("/leads/<int:lead_id>", methods=["GET"])
    def lead_detail(lead_id: int):
        db = get_db()
        lead = get_lead(db, lead_id)
        if not lead:
            abort(404)
        history = db.execute(
            "SELECT * FROM email_history WHERE lead_id = ? ORDER BY sent_at DESC", (lead_id,)
        ).fetchall()
        return render_template("lead_detail.html", lead=lead, history=history)

    @app.route("/leads/<int:lead_id>/classify", methods=["POST"])
    def lead_classify(lead_id: int):
        db = get_db()
        force_demo = request.form.get("force_demo") in {"1", "on", "true", "yes"}
        classify_leads(db, [lead_id], force_demo=force_demo)
        lead = get_lead(db, lead_id)
        if not lead:
            abort(404)
        confidence = lead["classification_confidence"]
        confidence_text = f" · confidence {confidence:.0%}" if confidence is not None else ""
        flash(
            f"Classification saved: {lead['buyer_type']} · {lead['classification_method']}"
            f"{confidence_text} · {lead['classification_reason']}",
            "success",
        )
        return redirect(url_for("lead_detail", lead_id=lead_id))

    @app.route("/leads/<int:lead_id>/edit", methods=["POST"])
    def lead_edit(lead_id: int):
        db = get_db()
        values = {key: request.form.get(key, "").strip() for key in (
            "contact_name", "company_name", "email", "phone", "website",
            "product", "country", "state", "city", "source", "role",
        )}
        success, message = update_lead(db, lead_id, values)
        flash(message, "success" if success else "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    @app.route("/leads/<int:lead_id>/delete", methods=["POST"])
    def lead_delete(lead_id: int):
        db = get_db()
        delete_leads(db, [lead_id])
        flash("Lead deleted.", "success")
        return redirect(url_for("leads"))

    @app.route("/outreach", methods=["GET"])
    def outreach():
        db = get_db()
        raw_ids = request.args.get("ids", "")
        ids: list[int] = []
        for value in raw_ids.split(","):
            try:
                ids.append(int(value))
            except ValueError:
                pass

        if ids:
            selected = [row for row in (get_lead(db, lead_id) for lead_id in ids) if row]
        else:
            selected = get_leads(db, {"validation": "valid", "buyer_type": "business"})[:MAX_EMAILS_PER_RUN]

        subject_template, body_template = template_defaults()
        return render_template(
            "outreach.html",
            selected=selected,
            subject_template=subject_template,
            body_template=body_template,
            attachments=available_attachments(),
            recent_history=get_email_history(db, 5),
        )

    @app.route("/outreach/send", methods=["POST"])
    def outreach_send():
        db = get_db()
        ids = selected_ids()
        if not ids:
            flash("Select at least one buyer to contact.", "error")
            return redirect(url_for("outreach"))

        subject_template = request.form.get("subject", "").strip() or "Introduction from our export team"
        body_template = request.form.get("body", "").strip()
        if not body_template:
            flash("Write an email message before sending.", "error")
            return redirect(url_for("outreach", ids=",".join(map(str, ids))))

        sender_name = request.form.get("sender_name", "").strip()
        company_name = request.form.get("company_name", "").strip()
        product = request.form.get("product", DEFAULT_PRODUCT).strip()
        country = request.form.get("country", DEFAULT_COUNTRY).strip()
        attachment_name = request.form.get("attachment", "")
        attachment_path = next(
            (Path(item["path"]) for item in available_attachments() if item["name"] == attachment_name),
            None,
        )
        business_only = request.form.get("business_only") in {"1", "on", "true", "yes"}
        try:
            run_limit = max(1, int(request.form.get("max_emails", MAX_EMAILS_PER_RUN)))
        except ValueError:
            run_limit = MAX_EMAILS_PER_RUN

        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        sent = skipped = failed = 0
        outcomes: list[str] = []

        for lead_id in ids:
            if sent >= run_limit:
                outcomes.append(f"Stopped at the run limit of {run_limit} email(s).")
                break
            lead = get_lead(db, lead_id)
            if not lead:
                continue
            if business_only and lead["buyer_type"] != "business":
                outcomes.append(f"Skipped {lead['contact_name'] or lead['company_name']}: not a classified business buyer.")
                skipped += 1
                continue
            if not lead["email_valid"]:
                outcomes.append(f"Skipped {lead['contact_name'] or lead['company_name']}: invalid email address.")
                skipped += 1
                continue
            if emails_to_lead_since(db, lead_id, since) >= MAX_EMAILS_PER_LEAD_PER_DAY:
                outcomes.append(f"Skipped {lead['contact_name'] or lead['company_name']}: contacted in the last 24 hours.")
                skipped += 1
                continue

            subject = personalize(subject_template, dict(lead), product, sender_name, company_name)
            body = personalize(body_template, dict(lead), product, sender_name, company_name)
            result = send_email(dict(lead), subject, body, attachment_path)
            record_email(
                db, lead, subject, body,
                attachment_path.name if attachment_path else "",
                result["status"], result["provider"], result["error"],
            )
            if result["status"] == "failed":
                failed += 1
                outcomes.append(f"Failed {lead['contact_name']}: {result['error']}")
            else:
                sent += 1
                label = "Simulated" if result["status"] == "demo_sent" else "Sent"
                outcomes.append(f"{label} to {lead['contact_name'] or lead['email']}.")

        flash(
            f"Outreach finished: {sent} sent/simulated, {skipped} skipped, {failed} failed. "
            + " ".join(outcomes[:5]),
            "success" if sent else "warning",
        )
        return redirect(url_for("history"))

    @app.route("/history")
    def history():
        db = get_db()
        return render_template("history.html", history=get_email_history(db, 200))

    @app.route("/reports")
    def reports():
        db = get_db()
        import_batches = db.execute(
            "SELECT * FROM import_batches ORDER BY created_at DESC, id DESC LIMIT 10"
        ).fetchall()
        discovery_runs = db.execute(
            "SELECT * FROM discovery_runs ORDER BY created_at DESC, id DESC LIMIT 10"
        ).fetchall()
        return render_template(
            "reports.html",
            stats=get_stats(db),
            sources=get_group_counts(db, "source", 10),
            countries=get_group_counts(db, "country", 10),
            states=get_group_counts(db, "state", 10),
            buyers=get_group_counts(db, "buyer_type", 5),
            classification_sources=get_group_counts(db, "classification_method", 5),
            statuses=get_group_counts(db, "status", 5),
            import_batches=import_batches,
            discovery_runs=discovery_runs,
        )

    @app.route("/reports/leads.csv")
    def reports_csv():
        db = get_db()
        leads = get_leads(db, filters_from_args())
        output = io.StringIO()
        writer = csv.writer(output)
        columns = [
            "id", "contact_name", "company_name", "email", "phone", "website",
            "product", "country", "state", "city", "source", "source_url",
            "role", "buyer_type", "email_valid", "classification_method",
            "classification_confidence", "classification_at", "classification_reason", "is_demo",
        ]
        writer.writerow(columns)
        for lead in leads:
            writer.writerow([lead[column] for column in columns])
        response = Response(output.getvalue(), mimetype="text/csv")
        response.headers["Content-Disposition"] = "attachment; filename=export_buyer_leads.csv"
        return response

    @app.route("/playbooks")
    def playbooks():
        product = request.args.get("product", DEFAULT_PRODUCT)
        country = request.args.get("country", DEFAULT_COUNTRY)
        state = request.args.get("state", DEFAULT_STATE)
        city = request.args.get("city", DEFAULT_CITY)
        return render_template(
            "playbooks.html",
            filters={"product": product, "country": country, "state": state, "city": city},
            playbooks=build_manual_playbooks(product, country, state, city),
            ladder=build_search_ladder(product, country, state, city),
            links=build_search_links(product, country, state, city),
        )

    @app.route("/integrations")
    def integrations():
        return render_template(
            "integrations.html",
            gmail=gmail_status(),
            gemini_configured=bool(GEMINI_API_KEY),
            gemini_model=GEMINI_MODEL,
            gemini_timeout=GEMINI_TIMEOUT_SECONDS,
            validation_mode=EMAIL_VALIDATION_MODE,
        )

    @app.route("/download/sample-pdf")
    def download_sample_pdf():
        if not SAMPLE_PDF_PATH.exists():
            abort(404)
        return send_from_directory(SAMPLE_PDF_PATH.parent, SAMPLE_PDF_PATH.name, as_attachment=True)

    @app.route("/uploads/<path:filename>")
    def uploaded_file(filename: str):
        safe_name = secure_filename(filename)
        if not safe_name or not (UPLOAD_DIR / safe_name).exists():
            abort(404)
        return send_from_directory(UPLOAD_DIR, safe_name, as_attachment=True)

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=5000, debug=True)
