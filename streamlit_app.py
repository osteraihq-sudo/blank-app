# streamlit_app.py — The Hive (single file)
# =============================================================================
# RUN:
#   pip install streamlit streamlit-elements streamlit-calendar pillow
#   # Optional MIME:  python-magic-bin (Windows)  |  python-magic (Linux/Mac)
#   # Optional video thumbnails: pip install moviepy
#   streamlit run streamlit_app.py
#
# Highlights:
# - 📌 Corkboard: drag stickies (streamlit-elements), comments, reactions (toggle), soft delete, promote→event
# - 📝 Lists: CRUD (soft delete) — inserts set done=0 (fix)
# - 🗒️ Notepad: CRUD (soft delete) — inserts include content='' (fix)
# - 📅 Calendar: UTC-safe, edit/delete panel; fixed naive/aware compare + no .get() on sqlite3.Row
# - 📷 Family Feed: photos + 🎥 video (mp4/webm/mov), optional thumbs, likes/comments, delete
# - 🛡️ Hardening: auth shim, MIME/size checks, EXIF strip, thumbnails, XSS escape, pagination, indexes, WAL
# - 🧱 Migrations: adds deleted_at, thumb_path, media_type, all_day; backfills legacy NULLs
# - 🧹 Deprecations: uses use_container_width
# - 💬 Group Chat: IM-style room chat (scroll box, last 100 msgs, auto-scroll, live updates, notifications)
# =============================================================================

from __future__ import annotations

import os, uuid, io, calendar
import sqlite3
from pathlib import Path
from typing import Iterable, Optional, List, Dict, Tuple
from datetime import datetime, date, time, timedelta, timezone

import streamlit as st

# ---------- optional auto-refresh ----------
try:
    from streamlit_extras.st_autorefresh import st_autorefresh
except Exception:
    def st_autorefresh(*_, **__):
        return None

# ---------- free-form drag for corkboard ----------
ELEMENTS_OK = False
try:
    from streamlit_elements import elements, mui, dashboard, html
    ELEMENTS_OK = True
except Exception:
    ELEMENTS_OK = False

# ---------- FullCalendar wrapper ----------
CAL_OK = False
try:
    from streamlit_calendar import calendar as fc_calendar
    CAL_OK = True
except Exception:
    CAL_OK = False

# ---------- Imaging / MIME ----------
from PIL import Image
try:
    import magic  # python-magic or python-magic-bin
    MAGIC_OK = True
except Exception:
    MAGIC_OK = False

# ---------- Optional video thumbs ----------
try:
    from moviepy.editor import VideoFileClip
    MOVIEPY_OK = True
except Exception:
    MOVIEPY_OK = False
# === ADD-ON BLOCK A: Utilities & Add-on schema (profiles, rsvp, settings, reset, sticky, theme) ===
def _init_addon_schema():
    c = get_conn(); cur = c.cursor()
    # Profiles (per Family + unique username)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            family TEXT NOT NULL,
            username TEXT NOT NULL,
            first_name TEXT,
            last_name TEXT,
            avatar_path TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(family, username)
        );
    """)
    # App settings (key/value by family) — for last_active_user, theme, etc.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_settings(
            family TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            PRIMARY KEY (family, key)
        );
    """)
    # RSVPs for events
    cur.execute("""
        CREATE TABLE IF NOT EXISTS event_rsvps(
            event_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('going','maybe','cant')),
            responded_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY(event_id, username),
            FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
        );
    """)
    c.commit()

def _get_setting(family:str, key:str, default:str|None=None) -> str|None:
    rows = q("SELECT value FROM app_settings WHERE family=? AND key=? LIMIT 1", (family, key))
    return (rows[0]["value"] if rows else default)

def _set_setting(family:str, key:str, value:str|None):
    if value is None:
        exec1("DELETE FROM app_settings WHERE family=? AND key=?", (family, key))
    else:
        exec1("INSERT INTO app_settings(family, key, value) VALUES(?,?,?) ON CONFLICT(family,key) DO UPDATE SET value=excluded.value",
              (family, key, value))

def _get_or_create_profile(family:str, username:str, first:str|None=None, last:str|None=None) -> dict:
    rows = q("SELECT * FROM user_profiles WHERE family=? AND username=? LIMIT 1", (family, username))
    if rows:
        return dict(rows[0])
    exec1("INSERT OR IGNORE INTO user_profiles(family, username, first_name, last_name) VALUES(?,?,?,?)",
          (family, username, first or None, last or None))
    rows = q("SELECT * FROM user_profiles WHERE family=? AND username=? LIMIT 1", (family, username))
    return dict(rows[0]) if rows else {}

def _save_avatar(upload) -> str|None:
    try:
        path, thumb, mime, mtype = save_media(upload)
        if mtype != "image":
            return None
        return thumb or path
    except Exception:
        return None

def _sticky_user_bootstrap():
    """
    Run AFTER init_schema() and BEFORE you read DISPLAY_NAME/FAMILY.
    Uses query params + app_settings to keep user/family sticky across refresh.
    """
    try:
        qp = st.query_params
    except Exception:
        qp = {}

    # Initialize session defaults
    if "user" not in st.session_state:
        st.session_state["user"] = qp.get("user", ["Guest"])[0] if isinstance(qp.get("user"), list) else qp.get("user", "Guest")
    if "family" not in st.session_state:
        st.session_state["family"] = qp.get("family", ["public"])[0] if isinstance(qp.get("family"), list) else qp.get("family", "public")

    fam = st.session_state.get("family") or "public"
    # If no user in query params, fall back to last_active_user
    if not qp.get("user"):
        last = _get_setting(fam, "last_active_user", None)
        if last and st.session_state.get("user") != last:
            st.session_state["user"] = last

def _stick_user_to_url_and_settings(username:str, family:str):
    # Update query params (so refresh keeps user/family)
    try:
        st.query_params.update({"user": username, "family": family})
    except Exception:
        pass
    # Remember as last active for this family
    _set_setting(family, "last_active_user", username)

def _theme_css():
    st.markdown("""
    <style>
      /* Make text readable in both light/dark themes via inherited colors */
      .stApp, .stApp * { color-scheme: light dark; }
      .stApp [data-testid="stMarkdown"] p, 
      .stApp [data-testid="stSidebar"] * { 
        color: inherit !important;
      }
      /* Inputs and captions readable in dark */
      .stApp .stCaption,
      .stApp [data-baseweb="base-input"] input,
      .stApp [data-testid="stSelectbox"] div {
        color: inherit !important;
      }
      /* RSVP chips */
      .chip { display:inline-flex; align-items:center; gap:6px; padding:4px 8px; border-radius:999px; border:1px solid var(--chip-b, #ccc); margin:2px; font-size:12px;}
      .chip img { width:20px; height:20px; border-radius:50%; object-fit:cover; }
    </style>
    """, unsafe_allow_html=True)

def _factory_reset_ui():
    with st.sidebar.expander("🧨 Admin · Factory Reset", expanded=False):
        st.caption("Type **RESET** to purge all data (DB tables & /uploads).")
        txt = st.text_input("Confirm", key="__reset_confirm__", placeholder="RESET")
        if st.button("⚠️ Wipe everything"):
            if txt.strip() == "RESET":
                try:
                    # Clear DB tables instead of deleting file to keep schema
                    exec1("DELETE FROM reactions", ())
                    exec1("DELETE FROM comments", ())
                    exec1("DELETE FROM list_items", ())
                    exec1("DELETE FROM lists", ())
                    exec1("DELETE FROM documents", ())
                    exec1("DELETE FROM notes", ())
                    exec1("DELETE FROM post_comments", ())
                    exec1("DELETE FROM post_likes", ())
                    exec1("DELETE FROM post_media", ())
                    exec1("DELETE FROM posts", ())
                    exec1("DELETE FROM albums", ())
                    exec1("DELETE FROM chat_messages", ())
                    exec1("DELETE FROM event_rsvps", ())
                    exec1("DELETE FROM events", ())
                    exec1("DELETE FROM user_profiles", ())
                    exec1("DELETE FROM app_settings", ())
                    # Nuke uploads
                    try:
                        for p in UPLOAD_DIR.glob("*"):
                            p.unlink(missing_ok=True)
                    except Exception:
                        pass
                    # Vacuum
                    exec1("VACUUM", ())
                    st.success("Factory reset complete.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Reset failed: {e}")
            else:
                st.warning("Type RESET exactly to confirm.")
# === END ADD-ON BLOCK A ===

# ==================== CONFIG ====================
DB_PATH = Path("hive.db")
UPLOAD_DIR = Path("uploads"); UPLOAD_DIR.mkdir(exist_ok=True)

COLOR_PRESETS = {"Yellow":"#FFF176","Red":"#EE0E2C","Green":"#81C784","Blue":"#64B5F6","Pink":"#F48FB1"}
EMOJI_CHOICES = ["👍","❤️","😂","🤔","✅"]

MAX_MB = 200
IMG_MIMES = {"image/jpeg","image/png","image/gif"}
VID_MIMES = {"video/mp4","video/webm","video/quicktime"}  # .mov
ALLOWED_MIMES = IMG_MIMES | VID_MIMES
THUMB_MAX = (1200, 1200)

PAGE_SIZE = 25

# ==================== DB ====================
@st.cache_resource(show_spinner=False)
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=3000;")
    return conn

def q(sql: str, args: Iterable = ()):
    return get_conn().execute(sql, args).fetchall()

def exec1(sql: str, args: Iterable = ()):
    get_conn().execute(sql, args)

def has_col(table:str, col:str)->bool:
    return any(r["name"]==col for r in get_conn().execute(f"PRAGMA table_info({table});").fetchall())

def add_col(table:str,col:str,typ:str,default:Optional[str]):
    if not has_col(table,col):
        ddl=f"ALTER TABLE {table} ADD COLUMN {col} {typ}"
        if default is not None: ddl+=f" DEFAULT {default}"
        get_conn().execute(ddl)

