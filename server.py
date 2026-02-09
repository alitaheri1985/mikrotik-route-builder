import os
import base64
import time
import re
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
import paramiko
from sqlalchemy.exc import OperationalError

# ================== ENV CHECK ==================
WEB_USER = os.environ.get("WEB_USER")
WEB_PASS = os.environ.get("WEB_PASS")

if not WEB_USER or not WEB_PASS:
    raise RuntimeError("WEB_USER and WEB_PASS environment variables are required")

# Database environment
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASS = os.environ.get("DB_PASS", "postgres")
DB_NAME = os.environ.get("DB_NAME", "routebuilder")
DB_HOST = os.environ.get("DB_HOST", "db")
DB_PORT = os.environ.get("DB_PORT", "5432")

# ================== APP INIT ==================
app = Flask(__name__, static_folder='.')
app.config['SQLALCHEMY_DATABASE_URI'] = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ================== MODELS ==================
class CIDRFile(db.Model):
    __tablename__ = 'cidr_files'
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(256))
    uploaded_at = db.Column(db.DateTime, default=db.func.now())
    routes = db.relationship('Route', backref='file', lazy=True)

class Route(db.Model):
    __tablename__ = 'routes'
    id = db.Column(db.Integer, primary_key=True)
    cidr = db.Column(db.String(50))
    gateway = db.Column(db.String(50))
    table = db.Column(db.String(50))
    file_id = db.Column(db.Integer, db.ForeignKey('cidr_files.id'))
    valid = db.Column(db.Boolean, default=True)
    duplicate = db.Column(db.Boolean, default=False)

class PushLog(db.Model):
    __tablename__ = 'push_logs'
    id = db.Column(db.Integer, primary_key=True)
    router_ip = db.Column(db.String(50))
    username = db.Column(db.String(50))
    pushed_at = db.Column(db.DateTime, default=db.func.now())
    command = db.Column(db.Text)

# ================== BASIC AUTH ==================
def check_auth(auth_header):
    if not auth_header:
        return False
    try:
        encoded = auth_header.split(" ")[1]
        decoded = base64.b64decode(encoded).decode()
        user, pwd = decoded.split(":")
        return user == WEB_USER and pwd == WEB_PASS
    except Exception:
        return False

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get('Authorization')
        if not check_auth(auth):
            return ('Unauthorized', 401, {'WWW-Authenticate': 'Basic realm="Login Required"'})
        return f(*args, **kwargs)
    return decorated

# ================== CIDR VALIDATION ==================
CIDR_REGEX = re.compile(
    r'^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)'
    r'(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}/([0-9]|[12][0-9]|3[0-2])$'
)

# ================== BUILD ROUTES ==================
@app.route("/build", methods=["POST"])
@requires_auth
def build_routes():
    try:
        file = request.files.get('file')
        gateway = request.form.get('gateway')
        table = request.form.get('table')

        if not file or not gateway or not table:
            return jsonify({"error": "File, gateway and table are required"}), 400

        # Save file record
        cidr_file = CIDRFile(filename=file.filename)
        db.session.add(cidr_file)
        db.session.commit()

        lines = file.read().decode().splitlines()
        unique_cidrs = set()
        valid_cidrs = []
        duplicate_cidrs = []
        invalid_cidrs = []

        commands = []

        for line in lines:
            cidr = line.strip()
            if not cidr:
                continue
            if cidr in unique_cidrs:
                duplicate_cidrs.append(cidr)
                route = Route(cidr=cidr, gateway=gateway, table=table, file_id=cidr_file.id,
                              valid=False, duplicate=True)
                db.session.add(route)
                continue
            unique_cidrs.add(cidr)
            if not CIDR_REGEX.match(cidr):
                invalid_cidrs.append(cidr)
                route = Route(cidr=cidr, gateway=gateway, table=table, file_id=cidr_file.id,
                              valid=False, duplicate=False)
                db.session.add(route)
                continue
            # valid route
            valid_cidrs.append(cidr)
            route = Route(cidr=cidr, gateway=gateway, table=table, file_id=cidr_file.id)
            db.session.add(route)
            commands.append(f"/ip route add dst-address={cidr} gateway={gateway} routing-table={table}")

        db.session.commit()

        return jsonify({
            "total": len(lines),
            "valid": valid_cidrs,
            "duplicates": duplicate_cidrs,
            "invalid": invalid_cidrs,
            "commands": "\n".join(commands)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ================== RUN COMMAND ON ROUTER ==================
@app.route("/run", methods=["POST"])
@requires_auth
def run_on_router():
    try:
        data = request.json
        router_ip = data.get("router_ip")
        username = data.get("username")
        password = data.get("password")
        command = data.get("command")

        if not router_ip or not username or not password or not command:
            return jsonify({"error": "All fields required"}), 400

        output = ""
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(router_ip, username=username, password=password, timeout=10)
        stdin, stdout, stderr = ssh.exec_command(command)
        output = stdout.read().decode() + stderr.read().decode()
        ssh.close()

        log = PushLog(router_ip=router_ip, username=username, command=command)
        db.session.add(log)
        db.session.commit()

        return jsonify({"output": output})
    except Exception as e:
        return jsonify({"output": str(e)}), 500

# ================== UI ==================
@app.route("/")
@requires_auth
def index():
    return send_from_directory(".", "mikrotik_route_builder.html")

with app.app_context():
    db.create_all()
# ================== START SERVER ==================
if __name__ == "__main__":
    # Wait for DB ready
    attempts = 0
    while attempts < 10:
        try:
            with app.app_context():
                db.create_all()
            print("Database ready ✅")
            break
        except OperationalError:
            attempts += 1
            print("Waiting for database... attempt", attempts)
            time.sleep(3)
    else:
        raise RuntimeError("Database not available after 10 attempts")

    app.run(host="0.0.0.0", port=5000)
