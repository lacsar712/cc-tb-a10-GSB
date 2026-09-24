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
            created_by text NOT NULL,
            temperature double precision NOT NULL DEFAULT 80
        )"""
    )
    # 兼容旧数据卷：先补列再回填，最后收紧为 NOT NULL
    cur.execute("ALTER TABLE cuppings ADD COLUMN IF NOT EXISTS temperature double precision")
    cur.execute("UPDATE cuppings SET temperature = 80 WHERE temperature IS NULL")
    cur.execute(
        """DO $$
           BEGIN
               IF EXISTS (SELECT 1 FROM information_schema.columns
                          WHERE table_name = 'cuppings'
                            AND column_name = 'temperature'
                            AND is_nullable = 'YES') THEN
                   ALTER TABLE cuppings ALTER COLUMN temperature SET NOT NULL;
               END IF;
           END $$"""
    )

    # 杯温门禁：单行表，id 恒为 1，初始温区 75–85℃（闭区间）
    cur.execute(
        """CREATE TABLE IF NOT EXISTS temperature_gate (
            id integer PRIMARY KEY,
            low double precision NOT NULL,
            high double precision NOT NULL,
            updated_by text NOT NULL,
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT temperature_gate_singleton CHECK (id = 1)
        )"""
    )
    cur.execute(
        """INSERT INTO temperature_gate (id, low, high, updated_by)
           VALUES (1, 75, 85, 'taster')
           ON CONFLICT (id) DO NOTHING"""
    )

    # 温度履历：只追加。改正温度插新行，旧行数字永不更新、不删除
    cur.execute(
        """CREATE TABLE IF NOT EXISTS temperature_history (
            id serial PRIMARY KEY,
            cupping_id integer NOT NULL,
            lot text NOT NULL,
            temperature double precision NOT NULL,
            temperature_raw text NOT NULL,
            action text NOT NULL,
            changed_by text NOT NULL,
            created_at timestamp NOT NULL DEFAULT now()
        )"""
    )

    cur.execute("SELECT COUNT(*) FROM cuppings")
    if cur.fetchone()[0] == 0:
        for lot, aroma, taste, liquor, temperature in (
            ("春茶-A", 8, 8, 7, 80),
            ("夏茶-C", 5, 4, 6, 80),
        ):
            verdict, note, score = weigh(aroma, taste, liquor)
            cur.execute(
                """INSERT INTO cuppings
                       (lot, aroma, taste, liquor, score, verdict, note, created_by, temperature)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING id""",
                (lot, aroma, taste, liquor, score, verdict, note, "taster", temperature),
            )
            cupping_id = cur.fetchone()[0]
            cur.execute(
                """INSERT INTO temperature_history
                       (cupping_id, lot, temperature, temperature_raw, action, changed_by)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (cupping_id, lot, temperature, str(int(temperature)), "提交", "taster"),
            )
    else:
        # 旧卷升级：给没有履历的老记录补一条提交履历
        cur.execute(
            """INSERT INTO temperature_history
                   (cupping_id, lot, temperature, temperature_raw, action, changed_by)
               SELECT c.id, c.lot, c.temperature, c.temperature::text, '提交', c.created_by
               FROM cuppings c
               WHERE NOT EXISTS (
                   SELECT 1 FROM temperature_history h WHERE h.cupping_id = c.id
               )"""
        )
    conn.commit()
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
