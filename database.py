import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).with_name("travel_wallet.db")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                from_country TEXT NOT NULL,
                to_country TEXT NOT NULL,
                home_currency TEXT NOT NULL,
                travel_currency TEXT NOT NULL,
                rate REAL NOT NULL,
                balance_home REAL NOT NULL,
                balance_travel REAL NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id INTEGER NOT NULL,
                amount_travel REAL NOT NULL,
                amount_home REAL NOT NULL,
                description TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(trip_id) REFERENCES trips(id)
            )
            """
        )


def create_trip(
    user_id: int,
    name: str,
    from_country: str,
    to_country: str,
    home_currency: str,
    travel_currency: str,
    rate: float,
    initial_home_amount: float,
) -> int:
    balance_travel = initial_home_amount / rate if rate else 0
    with get_conn() as conn:
        conn.execute("UPDATE trips SET is_active = 0 WHERE user_id = ?", (user_id,))
        cursor = conn.execute(
            """
            INSERT INTO trips (
                user_id, name, from_country, to_country, home_currency, travel_currency,
                rate, balance_home, balance_travel, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                user_id,
                name,
                from_country,
                to_country,
                home_currency,
                travel_currency,
                rate,
                initial_home_amount,
                balance_travel,
            ),
        )
        return int(cursor.lastrowid)


def get_trips(user_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trips WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return rows


def get_trip_by_id(trip_id: int, user_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trips WHERE id = ? AND user_id = ?",
            (trip_id, user_id),
        ).fetchone()
    return row


def get_active_trip(user_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trips WHERE user_id = ? AND is_active = 1 LIMIT 1",
            (user_id,),
        ).fetchone()
    return row


def set_active_trip(user_id: int, trip_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE trips SET is_active = 0 WHERE user_id = ?", (user_id,))
        conn.execute(
            "UPDATE trips SET is_active = 1 WHERE user_id = ? AND id = ?",
            (user_id, trip_id),
        )


def add_expense(trip_id: int, amount_travel: float, amount_home: float, description: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO expenses (trip_id, amount_travel, amount_home, description)
            VALUES (?, ?, ?, ?)
            """,
            (trip_id, amount_travel, amount_home, description),
        )
        conn.execute(
            """
            UPDATE trips
            SET balance_travel = balance_travel - ?, balance_home = balance_home - ?
            WHERE id = ?
            """,
            (amount_travel, amount_home, trip_id),
        )


def get_expenses(trip_id: int, limit: int = 10) -> list[sqlite3.Row]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM expenses
            WHERE trip_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (trip_id, limit),
        ).fetchall()
    return rows


def update_trip_rate(trip_id: int, rate: float) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE trips SET rate = ? WHERE id = ?", (rate, trip_id))


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)
