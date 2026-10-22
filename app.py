import re
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from functools import wraps

import requests as http_requests
from flask import Flask, jsonify, render_template, request, redirect, url_for
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from ldap3 import Server, Connection, ALL, SUBTREE
    LDAP3_AVAILABLE = True
except ImportError:
    LDAP3_AVAILABLE = False

try:
    import oidc as oidc_helper
    OIDC_AVAILABLE = True
except ImportError:
    OIDC_AVAILABLE = False

import discover
import hardcover
from bookshelf import BookshelfClient
from readarr import ReadarrClient
from lazylibrarian import LazyLibrarianClient

app = Flask(__name__)

# Honor X-Forwarded-* headers from a reverse proxy (haproxy, nginx, traefik, etc.)
# so url_for(..., _external=True) generates correct https URLs. Required for the
# OIDC redirect_uri to match what's registered at the IdP when the app sits
# behind a proxy. No-op when no proxy is in front (headers absent).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)


def _load_or_create_secret_key():
    """Load secret key from env, or persist one to data/secret_key."""
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    key_file = os.path.join(os.path.dirname(__file__), "data", "secret_key")
    if os.path.exists(key_file):
        with open(key_file) as f:
            return f.read().strip()
    os.makedirs(os.path.dirname(key_file), exist_ok=True)
    key = os.urandom(32).hex()
    with open(key_file, "w") as f:
        f.write(key)
    return key


app.secret_key = _load_or_create_secret_key()

# Configure logging to stdout so it shows in docker logs
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
app.logger.setLevel(logging.DEBUG)

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "data", "config.json")
REQUESTS_FILE = os.path.join(os.path.dirname(__file__), "data", "requests.json")
USERS_FILE = os.path.join(os.path.dirname(__file__), "data", "users.json")

# In-memory state
config = {"ebook": {}, "audiobook": {}, "ldap": {}, "oidc": {}}
requests_history = []
users = []
lock = threading.Lock()

# ─── Flask-Login Setup ───

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


class User:
    """Flask-Login user wrapper."""

    def __init__(self, data):
        self._data = data

    @property
    def is_authenticated(self):
        return True

    @property
    def is_active(self):
        return True

    @property
    def is_anonymous(self):
        return False

    @property
    def username(self):
        return self._data["username"]

    @property
    def role(self):
        return self._data.get("role", "user")

    def get_id(self):
        return self.username


@login_manager.user_loader
def load_user(username):
    for u in users:
        if u["username"] == username:
            return User(u)
    return None


@login_manager.unauthorized_handler
def handle_unauthorized():
    if request.path.startswith("/api/"):
        return jsonify({"error": "Authentication required"}), 401
    return redirect(url_for("login"))


def admin_required(f):
    """Decorator: require admin role."""
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if current_user.role != "admin":
            return jsonify({"error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated


# ─── Data Persistence ───

def ensure_data_dir():
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(data_dir, exist_ok=True)


def save_config():
    ensure_data_dir()
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                config = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass


def save_requests():
    ensure_data_dir()
    with open(REQUESTS_FILE, "w") as f:
        json.dump(requests_history, f, indent=2, default=str)


def load_requests():
    global requests_history
    if os.path.exists(REQUESTS_FILE):
        try:
            with open(REQUESTS_FILE) as f:
                requests_history = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass


def save_users():
    ensure_data_dir()
    # Strip password_hash before logging
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)


def load_users():
    global users
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE) as f:
                users = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass


def init_default_admin():
    """Create a default admin account if no users exist."""
    if not users:
        users.append({
            "username": "admin",
            "password_hash": generate_password_hash("admin"),
            "role": "admin",
            "created_at": datetime.utcnow().isoformat(),
        })
        save_users()
        app.logger.warning(
            "Default admin account created (username: admin, password: admin). "
            "Please change the password immediately!"
        )


load_config()
load_requests()
load_users()
init_default_admin()

if OIDC_AVAILABLE:
    oidc_helper.init_oidc(app, config)


@app.before_request
def reload_state():
    """Reload shared state from disk so multiple Gunicorn workers stay in sync."""
    load_config()
    load_requests()
    load_users()
    # Re-init the OIDC client if config changed in another worker. init_oidc
    # is idempotent — registers/unregisters the client based on enabled flag.
    if OIDC_AVAILABLE and not app.extensions.get("oidc_client") and config.get("oidc", {}).get("enabled"):
        oidc_helper.init_oidc(app, config)


# ─── LDAP Auth ───

def _get_ldap_defaults():
    return {
        "enabled": False,
        "server_url": "",
        "bind_dn": "",
        "bind_password": "",
        "base_dn": "",
        "user_search_filter": "(sAMAccountName={username})",
        "default_role": "user",
    }


def try_ldap_auth(username, password):
    """Attempt LDAP bind authentication.

    Returns (success: bool, user_dn: str, error: str).
    """
    if not LDAP3_AVAILABLE:
        return False, "", "ldap3 library is not installed"

    ldap = config.get("ldap", {})
    if not ldap.get("enabled"):
        return False, "", "LDAP is not enabled"

    server_url = ldap.get("server_url", "")
    bind_dn = ldap.get("bind_dn", "")
    bind_password = ldap.get("bind_password", "")
    base_dn = ldap.get("base_dn", "")
    search_filter = ldap.get("user_search_filter", "(sAMAccountName={username})")

    if not server_url or not base_dn:
        return False, "", "LDAP server_url or base_dn not configured"

    search_filter = search_filter.replace("{username}", username)

    try:
        server = Server(server_url, get_info=ALL)
        conn = Connection(server, bind_dn, bind_password, auto_bind=True)
        conn.search(base_dn, search_filter, search_scope=SUBTREE)
        if not conn.entries:
            conn.unbind()
            return False, "", "User not found in LDAP directory"
        user_dn = conn.entries[0].entry_dn
        conn.unbind()

        # Attempt to bind as the user to verify their password
        user_conn = Connection(server, user_dn, password, auto_bind=True)
        user_conn.unbind()
        return True, user_dn, ""
    except Exception as e:
        return False, "", str(e)


