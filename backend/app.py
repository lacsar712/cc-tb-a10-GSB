import os
from functools import wraps

import psycopg2
from flask import Flask, flash, redirect, render_template, request, session, url_for
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


def writer_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "writer":
            return ("只读账号不可执行该操作", 403)
        return fn(*args, **kwargs)

    return wrap


def parse_float(form, field, label):
    raw = (form.get(field) or "").strip()
    if raw == "":
        raise ValueError(f"{label}必填")
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{label}必须是数字") from None


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
    return render_template("home.html", rows=rows, can_write=session.get("role") == "writer")


@app.post("/cuppings")
@writer_required
def create():
    try:
        aroma = parse_float(request.form, "aroma", "香气")
        taste = parse_float(request.form, "taste", "滋味")
        liquor = parse_float(request.form, "liquor", "汤色")
        cup_temp = parse_float(request.form, "cup_temp", "杯面温度")
    except ValueError as exc:
        return (str(exc), 400)
    lot = request.form["lot"].strip()

    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT low, high FROM temp_zone WHERE id = 1")
        zone = cur.fetchone()
        if not (zone["low"] <= cup_temp <= zone["high"]):
            return (
                f"杯面温度 {cup_temp:g}℃ 超出当前温区 [{zone['low']:g}, {zone['high']:g}]℃（闭区间），交评被拒绝",
                400,
            )
        verdict, note, score = weigh(aroma, taste, liquor)
        cur.execute(
            """INSERT INTO cuppings (lot, aroma, taste, liquor, cup_temp, score, verdict, note, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (lot, aroma, taste, liquor, cup_temp, score, verdict, note, session["user"]),
        )
        row = cur.fetchone()
        # 温度原文落履历；只追加，后续改正不会动这一行
        cur.execute(
            """INSERT INTO cup_temp_events (cupping_id, cup_temp, action, recorded_by)
               VALUES (%s,%s,'submit',%s)""",
            (row["id"], cup_temp, session["user"]),
        )
        conn.commit()
    if request.headers.get("HX-Request"):
        return render_template("_row.html", row=row, can_write=True)
    return redirect(url_for("home"))


@app.post("/cuppings/<int:cupping_id>/cup-temp")
@writer_required
def correct_cup_temp(cupping_id):
    try:
        cup_temp = parse_float(request.form, "cup_temp", "杯面温度")
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("home"))

    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT low, high FROM temp_zone WHERE id = 1")
        zone = cur.fetchone()
        if not (zone["low"] <= cup_temp <= zone["high"]):
            flash(
                f"杯面温度 {cup_temp:g}℃ 超出当前温区 [{zone['low']:g}, {zone['high']:g}]℃（闭区间），改正被拒绝",
                "error",
            )
            return redirect(url_for("home"))
        cur.execute("SELECT id FROM cuppings WHERE id = %s", (cupping_id,))
        if cur.fetchone() is None:
            return ("审评记录不存在", 404)
        cur.execute("UPDATE cuppings SET cup_temp = %s WHERE id = %s", (cup_temp, cupping_id))
        # 旧温度事件原样保留，另起一行 correct 事件
        cur.execute(
            """INSERT INTO cup_temp_events (cupping_id, cup_temp, action, recorded_by)
               VALUES (%s,%s,'correct',%s)""",
            (cupping_id, cup_temp, session["user"]),
        )
        conn.commit()
    flash(f"第 {cupping_id} 行杯温已改正为 {cup_temp:g}℃，原温度仍保留在履历中", "ok")
    return redirect(url_for("home"))


@app.get("/temperature")
@login_required
def temperature_gate():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT low, high, updated_by, updated_at FROM temp_zone WHERE id = 1")
        zone = cur.fetchone()
        cur.execute(
            """SELECT e.cup_temp, e.action, e.recorded_by, e.recorded_at, e.cupping_id, c.lot
               FROM cup_temp_events e
               LEFT JOIN cuppings c ON c.id = e.cupping_id
               ORDER BY e.id DESC"""
        )
        events = cur.fetchall()
    return render_template(
        "gate.html",
        zone=zone,
        events=events,
        can_write=session.get("role") == "writer",
    )


@app.post("/temperature/zone")
@writer_required
def update_temp_zone():
    try:
        low = parse_float(request.form, "low", "温区下限")
        high = parse_float(request.form, "high", "温区上限")
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("temperature_gate"))
    if low > high:
        flash(f"温区下限 {low:g} 不能高于上限 {high:g}", "error")
        return redirect(url_for("temperature_gate"))

    with db() as conn, conn.cursor() as cur:
        # 只改单行当前值；既有审评与其履历不受追溯影响，仅约束之后的提交
        cur.execute(
            """UPDATE temp_zone
               SET low = %s, high = %s, updated_by = %s, updated_at = now()
               WHERE id = 1""",
            (low, high, session["user"]),
        )
        conn.commit()
    flash(f"温区已更新为 [{low:g}, {high:g}]℃，仅约束此后的提交", "ok")
    return redirect(url_for("temperature_gate"))
