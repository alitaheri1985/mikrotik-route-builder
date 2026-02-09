from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
import paramiko
from datetime import datetime

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://postgres:postgres@db:5432/mikrotik'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)


# ------------------ DATABASE MODEL ------------------
class Route(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cidr = db.Column(db.String(50), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


with app.app_context():
    db.create_all()


# ------------------ UTILS ------------------
def parse_file(file):
    cidrs = set()
    for line in file.read().decode().splitlines():
        line = line.strip()
        if line:
            cidrs.add(line)
    return cidrs


def ssh_execute(ip, username, password, commands):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(ip, username=username, password=password)
    stdin, stdout, stderr = ssh.exec_command(commands)
    output = stdout.read().decode() + stderr.read().decode()
    ssh.close()
    return output


# ------------------ ROUTES ------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/build", methods=["POST"])
def build_routes():
    file = request.files["file"]
    gateway = request.form["gateway"]
    table = request.form["table"]

    cidrs = parse_file(file)
    commands = ""
    for cidr in cidrs:
        commands += f"/ip route add dst-address={cidr} gateway={gateway} routing-table={table}\n"

    return jsonify({
        "count": len(cidrs),
        "commands": commands
    })


@app.route("/diff", methods=["POST"])
def diff_routes():
    file = request.files["file"]
    new_cidrs = parse_file(file)
    existing_cidrs = set(r.cidr for r in Route.query.all())

    added = new_cidrs - existing_cidrs
    removed = existing_cidrs - new_cidrs
    unchanged = new_cidrs & existing_cidrs

    return jsonify({
        "added": sorted(list(added)),
        "removed": sorted(list(removed)),
        "added_count": len(added),
        "removed_count": len(removed),
        "unchanged": len(unchanged)
    })


@app.route("/apply-diff", methods=["POST"])
def apply_diff():
    data = request.json
    router_ip = data["router_ip"]
    username = data["username"]
    password = data["password"]
    commands_list = data.get("commands", [])

    commands = "\n".join(commands_list)

    # Execute commands on router
    output = ssh_execute(router_ip, username, password, commands)

    # Update DB with new routes
    for line in commands_list:
        if "add dst-address=" in line:
            cidr = line.split("dst-address=")[1].split()[0]
            if not Route.query.filter_by(cidr=cidr).first():
                db.session.add(Route(cidr=cidr))
        elif "remove" in line and "dst-address=" in line:
            cidr = line.split("dst-address=")[1].split("]")[0]
            Route.query.filter_by(cidr=cidr).delete()

    db.session.commit()

    return jsonify({
        "output": output
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
