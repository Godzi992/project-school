import os
import re
import uuid
import ipaddress
import json
import base64
from io import BytesIO
from datetime import datetime
from xml.sax.saxutils import escape

from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_
from PIL import Image
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

try:
    from reportlab.lib import colors  # type: ignore[import-not-found]
    from reportlab.lib.enums import TA_CENTER  # type: ignore[import-not-found]
    from reportlab.lib.pagesizes import A4  # type: ignore[import-not-found]
    from reportlab.lib.styles import ParagraphStyle  # type: ignore[import-not-found]
    from reportlab.pdfgen import canvas  # type: ignore[import-not-found]
    from reportlab.platypus import BaseDocTemplate, Frame, KeepInFrame, PageTemplate, Paragraph, Spacer, Image as RLImage  # type: ignore[import-not-found]
except ImportError:
    colors = None
    TA_CENTER = None
    A4 = None
    ParagraphStyle = None
    BaseDocTemplate = None
    Frame = None
    KeepInFrame = None
    PageTemplate = None
    Paragraph = None
    Spacer = None
    RLImage = None
    canvas = None

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


class GuestThemePreference(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ip_address = db.Column(db.String(64), unique=True, nullable=False, index=True)
    ui_theme = db.Column(db.String(20), nullable=False, default="light")
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MindmapPublication(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(140), nullable=False)
    subject = db.Column(db.String(60), nullable=False)
    level = db.Column(db.String(20), nullable=False)
    payload_json = db.Column(db.Text, nullable=False)
    preview_image_data = db.Column(db.Text, nullable=False, default="")
    share_token = db.Column(db.String(40), unique=True, nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    created_by = db.relationship("User", backref=db.backref("published_mindmaps", lazy=True))


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def get_client_ip() -> str:
    """Try to resolve the visitor public IP from proxy headers, then fallback to remote address."""
    candidates = []
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        candidates.extend([part.strip() for part in xff.split(",") if part.strip()])

    x_real_ip = request.headers.get("X-Real-IP", "").strip()
    if x_real_ip:
        candidates.append(x_real_ip)

    remote = (request.remote_addr or "").strip()
    if remote:
        candidates.append(remote)

    if not candidates:
        return "unknown"

    for candidate in candidates:
        try:
            parsed = ipaddress.ip_address(candidate)
            if parsed.is_global:
                return candidate
        except ValueError:
            continue

    return candidates[0]


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
    else:
        client_ip = get_client_ip()
        stored_pref = GuestThemePreference.query.filter_by(ip_address=client_ip).first()
        if stored_pref and stored_pref.ui_theme in {"light", "dark"}:
            ui_theme = stored_pref.ui_theme
        else:
            ui_theme = session.get("guest_ui_theme", "light")
    return {
        "SUBJECTS": SUBJECTS,
        "LEVELS": LEVELS,
        "new_reports_count": new_reports,
        "ui_theme": ui_theme,
        "ui_density": ui_density,
        "profile_image_url": profile_image_url,
    }


def get_effective_theme() -> str:
    if current_user.is_authenticated:
        return current_user.ui_theme if current_user.ui_theme in {"light", "dark"} else "light"

    client_ip = get_client_ip()
    stored_pref = GuestThemePreference.query.filter_by(ip_address=client_ip).first()
    if stored_pref and stored_pref.ui_theme in {"light", "dark"}:
        return stored_pref.ui_theme
    return session.get("guest_ui_theme", "light")


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


def infer_paragraphs(content: str):
    """Create readable paragraphs from raw user text when no blank lines are provided."""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    if "\n\n" in normalized:
        blocks = [block.strip() for block in re.split(r"\n\s*\n+", normalized) if block.strip()]
        return blocks

    lines = [line.strip() for line in normalized.split("\n") if line.strip()]
    if len(lines) > 1:
        return lines

    sentence_parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", normalized) if part.strip()]
    if len(sentence_parts) <= 2:
        return [normalized]

    paragraphs = []
    current = []
    current_len = 0
    for sentence in sentence_parts:
        current.append(sentence)
        current_len += len(sentence)
        if len(current) >= 3 or current_len >= 280:
            paragraphs.append(" ".join(current).strip())
            current = []
            current_len = 0

    if current:
        paragraphs.append(" ".join(current).strip())
    return paragraphs


def normalize_structured_content(content: str) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""

    # Fix glued words from some AI outputs, e.g. "DefinitionUne" -> "Definition Une".
    normalized = re.sub(r"([a-zA-ZÀ-ÿ])([A-ZÀ-ÖØ-Þ])", r"\1 \2", normalized)

    # If numbered sections are glued in one line, force visual section breaks.
    normalized = re.sub(r"([.!?])\s+(\d+\.\s+[A-ZÀ-ÖØ-Þ])", r"\1\n\n\2", normalized)
    return normalized


def latex_to_readable(math_expr: str) -> str:
    expr = math_expr.strip()
    if not expr:
        return ""

    expr = expr.replace("\\left", "").replace("\\right", "")

    for _ in range(5):
        updated = re.sub(r"\\frac\{([^{}]+)\}\{([^{}]+)\}", r"(\1)/(\2)", expr)
        if updated == expr:
            break
        expr = updated

    expr = re.sub(r"\\sqrt\{([^{}]+)\}", r"sqrt(\1)", expr)
    expr = re.sub(r"\^\{([^{}]+)\}", r"^(\1)", expr)
    expr = re.sub(r"_\{([^{}]+)\}", r"_(\1)", expr)

    replacements = {
        r"\\times": "x",
        r"\\cdot": ".",
        r"\\neq": "!=",
        r"\\leq": "<=",
        r"\\geq": ">=",
        r"\\to": "->",
        r"\\alpha": "alpha",
        r"\\beta": "beta",
        r"\\gamma": "gamma",
        r"\\Delta": "Delta",
        r"\\sum": "sum",
        r"\\int": "int",
    }
    for latex_token, replacement in replacements.items():
        expr = expr.replace(latex_token, replacement)

    expr = expr.replace("{", "(").replace("}", ")")
    expr = re.sub(r"\s+", " ", expr).strip()
    return expr


def render_custom_markup(text: str) -> str:
    """Convert lightweight user markers and inline math to ReportLab paragraph markup.

    Supported markers:
    - §texte§ or **texte** => bold
    - *texte* => italic
    - $...$ => inline math (readable conversion)
    """

    segments = re.split(r"(\$[^$\n]+\$)", text)
    rendered_parts = []

    for segment in segments:
        if not segment:
            continue

        if segment.startswith("$") and segment.endswith("$"):
            inline_math = escape(latex_to_readable(segment[1:-1]))
            rendered_parts.append(f"<font name='Courier-Bold'>{inline_math}</font>")
            continue

        safe = escape(segment)
        safe = re.sub(r"§([^§\n]{1,200})§", r"<b>\1</b>", safe)
        safe = re.sub(r"\*\*([^*\n]{1,200})\*\*", r"<b>\1</b>", safe)
        safe = re.sub(r"__([^_\n]{1,200})__", r"<b>\1</b>", safe)
        safe = re.sub(r"(?<!\*)\*([^*\n]{1,200})\*(?!\*)", r"<i>\1</i>", safe)
        rendered_parts.append(safe)

    return "".join(rendered_parts).replace("\n", "<br/>")


def parse_content_blocks(content: str):
    """Parse AI/user text into structured blocks: heading, paragraph, bullet, math."""
    normalized = normalize_structured_content(content)
    if not normalized:
        return []

    lines = normalized.split("\n")
    blocks = []
    current_paragraph_lines = []
    math_buffer = []
    in_math_block = False

    def flush_paragraph_lines():
        nonlocal current_paragraph_lines
        if not current_paragraph_lines:
            return
        chunk = "\n".join(current_paragraph_lines).strip()
        for para in infer_paragraphs(chunk):
            blocks.append({"type": "paragraph", "text": para})
        current_paragraph_lines = []

    for raw_line in lines:
        line = raw_line.strip()

        if in_math_block:
            if "$$" in line:
                before, _, after = line.partition("$$")
                if before.strip():
                    math_buffer.append(before.strip())
                if math_buffer:
                    blocks.append({"type": "math", "text": " ".join(math_buffer)})
                math_buffer = []
                in_math_block = False
                line = after.strip()
                if not line:
                    continue
            else:
                if line:
                    math_buffer.append(line)
                continue

        if not line:
            flush_paragraph_lines()
            continue

        if line.startswith("$$") and line.endswith("$$") and len(line) > 4:
            flush_paragraph_lines()
            blocks.append({"type": "math", "text": line[2:-2].strip()})
            continue

        if line.startswith("$$"):
            flush_paragraph_lines()
            in_math_block = True
            start_part = line[2:].strip()
            if start_part:
                math_buffer.append(start_part)
            continue

        heading_only_match = re.match(r"^#{1,3}\s+(.+)$", line)
        if heading_only_match:
            flush_paragraph_lines()
            blocks.append({"type": "heading", "text": heading_only_match.group(1).strip()})
            continue

        numbered_heading_match = re.match(r"^(\d+\.\s+[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ'\- ]{2,55}?)(?=\s+[A-ZÀ-ÖØ-Þ][a-zà-ÿ]{2,}\s)", line)
        if numbered_heading_match and len(line) > len(numbered_heading_match.group(1)) + 12:
            flush_paragraph_lines()
            heading = numbered_heading_match.group(1).strip()
            remainder = line[len(numbered_heading_match.group(1)) :].strip()
            blocks.append({"type": "heading", "text": heading})
            if remainder:
                current_paragraph_lines.append(remainder)
            continue

        if re.match(r"^\d+\.\s+.+$", line):
            flush_paragraph_lines()
            blocks.append({"type": "heading", "text": line})
            continue

        bullet_match = re.match(r"^[-*•]\s+(.+)$", line)
        if bullet_match:
            flush_paragraph_lines()
            blocks.append({"type": "bullet", "text": bullet_match.group(1).strip()})
            continue

        single_math_match = re.match(r"^\$(.+)\$$", line)
        if single_math_match and "$" not in single_math_match.group(1):
            flush_paragraph_lines()
            blocks.append({"type": "math", "text": single_math_match.group(1).strip()})
            continue

        current_paragraph_lines.append(line)

    flush_paragraph_lines()
    if math_buffer:
        blocks.append({"type": "math", "text": " ".join(math_buffer)})

    return blocks


DEFAULT_PDF_STUDIO_SETTINGS = {
    "header_height": 128,
    "title_font_size": 22,
    "subtitle_font_size": 11,
    "body_font_size": 11,
    "heading_font_size": 14,
    "title_left_pct": 7.2,
    "title_top_pct": 6.4,
    "subtitle_left_pct": 7.2,
    "subtitle_top_pct": 8.8,
    "content_left_pct": 7.2,
    "content_top_pct": 26.5,
}


def clamp_number(value, min_value: float, max_value: float, default: float):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(max_value, parsed))


def parse_pdf_studio_settings(raw_layout: str | None):
    settings = dict(DEFAULT_PDF_STUDIO_SETTINGS)
    if not raw_layout:
        return settings

    try:
        payload = json.loads(raw_layout)
    except (TypeError, ValueError, json.JSONDecodeError):
        return settings

    if not isinstance(payload, dict):
        return settings

    settings["header_height"] = int(clamp_number(payload.get("header_height"), 90, 210, settings["header_height"]))
    settings["title_font_size"] = int(clamp_number(payload.get("title_font_size"), 16, 34, settings["title_font_size"]))
    settings["subtitle_font_size"] = int(
        clamp_number(payload.get("subtitle_font_size"), 9, 20, settings["subtitle_font_size"])
    )
    settings["body_font_size"] = int(clamp_number(payload.get("body_font_size"), 9, 16, settings["body_font_size"]))
    settings["heading_font_size"] = int(
        clamp_number(payload.get("heading_font_size"), 11, 22, settings["heading_font_size"])
    )

    settings["title_left_pct"] = clamp_number(payload.get("title_left_pct"), 4.0, 75.0, settings["title_left_pct"])
    settings["title_top_pct"] = clamp_number(payload.get("title_top_pct"), 4.0, 26.0, settings["title_top_pct"])
    settings["subtitle_left_pct"] = clamp_number(
        payload.get("subtitle_left_pct"), 4.0, 78.0, settings["subtitle_left_pct"]
    )
    settings["subtitle_top_pct"] = clamp_number(payload.get("subtitle_top_pct"), 6.0, 30.0, settings["subtitle_top_pct"])
    settings["content_left_pct"] = clamp_number(
        payload.get("content_left_pct"), 4.0, 30.0, settings["content_left_pct"]
    )
    settings["content_top_pct"] = clamp_number(payload.get("content_top_pct"), 22.0, 70.0, settings["content_top_pct"])
    return settings


def generate_stylish_pdf(
    file_path: str,
    title: str,
    subtitle: str,
    content: str,
    author_name: str,
    accent_hex: str,
    studio_settings: dict | None = None,
    mindmap_image_data: str = "",
):
    if (
        colors is None
        or A4 is None
        or canvas is None
        or ParagraphStyle is None
        or BaseDocTemplate is None
        or Frame is None
        or KeepInFrame is None
        or PageTemplate is None
        or Paragraph is None
        or Spacer is None
        or RLImage is None
    ):
        raise RuntimeError("ReportLab non disponible")

    settings = dict(DEFAULT_PDF_STUDIO_SETTINGS)
    if studio_settings:
        settings.update(studio_settings)

    page_width, page_height = A4
    margin = 42
    header_height = int(settings["header_height"])
    accent = colors.HexColor(accent_hex)
    accent_soft = colors.Color(
        min(accent.red + 0.22, 1.0),
        min(accent.green + 0.22, 1.0),
        min(accent.blue + 0.22, 1.0),
    )

    def draw_page_header(pdf_canvas, page_number: int):
        pdf_canvas.setFillColor(accent)
        pdf_canvas.rect(0, page_height - header_height, page_width, header_height, stroke=0, fill=1)

        pdf_canvas.setFillColor(accent_soft)
        pdf_canvas.circle(page_width - 48, page_height - 35, 55, stroke=0, fill=1)
        pdf_canvas.circle(page_width - 95, page_height - 78, 28, stroke=0, fill=1)

        title_x = (settings["title_left_pct"] / 100.0) * page_width
        title_y = page_height - ((settings["title_top_pct"] / 100.0) * page_height)
        subtitle_x = (settings["subtitle_left_pct"] / 100.0) * page_width
        subtitle_y = page_height - ((settings["subtitle_top_pct"] / 100.0) * page_height)

        pdf_canvas.setFillColor(colors.white)
        pdf_canvas.setFont("Helvetica-Bold", int(settings["title_font_size"]))
        pdf_canvas.drawString(title_x, title_y, title[:70])

        if subtitle:
            pdf_canvas.setFont("Helvetica", int(settings["subtitle_font_size"]))
            pdf_canvas.drawString(subtitle_x, subtitle_y, subtitle[:110])

        pdf_canvas.setFont("Helvetica-Bold", 9)
        pdf_canvas.drawRightString(page_width - margin, page_height - 104, f"PAGE {page_number}")

    def draw_page_footer(pdf_canvas):
        pdf_canvas.setStrokeColor(colors.HexColor("#d6dce5"))
        pdf_canvas.setLineWidth(0.8)
        pdf_canvas.line(margin, 36, page_width - margin, 36)
        pdf_canvas.setFillColor(colors.HexColor("#5b6475"))
        pdf_canvas.setFont("Helvetica", 9)
        footer = f"Cree par {author_name} - {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        pdf_canvas.drawString(margin, 24, footer)
        pdf_canvas.drawRightString(page_width - margin, 24, "EcoShare - PDF Studio")

    content_top_px = (settings["content_top_pct"] / 100.0) * page_height

    def draw_decorations(pdf_canvas, doc):
        draw_page_header(pdf_canvas, doc.page)
        draw_page_footer(pdf_canvas)

        if doc.page == 1:
            metadata_top = page_height - (header_height + 40)
            pdf_canvas.setFillColor(colors.HexColor("#f5f8fc"))
            pdf_canvas.roundRect(margin, metadata_top - 62, page_width - (2 * margin), 62, 8, stroke=0, fill=1)
            pdf_canvas.setFillColor(colors.HexColor("#1f2a44"))
            pdf_canvas.setFont("Helvetica-Bold", 10)
            pdf_canvas.drawString(margin + 14, metadata_top - 22, "FICHE PEDAGOGIQUE")
            pdf_canvas.setFont("Helvetica", 10)
            pdf_canvas.drawString(margin + 14, metadata_top - 40, f"Auteur: {author_name}")
            pdf_canvas.drawRightString(
                page_width - margin - 14,
                metadata_top - 40,
                datetime.now().strftime("Edition du %d/%m/%Y"),
            )
            pdf_canvas.setFillColor(colors.HexColor("#1f2a44"))
            pdf_canvas.setFont("Helvetica-Bold", 13)
            content_label_x = (settings["content_left_pct"] / 100.0) * page_width
            content_label_y = page_height - content_top_px + 12
            pdf_canvas.drawString(content_label_x, content_label_y, "Contenu")

    document = BaseDocTemplate(
        file_path,
        pagesize=A4,
        title=title,
        author=author_name,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=52,
        bottomMargin=46,
    )

    min_content_top_px = header_height + 155
    safe_content_top_px = max(content_top_px, min_content_top_px)
    first_frame_top_y = page_height - safe_content_top_px - 20

    desired_frame_width = page_width * 0.76
    preferred_left = (settings["content_left_pct"] / 100.0) * page_width
    max_left_for_width = page_width - margin - desired_frame_width
    first_frame_x = max(24, min(max_left_for_width, preferred_left))
    first_frame_width = desired_frame_width
    first_frame_height = max(150, first_frame_top_y - 46)

    first_frame = Frame(
        first_frame_x,
        46,
        first_frame_width,
        first_frame_height,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
        id="first_frame",
    )
    later_frame = Frame(
        margin,
        46,
        page_width - (2 * margin),
        page_height - 190,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
        id="later_frame",
    )

    first_template = PageTemplate(id="first", frames=[first_frame], onPage=draw_decorations)
    document.addPageTemplates([first_template])

    body_style = ParagraphStyle(
        "BodyStyle",
        fontName="Helvetica",
        fontSize=int(settings["body_font_size"]),
        leading=int(settings["body_font_size"]) + 4,
        textColor=colors.HexColor("#1f2a44"),
        spaceAfter=8,
    )

    heading_style = ParagraphStyle(
        "HeadingStyle",
        parent=body_style,
        fontName="Helvetica-Bold",
        fontSize=int(settings["heading_font_size"]),
        leading=int(settings["heading_font_size"]) + 4,
        spaceBefore=10,
        spaceAfter=6,
    )

    bullet_style = ParagraphStyle(
        "BulletStyle",
        parent=body_style,
        leftIndent=14,
        firstLineIndent=-8,
        spaceAfter=4,
    )

    math_style = ParagraphStyle(
        "MathStyle",
        parent=body_style,
        fontName="Courier-Bold",
        fontSize=max(int(settings["body_font_size"]) + 1, 11),
        leading=max(int(settings["body_font_size"]) + 5, 15),
        alignment=TA_CENTER,
        backColor=colors.HexColor("#eef3fb"),
        borderColor=colors.HexColor("#d8e2f0"),
        borderWidth=0.6,
        borderPadding=6,
        borderRadius=4,
        spaceBefore=4,
        spaceAfter=8,
    )

    content_blocks = parse_content_blocks(content)
    story = []

    if mindmap_image_data.startswith("data:image/"):
        image_bytes = None
        try:
            _, encoded = mindmap_image_data.split(",", 1)
            image_bytes = base64.b64decode(encoded)
        except (ValueError, base64.binascii.Error):
            image_bytes = None

        if image_bytes and len(image_bytes) <= 5_000_000:
            try:
                image_stream = BytesIO(image_bytes)
                preview_image = RLImage(image_stream)
                max_img_width = first_frame_width
                max_img_height = min(220, first_frame_height * 0.38)
                preview_image._restrictSize(max_img_width, max_img_height)
                story.append(
                    Paragraph(
                        "<font color='#1f2a44'><b>Apercu de la carte mentale importee</b></font>",
                        body_style,
                    )
                )
                story.append(Spacer(1, 6))
                story.append(preview_image)
                story.append(Spacer(1, 10))
            except Exception:
                pass

    story.append(
        Paragraph(
            "<font color='#4f5b70'><i>Astuce: utilisez §texte§ ou **texte** pour le gras, *texte* pour l'italique et $...$ ou $$...$$ pour les formules.</i></font>",
            body_style,
        )
    )
    story.append(Spacer(1, 10))

    for block in content_blocks:
        block_type = block.get("type")
        text = block.get("text", "").strip()
        if not text:
            continue

        if block_type == "heading":
            story.append(Paragraph(render_custom_markup(text), heading_style))
            story.append(Spacer(1, 2))
            continue

        if block_type == "bullet":
            story.append(Paragraph(f"• {render_custom_markup(text)}", bullet_style))
            continue

        if block_type == "math":
            readable_math = escape(latex_to_readable(text))
            story.append(Paragraph(readable_math, math_style))
            continue

        story.append(Paragraph(render_custom_markup(text), body_style))
        story.append(Spacer(1, 6))

    single_page_story = [
        KeepInFrame(first_frame_width, first_frame_height, story, mode="shrink")
    ]
    document.build(single_page_story)


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


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        return redirect(url_for("index"))

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


@app.route("/theme/guest", methods=["POST"])
def update_guest_theme():
    theme = request.form.get("ui_theme", "light")
    next_url = request.form.get("next") or request.referrer or url_for("login")

    if theme not in {"light", "dark"}:
        theme = "light"

    client_ip = get_client_ip()
    pref = GuestThemePreference.query.filter_by(ip_address=client_ip).first()
    if pref is None:
        pref = GuestThemePreference(ip_address=client_ip, ui_theme=theme)
        db.session.add(pref)
    else:
        pref.ui_theme = theme
    db.session.commit()

    session["guest_ui_theme"] = theme
    return redirect(next_url)


@app.route("/theme/toggle", methods=["POST"])
def toggle_theme():
    next_url = request.form.get("next") or request.referrer or url_for("index")
    current_theme = get_effective_theme()
    new_theme = "dark" if current_theme == "light" else "light"

    if current_user.is_authenticated:
        current_user.ui_theme = new_theme
        db.session.commit()
        return redirect(next_url)

    client_ip = get_client_ip()
    pref = GuestThemePreference.query.filter_by(ip_address=client_ip).first()
    if pref is None:
        pref = GuestThemePreference(ip_address=client_ip, ui_theme=new_theme)
        db.session.add(pref)
    else:
        pref.ui_theme = new_theme
    db.session.commit()
    session["guest_ui_theme"] = new_theme
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
    flash("L'atelier schemas est desactive.", "error")
    return redirect(url_for("index"))

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
    flash("L'atelier schemas est desactive.", "error")
    return redirect(url_for("index"))

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


@app.route("/pdf/create", methods=["GET", "POST"])
@login_required
def create_stylish_pdf():
    palette = {
        "teal": "#0f766e",
        "coral": "#dc5f52",
        "indigo": "#3949ab",
        "emerald": "#1b8f5a",
        "slate": "#334155",
    }

    default_subject = request.args.get("subject", "")
    default_level = request.args.get("level", "")
    if default_subject not in SUBJECTS:
        default_subject = ""
    if default_level not in LEVELS:
        default_level = ""

    if current_user.role == "eleve":
        default_level = current_user.school_class

    if request.method == "POST":
        if canvas is None:
            flash("La creation PDF n'est pas disponible: installez reportlab.", "error")
            return redirect(url_for("create_stylish_pdf"))

        title = request.form.get("title", "").strip()
        subtitle = request.form.get("subtitle", "").strip()
        subject = request.form.get("subject", "").strip()
        level = request.form.get("level", "").strip()
        body = request.form.get("body", "").strip()
        theme = request.form.get("theme", "teal").strip()
        layout_json = request.form.get("layout_json", "")
        mindmap_image_data = request.form.get("mindmap_image_data", "").strip()
        studio_settings = parse_pdf_studio_settings(layout_json)

        if current_user.role == "eleve":
            level = current_user.school_class

        if not title or len(title) < 4:
            flash("Le titre doit contenir au moins 4 caracteres.", "error")
            return redirect(url_for("create_stylish_pdf"))
        if subject not in SUBJECTS or level not in LEVELS:
            flash("Matiere ou niveau invalide.", "error")
            return redirect(url_for("create_stylish_pdf"))
        if not body or len(body) < 40:
            flash("Ajoutez un contenu plus detaille (minimum 40 caracteres).", "error")
            return redirect(url_for("create_stylish_pdf"))
        if theme not in palette:
            theme = "teal"

        safe_title = secure_filename(title) or "fiche_stylisee"
        unique_name = f"pdf_{uuid.uuid4().hex}.pdf"
        output_path = os.path.join(app.config["UPLOAD_FOLDER"], unique_name)

        generate_stylish_pdf(
            file_path=output_path,
            title=title[:90],
            subtitle=subtitle[:140],
            content=body,
            author_name=current_user.username,
            accent_hex=palette[theme],
            studio_settings=studio_settings,
            mindmap_image_data=mindmap_image_data,
        )

        return send_file(
            output_path,
            as_attachment=True,
            download_name=f"{safe_title}.pdf",
            mimetype="application/pdf",
        )

    return render_template(
        "create_pdf.html",
        default_subject=default_subject,
        default_level=default_level,
        default_studio_settings=DEFAULT_PDF_STUDIO_SETTINGS,
    )


@app.route("/mindmap-studio")
@login_required
def mindmap_studio():
    published_token = (request.args.get("published") or "").strip()
    published_payload = None
    published_meta = None
    if published_token:
        publication = MindmapPublication.query.filter_by(share_token=published_token).first()
        if publication:
            try:
                parsed_payload = json.loads(publication.payload_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_payload = {}

            published_payload = {
                "cards": parsed_payload.get("cards", []),
                "edges": parsed_payload.get("edges", []),
                "showGrid": parsed_payload.get("showGrid", True),
                "zoom": parsed_payload.get("zoom", 1),
            }
            published_meta = {
                "title": publication.title,
                "subject": publication.subject,
                "level": publication.level,
                "author": publication.created_by.username if publication.created_by else "inconnu",
                "created_at": publication.created_at.isoformat() if publication.created_at else "",
            }
        else:
            flash("Publication MindMap introuvable.", "error")

    return render_template(
        "mindmap_studio.html",
        published_payload=published_payload,
        published_meta=published_meta,
    )


@app.route("/mindmap/public/<string:share_token>")
@login_required
def mindmap_public_view(share_token):
    flash("Le partage de MindMap est desactive.", "error")
    return redirect(url_for("mindmap_studio"))


@app.route("/mindmap/publish", methods=["POST"])
@login_required
def publish_mindmap():
    return jsonify({"ok": False, "error": "La publication de MindMap est desactivee."}), 403

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Payload invalide."}), 400

    title = str(payload.get("title", "")).strip()[:140]
    subject = str(payload.get("subject", "")).strip()
    level = str(payload.get("level", "")).strip()
    cards = payload.get("cards", [])
    edges = payload.get("edges", [])
    show_grid = bool(payload.get("showGrid", True))
    zoom = clamp_number(payload.get("zoom"), 0.45, 2.1, 1.0)
    preview_image_data = str(payload.get("preview_image_data", "")).strip()

    if len(title) < 3:
        return jsonify({"ok": False, "error": "Titre trop court (minimum 3 caracteres)."}), 400
    if subject not in SUBJECTS or level not in LEVELS:
        return jsonify({"ok": False, "error": "Matiere ou niveau invalide."}), 400
    if not isinstance(cards, list) or not cards:
        return jsonify({"ok": False, "error": "Ajoutez au moins une carte avant publication."}), 400
    if not isinstance(edges, list):
        edges = []
    if preview_image_data and not preview_image_data.startswith("data:image/"):
        preview_image_data = ""
    if len(preview_image_data) > 7_000_000:
        preview_image_data = ""

    safe_payload = {
        "cards": cards,
        "edges": edges,
        "showGrid": show_grid,
        "zoom": zoom,
    }
    share_token = uuid.uuid4().hex[:14]

    publication = MindmapPublication(
        title=title,
        subject=subject,
        level=level,
        payload_json=json.dumps(safe_payload, ensure_ascii=True),
        preview_image_data=preview_image_data,
        share_token=share_token,
        created_by_id=current_user.id,
    )
    db.session.add(publication)
    db.session.commit()

    return jsonify(
        {
            "ok": True,
            "share_url": url_for("mindmap_public_view", share_token=share_token, _external=True),
            "open_url": url_for("mindmap_studio", published=share_token),
        }
    )


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
        identifier = request.form.get("identifier", "").strip()
        if not identifier:
            # Backward compatibility if an old form still posts "email".
            identifier = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        identifier_lower = identifier.lower()

        user = User.query.filter(
            or_(
                User.email == identifier_lower,
                User.username.ilike(identifier),
            )
        ).first()
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
