import os
import uuid
from datetime import datetime

from flask import Flask, flash, redirect, render_template, request, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_
from PIL import Image
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

try:
    import easyocr
except ImportError:
    easyocr = None

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
PROFILE_FOLDER = os.path.join(BASE_DIR, "static", "profiles")

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "pdf"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg"}
PROFILE_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg"}
SUBJECTS = ["Maths", "Francais", "Histoire", "SVT", "Physique", "Anglais"]
LEVELS = ["5eme", "4eme", "3eme"]

# Moderation thresholds in normal mode.
OCR_MIN_TEXT_RATIO = 0.06
OCR_MIN_DETECTIONS = 2
FALLBACK_MIN_EDGE_RATIO = 0.06

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-eco-share")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(BASE_DIR, 'ecoshare.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["PROFILE_FOLDER"] = PROFILE_FOLDER

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs(app.config["PROFILE_FOLDER"], exist_ok=True)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

ocr_reader = None


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="eleve")
    school_class = db.Column(db.String(20), nullable=False, default="5eme")
    profile_image = db.Column(db.String(255), nullable=True)
    ui_theme = db.Column(db.String(20), nullable=False, default="light")
    ui_density = db.Column(db.String(20), nullable=False, default="comfortable")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Resource(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    filetype = db.Column(db.String(10), nullable=False)
    subject = db.Column(db.String(60), nullable=False)
    level = db.Column(db.String(20), nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_removed = db.Column(db.Boolean, default=False)

    uploaded_by = db.relationship("User", backref=db.backref("resources", lazy=True))


class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    resource_id = db.Column(db.Integer, db.ForeignKey("resource.id"), nullable=False)
    reporter_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    message = db.Column(db.String(255), nullable=False, default="Contenu signale")
    status = db.Column(db.String(20), nullable=False, default="new")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    resource = db.relationship("Resource", backref=db.backref("reports", lazy=True))
    reporter = db.relationship("User", backref=db.backref("reports", lazy=True))


class SyncLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    created_count = db.Column(db.Integer, nullable=False, default=0)
    updated_count = db.Column(db.Integer, nullable=False, default=0)
    triggered_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    triggered_by = db.relationship("User", backref=db.backref("sync_logs", lazy=True))


class AccountAuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    target_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    action = db.Column(db.String(60), nullable=False)
    details = db.Column(db.String(500), nullable=False, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    actor = db.relationship("User", foreign_keys=[actor_id], backref=db.backref("audit_actions", lazy=True))
    target_user = db.relationship("User", foreign_keys=[target_user_id], backref=db.backref("audit_events", lazy=True))


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


@app.context_processor
def inject_globals():
    new_reports = 0
    ui_theme = "light"
    ui_density = "comfortable"
    if current_user.is_authenticated and current_user.role == "admin":
        new_reports = Report.query.filter_by(status="new").count()
    if current_user.is_authenticated:
        ui_theme = current_user.ui_theme or "light"
        ui_density = current_user.ui_density or "comfortable"
    return {
        "SUBJECTS": SUBJECTS,
        "LEVELS": LEVELS,
        "new_reports_count": new_reports,
        "ui_theme": ui_theme,
        "ui_density": ui_density,
        "profile_image_url": profile_image_url,
    }


def ensure_user_preferences_columns():
    # Keeps existing SQLite databases compatible without needing Alembic migrations.
    with db.engine.connect() as conn:
        columns = conn.exec_driver_sql("PRAGMA table_info(user)").fetchall()
        column_names = {row[1] for row in columns}

        if "ui_theme" not in column_names:
            conn.exec_driver_sql("ALTER TABLE user ADD COLUMN ui_theme VARCHAR(20) NOT NULL DEFAULT 'light'")
        if "ui_density" not in column_names:
            conn.exec_driver_sql(
                "ALTER TABLE user ADD COLUMN ui_density VARCHAR(20) NOT NULL DEFAULT 'comfortable'"
            )
        if "profile_image" not in column_names:
            conn.exec_driver_sql("ALTER TABLE user ADD COLUMN profile_image VARCHAR(255)")


def is_admin():
    return current_user.is_authenticated and current_user.role == "admin"


def is_staff():
    return current_user.is_authenticated and current_user.role in {"admin", "prof"}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def allowed_profile_image(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in PROFILE_IMAGE_EXTENSIONS


def profile_image_url(user):
    if user and user.profile_image:
        return url_for("static", filename=f"profiles/{user.profile_image}")
    return None


def add_audit_log(target_user_id: int, action: str, details: str = "", actor_id: int | None = None):
    if actor_id is None and current_user.is_authenticated:
        actor_id = current_user.id
    log = AccountAuditLog(
        actor_id=actor_id,
        target_user_id=target_user_id,
        action=action,
        details=details[:500],
    )
    db.session.add(log)


def polygon_area(points):
    area = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def get_ocr_reader():
    global ocr_reader
    if ocr_reader is None and easyocr is not None:
        ocr_reader = easyocr.Reader(["fr", "en"], gpu=False)
    return ocr_reader


def text_ratio_from_image(image_path: str):
    if easyocr is None:
        return 0.0, 0

    with Image.open(image_path) as img:
        width, height = img.size
    image_area = max(width * height, 1)

    reader = get_ocr_reader()
    detections = reader.readtext(image_path)

    text_area = 0.0
    for detection in detections:
        bbox = detection[0]
        text_area += polygon_area(bbox)

    ratio = text_area / image_area
    return max(0.0, min(ratio, 1.0)), len(detections)


def fallback_text_like_ratio(image_path: str) -> float:
    """Estimate text-like density from local contrast when OCR is unavailable."""
    with Image.open(image_path) as img:
        gray = img.convert("L")
        gray.thumbnail((1200, 1200))

    pixels = list(gray.getdata())
    width, height = gray.size
    if width < 3 or height < 3:
        return 0.0

    strong_edges = 0
    samples = 0
    threshold = 28

    for y in range(height - 1):
        row_offset = y * width
        next_row_offset = (y + 1) * width
        for x in range(width - 1):
            idx = row_offset + x
            gx = abs(pixels[idx] - pixels[idx + 1])
            gy = abs(pixels[idx] - pixels[next_row_offset + x])
            if gx > threshold or gy > threshold:
                strong_edges += 1
            samples += 1

    return strong_edges / max(samples, 1)


def simulate_ecole_directe_import():
    return [
        {
            "username": "leo_m",
            "email": "leo.m@college.local",
            "role": "eleve",
            "school_class": "5eme",
            "password": "demo1234",
        },
        {
            "username": "sara_d",
            "email": "sara.d@college.local",
            "role": "eleve",
            "school_class": "4eme",
            "password": "demo1234",
        },
        {
            "username": "admin_college",
            "email": "admin@college.local",
            "role": "admin",
            "school_class": "3eme",
            "password": "admin1234",
        },
        {
            "username": "prof_maths",
            "email": "prof.maths@college.local",
            "role": "prof",
            "school_class": "3eme",
            "password": "prof1234",
        },
    ]


def synchronize_accounts_from_ecole_directe():
    created = 0
    updated = 0

    for item in simulate_ecole_directe_import():
        user = User.query.filter(or_(User.username == item["username"], User.email == item["email"])).first()
        if user is None:
            user = User(
                username=item["username"],
                email=item["email"],
                role=item["role"],
                school_class=item["school_class"],
                password_hash=generate_password_hash(item["password"]),
            )
            db.session.add(user)
            created += 1
            continue

        changed = False
        if user.username != item["username"]:
            user.username = item["username"]
            changed = True
        if user.role != item["role"]:
            user.role = item["role"]
            changed = True
        if user.school_class != item["school_class"]:
            user.school_class = item["school_class"]
            changed = True

        if changed:
            updated += 1

    return created, updated


@app.route("/")
def index():
    if not current_user.is_authenticated:
        return redirect(url_for("login"))

    class_filter = request.args.get("classe")
    subject_filter = request.args.get("matiere", "all").strip()
    search_query = request.args.get("q", "").strip()
    if not class_filter:
        if current_user.is_authenticated and current_user.role == "eleve":
            class_filter = current_user.school_class
        else:
            class_filter = "all"

    query = Resource.query.filter_by(is_removed=False)
    if class_filter != "all":
        query = query.filter_by(level=class_filter)
    if subject_filter != "all" and subject_filter in SUBJECTS:
        query = query.filter_by(subject=subject_filter)
    if search_query:
        like_query = f"%{search_query}%"
        query = query.filter(Resource.original_filename.ilike(like_query))

    resources = query.order_by(Resource.created_at.desc()).all()
    class_locked = current_user.is_authenticated and current_user.role == "eleve"
    density = "comfortable"
    if current_user.is_authenticated and current_user.ui_density in {"comfortable", "compact"}:
        density = current_user.ui_density

    return render_template(
        "index.html",
        resources=resources,
        class_filter=class_filter,
        subject_filter=subject_filter,
        search_query=search_query,
        class_locked=class_locked,
        density=density,
    )


@app.route("/settings/ui", methods=["POST"])
@login_required
def update_ui_settings():
    theme = request.form.get("ui_theme", "light")
    density = request.form.get("ui_density", "comfortable")
    next_url = request.form.get("next") or request.referrer or url_for("index")

    if theme not in {"light", "dark"}:
        theme = "light"
    if density not in {"comfortable", "compact"}:
        density = "comfortable"

    current_user.ui_theme = theme
    current_user.ui_density = density
    db.session.commit()
    flash("Preferences d'affichage mises a jour.", "success")
    return redirect(next_url)


@app.route("/settings", methods=["GET", "POST"])
@login_required
def account_settings():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        school_class = request.form.get("school_class", "").strip()
        new_password = request.form.get("new_password", "")
        ui_theme = request.form.get("ui_theme", "light")
        ui_density = request.form.get("ui_density", "comfortable")

        if not username or not email:
            flash("Nom d'utilisateur et email obligatoires.", "error")
            return redirect(url_for("account_settings"))

        if school_class not in LEVELS:
            flash("Classe invalide.", "error")
            return redirect(url_for("account_settings"))

        if ui_theme not in {"light", "dark"}:
            ui_theme = "light"
        if ui_density not in {"comfortable", "compact"}:
            ui_density = "comfortable"

        username_taken = User.query.filter(User.username == username, User.id != current_user.id).first()
        email_taken = User.query.filter(User.email == email, User.id != current_user.id).first()
        if username_taken or email_taken:
            flash("Nom d'utilisateur ou email deja utilise.", "error")
            return redirect(url_for("account_settings"))

        old_username = current_user.username
        old_email = current_user.email
        old_class = current_user.school_class
        old_theme = current_user.ui_theme
        old_density = current_user.ui_density

        changed_fields = []
        if old_username != username:
            changed_fields.append("username")
        if old_email != email:
            changed_fields.append("email")
        if old_class != school_class:
            changed_fields.append("classe")
        if old_theme != ui_theme:
            changed_fields.append("theme")
        if old_density != ui_density:
            changed_fields.append("densite")

        current_user.username = username
        current_user.email = email
        current_user.school_class = school_class
        current_user.ui_theme = ui_theme
        current_user.ui_density = ui_density

        if new_password:
            if len(new_password) < 6:
                flash("Le mot de passe doit contenir au moins 6 caracteres.", "error")
                return redirect(url_for("account_settings"))
            current_user.password_hash = generate_password_hash(new_password)
            changed_fields.append("mot_de_passe")

        if changed_fields:
            add_audit_log(
                target_user_id=current_user.id,
                action="self_account_update",
                details=f"Champs modifies: {', '.join(changed_fields)}",
            )

        db.session.commit()
        flash("Parametres du compte mis a jour.", "success")
        return redirect(url_for("account_settings"))

    return render_template("settings.html")


@app.route("/settings/profile-photo", methods=["POST"])
@login_required
def update_profile_photo():
    photo = request.files.get("profile_photo")
    if not photo or photo.filename == "":
        flash("Veuillez choisir une image de profil.", "error")
        return redirect(url_for("account_settings"))

    if not allowed_profile_image(photo.filename):
        flash("Format invalide pour la photo de profil (JPG/PNG).", "error")
        return redirect(url_for("account_settings"))

    ext = secure_filename(photo.filename).rsplit(".", 1)[1].lower()
    filename = f"user_{current_user.id}_{uuid.uuid4().hex}.{ext}"
    save_path = os.path.join(app.config["PROFILE_FOLDER"], filename)
    photo.save(save_path)

    if current_user.profile_image:
        old_path = os.path.join(app.config["PROFILE_FOLDER"], current_user.profile_image)
        if os.path.exists(old_path):
            os.remove(old_path)

    current_user.profile_image = filename
    add_audit_log(
        target_user_id=current_user.id,
        action="profile_photo_update",
        details="Photo de profil mise a jour",
    )
    db.session.commit()
    flash("Photo de profil mise a jour.", "success")
    return redirect(url_for("account_settings"))


@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    prefill_subject = request.args.get("subject", "").strip()
    prefill_level = request.args.get("level", "").strip()

    if prefill_subject not in SUBJECTS:
        prefill_subject = ""
    if prefill_level not in LEVELS:
        prefill_level = ""

    if current_user.role == "eleve":
        prefill_level = current_user.school_class

    if request.method == "POST":
        subject = request.form.get("subject", "").strip()
        level = request.form.get("level", "").strip()
        file = request.files.get("file")

        if current_user.role == "eleve":
            level = current_user.school_class

        if subject not in SUBJECTS or level not in LEVELS:
            flash("Matiere ou niveau invalide.", "error")
            return redirect(url_for("upload"))

        if not file or file.filename == "":
            flash("Veuillez selectionner un fichier.", "error")
            return redirect(url_for("upload"))

        if not allowed_file(file.filename):
            flash("Format non autorise. Utilisez JPG, PNG ou PDF.", "error")
            return redirect(url_for("upload"))

        original_filename = secure_filename(file.filename)
        ext = original_filename.rsplit(".", 1)[1].lower()
        unique_name = f"{uuid.uuid4().hex}.{ext}"
        temp_path = os.path.join(app.config["UPLOAD_FOLDER"], unique_name)
        file.save(temp_path)

        if ext in IMAGE_EXTENSIONS:
            ratio, detections_count = text_ratio_from_image(temp_path)
            # OCR unavailable: normal fallback filter.
            if easyocr is None:
                fallback_ratio = fallback_text_like_ratio(temp_path)
                if fallback_ratio < FALLBACK_MIN_EDGE_RATIO:
                    os.remove(temp_path)
                    flash("Ceci n'est pas un cours valide", "error")
                    return redirect(url_for("upload"))

            # OCR available: normal mode rejects only if both signals are weak.
            if ratio < OCR_MIN_TEXT_RATIO and detections_count < OCR_MIN_DETECTIONS:
                if easyocr is not None:
                    os.remove(temp_path)
                    flash("Ceci n'est pas un cours valide", "error")
                    return redirect(url_for("upload"))

        new_resource = Resource(
            filename=unique_name,
            original_filename=original_filename,
            filetype=ext,
            subject=subject,
            level=level,
            uploaded_by_id=current_user.id,
        )
        db.session.add(new_resource)
        db.session.commit()

        flash("Ressource ajoutee avec succes.", "success")
        return redirect(url_for("index"))

    recent_resources = (
        Resource.query.filter_by(uploaded_by_id=current_user.id, is_removed=False)
        .order_by(Resource.created_at.desc())
        .limit(5)
        .all()
    )
    recent_subjects = []
    for resource in recent_resources:
        if resource.subject not in recent_subjects:
            recent_subjects.append(resource.subject)

    return render_template(
        "upload.html",
        prefill_subject=prefill_subject,
        prefill_level=prefill_level,
        recent_subjects=recent_subjects,
    )


@app.route("/upload/quick")
@login_required
def quick_upload():
    last_resource = (
        Resource.query.filter_by(uploaded_by_id=current_user.id, is_removed=False)
        .order_by(Resource.created_at.desc())
        .first()
    )

    if not last_resource:
        flash("Aucune fiche precedente. Remplissez une premiere fiche.", "error")
        return redirect(url_for("upload"))

    flash("Reglages de votre derniere fiche precharges.", "success")
    return redirect(url_for("upload", subject=last_resource.subject, level=last_resource.level))


@app.route("/my-fiches")
@login_required
def my_resources():
    resources = (
        Resource.query.filter_by(uploaded_by_id=current_user.id, is_removed=False)
        .order_by(Resource.created_at.desc())
        .all()
    )
    return render_template("my_resources.html", resources=resources)


@app.route("/report/<int:resource_id>", methods=["POST"])
@login_required
def report_resource(resource_id):
    resource = Resource.query.get_or_404(resource_id)
    if resource.is_removed:
        flash("Ressource introuvable.", "error")
        return redirect(url_for("index"))

    existing = Report.query.filter_by(resource_id=resource_id, reporter_id=current_user.id).first()
    if existing:
        flash("Vous avez deja signale cette ressource.", "error")
        return redirect(url_for("index"))

    report = Report(resource_id=resource_id, reporter_id=current_user.id, message="Contenu non conforme")
    db.session.add(report)
    db.session.commit()
    flash("Signalement envoye a l'admin.", "success")
    return redirect(url_for("index"))


@app.route("/admin")
@login_required
def admin_dashboard():
    if not is_staff():
        flash("Acces reserve au staff.", "error")
        return redirect(url_for("index"))

    resources = Resource.query.order_by(Resource.created_at.desc()).all()
    reports = Report.query.order_by(Report.created_at.desc()).all()
    sync_logs = SyncLog.query.order_by(SyncLog.created_at.desc()).limit(10).all()
    users = User.query.order_by(User.created_at.desc()).all()
    audit_logs = AccountAuditLog.query.order_by(AccountAuditLog.created_at.desc()).limit(100).all()

    for report in reports:
        if report.status == "new":
            report.status = "seen"
    db.session.commit()

    return render_template(
        "admin.html",
        resources=resources,
        reports=reports,
        sync_logs=sync_logs,
        users=users,
        audit_logs=audit_logs,
    )


@app.route("/admin/report/<int:report_id>/close", methods=["POST"])
@login_required
def admin_close_report(report_id):
    if not is_staff():
        flash("Acces reserve au staff.", "error")
        return redirect(url_for("index"))

    report = Report.query.get_or_404(report_id)
    report.status = "closed"
    db.session.commit()
    flash("Signalement annule/ferme.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/resource/<int:resource_id>/delete", methods=["POST"])
@login_required
def admin_delete_resource(resource_id):
    if not is_staff():
        flash("Acces reserve au staff.", "error")
        return redirect(url_for("index"))

    resource = Resource.query.get_or_404(resource_id)
    resource.is_removed = True

    file_path = os.path.join(app.config["UPLOAD_FOLDER"], resource.filename)
    if os.path.exists(file_path):
        os.remove(file_path)

    for report in resource.reports:
        report.status = "closed"

    db.session.commit()
    flash("Ressource supprimee.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/import-demo", methods=["POST"])
@login_required
def admin_import_demo():
    if not is_admin():
        flash("Acces reserve aux admins.", "error")
        return redirect(url_for("index"))

    imported = 0
    for item in simulate_ecole_directe_import():
        user = User.query.filter((User.username == item["username"]) | (User.email == item["email"])).first()
        if user:
            continue
        new_user = User(
            username=item["username"],
            email=item["email"],
            role=item["role"],
            school_class=item["school_class"],
            password_hash=generate_password_hash(item["password"]),
        )
        db.session.add(new_user)
        imported += 1

    db.session.commit()
    flash(f"Import termine: {imported} compte(s) ajoute(s).", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/clear-history", methods=["POST"])
@login_required
def admin_clear_history():
    if not is_admin():
        flash("Acces reserve aux admins.", "error")
        return redirect(url_for("index"))

    resources = Resource.query.all()
    removed_files = 0
    for resource in resources:
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], resource.filename)
        if os.path.exists(file_path):
            os.remove(file_path)
            removed_files += 1

    Report.query.delete(synchronize_session=False)
    Resource.query.delete(synchronize_session=False)
    db.session.commit()

    flash(
        f"Historique supprime: {len(resources)} ressource(s), signalements associes, {removed_files} fichier(s) efface(s).",
        "success",
    )
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/user/<int:user_id>/delete", methods=["POST"])
@login_required
def staff_delete_user(user_id):
    if not is_staff():
        flash("Acces reserve au staff.", "error")
        return redirect(url_for("index"))

    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("Vous ne pouvez pas supprimer votre propre compte ici.", "error")
        return redirect(url_for("admin_dashboard"))

    if user.role == "admin" and current_user.role != "admin":
        flash("Seul un admin peut supprimer un compte admin.", "error")
        return redirect(url_for("admin_dashboard"))

    if current_user.role == "prof" and user.role != "eleve":
        flash("Un prof peut supprimer uniquement les comptes eleves.", "error")
        return redirect(url_for("admin_dashboard"))

    deleted_snapshot = f"username={user.username}; role={user.role}; classe={user.school_class}"

    user_resources = Resource.query.filter_by(uploaded_by_id=user.id).all()
    for resource in user_resources:
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], resource.filename)
        if os.path.exists(file_path):
            os.remove(file_path)
        Report.query.filter_by(resource_id=resource.id).delete(synchronize_session=False)

    if user.profile_image:
        profile_path = os.path.join(app.config["PROFILE_FOLDER"], user.profile_image)
        if os.path.exists(profile_path):
            os.remove(profile_path)

    Resource.query.filter_by(uploaded_by_id=user.id).delete(synchronize_session=False)
    Report.query.filter_by(reporter_id=user.id).delete(synchronize_session=False)
    db.session.delete(user)
    add_audit_log(
        target_user_id=user_id,
        action="staff_delete_user",
        details=deleted_snapshot,
    )
    db.session.commit()

    flash("Compte supprime par le staff.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/user/<int:user_id>/update", methods=["POST"])
@login_required
def admin_update_user(user_id):
    if not is_admin():
        flash("Acces reserve aux admins.", "error")
        return redirect(url_for("index"))

    user = User.query.get_or_404(user_id)
    original_role = user.role
    original_class = user.school_class
    role = request.form.get("role", user.role)
    school_class = request.form.get("school_class", user.school_class)
    new_password = request.form.get("new_password", "")

    if role not in {"eleve", "prof", "admin"}:
        flash("Role invalide.", "error")
        return redirect(url_for("admin_dashboard"))
    if school_class not in LEVELS:
        flash("Classe invalide.", "error")
        return redirect(url_for("admin_dashboard"))

    # Prevent locking the platform without any admin account.
    if user.id == current_user.id and role != "admin":
        flash("Vous ne pouvez pas retirer votre propre role admin.", "error")
        return redirect(url_for("admin_dashboard"))

    user.role = role
    user.school_class = school_class

    if new_password:
        if len(new_password) < 6:
            flash("Le nouveau mot de passe doit contenir au moins 6 caracteres.", "error")
            return redirect(url_for("admin_dashboard"))
        user.password_hash = generate_password_hash(new_password)

    changes = []
    if original_role != role:
        changes.append(f"role: {original_role} -> {role}")
    if original_class != school_class:
        changes.append(f"classe: {original_class} -> {school_class}")
    if new_password:
        changes.append("mot_de_passe: reset")

    if changes:
        add_audit_log(
            target_user_id=user.id,
            action="admin_update_user",
            details="; ".join(changes),
        )

    db.session.commit()
    flash("Compte utilisateur mis a jour.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/sync-comptes", methods=["POST"])
@login_required
def admin_sync_accounts():
    if not is_admin():
        flash("Acces reserve aux admins.", "error")
        return redirect(url_for("index"))

    created, updated = synchronize_accounts_from_ecole_directe()
    sync_log = SyncLog(created_count=created, updated_count=updated, triggered_by_id=current_user.id)
    db.session.add(sync_log)
    db.session.commit()

    flash(f"Synchronisation terminee: {created} cree(s), {updated} mis a jour.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/create-prof", methods=["POST"])
@login_required
def admin_create_prof():
    if not is_admin():
        flash("Acces reserve aux admins.", "error")
        return redirect(url_for("index"))

    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    school_class = request.form.get("school_class", "3eme")

    if not username or not email or len(password) < 6 or school_class not in LEVELS:
        flash("Donnees prof invalides.", "error")
        return redirect(url_for("admin_dashboard"))

    exists = User.query.filter(or_(User.username == username, User.email == email)).first()
    if exists:
        flash("Nom d'utilisateur ou email deja utilise.", "error")
        return redirect(url_for("admin_dashboard"))

    prof = User(
        username=username,
        email=email,
        password_hash=generate_password_hash(password),
        role="prof",
        school_class=school_class,
    )
    db.session.add(prof)
    db.session.flush()
    add_audit_log(
        target_user_id=prof.id,
        action="admin_create_prof",
        details=f"Compte prof cree (classe {school_class})",
    )
    db.session.commit()
    flash("Compte prof cree.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = "eleve"
        school_class = request.form.get("school_class", "5eme")

        if school_class not in LEVELS:
            flash("Classe invalide.", "error")
            return redirect(url_for("register"))

        exists = User.query.filter((User.username == username) | (User.email == email)).first()
        if exists:
            flash("Nom d'utilisateur ou email deja utilise.", "error")
            return redirect(url_for("register"))

        user = User(
            username=username,
            email=email,
            password_hash=generate_password_hash(password),
            role=role,
            school_class=school_class,
        )
        db.session.add(user)
        db.session.flush()
        add_audit_log(
            target_user_id=user.id,
            action="account_created",
            details=f"Inscription ({user.role})",
            actor_id=user.id,
        )
        db.session.commit()

        flash("Compte cree, vous pouvez vous connecter.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            flash("Connexion reussie.", "success")
            return redirect(url_for("index"))

        flash("Identifiants invalides.", "error")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Deconnexion effectuee.", "success")
    return redirect(url_for("index"))


@app.route("/settings/delete-account", methods=["POST"])
@login_required
def delete_account():
    password = request.form.get("password", "")
    if not check_password_hash(current_user.password_hash, password):
        flash("Mot de passe incorrect. Suppression annulee.", "error")
        return redirect(url_for("account_settings"))

    user_id = current_user.id
    logout_user()

    user = db.session.get(User, user_id)
    if user is None:
        flash("Compte deja supprime.", "error")
        return redirect(url_for("login"))

    user_resources = Resource.query.filter_by(uploaded_by_id=user_id).all()
    for resource in user_resources:
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], resource.filename)
        if os.path.exists(file_path):
            os.remove(file_path)
        Report.query.filter_by(resource_id=resource.id).delete(synchronize_session=False)

    if user.profile_image:
        profile_path = os.path.join(app.config["PROFILE_FOLDER"], user.profile_image)
        if os.path.exists(profile_path):
            os.remove(profile_path)

    Resource.query.filter_by(uploaded_by_id=user_id).delete(synchronize_session=False)
    Report.query.filter_by(reporter_id=user_id).delete(synchronize_session=False)
    db.session.delete(user)
    db.session.commit()

    flash("Votre compte a ete supprime.", "success")
    return redirect(url_for("login"))


@app.cli.command("init-db")
def init_db_command():
    db.create_all()
    ensure_user_preferences_columns()

    if not User.query.filter_by(email="admin@eco-share.local").first():
        admin = User(
            username="superadmin",
            email="admin@eco-share.local",
            password_hash=generate_password_hash("admin1234"),
            role="admin",
            school_class="3eme",
        )
        db.session.add(admin)
        db.session.commit()

    print("Base initialisee.")


def bootstrap():
    with app.app_context():
        db.create_all()
        ensure_user_preferences_columns()
        if not User.query.filter_by(email="admin@eco-share.local").first():
            admin = User(
                username="superadmin",
                email="admin@eco-share.local",
                password_hash=generate_password_hash("admin1234"),
                role="admin",
                school_class="3eme",
            )
            db.session.add(admin)
            db.session.commit()


if __name__ == "__main__":
    bootstrap()
    app.run(debug=True)