def get_client(server_type: str) -> ReadarrClient | BookshelfClient | LazyLibrarianClient | None:
    """Get a client for the given server type based on server_software setting."""
    server = config.get(server_type, {})
    if server.get("url") and server.get("api_key"):
        if server.get("server_software") == "bookshelf":
            return BookshelfClient(server["url"], server["api_key"])
        if server.get("server_software") == "lazylibrarian":
            return LazyLibrarianClient(server["url"], server["api_key"])
        return ReadarrClient(server["url"], server["api_key"])
    return None


# ─── Pages ───

@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/login")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    return render_template("login.html")


# ─── Auth API ───

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.json
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400

    for u in users:
        if u["username"] == username and check_password_hash(u["password_hash"], password):
            login_user(User(u))
            return jsonify({"success": True, "username": u["username"], "role": u.get("role", "user")})

    # Fall through to LDAP if configured
    ldap = config.get("ldap", {})
    if ldap.get("enabled"):
        app.logger.info("LDAP enabled, attempting auth for '%s'", username)
        success, _user_dn, error = try_ldap_auth(username, password)
        app.logger.info("LDAP result: success=%s, dn=%s, error=%s", success, _user_dn, error)
        if success:
            existing = next((u for u in users if u["username"] == username), None)
            if not existing:
                existing = {
                    "username": username,
                    "password_hash": "ldap",
                    "role": ldap.get("default_role", "user"),
                    "created_at": datetime.utcnow().isoformat(),
                }
                users.append(existing)
                save_users()
            app.logger.info("About to call login_user for '%s'", username)
            ok = login_user(User(existing))
            app.logger.info("login_user returned %s for '%s'", ok, username)
            return jsonify({"success": True, "username": existing["username"], "role": existing.get("role", "user")})
        app.logger.info("LDAP auth failed for '%s': %s", username, error)

    return jsonify({"error": "Invalid username or password"}), 401


@app.route("/api/auth/logout", methods=["POST"])
@login_required
def api_logout():
    logout_user()
    return jsonify({"success": True})


@app.route("/api/auth/me", methods=["GET"])
@login_required
def api_me():
    return jsonify({
        "username": current_user.username,
        "role": current_user.role,
    })


# ─── User Management API ───

@app.route("/api/users", methods=["GET"])
@admin_required
def get_users():
    safe_users = []
    for u in users:
        safe_users.append({
            "username": u["username"],
            "role": u.get("role", "user"),
            "created_at": u.get("created_at", ""),
        })
    return jsonify(safe_users)


@app.route("/api/users", methods=["POST"])
@admin_required
def create_user():
    data = request.json
    username = data.get("username", "").strip()
    password = data.get("password", "")
    role = data.get("role", "user")

    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400

    if role not in ("admin", "user"):
        return jsonify({"error": "Role must be 'admin' or 'user'"}), 400

    for u in users:
        if u["username"] == username:
            return jsonify({"error": "Username already exists"}), 400

    new_user = {
        "username": username,
        "password_hash": generate_password_hash(password),
        "role": role,
        "created_at": datetime.utcnow().isoformat(),
    }
    users.append(new_user)
    save_users()
    return jsonify({"success": True, "username": username, "role": role}), 201


@app.route("/api/users/<username>", methods=["PUT"])
@admin_required
def update_user(username):
    data = request.json

    target = None
    for u in users:
        if u["username"] == username:
            target = u
            break

    if not target:
        return jsonify({"error": "User not found"}), 404

    if "password" in data and data["password"]:
        target["password_hash"] = generate_password_hash(data["password"])

    if "role" in data:
        if data["role"] not in ("admin", "user"):
            return jsonify({"error": "Role must be 'admin' or 'user'"}), 400
        target["role"] = data["role"]

    save_users()
    return jsonify({"success": True, "username": target["username"], "role": target.get("role", "user")})


@app.route("/api/users/<username>", methods=["DELETE"])
@admin_required
def delete_user(username):
    if username == current_user.username:
        return jsonify({"error": "Cannot delete your own account"}), 400

    global users
    original_len = len(users)
    users = [u for u in users if u["username"] != username]

    if len(users) == original_len:
        return jsonify({"error": "User not found"}), 404

    save_users()
    return jsonify({"success": True})


# ─── LDAP Config API ───

@app.route("/api/ldap", methods=["GET"])
@admin_required
def get_ldap():
    ldap = config.get("ldap", _get_ldap_defaults())
    return jsonify({
        "enabled": ldap.get("enabled", False),
        "server_url": ldap.get("server_url", ""),
        "bind_dn": ldap.get("bind_dn", ""),
        "bind_password": ldap.get("bind_password", ""),
        "base_dn": ldap.get("base_dn", ""),
        "user_search_filter": ldap.get("user_search_filter", "(sAMAccountName={username})"),
        "default_role": ldap.get("default_role", "user"),
    })


