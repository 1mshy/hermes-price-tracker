"""Schema upkeep: a column added to a model reaches databases created before it."""
from sqlalchemy import create_engine, inspect, text

from pricewatch.db import add_missing_columns


def test_missing_columns_are_added_in_place(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE trackers (id INTEGER PRIMARY KEY, "
                          "product_id INTEGER NOT NULL, label VARCHAR(300) NOT NULL)"))
    added = add_missing_columns(engine)
    assert "trackers.currency" in added
    assert "currency" in {c["name"] for c in inspect(engine).get_columns("trackers")}
    assert add_missing_columns(engine) == []                 # idempotent
