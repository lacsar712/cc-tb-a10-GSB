import os
from functools import wraps

import psycopg2
from flask import Flask, redirect, render_template, request, session, url_for
from psycopg2.extras import RealDictCursor

from rules import weigh

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "tea-cupping-dev-secret")

ACCOUNTS = {
    "taster": {"password": "tea123456", "role": "writer"},
    "observer": {"password": "look123456", "role": "reader"},
}


def db():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def login_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrap


def is_writer():
    return session.get("role") == "writer"


def fmt_temp(value):
    """80.0 -> '80'，82.5 -> '82.5'。"""
    value = float(value)
    return str(int(value)) if value.is_integer() else str(value)


app.jinja_env.filters["tempfmt"] = fmt_temp


def current_gate(cur):
    cur.execute("SELECT low, high, updated_by, updated_at FROM temperature_gate WHERE id = 1")
    return cur.fetchone()


def gate_message(value, gate):
    return (
        f"杯面温度 {fmt_temp(value)}℃ 超出当前温区 "
        f"{fmt_temp(gate['low'])}℃–{fmt_temp(gate['high'])}℃（闭区间），已拒交"
    )


def parse_temperature():
    """返回 ((原文, 数值), None) 或 (None, 错误提示)。"""
    raw = (request.form.get("temperature") or "").strip()
    if not raw:
        return None, "杯面温度为必填项，交评已被拒绝"
    try:
        value = float(raw)
    except ValueError:
        return None, f"杯面温度“{raw}”不是有效数字"
    return (raw, value), None


@app.get("/health")
def health():
    return {"status": "ok", "service": "tea-blend-cupping"}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        name = request.form.get("username", "").strip()
        account = ACCOUNTS.get(name)
        if not account or account["password"] != request.form.get("password", ""):
            error = "用户名或密码错误"
        else:
            session["user"] = name
            session["role"] = account["role"]
            return redirect(url_for("home"))
    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def home():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM cuppings ORDER BY id DESC")
        rows = cur.fetchall()
    return render_template("home.html", rows=rows, can_write=is_writer())


@app.post("/cuppings")
@login_required
def create():
    if not is_writer():
        return ("仅审评员可提交拼配审评", 403)
    parsed, error = parse_temperature()
    if error:
        return (error, 400)
    temp_raw, temperature = parsed
    aroma = float(request.form["aroma"])
    taste = float(request.form["taste"])
    liquor = float(request.form["liquor"])
    lot = request.form["lot"].strip()
    verdict, note, score = weigh(aroma, taste, liquor)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        gate = current_gate(cur)
        if not (gate["low"] <= temperature <= gate["high"]):
            return (gate_message(temperature, gate), 400)
        cur.execute(
            """INSERT INTO cuppings
                   (lot, aroma, taste, liquor, score, verdict, note, created_by, temperature)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (lot, aroma, taste, liquor, score, verdict, note, session["user"], temperature),
        )
        row = cur.fetchone()
        # 履历只追加：温度原文随交评落档，永不更新或删除
        cur.execute(
            """INSERT INTO temperature_history
                   (cupping_id, lot, temperature, temperature_raw, action, changed_by)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (row["id"], lot, temperature, temp_raw, "提交", session["user"]),
        )
        conn.commit()
    if request.headers.get("HX-Request"):
        return render_template("_row.html", row=row)
    return redirect(url_for("home"))


@app.post("/cuppings/<int:cupping_id>/temperature")
@login_required
def correct_temperature(cupping_id):
    if not is_writer():
        return ("仅审评员可改正杯面温度", 403)
    parsed, error = parse_temperature()
    if error:
        return (error, 400)
    temp_raw, temperature = parsed
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        gate = current_gate(cur)
        if not (gate["low"] <= temperature <= gate["high"]):
            return (gate_message(temperature, gate), 400)
        cur.execute(
            "UPDATE cuppings SET temperature = %s WHERE id = %s RETURNING *",
            (temperature, cupping_id),
        )
        row = cur.fetchone()
        if row is None:
            return ("审评记录不存在", 404)
        # 改正只追加新行；早先那行的数字保持原样，不得覆盖
        cur.execute(
            """INSERT INTO temperature_history
                   (cupping_id, lot, temperature, temperature_raw, action, changed_by)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (row["id"], row["lot"], temperature, temp_raw, "改正", session["user"]),
        )
        conn.commit()
    return redirect(url_for("home"))


@app.get("/temperature")
@login_required
def temperature_page():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        gate = current_gate(cur)
        cur.execute("SELECT * FROM temperature_history ORDER BY id DESC")
        history = cur.fetchall()
    return render_template(
        "gate.html", gate=gate, history=history, can_write=is_writer()
    )


@app.post("/temperature/range")
@login_required
def update_gate():
    if not is_writer():
        return ("仅审评员可修改杯温温区", 403)
    try:
        low = float((request.form.get("low") or "").strip())
        high = float((request.form.get("high") or "").strip())
    except ValueError:
        return ("温区上下限必须是数字", 400)
    if low > high:
        return (
            f"温区下限 {fmt_temp(low)}℃ 不得高于上限 {fmt_temp(high)}℃，温区未修改",
            400,
        )
    with db() as conn, conn.cursor() as cur:
        # 改完只约束之后的交评：旧记录与旧履历不溯及既往
        cur.execute(
            """INSERT INTO temperature_gate (id, low, high, updated_by, updated_at)
                   VALUES (1, %s, %s, %s, now())
               ON CONFLICT (id) DO UPDATE
                   SET low = EXCLUDED.low,
                       high = EXCLUDED.high,
                       updated_by = EXCLUDED.updated_by,
                       updated_at = now()""",
            (low, high, session["user"]),
        )
        conn.commit()
    return redirect(url_for("temperature_page"))