@app.route("/api/ldap", methods=["POST"])
@admin_required
def update_ldap():
    data = request.json
    if data.get("default_role") not in ("admin", "user"):
        return jsonify({"error": "Role must be 'admin' or 'user'"}), 400
    config["ldap"] = {
        "enabled": bool(data.get("enabled")),
        "server_url": data.get("server_url", "").strip(),
        "bind_dn": data.get("bind_dn", "").strip(),
        "bind_password": data.get("bind_password", ""),
        "base_dn": data.get("base_dn", "").strip(),
        "user_search_filter": data.get("user_search_filter", "").strip() or "(sAMAccountName={username})",
        "default_role": data.get("default_role", "user"),
    }
    save_config()
    return jsonify({"success": True})


@app.route("/api/ldap/test", methods=["POST"])
@admin_required
def test_ldap():
    if not LDAP3_AVAILABLE:
        return jsonify({"error": "ldap3 library is not installed"}), 400
    data = request.json
    server_url = data.get("server_url", "").strip()
    bind_dn = data.get("bind_dn", "").strip()
    bind_password = data.get("bind_password", "")
    base_dn = data.get("base_dn", "").strip()
    search_filter = data.get("user_search_filter", "").strip() or "(sAMAccountName={username})"

    if not server_url or not base_dn:
        return jsonify({"error": "server_url and base_dn are required"}), 400

    try:
        server = Server(server_url, get_info=ALL)
        conn = Connection(server, bind_dn, bind_password, auto_bind=True)
        test_filter = search_filter.replace("{username}", "test")
        conn.search(base_dn, test_filter, search_scope=SUBTREE, size_limit=1)
        conn.unbind()
        return jsonify({"success": True, "message": "Connected to LDAP server successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ─── OIDC Config API ───

@app.route("/api/oidc", methods=["GET"])
@admin_required
def get_oidc():
    if not OIDC_AVAILABLE:
        return jsonify({
            "available": False,
            "enabled": False, "display_name": "OIDC", "issuer_url": "",
            "client_id": "", "client_secret": "", "scope": "openid profile email",
            "username_claim": "preferred_username", "default_role": "user",
            "auto_create_users": False, "auto_redirect": False,
        })
    defaults = oidc_helper.get_oidc_defaults()
    oidc = config.get("oidc", defaults)
    return jsonify({
        "available": True,
        "enabled": oidc.get("enabled", False),
        "display_name": oidc.get("display_name", defaults["display_name"]),
        "issuer_url": oidc.get("issuer_url", ""),
        "client_id": oidc.get("client_id", ""),
        "client_secret": oidc.get("client_secret", ""),
        "scope": oidc.get("scope", defaults["scope"]),
        "username_claim": oidc.get("username_claim", defaults["username_claim"]),
        "default_role": oidc.get("default_role", "user"),
        "auto_create_users": oidc.get("auto_create_users", False),
        "auto_redirect": oidc.get("auto_redirect", False),
    })


@app.route("/api/oidc", methods=["POST"])
@admin_required
def update_oidc():
    if not OIDC_AVAILABLE:
        return jsonify({"error": "authlib library is not installed"}), 400
    data = request.json
    if data.get("default_role") not in ("admin", "user"):
        return jsonify({"error": "Role must be 'admin' or 'user'"}), 400
    config["oidc"] = {
        "enabled": bool(data.get("enabled")),
        "display_name": data.get("display_name", "").strip() or "OIDC",
        "issuer_url": data.get("issuer_url", "").strip(),
        "client_id": data.get("client_id", "").strip(),
        "client_secret": data.get("client_secret", ""),
        "scope": data.get("scope", "").strip() or "openid profile email",
        "username_claim": data.get("username_claim", "").strip() or "preferred_username",
        "default_role": data.get("default_role", "user"),
        "auto_create_users": bool(data.get("auto_create_users")),
        "auto_redirect": bool(data.get("auto_redirect")),
    }
    save_config()
    # Re-register the OAuth client so the new config takes effect immediately.
    oidc_helper.init_oidc(app, config)
    return jsonify({"success": True})


@app.route("/api/oidc/test", methods=["POST"])
@admin_required
def test_oidc():
    if not OIDC_AVAILABLE:
        return jsonify({"error": "authlib library is not installed"}), 400
    data = request.json
    issuer_url = data.get("issuer_url", "").strip()
    if not issuer_url:
        return jsonify({"error": "issuer_url is required"}), 400
    try:
        doc = oidc_helper.fetch_discovery(issuer_url)
        ok, msg = oidc_helper.validate_discovery(doc)
        if not ok:
            return jsonify({"error": msg}), 400
        return jsonify({
            "success": True,
            "message": f"Discovery OK. Issuer: {doc.get('issuer', '')}",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ─── OIDC Auth Flow ───

@app.route("/api/auth/oidc/login")
def oidc_login():
    """Initiates the OIDC redirect to the IdP."""
    if not OIDC_AVAILABLE or not config.get("oidc", {}).get("enabled"):
        return redirect(url_for("login"))
    client = oidc_helper.get_client(app)
    if client is None:
        # Configured but client failed to init (bad issuer, etc.) — fall back.
        return redirect(url_for("login"))
    redirect_uri = url_for("oidc_callback", _external=True)
    return client.authorize_redirect(redirect_uri)


@app.route("/api/auth/oidc/callback")
def oidc_callback():
    """Handles the redirect back from the IdP, exchanges code for tokens,
    finds or provisions the user, logs them in."""
    oidc_cfg = config.get("oidc", {})
    if not OIDC_AVAILABLE or not oidc_cfg.get("enabled"):
        return redirect(url_for("login"))
    client = oidc_helper.get_client(app)
    if client is None:
        return redirect(url_for("login") + "?error=oidc_not_initialized")
    try:
        token = client.authorize_access_token()
    except Exception as e:
        app.logger.warning("OIDC token exchange failed: %s", e)
        return redirect(url_for("login") + "?error=oidc_token_exchange_failed")

    userinfo = token.get("userinfo")
    if not userinfo:
        try:
            userinfo = client.userinfo(token=token)
        except Exception as e:
            app.logger.warning("OIDC userinfo fetch failed: %s", e)
            return redirect(url_for("login") + "?error=oidc_userinfo_failed")

    username = oidc_helper.extract_username(userinfo, oidc_cfg.get("username_claim", "preferred_username"))
    if not username:
        app.logger.warning("OIDC token contains no usable username claim: %s", userinfo)
        return redirect(url_for("login") + "?error=oidc_no_username")

    existing = next((u for u in users if u["username"] == username), None)
    if not existing:
        if not oidc_cfg.get("auto_create_users"):
            app.logger.info("OIDC login rejected for '%s' — user does not exist and auto_create_users is off", username)
            return redirect(url_for("login") + "?error=account_not_found")
        existing = {
            "username": username,
            "password_hash": "oidc",
            "role": oidc_cfg.get("default_role", "user"),
            "auth_source": "oidc",
            "created_at": datetime.utcnow().isoformat(),
        }
        users.append(existing)
        save_users()
        app.logger.info("Auto-provisioned OIDC user '%s'", username)

    login_user(User(existing))
    return redirect(url_for("index"))


# ─── Auth provider discovery (for login page UI) ───

@app.route("/api/auth/providers", methods=["GET"])
def auth_providers():
    """Tells the login page which alt providers (beyond local username/password)
    are enabled, so it can render the appropriate buttons. Public — no auth
    required, since the login page is itself public."""
    oidc_cfg = config.get("oidc") or {}
    return jsonify({
        "oidc": {
            "enabled": bool(OIDC_AVAILABLE and oidc_cfg.get("enabled")),
            "display_name": oidc_cfg.get("display_name") or "OIDC",
            "auto_redirect": bool(oidc_cfg.get("auto_redirect")),
        },
    })


# ─── Config API ───

@app.route("/api/config", methods=["GET"])
@login_required
def get_config():
    return jsonify({
        "ebook": {
            "url": config["ebook"].get("url", ""),
            "api_key": config["ebook"].get("api_key", "") if current_user.role == "admin" else "",
            "server_software": config["ebook"].get("server_software", "readarr"),
            "configured": bool(config["ebook"].get("url") and config["ebook"].get("api_key")),
        },
        "audiobook": {
            "url": config["audiobook"].get("url", ""),
            "api_key": config["audiobook"].get("api_key", "") if current_user.role == "admin" else "",
            "server_software": config["audiobook"].get("server_software", "readarr"),
            "configured": bool(config["audiobook"].get("url") and config["audiobook"].get("api_key")),
        },
    })


@app.route("/api/config", methods=["POST"])
@admin_required
def update_config():
    data = request.json
    server_type = data.get("server_type")
    if server_type not in ("ebook", "audiobook"):
        return jsonify({"error": "server_type must be 'ebook' or 'audiobook'"}), 400

    config[server_type] = {
        "url": data.get("url", "").strip(),
        "api_key": data.get("api_key", "").strip(),
        "server_software": data.get("server_software", "readarr"),
    }
    save_config()
    return jsonify({"success": True})


@app.route("/api/config/test", methods=["POST"])
@admin_required
def test_config():
    data = request.json
    url = data.get("url", "").strip()
    api_key = data.get("api_key", "").strip()
    if not url or not api_key:
        return jsonify({"error": "url and api_key are required"}), 400
    try:
        server_software = data.get("server_software", "readarr")
        if server_software == "bookshelf":
            client = BookshelfClient(url, api_key)
        elif server_software == "lazylibrarian":
            client = LazyLibrarianClient(url, api_key)
        else:
            client = ReadarrClient(url, api_key)
        status = client.test_connection()
        return jsonify({"success": True, "status": status})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ─── Search & Discovery API (Open Library) ───

def _normalize_ol_doc(doc):
    """Normalize a single Open Library search.json doc to our book schema."""
    isbns = doc.get("isbn", [])
    isbn_13 = next((i for i in isbns if len(i) == 13), "")
    isbn_10 = next((i for i in isbns if len(i) == 10), "")
    if not isbn_13 and not isbn_10 and isbns:
        isbn_13 = isbns[0]

    cover_i = doc.get("cover_i")
    cover = f"https://covers.openlibrary.org/b/id/{cover_i}-M.jpg" if cover_i else ""

    ol_key = doc.get("key", "")
    ol_id = ol_key.split("/")[-1] if ol_key else ""

    year = doc.get("first_publish_year")
    published_date = str(year) if year else ""

    return {
        "id": ol_id,
        "title": doc.get("title", "Unknown"),
        "authors": doc.get("author_name", []),
        "publishedDate": published_date,
        "description": "",
        "pageCount": doc.get("number_of_pages_median", 0),
        "categories": doc.get("subject", [])[:5] if doc.get("subject") else [],
        "isbn_13": isbn_13,
        "isbn_10": isbn_10,
        "cover": cover,
        "language": (doc.get("language", ["en"])[0]
                     if doc.get("language") else "en"),
    }


def _normalize_ol_subject_work(work):
    """Normalize a single work from Open Library /subjects/{subject}.json."""
    authors = [a.get("name", "") for a in work.get("authors", []) if a.get("name")]

    cover_id = work.get("cover_id")
    cover = f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg" if cover_id else ""

    ol_key = work.get("key", "")
    ol_id = ol_key.split("/")[-1] if ol_key else ""

    year = work.get("first_publish_year")
    published_date = str(year) if year else ""

    return {
        "id": ol_id,
        "title": work.get("title", "Unknown"),
        "authors": authors,
        "publishedDate": published_date,
        "description": "",
        "pageCount": 0,
        "categories": [],
        "isbn_13": "",
        "isbn_10": "",
        "cover": cover,
        "language": "en",
    }


# Category keys mapped to Open Library API details
_DISCOVER_CATEGORIES = {
    "new_releases":   ("search.json",  {"sort": "new", "limit": 20}),
    "trending":       ("search.json",  {"sort": "rating", "limit": 20}),
    "best_sellers":   ("search.json",  {"q": "subject:bestsellers", "sort": "rating", "limit": 20}),
    "classics":       ("search.json",  {"q": "subject:classics", "sort": "rating", "limit": 20}),
    "fiction":        ("subjects/fiction.json",          {"limit": 20}),
    "science_fiction":("subjects/science_fiction.json",  {"limit": 20}),
    "mystery":        ("subjects/mystery.json",          {"limit": 20}),
    "fantasy":        ("subjects/fantasy.json",          {"limit": 20}),
    "romance":        ("subjects/romance.json",          {"limit": 20}),
    "nonfiction":     ("subjects/non-fiction.json",      {"limit": 20}),
    "history":        ("subjects/history.json",          {"limit": 20}),
}


@app.route("/api/discover/rows")
@login_required
def discover_rows():
    """Discover rows to show, in display order. Empty until the first background build finishes."""
    return jsonify(discover.row_list())


@app.route("/api/discover")
@login_required
def discover_books():
    category = request.args.get("category", "").strip()
    # Hardcover / NYT rows are built in the background (see discover.py)
    cached_row = discover.row_books(category) if category else None
    if cached_row is not None:
        return jsonify(cached_row)
    if not category or category not in _DISCOVER_CATEGORIES:
        return jsonify({"error": "Invalid category"}), 400
    try:
        endpoint, params = _DISCOVER_CATEGORIES[category]
        resp = http_requests.get(
            f"https://openlibrary.org/{endpoint}",
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if endpoint == "search.json":
            results = [_normalize_ol_doc(doc) for doc in data.get("docs", [])]
        else:
            # /subjects/ endpoint returns a "works" array
            results = [_normalize_ol_subject_work(w) for w in data.get("works", [])]

        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/book/<book_id>")
@login_required
def book_details(book_id):
    """Details (description, rating, genres, series) for a Hardcover book id, for the details modal."""
    book = hardcover.get_book(book_id)
    if not book:
        return jsonify({"error": "Details unavailable"}), 404
    return jsonify(book)


@app.route("/api/search")
@login_required
def search_books():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])
    hc_results = hardcover.search_books(query)
    if hc_results:
        return jsonify(hc_results)
    try:
        resp = http_requests.get(
            "https://openlibrary.org/search.json",
            params={"q": query, "limit": 20},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        results = [_normalize_ol_doc(doc) for doc in data.get("docs", [])]
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500



_GENERIC_SUBTITLE = re.compile(r"\s*[:\-]\s*an?\s+(?:novel|memoir|thriller|mystery|romance|story|tale|biography)\s*$", re.I)


def norm_title(t):
    """Normalise a title so "Project Hail Mary: A Novel" matches "Project Hail Mary"."""
    t = (t or "").lower()
    t = re.sub(r"[\(\[].*?[\)\]]", " ", t)
    t = _GENERIC_SUBTITLE.sub("", t)
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    t = re.sub(r"(sorcerer|philosopher) ?s stone", "philosophers stone", t)  # US/UK title
    return re.sub(r"^(the|a|an) ", "", t)


_availability_cache = {"time": 0, "data": None}


def _library_snapshot():
    """What each configured server already has: Hardcover/Bookshelf ids, ISBNs, titles, authors."""
    result = {"ebook": {"isbns": [], "titles": [], "ids": [], "authors": []},
              "audiobook": {"isbns": [], "titles": [], "ids": [], "authors": []}}

    for server_type in ("ebook", "audiobook"):
        client = get_client(server_type)
        if not client:
            continue
        try:
            books = client.get_books()
            isbns, titles, ids, authors, have_ids = set(), set(), set(), set(), set()
            for book in books:
                # Unmonitored catalogue entries with no file aren't "in the library"
                if not book.get("monitored", True) and not (book.get("statistics") or {}).get("bookFileCount"):
                    continue
                title = ""
                # Bookshelf book ids are Hardcover book ids
                if book.get("foreignBookId"):
                    ids.add(str(book["foreignBookId"]))
                    if (book.get("statistics") or {}).get("bookFileCount"):
                        have_ids.add(str(book["foreignBookId"]))  # downloaded, vs. monitored and still wanted
                # Readarr/Bookshelf format: editions array with isbn fields
                if isinstance(book.get("editions"), list):
                    for edition in book["editions"]:
                        for key in ("isbn13", "isbn_13", "isbn10", "isbn_10"):
                            val = edition.get(key, "")
                            if val:
                                isbns.add(val)
                    for key in ("isbn13", "isbn_13", "isbn10", "isbn_10"):
                        val = book.get(key, "")
                        if val:
                            isbns.add(val)
                    title = book.get("title", "")
                # LazyLibrarian format: flat dicts with bookisbn, bookname
                else:
                    isbn = book.get("bookisbn", book.get("isbn", ""))
                    if isbn:
                        isbns.add(isbn)
                    title = book.get("bookname", book.get("title", ""))
                if title:
                    titles.add(title.lower())
                    titles.add(norm_title(title))
            if hasattr(client, "get_authors"):
                try:
                    for a in client.get_authors():
                        if a.get("authorName") and a.get("monitored", True):
                            authors.add(hardcover.norm_title(a["authorName"]))
                except Exception as exc:
                    app.logger.debug("Could not list authors from %s: %s", server_type, exc)
            result[server_type] = {
                "isbns": list(isbns),
                "titles": list(titles),
                "ids": list(ids),
                "have_ids": list(have_ids),
                "authors": list(authors),
            }
        except Exception as e:
            app.logger.warning("Failed to get books from %s: %s", server_type, e)
    return result


@app.route("/api/availability")
@login_required
def check_availability():
    """Which books are already on the configured ebook/audiobook servers, or requested.

    The library part is cached for 60 s because listing every book is slow.
    """
    now = time.time()
    with lock:
        cached = _availability_cache["data"] if now - _availability_cache["time"] < 60 else None
    if cached is None:
        cached = _library_snapshot()
        with lock:
            _availability_cache.update(time=now, data=cached)
    result = {k: dict(v) for k, v in cached.items()}
    result["configured"] = {t: bool(get_client(t)) for t in ("ebook", "audiobook")}

    # Books with active requests (pending/processing/downloading) count as requested
    active_statuses = {"pending", "processing", "downloading"}
    requested = {"ebook": {"isbns": set(), "titles": set(), "ids": set()},
                 "audiobook": {"isbns": set(), "titles": set(), "ids": set()}}
    with lock:
        load_requests()
        for req in requests_history:
            if req.get("status") not in active_statuses:
                continue
            bucket = requested.get(req.get("server_type", ""))
            if bucket is None:
                continue
            if req.get("isbn"):
                bucket["isbns"].add(req["isbn"])
            if req.get("hardcover_id"):
                bucket["ids"].add(str(req["hardcover_id"]))
            title = req.get("title", "")
            if title:
                bucket["titles"].add(title.lower())
                bucket["titles"].add(norm_title(title))

    for server_type, bucket in requested.items():
        result[f"{server_type}_requests"] = {k: list(v) for k, v in bucket.items()}
    return jsonify(result)


@app.route("/api/profiles/<server_type>")
@login_required
def get_profiles(server_type):
    client = get_client(server_type)
    if not client:
        return jsonify({"error": f"{server_type} server not configured"}), 400
    try:
        profiles = client.get_quality_profiles()
        return jsonify(profiles)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rootfolders/<server_type>")
@login_required
def get_root_folders(server_type):
    client = get_client(server_type)
    if not client:
        return jsonify({"error": f"{server_type} server not configured"}), 400
    try:
        folders = client.get_root_folders()
        return jsonify(folders)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Download / Request API ───

STALE_PENDING_SECONDS = 600
RETENTION_COMPLETED_DAYS = float(os.environ.get("REQUEST_RETENTION_COMPLETED_DAYS", "1"))
RETENTION_ERROR_DAYS = float(os.environ.get("REQUEST_RETENTION_ERROR_DAYS", "2"))
SYNC_INTERVAL_SECONDS = 60
LEADER_LOCK_FILE = os.path.join(os.path.dirname(__file__), "data", "leader.lock")


def _now():
    return datetime.utcnow()


def _age_seconds(iso):
    try:
        return (_now() - datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return 0


def _update_request(entry_id, updates):
    """Apply updates to one request, reloading from disk first so other workers' changes aren't lost."""
    with lock:
        load_requests()
        for r in requests_history:
            if r["id"] == entry_id:
                r.update(updates)
                r["updated_at"] = _now().isoformat()
                break
        save_requests()


def _process_request(entry_id, server_type, book_data, quality_profile_id, root_folder):
    """Look the book up and add it to the server (runs in a background thread)."""
    updates = {}
    try:
        client = get_client(server_type)
        if not client:
            raise RuntimeError(f"{server_type} server not configured")
        title = book_data.get("title", "Unknown")
        authors = book_data.get("authors", [])
        author_name = authors[0] if authors else "Unknown"
        isbn = book_data.get("isbn_13") or book_data.get("isbn_10", "")
        readarr_books = []

        # Resolve the exact book on Hardcover (its ids are Bookshelf's ids), see hardcover.py
        if hasattr(client, "lookup_by_work_id"):
            hc_id = book_data.get("hardcover_id")
            if not hc_id:
                hc = hardcover.find_book(title, author_name)
                if hc:
                    hc_id = hc["id"]
                    app.logger.info("Hardcover match for '%s': id=%s '%s' by %s (%s readers)",
                                    title, hc["id"], hc["title"], hc["author"], hc["users"])
            if hc_id:
                updates["hardcover_id"] = str(hc_id)
                try:
                    readarr_books = client.lookup_by_work_id(hc_id)
                except Exception as exc:
                    app.logger.warning("Bookshelf lookup of Hardcover id %s failed: %s", hc_id, exc)
        if isbn and not readarr_books:
            readarr_books = client.lookup_by_isbn(isbn)
        if not readarr_books:
            readarr_books = client.search_books(f"{title} {author_name}")

        if readarr_books:
            # Prefer the result whose title matches the requested one, not just the first hit
            want = norm_title(title)
            bad = re.compile(r"dramati[sz]ed|adaptation|graphic|collection|box(ed)? set|(?<![a-z])books? [0-9]|(?<![a-z])series(?![a-z])|companion|omnibus|complete| / ", re.I)

            def rank(b):
                t = b.get("title", "")
                n = norm_title(t)
                votes = (b.get("ratings") or {}).get("votes") or 0
                return (bool(bad.search(t)), n != want, not n.startswith(want), -votes)

            if not updates.get("hardcover_id"):
                readarr_books = sorted(readarr_books, key=rank)
            readarr_book = readarr_books[0]
            if not readarr_book.get("author", {}).get("authorName"):
                readarr_book["author"] = {"authorName": author_name, "foreignAuthorId": ""}
            app.logger.info("Readarr match for '%s': title='%s', author=%s",
                            title, readarr_book.get("title"), json.dumps(readarr_book.get("author", {})))
        else:
            # Fallback: build data from the search result
            readarr_book = {
                "title": title,
                "author": {"authorName": author_name, "foreignAuthorId": ""},
                "foreignBookId": isbn or book_data.get("id", ""),
            }
            app.logger.info("No Readarr match, using search-result fallback for '%s' by '%s'", title, author_name)

        updates["status"] = "processing"
        result = client.add_book(readarr_book, quality_profile_id, root_folder)
        updates["readarr_book_id"] = result.get("id")
        updates["error"] = None
    except Exception as e:
        updates["status"] = "error"
        updates["error"] = str(e)
        app.logger.warning("Background request %s failed: %s", entry_id, e)
    _update_request(entry_id, updates)


def _normalise_targets(data):
    """A request names one or more servers: {"targets": [{server_type, quality_profile_id, root_folder}, ...]},
    or the older single-server fields. Returns (targets, error message)."""
    targets = data.get("targets")
    if not targets:
        targets = [{"server_type": data.get("server_type"), "quality_profile_id": data.get("quality_profile_id"),
                    "root_folder": data.get("root_folder")}]
    seen = set()
    clean = []
    for t in targets:
        if not all([t.get("server_type"), t.get("quality_profile_id"), t.get("root_folder")]):
            return None, "Missing required fields"
        if not get_client(t["server_type"]):
            return None, f"{t['server_type']} server not configured"
        if t["server_type"] not in seen:
            seen.add(t["server_type"])
            clean.append(t)
    return clean, None


@app.route("/api/request", methods=["POST"])
@login_required
def create_request():
    """Request a book on one or both servers (one request entry per server)."""
    data = request.json or {}
    book_data = data.get("book")
    targets, error = _normalise_targets(data)
    if error or not book_data:
        return jsonify({"error": error or "Missing required fields"}), 400
    entries = [_create_one(t["server_type"], book_data, t["quality_profile_id"], t["root_folder"], index)
               for index, t in enumerate(targets)]
    return jsonify({"requests": entries, **entries[0]})


def _create_one(server_type, book_data, quality_profile_id, root_folder, index=0):
    title = book_data.get("title", "Unknown")
    authors = book_data.get("authors", [])
    author_name = authors[0] if authors else "Unknown"
    isbn = book_data.get("isbn_13") or book_data.get("isbn_10", "")

    request_entry = {
        "id": int(time.time() * 1000) + index,
        "title": title,
        "author": author_name,
        "cover_url": book_data.get("cover", ""),
        "server_type": server_type,
        "quality_profile_id": quality_profile_id,
        "root_folder": root_folder,
        "isbn": isbn,
        "hardcover_id": str(book_data.get("hardcover_id") or ""),
        "requested_by": current_user.username,
        # Kept so a failed request can be retried without the original search result
        "book": {k: book_data.get(k) for k in ("id", "title", "authors", "cover", "isbn_13", "isbn_10", "hardcover_id")},
        "status": "pending",
        "progress": 0,
        "error": None,
        "created_at": _now().isoformat(),
        "updated_at": _now().isoformat(),
    }

    # Queue the request and do the slow lookup/add in the background so the page returns immediately.
    with lock:
        load_requests()
        requests_history.insert(0, request_entry)
        save_requests()
        _availability_cache["time"] = 0

    threading.Thread(
        target=_process_request,
        args=(request_entry["id"], server_type, book_data, quality_profile_id, root_folder),
        daemon=True,
    ).start()
    return request_entry


def _process_author_request(entry_id, server_type, author_name, quality_profile_id, root_folder):
    """Add the author to the server; the curated book list arrives with the author's first refresh."""
    updates = {}
    try:
        client = get_client(server_type)
        if not client or not hasattr(client, "add_author"):
            raise RuntimeError(f"{server_type} server does not support adding authors")
        author = client.add_author(author_name, quality_profile_id, root_folder)
        updates.update(status="completed", progress=100, completed_at=_now().isoformat(),
                       error=None, author_id=author.get("id"))
    except Exception as e:
        updates.update(status="error", error=str(e))
        app.logger.warning("Author request %s failed: %s", entry_id, e)
    _update_request(entry_id, updates)


@app.route("/api/request-author", methods=["POST"])
@login_required
def create_author_request():
    """Add an author (and so their curated books) to one or both servers."""
    data = request.json or {}
    author_name = (data.get("author_name") or "").strip()
    targets, error = _normalise_targets(data)
    if error or not author_name:
        return jsonify({"error": error or "author_name required"}), 400
    entries = []
    for index, t in enumerate(targets):
        entry = {
            "id": int(time.time() * 1000) + index,
            "title": f"All books by {author_name}",
            "author": author_name,
            "kind": "author",
            "cover_url": "",
            "server_type": t["server_type"],
            "quality_profile_id": t["quality_profile_id"],
            "root_folder": t["root_folder"],
            "isbn": "",
            "hardcover_id": "",
            "requested_by": current_user.username,
            "status": "processing",
            "progress": 0,
            "error": None,
            "created_at": _now().isoformat(),
            "updated_at": _now().isoformat(),
        }
        with lock:
            load_requests()
            requests_history.insert(0, entry)
            save_requests()
            _availability_cache["time"] = 0
        threading.Thread(target=_process_author_request, daemon=True,
                         args=(entry["id"], t["server_type"], author_name, t["quality_profile_id"], t["root_folder"])).start()
        entries.append(entry)
    return jsonify({"requests": entries})


@app.route("/api/requests/<int:request_id>/retry", methods=["POST"])
@login_required
def retry_request(request_id):
    with lock:
        load_requests()
        req = next((r for r in requests_history if r["id"] == request_id), None)
        if not req:
            return jsonify({"error": "Request not found"}), 404
        if req.get("status") != "error":
            return jsonify({"error": "Only failed requests can be retried"}), 400
        req.update(status="pending", error=None, progress=0, updated_at=_now().isoformat(),
                   created_at=_now().isoformat())
        save_requests()
        args = (req["id"], req["server_type"], req.get("book") or {"title": req["title"], "authors": [req["author"]]},
                req.get("quality_profile_id"), req.get("root_folder"))
    if not args[3] or not args[4]:
        _update_request(request_id, {"status": "error", "error": "Missing profile/root folder; request it again"})
        return jsonify({"error": "Missing profile/root folder; request it again"}), 400
    threading.Thread(target=_process_request, args=args, daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/requests", methods=["GET"])
@login_required
def get_requests():
    with lock:
        load_requests()
        return jsonify(requests_history)


def sync_requests():
    """Update every open request from the server's queue/library, fail stuck ones, prune old ones.

    Network calls happen outside the lock so a slow server can't stall the web UI.
    """
    with lock:
        load_requests()
        open_reqs = [(r["id"], r["server_type"], r.get("readarr_book_id")) for r in requests_history
                     if r["status"] not in ("completed", "error") and r.get("readarr_book_id")]

    queues, books = {}, {}
    for _id, server_type, book_id in open_reqs:
        client = get_client(server_type)
        if not client:
            continue
        try:
            if server_type not in queues:
                queues[server_type] = client.get_queue()
            if not any(str(q.get("bookId")) == str(book_id) for q in queues[server_type]):
                books[(server_type, book_id)] = client.get_book_status(book_id)
        except Exception as exc:
            app.logger.debug("Request sync skipped for %s book %s: %s", server_type, book_id, exc)

    with lock:
        load_requests()
        changed = False
        for req in requests_history:
            if req["status"] in ("completed", "error"):
                continue
            # A request that never got a Bookshelf id is stuck (e.g. lost in a restart)
            if (req["status"] == "pending" and not req.get("readarr_book_id")
                    and _age_seconds(req.get("updated_at") or req.get("created_at")) > STALE_PENDING_SECONDS):
                req.update(status="error", error="Request was interrupted. Use Retry.", updated_at=_now().isoformat())
                changed = True
                continue
            book_id = req.get("readarr_book_id")
            if not book_id or req["server_type"] not in queues:
                continue
            before = (req["status"], req.get("progress"))
            matching = [q for q in queues[req["server_type"]] if str(q.get("bookId")) == str(book_id)]
            if matching:
                q = matching[0]
                size, size_left = q.get("size", 0), q.get("sizeleft", 0)
                req["status"] = "downloading"
                if size > 0:
                    req["progress"] = round((1 - size_left / size) * 100)
            elif (req["server_type"], book_id) in books:
                stats = (books[(req["server_type"], book_id)] or {}).get("statistics") or {}
                if stats.get("bookFileCount", 0) > 0:
                    req.update(status="completed", progress=100, completed_at=_now().isoformat())
                elif req["status"] == "downloading":
                    req.update(status="processing", progress=0)
            if (req["status"], req.get("progress")) != before:
                req["updated_at"] = _now().isoformat()
                changed = True

        # Prune finished requests after the retention period
        kept = []
        for req in requests_history:
            if req["status"] == "completed" and _age_seconds(req.get("completed_at") or req.get("updated_at") or req.get("created_at")) > RETENTION_COMPLETED_DAYS * 86400:
                changed = True
                continue
            if req["status"] == "error" and _age_seconds(req.get("updated_at") or req.get("created_at")) > RETENTION_ERROR_DAYS * 86400:
                changed = True
                continue
            kept.append(req)
        requests_history[:] = kept
        if changed:
            save_requests()
            _availability_cache["time"] = 0
        return list(requests_history)


@app.route("/api/requests/refresh", methods=["POST"])
@login_required
def refresh_requests():
    """Refresh request statuses now (also done automatically every minute)."""
    return jsonify(sync_requests())


@app.route("/api/requests/<int:request_id>", methods=["DELETE"])
@login_required
def delete_request(request_id):
    with lock:
        load_requests()
        requests_history[:] = [r for r in requests_history if r["id"] != request_id]
        save_requests()
        _availability_cache["time"] = 0
    return jsonify({"success": True})


@app.route("/api/requests/clear", methods=["POST"])
@login_required
def clear_requests():
    """Remove finished requests: {"status": "completed" | "error" | "finished"}."""
    which = (request.json or {}).get("status", "finished")
    drop = {"completed": {"completed"}, "error": {"error"}}.get(which, {"completed", "error"})
    with lock:
        load_requests()
        requests_history[:] = [r for r in requests_history if r["status"] not in drop]
        save_requests()
    return jsonify({"success": True})


def _sync_loop():
    while True:
        time.sleep(SYNC_INTERVAL_SECONDS)
        try:
            sync_requests()
        except Exception as exc:
            app.logger.warning("Request sync failed: %s", exc)


def _start_background_tasks():
    """Run the request sync and Discover refresh in exactly one gunicorn worker."""
    try:
        import fcntl
        handle = open(LEADER_LOCK_FILE, "w")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, OSError):
        return
    globals()["_leader_handle"] = handle  # hold the lock for the life of this worker
    discover.start_loop()
    threading.Thread(target=_sync_loop, daemon=True, name="request-sync").start()
    app.logger.info("Background tasks started in this worker")


_start_background_tasks()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
