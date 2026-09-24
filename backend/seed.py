import os
import time

import psycopg2

from rules import weigh


def connect():
    last = None
    for _ in range(30):
        try:
            return psycopg2.connect(os.environ["DATABASE_URL"])
        except psycopg2.OperationalError as exc:
            last = exc
            time.sleep(1)
    raise last


def main():
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS cuppings (
            id serial PRIMARY KEY,
            lot text NOT NULL,
            aroma double precision NOT NULL,
            taste double precision NOT NULL,
            liquor double precision NOT NULL,
            score double precision NOT NULL,
            verdict text NOT NULL,
            note text NOT NULL,
            created_by text NOT NULL
        )"""
    )
    # 杯面温度：交评必填；老数据补列时允许为空
    cur.execute("ALTER TABLE cuppings ADD COLUMN IF NOT EXISTS cup_temp double precision")

    # 温区：全局单行（id 固定为 1），闭区间；改动只约束之后的提交
    cur.execute(
        """CREATE TABLE IF NOT EXISTS temp_zone (
            id smallint PRIMARY KEY DEFAULT 1,
            low double precision NOT NULL,
            high double precision NOT NULL,
            updated_by text NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT temp_zone_singleton CHECK (id = 1),
            CONSTRAINT temp_zone_order CHECK (low <= high)
        )"""
    )
    cur.execute(
        """INSERT INTO temp_zone (id, low, high, updated_by)
           VALUES (1, 75, 85, 'taster')
           ON CONFLICT (id) DO NOTHING"""
    )

    # 杯温履历：只追加。任何一次提交/改正的温度原文都留痕，永不覆盖
    cur.execute(
        """CREATE TABLE IF NOT EXISTS cup_temp_events (
            id serial PRIMARY KEY,
            cupping_id integer REFERENCES cuppings(id) ON DELETE CASCADE,
            cup_temp double precision NOT NULL,
            action text NOT NULL CHECK (action IN ('submit', 'correct')),
            recorded_by text NOT NULL,
            recorded_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cup_temp_events_cupping ON cup_temp_events (cupping_id)")

    cur.execute("SELECT COUNT(*) FROM cuppings")
    if cur.fetchone()[0] == 0:
        for lot, aroma, taste, liquor in (("春茶-A", 8, 8, 7), ("夏茶-C", 5, 4, 6)):
            verdict, note, score = weigh(aroma, taste, liquor)
            cur.execute(
                """INSERT INTO cuppings (lot, aroma, taste, liquor, score, verdict, note, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (lot, aroma, taste, liquor, score, verdict, note, "taster"),
            )
    conn.commit()
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
