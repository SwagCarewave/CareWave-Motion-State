from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings

MIGRATIONS = ROOT / "supabase" / "migrations"

TRACKING = """
create schema if not exists supabase_migrations;
create table if not exists supabase_migrations.schema_migrations (
    version text primary key,
    statements text[],
    name text
);
"""


def connect():
    s = load_settings()
    missing = [k for k, v in {"SUPABASE_URL": s.supabase_ref, "SUPABASE_DB_HOST": s.supabase_db_host,
                              "SUPABASE_DB_PASSWORD": s.supabase_db_password}.items() if not v]
    if missing:
        sys.exit(f".env에 값이 없습니다: {', '.join(missing)}")
    return psycopg.connect(host=s.supabase_db_host, port=5432, dbname="postgres", user=f"postgres.{s.supabase_ref}",
                           password=s.supabase_db_password, sslmode="require", connect_timeout=15)


def pending(conn) -> list[Path]:
    conn.execute(TRACKING)
    done = {r[0] for r in conn.execute("select version from supabase_migrations.schema_migrations")}
    return [p for p in sorted(MIGRATIONS.glob("*.sql")) if p.name.split("_", 1)[0] not in done]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="supabase/migrations 의 SQL을 순서대로 적용합니다.")
    ap.add_argument("--apply", action="store_true", help="실제로 적용합니다. 없으면 적용할 목록만 보여줍니다.")
    args = ap.parse_args()
    with connect() as conn:
        todo = pending(conn)
        conn.commit()
        if not todo:
            print("적용할 마이그레이션이 없습니다.")
            return
        for path in todo:
            print(("적용: " if args.apply else "대기: ") + path.name)
            if not args.apply:
                continue
            version, _, name = path.stem.partition("_")
            sql = path.read_text(encoding="utf-8")
            with conn.transaction():
                conn.execute(sql)
                conn.execute("insert into supabase_migrations.schema_migrations (version, statements, name) "
                             "values (%s, %s, %s)", (version, [sql], name))
        print("완료" if args.apply else "--apply 를 붙이면 적용합니다.")


if __name__ == "__main__":
    main()