def init_schema():
    c = get_conn(); cur = c.cursor()

    # Core tables
    cur.execute("""CREATE TABLE IF NOT EXISTS notes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        color   TEXT NOT NULL,
        x REAL DEFAULT 40,
        y REAL DEFAULT 40,
        z INTEGER DEFAULT 0,
        type TEXT DEFAULT 'text',
        assignee TEXT,
        due_at TEXT,
        tags TEXT,
        order_index INTEGER DEFAULT 0,
        linked_event_id INTEGER,
        family TEXT NOT NULL DEFAULT 'public'
    );""")

    cur.execute("""CREATE TABLE IF NOT EXISTS lists(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        family TEXT NOT NULL DEFAULT 'public'
    );""")

    cur.execute("""CREATE TABLE IF NOT EXISTS list_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        list_id INTEGER NOT NULL,
        text    TEXT NOT NULL,
        done    INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(list_id) REFERENCES lists(id) ON DELETE CASCADE
    );""")

    cur.execute("""CREATE TABLE IF NOT EXISTS documents(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title   TEXT NOT NULL,
        content TEXT NOT NULL DEFAULT '',
        family TEXT NOT NULL DEFAULT 'public'
    );""")

    cur.execute("""CREATE TABLE IF NOT EXISTS comments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        note_id INTEGER NOT NULL,
        author TEXT NOT NULL,
        text   TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY(note_id) REFERENCES notes(id) ON DELETE CASCADE
    );""")

    cur.execute("""CREATE TABLE IF NOT EXISTS reactions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        note_id INTEGER NOT NULL,
        emoji TEXT NOT NULL,
        author TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY(note_id) REFERENCES notes(id) ON DELETE CASCADE
    );""")

    cur.execute("""CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        start_at TEXT NOT NULL,
        end_at   TEXT,
        assignees TEXT,
        family TEXT NOT NULL
    );""")

    # Family Feed
    cur.execute("""CREATE TABLE IF NOT EXISTS albums(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        family TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );""")

    cur.execute("""CREATE TABLE IF NOT EXISTS posts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        family TEXT NOT NULL,
        album_id INTEGER,
        author TEXT NOT NULL,
        caption TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY(album_id) REFERENCES albums(id) ON DELETE SET NULL
    );""")

    cur.execute("""CREATE TABLE IF NOT EXISTS post_media(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER NOT NULL,
        path TEXT NOT NULL,
        thumb_path TEXT,
        mime TEXT,
        media_type TEXT NOT NULL DEFAULT 'image',
        FOREIGN KEY(post_id) REFERENCES posts(id) ON DELETE CASCADE
    );""")

    cur.execute("""CREATE TABLE IF NOT EXISTS post_likes(
        post_id INTEGER NOT NULL,
        author  TEXT NOT NULL,
        PRIMARY KEY(post_id, author),
        FOREIGN KEY(post_id) REFERENCES posts(id) ON DELETE CASCADE
    );""")

    cur.execute("""CREATE TABLE IF NOT EXISTS post_comments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER NOT NULL,
        author TEXT NOT NULL,
        text   TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY(post_id) REFERENCES posts(id) ON DELETE CASCADE
    );""")

    # ---- NEW: Group Chat table ----
    cur.execute("""CREATE TABLE IF NOT EXISTS chat_messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        family TEXT NOT NULL,
        room TEXT NOT NULL DEFAULT 'general',
        author TEXT NOT NULL,
        text   TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );""")

    # ---- migrations (safe no-ops if present) ----
    add_col("notes", "deleted_at", "TEXT", None)
    add_col("lists", "deleted_at", "TEXT", None)
    add_col("documents", "deleted_at", "TEXT", None)
    add_col("post_media", "thumb_path", "TEXT", None)
    add_col("post_media", "media_type", "TEXT", "'image'")
    add_col("events", "all_day", "INTEGER", "0")
    
    # ---- wishlist migrations ----
    add_col("lists", "type", "TEXT", "'normal'")
    add_col("lists", "created_by", "TEXT", None)
    add_col("list_items", "claimed_by", "TEXT", None)
    add_col("list_items", "url", "TEXT", None)
    add_col("list_items", "purchased_by", "TEXT", None)
    add_col("list_items", "image_url", "TEXT", None)

    # Indices
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uniq_rx ON reactions(note_id, emoji, author);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_notes_family ON notes(family);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_notes_order ON notes(order_index DESC, id DESC);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_comments_note ON comments(note_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_reactions_note ON reactions(note_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_family_start ON events(family, start_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_lists_family ON lists(family);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_docs_family ON documents(family);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_posts_family ON posts(family);")
    # NEW: chat indexes
    cur.execute("CREATE INDEX IF NOT EXISTS idx_chat_family_room_time ON chat_messages(family, room, created_at);")

    # Backfill legacy NULLs
    cur.execute("UPDATE list_items SET done=0 WHERE done IS NULL;")
    cur.execute("UPDATE documents SET content='' WHERE content IS NULL;")
    cur.execute("UPDATE events SET all_day=0 WHERE all_day IS NULL;")

    c.commit()

# ==================== UTIL ====================
import html as _html

def esc(s: Optional[str]) -> str:
    return _html.escape(s or "")

def iso_utc(d: Optional[date], t: Optional[time]) -> Optional[str]:
    if not d: return None
    if not t: t = time(0, 0)
    return datetime.combine(d, t, tzinfo=timezone.utc).isoformat()

def now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

def parse_aware(dt_str: str) -> datetime:
    """Parse ISO string and return an aware (UTC) datetime. Treat naive as UTC."""
    if not dt_str:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def sniff_mime(data: bytes, fallback_ext: str) -> Tuple[str, str]:
    if MAGIC_OK:
        try:
            m = magic.Magic(mime=True)
            mime = m.from_buffer(data[:2048])
        except Exception:
            mime = "application/octet-stream"
    else:
        mime = "application/octet-stream"
        ext_map = {
            ".jpg":"image/jpeg",".jpeg":"image/jpeg",".png":"image/png",".gif":"image/gif",
            ".mp4":"video/mp4",".webm":"video/webm",".mov":"video/quicktime"
        }
        mime = ext_map.get(fallback_ext.lower(), mime)
    if mime in ("image/jpeg","image/jpg"): ext=".jpg"
    elif mime=="image/png": ext=".png"
    elif mime=="image/gif": ext=".gif"
    elif mime=="video/mp4": ext=".mp4"
    elif mime=="video/webm": ext=".webm"
    elif mime=="video/quicktime": ext=".mov"
    else:
        ext = fallback_ext if fallback_ext.lower() in (".jpg",".jpeg",".png",".gif",".mp4",".webm",".mov") else ".jpg"
    return mime, ext

def save_image(upload, mime:str, ext:str) -> Tuple[str, Optional[str]]:
    raw = upload.getbuffer()
    name = f"{uuid.uuid4().hex}{ext}"
    out = UPLOAD_DIR / name
    with open(out, "wb") as f: f.write(raw)
    thumb_path = None
    if mime != "image/gif":
        try:
            im = Image.open(io.BytesIO(raw))
            im.info.pop("exif", None)
            im.thumbnail(THUMB_MAX)
            thumb = UPLOAD_DIR / f"{out.stem}_thumb.webp"
            im.save(thumb, "WEBP", quality=70, method=6)
            thumb_path = str(thumb)
        except Exception:
            thumb_path = None
    return str(out), thumb_path

def save_video(upload, ext:str) -> Tuple[str, Optional[str]]:
    raw = upload.getbuffer()
    name = f"{uuid.uuid4().hex}{ext}"
    out = UPLOAD_DIR / name
    with open(out, "wb") as f: f.write(raw)
    thumb_path = None
    if MOVIEPY_OK:
        try:
            clip = VideoFileClip(str(out))
            frame = clip.get_frame(0.0)
            im = Image.fromarray(frame)
            im.thumbnail(THUMB_MAX)
            thumb = UPLOAD_DIR / f"{out.stem}_thumb.webp"
            im.save(thumb, "WEBP", quality=70, method=6)
            thumb_path = str(thumb)
            clip.reader.close(); clip.close()
        except Exception:
            thumb_path = None
    return str(out), thumb_path

def save_media(upload) -> Tuple[str, Optional[str], str, str]:
    raw = upload.getbuffer()
    if len(raw) > MAX_MB * 1024 * 1024:
        st.error(f"File too large (> {MAX_MB}MB)."); st.stop()
    _, ext = os.path.splitext(upload.name)
    mime, ext2 = sniff_mime(raw, ext or ".jpg")
    if mime not in ALLOWED_MIMES:
        st.error("Unsupported file type."); st.stop()
    if mime in IMG_MIMES:
        path, thumb = save_image(upload, mime, ext2)
        return path, thumb, mime, "image"
    else:
        path, thumb = save_video(upload, ext2)
        return path, thumb, mime, "video"

# ==================== APP ====================
st.set_page_config(page_title="🐝 The Hive", layout="wide")
init_schema()
# === ADD-ON BLOCK B: Boot the add-on schema + sticky user + theme CSS ===
_init_addon_schema()          # create add-on tables if missing
_sticky_user_bootstrap()      # apply sticky user/family before sidebar reads them
_theme_css()                  # inject contrast-safe CSS for light/dark
# === END ADD-ON BLOCK B ===
# === ADD-ON BLOCK B++ : Max-contrast dark theme fix ===
st.markdown("""
<style>
/* Target Streamlit's dark theme both by data attr and media query */
html[data-theme="dark"], @media (prefers-color-scheme: dark) {
}

/* Global text */
html[data-theme="dark"] .stApp,
html[data-theme="dark"] .stApp * {
  color: #e9e9e9 !important;
}

/* Markdown paragraphs, captions, labels */
html[data-theme="dark"] .stApp [data-testid="stMarkdown"] p,
html[data-theme="dark"] .stApp .stCaption,
html[data-theme="dark"] .stApp label,
html[data-theme="dark"] .stApp details > summary {
  color: #e9e9e9 !important;
}

/* Text inputs, text areas, date/time inputs */
html[data-theme="dark"] .stApp [data-baseweb="base-input"] input,
html[data-theme="dark"] .stApp textarea,
html[data-theme="dark"] .stApp [data-testid="stTextArea"] textarea,
html[data-theme="dark"] .stApp [data-testid="stDateInput"] input,
html[data-theme="dark"] .stApp [data-testid="stTimeInput"] input {
  color: #e9e9e9 !important;
  border-color: #666 !important;
  background: transparent !important;
}

/* Placeholders */
html[data-theme="dark"] .stApp input::placeholder,
html[data-theme="dark"] .stApp textarea::placeholder {
  color: #bbbbbb !important;
}

/* Selectbox and multiselect rendered area */
html[data-theme="dark"] .stApp [data-testid="stSelectbox"] div,
html[data-theme="dark"] .stApp [data-testid="stMultiSelect"] div[role="combobox"] {
  color: #e9e9e9 !important;
}

/* Radio / Checkbox labels */
html[data-theme="dark"] .stApp [role="radiogroup"],
html[data-theme="dark"] .stApp [role="checkbox"] {
  color: #e9e9e9 !important;
}

/* File uploader */
html[data-theme="dark"] .stApp [data-testid="stFileUploaderDropzone"] {
  color: #e9e9e9 !important;
  border-color: #666 !important;
  background: rgba(255,255,255,0.02) !important;
}

/* Expander borders / hr lines */
html[data-theme="dark"] .stApp hr,
html[data-theme="dark"] .stApp .st-emotion-cache-hr {
  border-color: #444 !important;
}

/* Buttons: ensure text is visible on dark backgrounds */
html[data-theme="dark"] .stApp button[kind="primary"],
html[data-theme="dark"] .stApp button {
  color: #f5f5f5 !important;
}

/* Sidebar parity */
html[data-theme="dark"] .stApp [data-testid="stSidebar"] * {
  color: #e9e9e9 !important;
}

/* Tabs text */
html[data-theme="dark"] .stApp [data-baseweb="tab"] {
  color: #e9e9e9 !important;
}

/* Chips we added for RSVP */
html[data-theme="dark"] .stApp .chip {
  border-color: #555 !important;
  color: #e9e9e9 !important;
}
</style>
""", unsafe_allow_html=True)
# === END ADD-ON BLOCK B++ ===
# --------- Auth shim / Family selection ---------
with st.sidebar:
    st.header("⚙️ Controls")
    if "user" not in st.session_state: st.session_state["user"] = "Guest"
    if "family" not in st.session_state: st.session_state["family"] = "public"
    st.text_input("Display name", key="user")
    st.text_input("Family name", key="family", help="Use a unique family/group name to partition data.")
    who = st.selectbox("Color preset", list(COLOR_PRESETS.keys()), index=0)
    preset_color = COLOR_PRESETS[who]
    # === ADD-ON BLOCK C (WRAPPED): Profile, Theme, Factory Reset — always in sidebar ===
with st.sidebar:
    st.markdown("---")
    st.header("👤 Profile")

    c0, c1 = st.columns([0.55, 0.45])
    with c0:
        first = st.text_input("First name", key="__first_name__", placeholder="e.g., Alex")
    with c1:
        last = st.text_input("Last name", key="__last_name__", placeholder="e.g., Morgan")

    avatar_up = st.file_uploader(
        "Profile photo (jpg/png)",
        type=["jpg","jpeg","png"],
        key="__avatar_up__",
        accept_multiple_files=False
    )

    # Save / persist the profile tied to (FAMILY, DISPLAY_NAME)
    if st.button("💾 Save Profile"):
        fam = st.session_state.get("family") or "public"
        user = st.session_state.get("user") or "Guest"
        prof = _get_or_create_profile(fam, user, first, last)
        if avatar_up is not None:
            ap = _save_avatar(avatar_up)
            if ap:
                exec1(
                    "UPDATE user_profiles SET avatar_path=?, first_name=?, last_name=? WHERE family=? AND username=?",
                    (ap, first or None, last or None, fam, user)
                )
            else:
                exec1(
                    "UPDATE user_profiles SET first_name=?, last_name=? WHERE family=? AND username=?",
                    (first or None, last or None, fam, user)
                )
        else:
            exec1(
                "UPDATE user_profiles SET first_name=?, last_name=? WHERE family=? AND username=?",
                (first or None, last or None, fam, user)
            )
        _stick_user_to_url_and_settings(user, fam)
        st.success("Profile saved and user anchored to URL. Refresh will keep you signed in.")

    st.markdown("---")
    auto = st.checkbox("Auto-refresh (every N sec)", value=False)
    secs = st.slider("Refresh interval", 2, 15, 5)
    if auto: st_autorefresh(interval=secs*1000, key="auto")
    st.markdown("---")
    st.caption("Corkboard filters")
    f_search = st.text_input("Search text")
    f_assignee = st.text_input("Assignee contains")
    f_tags = st.text_input("Tags contains")
    f_due = st.selectbox("Due filter", ["All","Due today","Overdue"])

  # === ADD-ON BLOCK F (REPLACEMENT): Password-gated Factory Reset ===
def _factory_reset_ui_secured():
    with st.sidebar.expander("🧨 Admin · Factory Reset", expanded=False):
        st.caption("Admin-only. Enter password to unlock reset.")

        # Password source: secrets or env var
        ADMIN_KEY = None
        try:
            # .streamlit/secrets.toml -> ADMIN_RESET_PASSWORD = "yourpassword"
            if hasattr(st, "secrets") and "ADMIN_RESET_PASSWORD" in st.secrets:
                ADMIN_KEY = st.secrets["ADMIN_RESET_PASSWORD"]
        except Exception:
            pass
        ADMIN_KEY = ADMIN_KEY or os.environ.get("HIVE_ADMIN_RESET")

        pwd = st.text_input("Admin password", type="password", key="__reset_pwd__")

        if not ADMIN_KEY:
            st.info("No admin password configured. Set `ADMIN_RESET_PASSWORD` in .streamlit/secrets.toml "
                    "or env var `HIVE_ADMIN_RESET` to enable this.")
            return

        if pwd != ADMIN_KEY:
            st.caption("Enter the correct admin password to reveal the reset controls.")
            return

        # Auth OK → show the original confirmation + wipe logic (inline)
        st.success("Authenticated.")
        st.caption("Type **RESET** to purge all data (DB tables & /uploads).")
        txt = st.text_input("Confirm", key="__reset_confirm__", placeholder="RESET")

        if st.button("⚠️ Wipe everything"):
            if txt.strip() == "RESET":
                try:
                    # Clear DB tables instead of deleting file to keep schema
                    exec1("DELETE FROM reactions", ())
                    exec1("DELETE FROM comments", ())
                    exec1("DELETE FROM list_items", ())
                    exec1("DELETE FROM lists", ())
                    exec1("DELETE FROM documents", ())
                    exec1("DELETE FROM notes", ())
                    exec1("DELETE FROM post_comments", ())
                    exec1("DELETE FROM post_likes", ())
                    exec1("DELETE FROM post_media", ())
                    exec1("DELETE FROM posts", ())
                    exec1("DELETE FROM albums", ())
                    exec1("DELETE FROM chat_messages", ())
                    exec1("DELETE FROM event_rsvps", ())
                    exec1("DELETE FROM events", ())
                    exec1("DELETE FROM user_profiles", ())
                    exec1("DELETE FROM app_settings", ())

                    # Nuke uploads directory
                    try:
                        for p in UPLOAD_DIR.glob("*"):
                            p.unlink(missing_ok=True)
                    except Exception:
                        pass

                    # Vacuum DB
                    exec1("VACUUM", ())

                    st.success("Factory reset complete.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Reset failed: {e}")
            else:
                st.warning("Type RESET exactly to confirm.")

_factory_reset_ui_secured()
# === END ADD-ON BLOCK F ===

# === END ADD-ON BLOCK C (WRAPPED) ===
DISPLAY_NAME = st.session_state["user"] or "Guest"
FAMILY = st.session_state["family"] or "public"

st.title("🐝 The Hive")
st.caption(f"Welcome, {esc(DISPLAY_NAME)} — Family: {esc(FAMILY)}")

tabs = st.tabs(["📌 Corkboard","📝 Lists","🗒️ Notepad","📅 Calendar","📷 Family Feed","💬 Chat"])

# -------------------- CORKBOARD --------------------
with tabs[0]:
    st.subheader("Corkboard")

    with st.form("add_note", clear_on_submit=True):
        st.markdown("#### New sticky")
        type_choice = st.selectbox("Type", ["text","photo","link","reminder"])
        c1, c2 = st.columns([4,1])
        content_text = ""
        upload = None
        with c1:
            if type_choice == "text":
                content_text = st.text_input("Text", placeholder="e.g., Pick up milk")
            elif type_choice == "link":
                content_text = st.text_input("URL (http/https only)", placeholder="https://…")
            elif type_choice == "photo":
                upload = st.file_uploader("Photo", type=["png","jpg","jpeg","gif"])
            else:
                content_text = st.text_input("Reminder text", placeholder="Take out trash")
        with c2:
            color_mode = st.radio("Color", ["Preset","Custom"], horizontal=True)
            color = preset_color if color_mode == "Preset" else st.color_picker("Pick", "#FFF176")

        m1, m2, m3 = st.columns(3)
        with m1: assignee = st.text_input("Assignee (optional)", placeholder="e.g., Sam")
        with m2: tags = st.text_input("Tags (comma separated)", placeholder="e.g., chores,urgent")
        with m3:
            due_d = st.date_input("Due date", value=None)
            due_t = st.time_input("Due time", value=None, step=300)

        if st.form_submit_button("Add Note"):
            if type_choice == "photo":
                if upload is None: st.error("Please upload a photo."); st.stop()
                path, thumb, mime, _ = save_media(upload)
                if mime not in IMG_MIMES: st.error("Only images allowed for photo sticky."); st.stop()
                content_text = path
            if type_choice == "link":
                if not (content_text.startswith("http://") or content_text.startswith("https://")):
                    st.error("Enter a valid link (http/https)."); st.stop()
            due_iso = iso_utc(due_d, due_t)
            row = q("SELECT COALESCE(MAX(order_index),0) m FROM notes WHERE family=? AND deleted_at IS NULL", (FAMILY,))
            next_ord = (row[0]["m"] or 0) + 1
            exec1("""INSERT INTO notes(content,color,x,y,z,type,assignee,due_at,tags,family,order_index)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                  (content_text, color, 40, 40, next_ord, type_choice,
                   assignee or None, due_iso, tags or None, FAMILY, next_ord))
            st.success("Note added."); st.rerun()

    page = st.session_state.get("notes_page", 0)
    notes = q("""SELECT * FROM notes 
                 WHERE family=? AND (deleted_at IS NULL) 
                   AND (?='' OR LOWER(content) LIKE '%'||LOWER(?)||'%')
                   AND (?='' OR LOWER(COALESCE(assignee,'')) LIKE '%'||LOWER(?)||'%')
                   AND (?='' OR LOWER(COALESCE(tags,'')) LIKE '%'||LOWER(?)||'%')
                 ORDER BY order_index DESC, id DESC
                 LIMIT ? OFFSET ?""",
              (FAMILY, f_search or "", f_search or "", f_assignee or "", f_assignee or "",
               f_tags or "", f_tags or "", PAGE_SIZE, page*PAGE_SIZE))

    def include_due(n):
        if f_due == "All": return True
        if not n["due_at"]: return False
        try:
            dt = parse_aware(n["due_at"])
        except Exception:
            return False
        if f_due == "Due today":
            return dt.date() == datetime.now(timezone.utc).date()
        elif f_due == "Overdue":
            return dt < datetime.now(timezone.utc)
        return True

    notes = [n for n in notes if include_due(n)]

    # Drag board
    st.markdown("#### Drag notes on the board (positions are saved)")
    if ELEMENTS_OK and notes:
        def to_grid(n) -> Dict:
            x_units = max(0, min(11, int((n["x"] or 40) // 80)))
            y_units = max(0, int((n["y"] or 40) // 60))
            return dict(i=str(n["id"]), x=x_units, y=y_units, w=4, h=3, static=False)
        layout = [dashboard.Item(**to_grid(n)) for n in notes]

        with elements("corkboard_grid"):
            with dashboard.Grid(
                layout=layout, cols=12, rowHeight=30,
                compactType=None, draggableHandle=None,
                isDraggable=True, isResizable=False
            ):
                for n in notes:
                    key = str(n["id"])
                    with mui.Paper(key=key, elevation=3,
                                   sx={"p":1,"backgroundColor": n["color"],"overflow":"hidden","cursor":"grab"}):
                        mui.Typography((n["type"] or "text").capitalize(), variant="caption")
                        if n["type"] == "link":
                            mui.Link(esc(n["content"]), href=n["content"], target="_blank", rel="noopener")
                        elif n["type"] == "photo" and os.path.exists(n["content"]):
                            html.img(src=n["content"], style={"width":"100%","borderRadius":"6px"})
                        else:
                            mui.Typography(esc(n["content"][:240]) + ("…" if len(n["content"])>240 else ""))

        # Persist positions
        for state_key in ["elements/corkboard_grid","corkboard_grid"]:
            if state_key in st.session_state and "layout" in st.session_state[state_key]:
                for item in st.session_state[state_key]["layout"]:
                    nid = int(item["i"])
                    x_px = item["x"] * 80
                    y_px = item["y"] * 60
                    exec1("UPDATE notes SET x=?, y=? WHERE id=? AND family=?", (x_px, y_px, nid, FAMILY))
                break

        st.caption("Positions auto-save. Need to force it?")
        if st.button("💾 Save layout now"):
            for state_key in ["elements/corkboard_grid","corkboard_grid"]:
                if state_key in st.session_state and "layout" in st.session_state[state_key]:
                    for item in st.session_state[state_key]["layout"]:
                        nid = int(item["i"]); x_px = item["x"]*80; y_px = item["y"]*60
                        exec1("UPDATE notes SET x=?, y=? WHERE id=? AND family=?", (x_px, y_px, nid, FAMILY))
            st.success("Layout saved.")
    elif not ELEMENTS_OK:
        st.info("Install `pip install streamlit-elements` to drag notes freely (optional).")

    c1,c2,c3 = st.columns(3)
    with c1:
        if st.button("◀ Prev", disabled=page==0, key="notes_prev"):
            st.session_state["notes_page"] = max(0, page-1); st.rerun()
    with c3:
        if st.button("Next ▶", disabled=len(notes)<PAGE_SIZE, key="notes_next"):
            st.session_state["notes_page"] = page+1; st.rerun()

    if not notes:
        st.info("No notes match filters on this page.")
    else:
        cols = st.columns(2)
        for i, n in enumerate(notes):
            with cols[i % 2]:
                with st.container(border=True):
                    meta = []
                    if n["assignee"]: meta.append(f"👤 {esc(n['assignee'])}")
                    if n["due_at"]:
                        try:
                            due_dt = parse_aware(n["due_at"])
                            overdue = due_dt < datetime.now(timezone.utc)
                            meta.append(("⏰ " if not overdue else "⚠️ Overdue ") + due_dt.strftime("%b %d %I:%M%p UTC"))
                        except Exception:
                            meta.append("⏰ " + esc(n["due_at"]))
                    if n["tags"]: meta.append("🏷 " + esc(n["tags"]))
                    if meta: st.caption(" • ".join(meta))
                    if n["type"] in (None,"text"):
                        st.markdown(
                            f"<div style='background:{n['color']};padding:10px;border-radius:8px;min-height:80px'>{esc(n['content'])}</div>",
                            unsafe_allow_html=True
                        )
                    elif n["type"] == "link":
                        url = n["content"]
                        if url.startswith("http://") or url.startswith("https://"):
                            st.markdown(
                                f"<div style='background:{n['color']};padding:10px;border-radius:8px'>🔗 <a href='{esc(url)}' target='_blank' rel='noopener'>{esc(url)}</a></div>",
                                unsafe_allow_html=True
                            )
                        else:
                            st.warning("Invalid link.")
                    elif n["type"] == "photo":
                        if os.path.exists(n["content"]):
                            st.image(n["content"], use_container_width=True, caption="Photo sticky")
                        else:
                            st.warning("Photo missing on disk.")
                    elif n["type"] == "reminder":
                        st.markdown(f"<div style='background:{n['color']};padding:10px;border-radius:8px'>⏰ {esc(n['content'])}</div>", unsafe_allow_html=True)

                    rx = q("SELECT emoji, COUNT(*) c FROM reactions WHERE note_id=? GROUP BY emoji", (n["id"],))
                    counts = {row["emoji"]: row["c"] for row in rx}
                    if counts:
                        st.caption(" ".join([f"{e} {counts.get(e,0)}" for e in EMOJI_CHOICES if e in counts]))

                    with st.expander("💬 Comments & Reactions"):
                        ecols = st.columns(len(EMOJI_CHOICES))
                        for col, emoji in zip(ecols, EMOJI_CHOICES):
                            with col:
                                if st.button(emoji, key=f"react_{emoji}_{n['id']}"):
                                    try:
                                        exec1("INSERT INTO reactions(note_id, emoji, author) VALUES(?,?,?)",
                                              (n["id"], emoji, DISPLAY_NAME))
                                    except sqlite3.IntegrityError:
                                        exec1("DELETE FROM reactions WHERE note_id=? AND emoji=? AND author=?",
                                              (n["id"], emoji, DISPLAY_NAME))
                                    st.rerun()
                        if st.button("Clear all reactions", key=f"clear_rx_{n['id']}"):
                            exec1("DELETE FROM reactions WHERE note_id=?", (n["id"],)); st.rerun()
                        with st.form(f"add_comment_{n['id']}", clear_on_submit=True):
                            txt = st.text_input("Add a comment", placeholder="Type and press Enter")
                            if st.form_submit_button("Comment") and (txt or "").strip():
                                exec1("INSERT INTO comments(note_id, author, text) VALUES(?,?,?)",
                                      (n["id"], DISPLAY_NAME, txt.strip()))
                                st.rerun()
                        com = q("SELECT * FROM comments WHERE note_id=? ORDER BY id DESC LIMIT 50", (n["id"],))
                        if not com:
                            st.caption("No comments yet.")
                        else:
                            for c in com:
                                row = st.columns([6,1])
                                with row[0]:
                                    ts = c["created_at"]
                                    st.markdown(f"**{esc(c['author'])}** · {esc(ts)}<br/>{esc(c['text'])}", unsafe_allow_html=True)
                                with row[1]:
                                    if st.button("🗑️", key=f"del_comment_{c['id']}"):
                                        exec1("DELETE FROM comments WHERE id=?", (c["id"],)); st.rerun()

                    with st.expander("Link / Delete / Promote"):
                        if n["type"] == "reminder":
                            st.markdown("**Promote to Calendar Event**")
                            ev_d = st.date_input(f"Start date #{n['id']}", value=date.today())
                            ev_s = st.time_input(f"Start time #{n['id']}", value=time(9,0))
                            ev_e = st.time_input(f"End time #{n['id']}", value=time(10,0))
                            all_day = st.checkbox(f"All-day #{n['id']}", value=False)
                            if st.button("Create Event from Reminder", key=f"note2event_{n['id']}"):
                                s_iso = iso_utc(ev_d, None if all_day else ev_s)
                                e_iso = iso_utc(ev_d, None if all_day else ev_e)
                                exec1("""INSERT INTO events(title, start_at, end_at, all_day, assignees, family) 
                                         VALUES(?,?,?,?,?,?)""",
                                      (n["content"], s_iso, e_iso, 1 if all_day else 0, n["assignee"] or "", FAMILY))
                                new_ev = q("SELECT id FROM events WHERE family=? ORDER BY id DESC LIMIT 1", (FAMILY,))
                                if new_ev:
                                    exec1("UPDATE notes SET linked_event_id=? WHERE id=?", (new_ev[0]["id"], n["id"]))
                                st.success("Event created."); st.rerun()
                        if n["linked_event_id"]:
                            if st.button("Unlink Event", key=f"unlink_{n['id']}"):
                                exec1("UPDATE notes SET linked_event_id=NULL WHERE id=?", (n["id"],)); st.success("Unlinked."); st.rerun()
                        del_ok = st.checkbox(f"Soft delete note #{n['id']}", key=f"conf_note_{n['id']}")
                        if st.button("🗑️ Delete Note", key=f"del_note_{n['id']}") and del_ok:
                            exec1("UPDATE notes SET deleted_at=? WHERE id=? AND family=?", (now_iso_utc(), n["id"], FAMILY)); st.rerun()

# -------------------- LISTS --------------------
with tabs[1]:
    st.subheader("Lists")

    # --- helper: fetch og:image from a product page (best-effort, safe to fail) ---
    def fetch_og_image(url: str) -> Optional[str]:
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            return None
        # Avoid doing heavy work on each rerun: simple in-memory memo
        cache_key = f"_og_{url}"
        if cache_key in st.session_state:
            return st.session_state[cache_key]

        try:
            import requests
            from bs4 import BeautifulSoup  # type: ignore
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/124.0 Safari/537.36"
            }
            r = requests.get(url, headers=headers, timeout=5)
            if r.status_code != 200 or "text/html" not in r.headers.get("Content-Type", ""):
                st.session_state[cache_key] = None
                return None
            soup = BeautifulSoup(r.text, "html.parser")
            # Try standard Open Graph first
            og = soup.find("meta", attrs={"property": "og:image"}) or soup.find("meta", attrs={"name": "og:image"})
            if og and og.get("content"):
                img = og["content"].strip()
                # Some sites give relative URLs
                from urllib.parse import urljoin
                img = urljoin(url, img)
                st.session_state[cache_key] = img
                return img
            # Fallbacks used by some stores
            tw = soup.find("meta", attrs={"name": "twitter:image"}) or soup.find("meta", attrs={"property": "twitter:image"})
            if tw and tw.get("content"):
                from urllib.parse import urljoin
                img = urljoin(url, tw["content"].strip())
                st.session_state[cache_key] = img
                return img
        except Exception:
            pass
        st.session_state[cache_key] = None
        return None

    # Create list form
    with st.expander("Create a new list"):
        with st.form("create_list", clear_on_submit=True):
            new_title = st.text_input("List name", placeholder="e.g., Groceries or Alex's Birthday")
            list_type = st.selectbox("List type", ["normal", "wishlist"])
            if st.form_submit_button("Create") and (new_title or "").strip():
                exec1(
                    "INSERT INTO lists(title, family, type, created_by) VALUES(?,?,?,?)",
                    (new_title.strip(), FAMILY, list_type, DISPLAY_NAME),
                )
                st.success("List created."); st.rerun()

    # Load lists for this family
    lists = q(
        """SELECT id, title, type, created_by 
           FROM lists 
           WHERE family=? AND (deleted_at IS NULL) 
           ORDER BY id DESC LIMIT 100""",
        (FAMILY,),
    )

    if not lists:
        st.info("No lists yet.")
    else:
        # tiny always-works placeholder
        inline_svg_data_uri = (
            "data:image/svg+xml;utf8,"
            "<svg xmlns='http://www.w3.org/2000/svg' width='150' height='150'>"
            "<rect width='100%' height='100%' fill='%23eeeeee'/>"
            "<text x='50%' y='50%' dominant-baseline='middle' text-anchor='middle' "
            "fill='%23999999' font-family='Arial' font-size='14'>Preview</text>"
            "</svg>"
        )

        def looks_like_image(url: str) -> bool:
            u = (url or "").lower().split("?", 1)[0]
            return any(u.endswith(ext) for ext in (".jpg",".jpeg",".png",".gif",".webp",".bmp"))

        for lst in lists:
            is_wishlist = (lst["type"] == "wishlist")
            you_are_creator = (DISPLAY_NAME == (lst["created_by"] or ""))

            with st.container(border=True):
                heading = f"### 🗒️ {esc(lst['title'])}"
                if is_wishlist:
                    heading += " &nbsp; <span style='font-size:12px;padding:2px 6px;border:1px solid #ddd;border-radius:10px;background:#fff;'>Wishlist</span>"
                st.markdown(heading, unsafe_allow_html=True)
                if is_wishlist and lst["created_by"]:
                    st.caption(f"List owner: {esc(lst['created_by'])}")

                # ---------------- Add items ----------------
                with st.form(f"add_item_{lst['id']}", clear_on_submit=True):
                    if is_wishlist:
                        c = st.columns([0.40, 0.32, 0.18, 0.10])
                        with c[0]:
                            t = st.text_input("Item name", key=f"item_txt_{lst['id']}")
                        with c[1]:
                            link = st.text_input("Link (optional)", placeholder="https://product-page…", key=f"item_link_{lst['id']}")
                        with c[2]:
                            img_url = st.text_input("Image URL (optional)", placeholder="https://image.jpg", key=f"item_img_{lst['id']}")
                        with c[3]:
                            add_btn = st.form_submit_button("Add")
                        if add_btn and (t or "").strip():
                            # If no image_url provided but link exists, try to auto-fetch og:image
                            final_img = (img_url or "").strip() or None
                            link_clean = (link or "").strip() or None
                            if not final_img and link_clean and not looks_like_image(link_clean):
                                final_img = fetch_og_image(link_clean)  # may return None; that's fine
                            exec1(
                                "INSERT INTO list_items(list_id, text, url, image_url, done) VALUES(?,?,?,?,0)",
                                (lst["id"], t.strip(), link_clean, final_img),
                            )
                            st.rerun()
                    else:
                        c = st.columns([0.85, 0.15])
                        with c[0]:
                            t = st.text_input("Add item", key=f"item_txt_{lst['id']}")
                        with c[1]:
                            add_btn = st.form_submit_button("Add")
                        if add_btn and (t or "").strip():
                            exec1(
                                "INSERT INTO list_items(list_id, text, done) VALUES(?,?,0)",
                                (lst["id"], t.strip()),
                            )
                            st.rerun()

                # ---------------- Fetch items ----------------
                items = q(
                    """SELECT id, text, url, image_url, done, claimed_by, purchased_by
                       FROM list_items WHERE list_id=? 
                       ORDER BY id DESC LIMIT 200""",
                    (lst["id"],),
                )

                # ---------------- Render items ----------------
                for it in items:
                    if is_wishlist:
                        # Wishlist display (no checkboxes, creator can't see claim/purchase state)
                        left, right = st.columns([0.75, 0.25])
                        with left:
                            # choose best image source
                            img_src = it["image_url"] or (it["url"] if looks_like_image(it["url"] or "") else inline_svg_data_uri)

                            # clickable image + title
                            if it["url"]:
                                st.markdown(
                                    f"""
                                    <div style="margin-bottom:10px;">
                                      <a href="{esc(it['url'])}" target="_blank" rel="noopener">
                                        <img src="{esc(img_src)}"
                                             style="border-radius:8px;max-width:150px;display:block;">
                                      </a><br/>
                                      <strong>{esc(it['text'])}</strong>
                                    </div>
                                    """,
                                    unsafe_allow_html=True
                                )
                            else:
                                st.markdown(
                                    f"""
                                    <div style="margin-bottom:10px;">
                                      <img src="{esc(img_src)}"
                                           style="border-radius:8px;max-width:150px;display:block;">
                                      <br/><strong>{esc(it['text'])}</strong>
                                    </div>
                                    """,
                                    unsafe_allow_html=True
                                )

                            # Privacy: creator sees no claim/purchase info
                            if you_are_creator:
                                pass
                            else:
                                # Non-creator sees status
                                if it["purchased_by"]:
                                    if it["purchased_by"] == DISPLAY_NAME:
                                        st.caption("🛒 Purchased by **you**")
                                    else:
                                        st.caption(f"🛒 Purchased by **{esc(it['purchased_by'])}**")
                                elif it["claimed_by"]:
                                    if it["claimed_by"] == DISPLAY_NAME:
                                        st.caption("✅ Reserved by **you** (not purchased yet)")
                                    else:
                                        st.caption(f"✅ Reserved by **{esc(it['claimed_by'])}** (not purchased yet)")

                        with right:
                            if you_are_creator:
                                pass
                            else:
                                if it["purchased_by"]:
                                    pass  # purchased → locked
                                elif it["claimed_by"] is None:
                                    if st.button("Claim", key=f"claim_{it['id']}"):
                                        exec1(
                                            "UPDATE list_items SET claimed_by=?, purchased_by=NULL WHERE id=?",
                                            (DISPLAY_NAME, it["id"]),
                                        )
                                        st.rerun()
                                elif it["claimed_by"] == DISPLAY_NAME:
                                    b1, b2 = st.columns(2)
                                    with b1:
                                        if st.button("Unclaim", key=f"unclaim_{it['id']}"):
                                            exec1(
                                                "UPDATE list_items SET claimed_by=NULL, purchased_by=NULL WHERE id=?",
                                                (it["id"],),
                                            )
                                            st.rerun()
                                    with b2:
                                        if st.button("Mark Purchased", key=f"purchase_{it['id']}"):
                                            exec1(
                                                "UPDATE list_items SET purchased_by=? WHERE id=?",
                                                (DISPLAY_NAME, it["id"]),
                                            )
                                            st.rerun()
                                else:
                                    pass  # claimed by someone else

                    else:
                        # Normal list (original behavior)
                        cols = st.columns([0.1, 0.70, 0.20])
                        with cols[0]:
                            changed = st.checkbox("", value=bool(it["done"]), key=f"done_{it['id']}")
                            if changed != bool(it["done"]):
                                exec1(
                                    "UPDATE list_items SET done=? WHERE id=?",
                                    (1 if changed else 0, it["id"]),
                                )
                        with cols[1]:
                            st.write(
                                ("~~" if it["done"] else "") + esc(it["text"]) + ("~~" if it["done"] else "")
                            )
                        with cols[2]:
                            if st.button("🗑️", key=f"del_item_{it['id']}"):
                                exec1("DELETE FROM list_items WHERE id=?", (it["id"],))
                                st.rerun()

                # ---------------- Delete entire list ----------------
                with st.expander("Delete list"):
                    if st.checkbox("Confirm delete list", key=f"conf_list_{lst['id']}"):
                        if st.button("🗑️ Delete List", key=f"do_del_list_{lst['id']}"):
                            exec1(
                                "UPDATE lists SET deleted_at=? WHERE id=?",
                                (now_iso_utc(), lst["id"]),
                            )
                            st.rerun()




# -------------------- NOTEPAD --------------------
with tabs[2]:
    st.subheader("Notepad")
    with st.form("create_doc", clear_on_submit=True):
        title = st.text_input("Title")
        if st.form_submit_button("Create") and (title or "").strip():
            # FIX: include content='' to satisfy old DBs with NOT NULL & no default
            exec1("INSERT INTO documents(title, content, family) VALUES(?,?,?)", (title.strip(), "", FAMILY)); st.success("Document created."); st.rerun()

    docs = q("""SELECT * FROM documents WHERE family=? AND (deleted_at IS NULL) ORDER BY id DESC LIMIT 50""", (FAMILY,))
    if not docs:
        st.info("No documents.")
    else:
        for d in docs:
            with st.expander(f"📝 {esc(d['title'])}", expanded=False):
                new = st.text_area("Content", value=d["content"], key=f"doc_{d['id']}", height=200)
                ccols = st.columns(2)
                with ccols[0]:
                    if st.button("💾 Save", key=f"save_doc_{d['id']}"):
                        exec1("UPDATE documents SET content=? WHERE id=?", (new, d["id"])); st.success("Saved.")
                with ccols[1]:
                    if st.button("🗑️ Delete", key=f"del_doc_{d['id']}"):
                        exec1("UPDATE documents SET deleted_at=? WHERE id=?", (now_iso_utc(), d["id"])); st.rerun()

# -------------------- CALENDAR (FullCalendar) --------------------
with tabs[3]:
    st.subheader("Calendar")

    # Helpers
    def to_exclusive_end(d: date) -> datetime:
        return datetime.combine(d + timedelta(days=1), time(0, 0))

    def from_iso(dt_s: Optional[str]) -> Optional[datetime]:
        if not dt_s: return None
        try: return datetime.fromisoformat(dt_s)
        except Exception: return None

    def load_events_between(d0: date, d1: date, who: str = "") -> List[Dict]:
        rows = q("SELECT * FROM events WHERE family=? ORDER BY start_at ASC", (FAMILY,))
        out: List[Dict] = []
        for e in rows:
            sdt = from_iso(e["start_at"])
            if not sdt: continue
            sd = sdt.date()
            if d0 <= sd <= d1:
                if who and who.lower() not in (e["assignees"] or "").lower():
                    continue
                s = (e["assignees"] or "default")
                h = 0
                for ch in s: h = (h * 33 + ord(ch)) & 0xFFFFFFFF
                palette = ["#4285F4","#DB4437","#F4B400","#0F9D58","#AB47BC","#00ACC1","#EF6C00","#5C6BC0","#26A69A","#EC407A"]
                color = palette[h % len(palette)]
                all_day = bool(e["all_day"])
                out.append({
                    "id": str(e["id"]),
                    "title": e["title"],
                    "start": e["start_at"],
                    "end":   e["end_at"],
                    "allDay": all_day,
                    "backgroundColor": color + "80",
                    "borderColor": color,
                    "extendedProps": {"assignees": e["assignees"] or ""},
                })
        return out

    today = date.today()
    view = st.session_state.get("fc_view", "dayGridMonth")
    ref = st.session_state.get("fc_ref", today)
    whof = st.text_input("Filter by assignee contains", key="fc_filter")

    nav = st.columns([1.6,1,2,1,1])
    with nav[0]:
        view = st.selectbox("View", ["dayGridMonth","timeGridWeek","timeGridDay","listMonth"],
                            index=["dayGridMonth","timeGridWeek","timeGridDay","listMonth"].index(view))
    with nav[1]:
        if st.button("◀️ Prev"):
            if view in ("dayGridMonth","listMonth"):
                m = ref.month - 1 or 12
                y = ref.year - 1 if ref.month == 1 else ref.year
                ref = date(y, m, 1)
            elif view == "timeGridWeek":
                ref = ref - timedelta(weeks=1)
            else:
                ref = ref - timedelta(days=1)
    with nav[2]:
        st.markdown(f"### {ref.strftime('%B %Y')}")
    with nav[3]:
        if st.button("Next ▶️"):
            if view in ("dayGridMonth","listMonth"):
                m = ref.month + 1 if ref.month < 12 else 1
                y = ref.year + 1 if ref.month == 12 else ref.year
                ref = date(y, m, 1)
            elif view == "timeGridWeek":
                ref = ref + timedelta(weeks=1)
            else:
                ref = ref + timedelta(days=1)
    with nav[4]:
        if st.button("Today"): ref = today

    st.session_state["fc_view"] = view
    st.session_state["fc_ref"] = ref

    # CREATE EVENT
    with st.expander("Create a new event"):
        with st.form("create_event", clear_on_submit=True):
            title = st.text_input("Title", placeholder="e.g., Soccer Practice")
            c = st.columns([1,1,1,1,1])
            with c[0]: sd = st.date_input("Start date", value=ref)
            with c[1]: ed = st.date_input("End date", value=ref)
            with c[2]: all_day = st.checkbox("All day", value=False)
            with c[3]:
                stime = st.time_input("Start time", value=time(9,0), disabled=all_day)
            with c[4]:
                etime = st.time_input("End time", value=time(10,0), disabled=all_day)
            assignees = st.text_input("Assignees (comma separated)", placeholder="e.g., Mom, Dad, Alex")

            if st.form_submit_button("Add Event") and title.strip():
                if ed < sd:
                    st.error("End date must be the same or after start date."); st.stop()
                if all_day:
                    s_iso = datetime.combine(sd, time(0,0)).isoformat()
                    e_iso = to_exclusive_end(ed).isoformat()
                else:
                    s_iso = datetime.combine(sd, stime).isoformat()
                    e_iso = datetime.combine(ed, etime).isoformat()
                exec1("""INSERT INTO events(title, start_at, end_at, assignees, family, all_day)
                         VALUES(?,?,?,?,?,?)""",
                      (title.strip(), s_iso, e_iso, assignees.strip(), FAMILY, 1 if all_day else 0))
                st.success("Event created."); st.rerun()

    # Determine window and load events
    if view in ("dayGridMonth","listMonth"):
        d0 = ref.replace(day=1)
        d1 = d0.replace(day=calendar.monthrange(d0.year, d0.month)[1])
    elif view == "timeGridWeek":
        d0 = ref - timedelta(days=ref.weekday())
        d1 = d0 + timedelta(days=6)
    else:
        d0 = ref; d1 = ref

    events = load_events_between(d0, d1, whof)

    options = {
        "initialDate": ref.isoformat(),
        "initialView": view,
        "headerToolbar": { "left": "", "center": "", "right": "" },
        "height": 740,
        "allDaySlot": True,
        "slotMinTime": "06:00:00",
        "slotMaxTime": "22:00:00",
        "nowIndicator": True,
        "weekNumbers": False,
        "eventOverlap": True,
        "expandRows": True,
        "dayMaxEvents": True,
        "firstDay": 0,
        "slotEventOverlap": True,
        "editable": False,
        "selectable": False,
        "eventTimeFormat": { "hour": "2-digit", "minute": "2-digit", "meridiem": False },
    }

    if CAL_OK:
        # Force FullCalendar to remount when view/date change
        cal_key = f"fullcalendar_{view}_{ref.isoformat()}"
        fc_calendar(events=events, options=options, key=cal_key)
    else:
        st.warning(
            "For a Google-like calendar, install `streamlit-calendar`.\n\n"
            "pip install streamlit-calendar (and add to requirements.txt)"
        )

    # Manage events (edit/delete)
    st.markdown("### Manage events")
    evs_db = q("SELECT * FROM events WHERE family=? ORDER BY start_at ASC", (FAMILY,))
    for e in evs_db:
        sdt = from_iso(e["start_at"]) or datetime.now()
        edt = from_iso(e["end_at"])
        all_day_val = bool(e["all_day"])
        disp_end_date = (edt - timedelta(days=1)).date() if (all_day_val and edt) else ((edt.date() if edt else sdt.date()))
        with st.expander(f"✏️ {e['title']} · {sdt.strftime('%Y-%m-%d %H:%M')}"):
            title = st.text_input("Title", value=e["title"], key=f"title_{e['id']}")
            cc = st.columns([1,1,1,1,1])
            with cc[0]: sd = st.date_input("Start Date", value=sdt.date(), key=f"sd_{e['id']}")
            with cc[1]: ed = st.date_input("End Date", value=disp_end_date, key=f"ed_{e['id']}")
            with cc[2]: all_day = st.checkbox("All day", value=all_day_val, key=f"ad_{e['id']}")
            with cc[3]:
                stime = st.time_input("Start Time", value=(sdt.time() if not all_day else time(0,0)),
                                      disabled=all_day, key=f"stm_{e['id']}")
            with cc[4]:
                etime = st.time_input("End Time", value=(edt.time() if (edt and not all_day) else time(0,0)),
                                      disabled=all_day, key=f"etm_{e['id']}")
            assignees = st.text_input("Assignees", value=e["assignees"] or "", key=f"asg_{e['id']}")

            b = st.columns([1,1])
            with b[0]:
                if st.button("💾 Update", key=f"upd_{e['id']}"):
                    if ed < sd:
                        st.error("End date must be the same or after start date.")
                    else:
                        if all_day:
                            s_iso = datetime.combine(sd, time(0,0)).isoformat()
                            e_iso = to_exclusive_end(ed).isoformat()
                        else:
                            s_iso = datetime.combine(sd, stime).isoformat()
                            e_iso = datetime.combine(ed, etime).isoformat()
                        exec1("""UPDATE events
                                 SET title=?, start_at=?, end_at=?, assignees=?, all_day=?
                                 WHERE id=? AND family=?""",
                              (title.strip() or "Untitled", s_iso, e_iso, assignees.strip(),
                               1 if all_day else 0, e["id"], FAMILY))
                        st.success("Updated."); st.rerun()
            with b[1]:
                if st.button("🗑️ Delete", key=f"del_{e['id']}"):
                    exec1("DELETE FROM events WHERE id=? AND family=?", (e["id"], FAMILY))
                    st.success("Deleted."); st.rerun()
            # === ADD-ON BLOCK D: RSVP controls + attendee chips (inside event expander) ===
            st.markdown("---")
            st.markdown("#### 🗳️ RSVP")

            # Current user's RSVP state
            _r = q("SELECT status FROM event_rsvps WHERE event_id=? AND username=? LIMIT 1", (e["id"], DISPLAY_NAME))
            _mine = (_r[0]["status"] if _r else None)

            # Buttons row
            rb1, rb2, rb3, rb4 = st.columns([0.18, 0.18, 0.18, 0.46])
            with rb1:
                lab = "✅ Going" + (" (you)" if _mine == "going" else "")
                if st.button(lab, key=f"rsvp_go_{e['id']}"):
                    exec1("""INSERT INTO event_rsvps(event_id, username, status) 
                             VALUES(?,?,?) 
                             ON CONFLICT(event_id, username) 
                             DO UPDATE SET status=excluded.status, responded_at=datetime('now')""",
                          (e["id"], DISPLAY_NAME, "going"))
                    st.rerun()
            with rb2:
                lab = "🤔 Maybe" + (" (you)" if _mine == "maybe" else "")
                if st.button(lab, key=f"rsvp_maybe_{e['id']}"):
                    exec1("""INSERT INTO event_rsvps(event_id, username, status) 
                             VALUES(?,?,?) 
                             ON CONFLICT(event_id, username) 
                             DO UPDATE SET status=excluded.status, responded_at=datetime('now')""",
                          (e["id"], DISPLAY_NAME, "maybe"))
                    st.rerun()
            with rb3:
                lab = "❌ Can't" + (" (you)" if _mine == "cant" else "")
                if st.button(lab, key=f"rsvp_cant_{e['id']}"):
                    exec1("""INSERT INTO event_rsvps(event_id, username, status) 
                             VALUES(?,?,?) 
                             ON CONFLICT(event_id, username) 
                             DO UPDATE SET status=excluded.status, responded_at=datetime('now')""",
                          (e["id"], DISPLAY_NAME, "cant"))
                    st.rerun()
            with rb4:
                if _mine is not None:
                    if st.button("Clear my RSVP", key=f"rsvp_clear_{e['id']}"):
                        exec1("DELETE FROM event_rsvps WHERE event_id=? AND username=?", (e["id"], DISPLAY_NAME))
                        st.rerun()
                else:
                    st.caption("No RSVP from you yet.")

            # Attendees (with profile pics)
            st.markdown("#### 👥 Attendees")
            _rows = q("""
                SELECT r.username, r.status,
                       COALESCE(p.first_name,'') AS first_name,
                       COALESCE(p.last_name,'')  AS last_name,
                       COALESCE(p.avatar_path,'') AS avatar_path
                FROM event_rsvps r
                LEFT JOIN user_profiles p 
                  ON p.family=? AND p.username=r.username
                WHERE r.event_id=?
                ORDER BY 
                  CASE r.status WHEN 'going' THEN 0 WHEN 'maybe' THEN 1 ELSE 2 END,
                  r.username COLLATE NOCASE
            """, (FAMILY, e["id"]))

            if not _rows:
                st.caption("No RSVPs yet.")
            else:
                # Inline chips with color by status (no external CSS dependency)
                def _chip_border(status: str) -> str:
                    if status == "going": return "#1b5e20"   # green-ish
                    if status == "maybe": return "#9e7500"   # amber-ish
                    return "#7b1c1c"                         # red-ish

                html_parts = []
                for r in _rows:
                    full = (r["first_name"] + " " + r["last_name"]).strip() or r["username"]
                    full = esc(full)
                    bcol = _chip_border(r["status"])
                    avat = r["avatar_path"] or ""

                    # Build avatar (always 32×32) inside an inline-block wrapper to avoid layout breaks
                    if avat and os.path.exists(avat):
                        avatar_inner = f"<img src='{esc(avat)}' style='width:100%;height:100%;object-fit:cover;'/>"
                    else:
                        initials = "".join([part[0].upper() for part in full.split()[:2] if part]) or "?"
                        avatar_inner = (
                            f"<span style='width:100%;height:100%;display:flex;align-items:center;justify-content:center;"
                            f"background:#888;color:#fff;font-size:14px;font-weight:600'>{initials}</span>"
                        )
                    avatar = (
                        f"<span style='display:inline-block;width:32px;height:32px;border-radius:50%;overflow:hidden'>"
                        f"{avatar_inner}</span>"
                    )

                    # One self-contained chip with inline styles
                    status_label = {"going":"Going","maybe":"Maybe","cant":"Can't"}.get(r["status"], r["status"])
                    chip = (
                        f"<span style='display:inline-flex;align-items:center;gap:8px;padding:6px 10px;"
                        f"border-radius:999px;border:1px solid {bcol};margin:2px;font-size:13px;line-height:1;'>"
                        f"{avatar}<span>{full}</span><span style='opacity:0.7'>· {status_label}</span></span>"
                    )
                    html_parts.append(chip)

                st.markdown(" ".join(html_parts), unsafe_allow_html=True)
            # === END ADD-ON BLOCK D ===

# -------------------- FAMILY FEED --------------------
with tabs[4]:
    st.subheader("Family Feed")

    with st.expander("Create album"):
        with st.form("create_album", clear_on_submit=True):
            name = st.text_input("Album name")
            if st.form_submit_button("Create") and (name or "").strip():
                exec1("INSERT INTO albums(name, family) VALUES(?,?)", (name.strip(), FAMILY)); st.success("Album created."); st.rerun()

    albums = q("SELECT * FROM albums WHERE family=? ORDER BY id DESC LIMIT 100", (FAMILY,))
    alb_opts = {a["name"]: a["id"] for a in albums} if albums else {}

    with st.form("create_post", clear_on_submit=True):
        st.markdown("#### New post")
        album_id = st.selectbox("Album (optional)", ["(none)"] + list(alb_opts.keys()))
        caption = st.text_input("Caption")
        uploads = st.file_uploader("Photos / Videos", type=["png","jpg","jpeg","gif","mp4","webm","mov"], accept_multiple_files=True)
        if st.form_submit_button("Post"):
            exec1("INSERT INTO posts(family, album_id, author, caption) VALUES(?,?,?,?)",
                  (FAMILY, alb_opts.get(album_id) if album_id != "(none)" else None, DISPLAY_NAME, caption or ""))
            post_id = q("SELECT id FROM posts WHERE family=? ORDER BY id DESC LIMIT 1", (FAMILY,))[0]["id"]
            if uploads:
                for up in uploads:
                    path, thumb, mime, media_type = save_media(up)
                    exec1("INSERT INTO post_media(post_id, path, thumb_path, mime, media_type) VALUES(?,?,?,?,?)",
                          (post_id, path, thumb, mime, media_type))
            st.success("Posted."); st.rerun()

    page = st.session_state.get("feed_page", 0)
    posts = q("""SELECT p.*,
                        (SELECT COUNT(*) FROM post_likes pl WHERE pl.post_id=p.id) AS like_count,
                        (SELECT COUNT(*) FROM post_comments pc WHERE pc.post_id=p.id) AS comment_count
                 FROM posts p 
                 WHERE p.family=? 
                 ORDER BY p.id DESC
                 LIMIT ? OFFSET ?""", (FAMILY, PAGE_SIZE, page*PAGE_SIZE))

    c1,c2,c3 = st.columns(3)
    with c1:
        if st.button("◀ Prev", disabled=page==0, key="feed_prev"):
            st.session_state["feed_page"] = max(0, page-1); st.rerun()
    with c3:
        if st.button("Next ▶", disabled=len(posts)<PAGE_SIZE, key="feed_next"):
            st.session_state["feed_page"] = page+1; st.rerun()

    if not posts:
        st.info("No posts yet.")
    else:
        for p in posts:
            with st.container(border=True):
                head = st.columns([0.7,0.3])
                with head[0]:
                    st.markdown(f"**{esc(p['author'])}** · {esc(p['created_at'])}")
                    if p["caption"]: st.write(esc(p["caption"]))
                with head[1]:
                    liked = bool(q("SELECT 1 FROM post_likes WHERE post_id=? AND author=? LIMIT 1", (p["id"], DISPLAY_NAME)))
                    if st.button(("❤️ Unlike" if liked else "🤍 Like") + f" ({p['like_count']})", key=f"like_{p['id']}"):
                        if liked:
                            exec1("DELETE FROM post_likes WHERE post_id=? AND author=?", (p["id"], DISPLAY_NAME))
                        else:
                            exec1("INSERT OR IGNORE INTO post_likes(post_id, author) VALUES(?,?)", (p["id"], DISPLAY_NAME))
                        st.rerun()

                media = q("SELECT * FROM post_media WHERE post_id=? ORDER BY id ASC LIMIT 12", (p["id"],))
                if media:
                    cols = st.columns(3)
                    for idx, m in enumerate(media):
                        with cols[idx % 3]:
                            if m["media_type"] == "video":
                                if os.path.exists(m["path"]):
                                    st.video(m["path"])
                                else:
                                    st.warning("Video missing.")
                                if m["thumb_path"] and os.path.exists(m["thumb_path"]):
                                    st.image(m["thumb_path"], use_container_width=True, caption="Preview")
                            else:
                                img_path = m["thumb_path"] or m["path"]
                                if os.path.exists(img_path):
                                    st.image(img_path, use_container_width=True)
                                else:
                                    st.warning("Image missing.")

                with st.expander(f"💬 Comments ({p['comment_count']})"):
                    with st.form(f"add_post_comment_{p['id']}", clear_on_submit=True):
                        txt = st.text_input("Add a comment", placeholder="Type and press Enter")
                        if st.form_submit_button("Comment") and (txt or "").strip():
                            exec1("INSERT INTO post_comments(post_id, author, text) VALUES(?,?,?)",
                                  (p["id"], DISPLAY_NAME, txt.strip())); st.rerun()
                    comments = q("SELECT * FROM post_comments WHERE post_id=? ORDER BY id DESC LIMIT 100", (p["id"],))
                    if not comments:
                        st.caption("No comments yet.")
                    else:
                        for c in comments:
                            row = st.columns([6,1])
                            with row[0]:
                                st.markdown(f"**{esc(c['author'])}** · {esc(c['created_at'])}<br/>{esc(c['text'])}", unsafe_allow_html=True)
                            with row[1]:
                                if st.button("🗑️", key=f"del_post_comm_{c['id']}"):
                                    exec1("DELETE FROM post_comments WHERE id=?", (c["id"],)); st.rerun()

                with st.expander("Delete post"):
                    if st.button("🗑️ Delete", key=f"del_post_{p['id']}"):
                        exec1("DELETE FROM posts WHERE id=?", (p["id"],)); st.rerun()

# -------------------- 💬 CHAT (IM-STYLE + LIVE + NOTIFICATIONS) --------------------
with tabs[5]:
    st.subheader("Group Chat")

    # Per-room live update & notifications controls (isolated here)
    c0, c1, c2 = st.columns([0.45, 0.3, 0.25])
    with c0:
        st.caption("Live updates")
        live = st.checkbox("Enable", value=True, key="chat_live_enable")
    with c1:
        interval = st.slider("Every (sec)", 2, 15, 5, key="chat_live_interval")
    with c2:
        notify = st.checkbox("Notifications (browser + beep)", value=True, key="chat_notify_enable")

    if live:
        st_autorefresh(interval=interval*1000, key=f"chat_live_autorefresh")

    # Rooms scoped to FAMILY
    existing_rooms = [r["room"] for r in q(
        "SELECT DISTINCT room FROM chat_messages WHERE family=? ORDER BY room COLLATE NOCASE", (FAMILY,)
    )]
    default_room = "general"
    if default_room not in existing_rooms:
        existing_rooms.insert(0, default_room)

    rleft, rright = st.columns([0.7, 0.3])
    with rleft:
        room = st.selectbox("Room", options=existing_rooms or [default_room], index=0, key="chat_room_select")
    with rright:
        with st.form("create_room", clear_on_submit=True):
            new_room = st.text_input("Create room", placeholder="e.g., planning, chores")
            if st.form_submit_button("Add Room") and (new_room or "").strip():
                exec1("INSERT INTO chat_messages(family, room, author, text) VALUES(?,?,?,?)",
                      (FAMILY, new_room.strip(), "system", f"Room '{new_room.strip()}' created"))
                st.success("Room created."); st.rerun()

    st.markdown("---")

    # ---- Fetch last N messages ----
    LAST_N = 100
    rows_desc = q("""SELECT * FROM chat_messages 
                     WHERE family=? AND room=?
                     ORDER BY id DESC
                     LIMIT ?""", (FAMILY, room, LAST_N))
    msgs = list(reversed(rows_desc))

    state_key = f"last_seen_chat_{FAMILY}_{room}"
    prev_seen = int(st.session_state.get(state_key, 0) or 0)
    last_id = int(msgs[-1]["id"]) if msgs else 0
    new_from_others = [m for m in msgs if m["id"] > prev_seen and m["author"] != DISPLAY_NAME]
    new_count = len(new_from_others)

    st.markdown("""
<style>
.chat-box {
    max-height: 520px;
    overflow-y: auto;
    padding: 8px 10px;
    border: 1px solid #ddd;
    border-radius: 10px;
    background: #fafafa;
}
[data-theme="dark"] .chat-box {
    background: #121212;
    border-color: #2e2e2e;
}

.msg {
    margin: 6px 0;
    display: flex;
    flex-direction: column;
}
.msg .meta {
    font-size: 11px;
    color: #666;
    margin: 0 6px 2px 6px;
}
[data-theme="dark"] .msg .meta {
    color: #c8c8c8;
}

.bubble {
    display: inline-block;
    padding: 8px 12px;
    border-radius: 14px;
    background: #ffffff;
    border: 1px solid #e6e6e6;
    color: #000000;
    max-width: 85%;
    word-wrap: break-word;
    white-space: pre-wrap;
}
[data-theme="dark"] .bubble {
    background: #1d1f22;
    border-color: #2f3236;
    color: #f0f0f0;
}
[data-theme="dark"] .bubble a {
    color: #a7c7ff;
}

.me {
    align-items: flex-end;
}
.me .bubble {
    background: #e8f0ff;
    border-color: #d0dcff;
}
[data-theme="dark"] .me .bubble {
    background: #18314f;
    border-color: #2b4a6f;
}
</style>
""", unsafe_allow_html=True)

    # Optional room badge
    if new_count > 0:
        st.caption(f"🔔 {new_count} new message(s) since you last viewed this room")

    # ---- Render messages in a scrollable window ----
    st.markdown(f'<div class="chat-box" id="chatbox">', unsafe_allow_html=True)
    if not msgs:
        st.markdown('<div class="msg"><div class="bubble">No messages yet. Say hi 👋</div></div>', unsafe_allow_html=True)
    else:
        for m in msgs:
            own = (m["author"] == DISPLAY_NAME)
            cls = "msg me" if own else "msg"
            author = esc(m["author"])
            ts = esc(m["created_at"])
            text = esc(m["text"])
            st.markdown(
                f'<div class="{cls}">'
                f'  <div class="meta">{author} · {ts}</div>'
                f'  <div class="bubble">{text}</div>'
                f'</div>',
                unsafe_allow_html=True
            )
    st.markdown("</div>", unsafe_allow_html=True)

    # ---- Auto-scroll to bottom on render ----
    st.markdown(
        """
        <script>
        const box = window.parent.document.getElementById('chatbox') || document.getElementById('chatbox');
        if (box) { box.scrollTop = box.scrollHeight; }
        </script>
        """,
        unsafe_allow_html=True
    )

    # ---- Browser notification + subtle beep for new messages from others ----
    if notify and new_count > 0:
        import json
        latest = new_from_others[-1] if new_from_others else msgs[-1]
        latest_author = latest["author"] if latest else ""
        latest_text = latest["text"] if latest else ""
        st.markdown(
            f"""
            <script>
            (function() {{
              const body = {json.dumps(str(latest_author) + ": " + str(latest_text))}.slice(0, 160);
              if (typeof Notification !== 'undefined') {{
                if (Notification.permission === 'default') {{
                  Notification.requestPermission();
                }}
                if (Notification.permission === 'granted') {{
                  const n = new Notification('Hive: new message', {{ body: body }});
                  setTimeout(() => n.close(), 5000);
                }}
              }}
              try {{
                const Ctx = window.AudioContext || window.webkitAudioContext;
                const ctx = new Ctx();
                const o = ctx.createOscillator();
                const g = ctx.createGain();
                o.type = 'sine';
                o.frequency.value = 880;
                o.connect(g); g.connect(ctx.destination);
                g.gain.setValueAtTime(0.0001, ctx.currentTime);
                g.gain.exponentialRampToValueAtTime(0.2, ctx.currentTime + 0.01);
                o.start();
                g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.3);
                o.stop(ctx.currentTime + 0.35);
              }} catch (e) {{}}
            }})();
            </script>
            """,
            unsafe_allow_html=True
        )

    # ---- Quick delete of your recent messages ----
    mine = q("""SELECT id, text, created_at FROM chat_messages 
                WHERE family=? AND room=? AND author=?
                ORDER BY id DESC LIMIT 10""", (FAMILY, room, DISPLAY_NAME))
    if mine:
        with st.expander("🗑️ Delete one of your last 10 messages"):
            for m in mine:
                cols = st.columns([0.1, 0.75, 0.15])
                with cols[0]:
                    if st.button("🗑️", key=f"del_chat_{m['id']}"):
                        exec1("DELETE FROM chat_messages WHERE id=? AND family=? AND room=? AND author=?",
                              (m["id"], FAMILY, room, DISPLAY_NAME))
                        st.rerun()
                with cols[1]:
                    st.caption(f"{esc(m['created_at'])}")
                    st.write(esc(m["text"]))
                with cols[2]:
                    pass

    st.markdown("---")

    # ---- Send box ----
    with st.form("send_message", clear_on_submit=True):
        msg = st.text_input("Message", placeholder="Type a message and press Send")
        cc1, cc2 = st.columns([0.2,0.8])
        with cc1:
            send = st.form_submit_button("Send")
        with cc2:
            if st.form_submit_button("↻ Refresh"):
                st.rerun()
        if send and (msg or "").strip():
            exec1("INSERT INTO chat_messages(family, room, author, text) VALUES(?,?,?,?)",
                  (FAMILY, room, DISPLAY_NAME, msg.strip()))
            st.rerun()

    # ---- Finalize "last seen" state ----
    st.session_state[state_key] = last_id

# =============================================================================
# End of file
# =============================================================================
