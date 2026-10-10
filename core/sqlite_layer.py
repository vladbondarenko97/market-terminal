import os
import sys

# Ensure config can be imported if this is run standalone
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from config import DB_PATH
from core import lake


def get_connection():
    conn = lake.connect(DB_PATH)
    lake.migrate(conn)
    return conn


def append_row(table_name, row_dict):
    """
    Appends a single dictionary as a row to the specified SQLite table using the table's real schema.
    New keys become new columns (ALTER TABLE ADD COLUMN); existing columns and rows are never replaced.
    Errors propagate so callers can report them instead of masking a failed save.
    """
    conn = get_connection()
    try:
        lake.append_legacy_row(conn, table_name, dict(row_dict))
    finally:
        conn.close()


def append_df(table_name, df):
    """
    Appends a pandas DataFrame to the specified SQLite table, row by row through the schema-aware writer.
    """
    conn = get_connection()
    try:
        for rec in df.to_dict(orient="records"):
            lake.append_legacy_row(conn, table_name, rec)
    finally:
        conn.close()


def replace_df(table_name, df):
    """
    Table replacement used to drop protected history. It is refused for every existing table;
    use append_df or the v2 importer (python main_pipeline.py import-history) instead.
    """
    conn = get_connection()
    try:
        exists = lake.table_columns(conn, table_name)
    finally:
        conn.close()
    if exists or table_name in lake.PROTECTED_TABLES:
        raise RuntimeError(
            f"replace_df('{table_name}') refused: it would drop existing history. "
            "Use append_df or `python main_pipeline.py import-history`."
        )
    append_df(table_name, df)


def init_historical_data():
    """
    Historical synchronization now runs through the idempotent, non-destructive v2 importer:
    old CSV ledgers, CME workbooks and daily XML files are captured with hashes and provenance,
    and existing tables are left exactly as they are.
    """
    from core.importer import import_history
    return import_history()


if __name__ == "__main__":
    init_historical_data()
