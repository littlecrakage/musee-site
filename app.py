import base64
import json
import os
import random
import sqlite3
import smtplib
import string
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
from io import BytesIO

import qrcode
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me-in-production")

CONTENT_FILE = os.path.join(os.path.dirname(__file__), "content.json")
DB_FILE      = os.path.join(os.path.dirname(__file__), "database.db")
UPLOAD_DIR        = os.path.join(os.path.dirname(__file__), "static", "uploads", "gallery")
IMAGES_UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "static", "uploads", "images")
ALLOWED_EXT       = {"jpg", "jpeg", "png", "webp", "gif"}

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(IMAGES_UPLOAD_DIR, exist_ok=True)

_raw_password       = os.getenv("ADMIN_PASSWORD", "admin123")
ADMIN_PASSWORD_HASH = generate_password_hash(_raw_password)

# French weekday name → Python weekday number (Monday=0)
DAY_MAP = {
    "Lundi": 0, "Mardi": 1, "Mercredi": 2, "Jeudi": 3,
    "Vendredi": 4, "Samedi": 5, "Dimanche": 6
}


# ── Database ─────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS bookings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            reference   TEXT    UNIQUE,
            name        TEXT    NOT NULL,
            email       TEXT    NOT NULL,
            phone       TEXT,
            visit_date  TEXT    NOT NULL,
            time_slot   TEXT    NOT NULL,
            num_people  INTEGER NOT NULL,
            message     TEXT,
            status      TEXT    DEFAULT 'pending',
            created_at  TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS gallery (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            filename    TEXT    NOT NULL,
            caption     TEXT,
            sort_order  INTEGER DEFAULT 0,
            created_at  TEXT    DEFAULT (datetime('now'))
        );
        """)
        # Migrate: add reference column if missing (existing databases)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(bookings)").fetchall()]
        if "reference" not in cols:
            conn.execute("ALTER TABLE bookings ADD COLUMN reference TEXT")


init_db()


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_content():
    with open(CONTENT_FILE, encoding="utf-8") as f:
        data = json.load(f)
    # Ensure images key always exists (migration for older content.json)
    data.setdefault("images", {"hero_bear": None, "logo": None, "about": None, "cta_bear": None})
    return data


def save_content(data):
    with open(CONTENT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated


def generate_reference():
    """Generate a unique booking reference like POB-250312-A3F7K2."""
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    date_str = datetime.now().strftime("%y%m%d")
    return f"POB-{date_str}-{suffix}"


def make_qr_base64(data: str) -> str:
    """
    Generate an 8-bit style QR code (large square modules, brown on cream)
    and return it as a base64-encoded PNG string.
    """
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=12,      # large pixels → retro/8-bit look
        border=3,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#5C2E00", back_color="#FFF8E7")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def send_confirmation_email(booking, qr_b64: str):
    """Send a styled HTML confirmation email with embedded QR code."""
    host = os.getenv("SMTP_HOST")
    if not host:
        return
    try:
        content = load_content()
        html = render_template(
            "email/confirmation.html",
            c=content,
            booking=booking,
        )

        # Outer wrapper: related allows embedding inline images via CID
        msg = MIMEMultipart("related")
        msg["Subject"] = f"🐻 Confirmation de visite – {content['site']['title']}"
        msg["From"]    = os.getenv("SMTP_FROM", os.getenv("SMTP_USER", ""))
        msg["To"]      = booking["email"]

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(html, "html", "utf-8"))
        msg.attach(alt)

        # Attach QR code as inline CID image
        qr_bytes = base64.b64decode(qr_b64)
        img = MIMEImage(qr_bytes, "png")
        img.add_header("Content-ID", "<qrcode>")
        img.add_header("Content-Disposition", "inline", filename="qrcode.png")
        msg.attach(img)

        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", 587))) as s:
            s.starttls()
            s.login(os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", ""))
            s.send_message(msg)
    except Exception as e:
        app.logger.warning(f"Email not sent: {e}")


def send_cancellation_email(booking):
    """Send a cancellation notification email to the visitor."""
    host = os.getenv("SMTP_HOST")
    if not host:
        return
    try:
        content = load_content()
        html = render_template("email/cancellation.html", c=content, booking=booking)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Annulation de votre visite – {content['site']['title']}"
        msg["From"]    = os.getenv("SMTP_FROM", os.getenv("SMTP_USER", ""))
        msg["To"]      = booking["email"]
        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", 587))) as s:
            s.starttls()
            s.login(os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", ""))
            s.send_message(msg)
    except Exception as e:
        app.logger.warning(f"Cancellation email not sent: {e}")


# ── Public routes ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    content = load_content()
    with get_db() as conn:
        photos = conn.execute(
            "SELECT * FROM gallery ORDER BY sort_order ASC, created_at DESC"
        ).fetchall()
    return render_template("index.html", c=content, photos=photos)


@app.route("/api/slots")
def api_slots():
    date_str = request.args.get("date", "")
    if not date_str:
        return jsonify([])
    try:
        visit_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify([])

    content      = load_content()
    booking_cfg  = content.get("booking", {})
    available_days = booking_cfg.get("available_days", [])
    all_slots      = booking_cfg.get("time_slots", [])
    max_per_slot   = int(booking_cfg.get("max_per_slot", 20))

    weekday   = visit_date.weekday()
    open_days = [DAY_MAP[d] for d in available_days if d in DAY_MAP]
    if weekday not in open_days:
        return jsonify([])

    with get_db() as conn:
        rows = conn.execute(
            "SELECT time_slot, SUM(num_people) AS total FROM bookings "
            "WHERE visit_date=? AND status != 'cancelled' GROUP BY time_slot",
            (date_str,)
        ).fetchall()
    booked = {r["time_slot"]: r["total"] for r in rows}

    return jsonify([
        {"slot": s, "remaining": max_per_slot - booked.get(s, 0)}
        for s in all_slots
        if max_per_slot - booked.get(s, 0) > 0
    ])


@app.route("/booking", methods=["POST"])
def booking():
    form       = request.form
    name       = form.get("name", "").strip()
    email      = form.get("email", "").strip()
    phone      = form.get("phone", "").strip()
    visit_date = form.get("visit_date", "").strip()
    time_slot  = form.get("time_slot", "").strip()
    num_people = int(form.get("num_people", 1))
    message    = form.get("message", "").strip()

    if not all([name, email, visit_date, time_slot]):
        flash("Veuillez remplir tous les champs obligatoires.")
        return redirect(url_for("index") + "#reserver")

    content      = load_content()
    max_per_slot = int(content.get("booking", {}).get("max_per_slot", 20))

    with get_db() as conn:
        row = conn.execute(
            "SELECT SUM(num_people) AS total FROM bookings "
            "WHERE visit_date=? AND time_slot=? AND status != 'cancelled'",
            (visit_date, time_slot)
        ).fetchone()
        if (row["total"] or 0) + num_people > max_per_slot:
            flash("Ce créneau est complet. Veuillez en choisir un autre.")
            return redirect(url_for("index") + "#reserver")

        # Generate unique reference
        reference = generate_reference()
        while conn.execute("SELECT 1 FROM bookings WHERE reference=?", (reference,)).fetchone():
            reference = generate_reference()

        conn.execute(
            "INSERT INTO bookings "
            "(reference,name,email,phone,visit_date,time_slot,num_people,message,status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (reference, name, email, phone, visit_date, time_slot, num_people, message, "confirmed")
        )

    # Build QR code encoding the reference
    qr_data = f"REF:{reference} | {visit_date} {time_slot} | {num_people}p | {name}"
    qr_b64  = make_qr_base64(qr_data)

    booking_data = {
        "reference": reference, "name": name, "email": email,
        "visit_date": visit_date, "time_slot": time_slot, "num_people": num_people,
    }
    send_confirmation_email(booking_data, qr_b64)

    return render_template(
        "booking_success.html", c=content,
        reference=reference, name=name,
        visit_date=visit_date, time_slot=time_slot,
        num_people=num_people, qr_b64=qr_b64,
    )


# ── Admin auth ────────────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_logged_in"):
        return redirect(url_for("admin_dashboard"))
    error = None
    if request.method == "POST":
        if check_password_hash(ADMIN_PASSWORD_HASH, request.form.get("password", "")):
            session["admin_logged_in"] = True
            return redirect(url_for("admin_dashboard"))
        error = "Mot de passe incorrect."
    return render_template("admin/login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("index"))


# ── Admin dashboard ───────────────────────────────────────────────────────────

@app.route("/admin")
@login_required
def admin_dashboard():
    content = load_content()
    with get_db() as conn:
        bookings = conn.execute(
            "SELECT * FROM bookings ORDER BY visit_date DESC, time_slot ASC"
        ).fetchall()
        photos = conn.execute(
            "SELECT * FROM gallery ORDER BY sort_order ASC, created_at DESC"
        ).fetchall()
    return render_template("admin/dashboard.html",
                           c=content, bookings=bookings, photos=photos)


@app.route("/admin/save", methods=["POST"])
@login_required
def admin_save():
    content = load_content()
    form    = request.form

    content["site"]["title"]     = form.get("site_title",     content["site"]["title"])
    content["site"]["subtitle"]  = form.get("site_subtitle",  content["site"]["subtitle"])
    content["site"]["hero_text"] = form.get("site_hero_text", content["site"]["hero_text"])
    content["site"]["about"]     = form.get("site_about",     content["site"]["about"])

    content["booking"]["text"]         = form.get("booking_text", content["booking"]["text"])
    content["booking"]["max_per_slot"] = int(form.get("booking_max_per_slot", content["booking"].get("max_per_slot", 20)))
    content["booking"]["available_days"] = form.getlist("booking_available_days")
    raw_slots = form.get("booking_time_slots", "")
    content["booking"]["time_slots"] = [s.strip() for s in raw_slots.splitlines() if s.strip()]

    content["footer"]["address"] = form.get("footer_address", content["footer"]["address"])
    content["footer"]["phone"]   = form.get("footer_phone",   content["footer"]["phone"])
    content["footer"]["email"]   = form.get("footer_email",   content["footer"]["email"])

    for day in content["footer"]["hours"]:
        if f"hours_{day}" in form:
            content["footer"]["hours"][day] = form[f"hours_{day}"]

    if "hours_special" not in content["footer"]:
        content["footer"]["hours_special"] = {}
    for label in list(content["footer"].get("hours_special", {})):
        if f"hours_special_{label}" in form:
            content["footer"]["hours_special"][label] = form[f"hours_special_{label}"]
    new_label = form.get("new_special_label", "").strip()
    new_value = form.get("new_special_value", "").strip()
    if new_label and new_value:
        content["footer"]["hours_special"][new_label] = new_value

    highlights = []
    for i in range(6):
        title   = form.get(f"hl_title_{i}", "").strip()
        text    = form.get(f"hl_text_{i}",  "").strip()
        icon    = form.get(f"hl_icon_{i}",  "").strip()
        visible = f"hl_visible_{i}" in form
        if title:
            highlights.append({"title": title, "text": text, "icon": icon, "visible": visible})
    if highlights:
        content["highlights"] = highlights

    save_content(content)
    flash("Modifications enregistrées avec succès !")
    return redirect(url_for("admin_dashboard"))


# ── Visitor cancellation ──────────────────────────────────────────────────────

@app.route("/booking/cancel/<reference>", methods=["GET", "POST"])
def booking_cancel(reference):
    with get_db() as conn:
        booking = conn.execute(
            "SELECT * FROM bookings WHERE reference=?", (reference,)
        ).fetchone()

    content = load_content()
    if not booking:
        return render_template("cancel.html", c=content, booking=None)

    booking = dict(booking)

    if booking["status"] == "cancelled":
        return render_template("cancel.html", c=content, booking=booking, done=True, already=True)

    if request.method == "POST":
        with get_db() as conn:
            conn.execute("UPDATE bookings SET status='cancelled' WHERE reference=?", (reference,))
        send_cancellation_email(booking)
        return render_template("cancel.html", c=content, booking=booking, done=True, already=False)

    return render_template("cancel.html", c=content, booking=booking, done=False)


# ── Admin bookings ────────────────────────────────────────────────────────────

@app.route("/admin/bookings/<int:booking_id>/cancel", methods=["POST"])
@login_required
def admin_booking_cancel(booking_id):
    with get_db() as conn:
        booking = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if booking and booking["status"] != "cancelled":
            conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,))
            send_cancellation_email(dict(booking))
    return redirect(url_for("admin_dashboard") + "#section-bookings")


@app.route("/admin/bookings/<int:booking_id>/delete", methods=["POST"])
@login_required
def admin_booking_delete(booking_id):
    with get_db() as conn:
        conn.execute("DELETE FROM bookings WHERE id=?", (booking_id,))
    return redirect(url_for("admin_dashboard") + "#section-bookings")


# ── Admin gallery ─────────────────────────────────────────────────────────────

@app.route("/admin/gallery/upload", methods=["POST"])
@login_required
def admin_gallery_upload():
    files   = request.files.getlist("photos")
    caption = request.form.get("caption", "").strip()
    for f in files:
        if f and allowed_file(f.filename):
            ts       = datetime.now().strftime("%Y%m%d_%H%M%S_")
            filename = ts + secure_filename(f.filename)
            f.save(os.path.join(UPLOAD_DIR, filename))
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO gallery (filename, caption) VALUES (?,?)",
                    (filename, caption)
                )
    flash("Photo(s) ajoutée(s) avec succès !")
    return redirect(url_for("admin_dashboard") + "#section-gallery")


@app.route("/admin/gallery/<int:photo_id>/delete", methods=["POST"])
@login_required
def admin_gallery_delete(photo_id):
    with get_db() as conn:
        row = conn.execute("SELECT filename FROM gallery WHERE id=?", (photo_id,)).fetchone()
        if row:
            fp = os.path.join(UPLOAD_DIR, row["filename"])
            if os.path.exists(fp):
                os.remove(fp)
            conn.execute("DELETE FROM gallery WHERE id=?", (photo_id,))
    return redirect(url_for("admin_dashboard") + "#section-gallery")


# ── Admin sections ────────────────────────────────────────────────────────────

@app.route("/admin/sections/save", methods=["POST"])
@login_required
def admin_save_sections():
    raw = request.form.get("sections_json", "[]")
    try:
        incoming = json.loads(raw)   # [{id, visible}, ...]
    except (ValueError, TypeError):
        flash("Erreur lors de la sauvegarde des sections.")
        return redirect(url_for("admin_dashboard") + "#section-sections")

    content  = load_content()
    # Build a lookup of existing section metadata (label, icon)
    existing = {s["id"]: s for s in content.get("sections", [])}

    # Rebuild sections list in the new order, preserving label/icon
    KNOWN = {
        "hero":       {"label": "Héro & accroche",  "icon": "🦸"},
        "about":      {"label": "À propos",          "icon": "🏛️"},
        "highlights": {"label": "Points forts",      "icon": "✨"},
        "booking":    {"label": "Réservation",       "icon": "📅"},
        "gallery":    {"label": "Galerie photos",    "icon": "📸"},
        "cta":        {"label": "Appel à l'action",  "icon": "🐻"},
    }
    updated = []
    seen = set()
    for item in incoming:
        sid = item.get("id", "")
        if sid not in KNOWN or sid in seen:
            continue
        meta = existing.get(sid, KNOWN[sid])
        updated.append({
            "id":      sid,
            "label":   meta.get("label", KNOWN[sid]["label"]),
            "icon":    meta.get("icon",  KNOWN[sid]["icon"]),
            "visible": bool(item.get("visible", True)),
        })
        seen.add(sid)
    # Append any sections not sent by the form (safety net)
    for sid, meta in KNOWN.items():
        if sid not in seen:
            src = existing.get(sid, meta)
            updated.append({"id": sid, "label": src.get("label", meta["label"]),
                            "icon": src.get("icon", meta["icon"]), "visible": True})

    content["sections"] = updated
    save_content(content)
    flash("Sections mises à jour !")
    return redirect(url_for("admin_dashboard") + "#section-sections")


# ── Admin images ──────────────────────────────────────────────────────────────

VALID_IMAGE_SLOTS = {"hero_bear", "logo", "about", "cta_bear"}

@app.route("/admin/images/upload", methods=["POST"])
@login_required
def admin_images_upload():
    slot = request.form.get("slot", "").strip()
    if slot not in VALID_IMAGE_SLOTS:
        flash("Slot d'image invalide.")
        return redirect(url_for("admin_dashboard") + "#section-images")

    f = request.files.get("image")
    if not f or not f.filename or not allowed_file(f.filename):
        flash("Fichier invalide. Formats acceptés : JPG, PNG, WebP, GIF.")
        return redirect(url_for("admin_dashboard") + "#section-images")

    content = load_content()
    if "images" not in content:
        content["images"] = {}

    # Delete old file if one exists
    old = content["images"].get(slot)
    if old:
        old_path = os.path.join(IMAGES_UPLOAD_DIR, old)
        if os.path.exists(old_path):
            os.remove(old_path)

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S_")
    filename = ts + secure_filename(f.filename)
    f.save(os.path.join(IMAGES_UPLOAD_DIR, filename))

    content["images"][slot] = filename
    save_content(content)
    flash("Image mise à jour !")
    return redirect(url_for("admin_dashboard") + "#section-images")


@app.route("/admin/images/delete", methods=["POST"])
@login_required
def admin_images_delete():
    slot = request.form.get("slot", "").strip()
    if slot not in VALID_IMAGE_SLOTS:
        flash("Slot d'image invalide.")
        return redirect(url_for("admin_dashboard") + "#section-images")

    content = load_content()
    if "images" not in content:
        content["images"] = {}

    old = content["images"].get(slot)
    if old:
        old_path = os.path.join(IMAGES_UPLOAD_DIR, old)
        if os.path.exists(old_path):
            os.remove(old_path)
        content["images"][slot] = None
        save_content(content)
        flash("Image supprimée.")
    return redirect(url_for("admin_dashboard") + "#section-images")


if __name__ == "__main__":
    app.run(debug=True)
