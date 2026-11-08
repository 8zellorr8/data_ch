"""Durable, conservative token reservations shared by CLI runs and API workers."""

import argparse
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from backend.errors import DomainError


@dataclass(frozen=True)
class TokenReservation:
    id: str
    tokens: int


class TokenBudget:
    def __init__(
        self,
        path: Path,
        *,
        total: int = 2_000_000,
        jury_reserve: int = 0,
        initial_spent: int = 0,
        use_jury_reserve: bool = False,
    ) -> None:
        if total <= 0 or not 0 <= jury_reserve <= total or not 0 <= initial_spent <= total:
            raise ValueError("Invalid token budget policy")
        self.path = path
        self.total = total
        self.jury_reserve = jury_reserve
        self.initial_spent = initial_spent
        self.use_jury_reserve = use_jury_reserve

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=30)) as connection, connection:
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS token_policy ("
                "id INTEGER PRIMARY KEY CHECK(id=1), total INTEGER NOT NULL, "
                "jury_reserve INTEGER NOT NULL, initial_spent INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS token_requests ("
                "id TEXT PRIMARY KEY, created_at TEXT NOT NULL, model TEXT NOT NULL, "
                "held INTEGER NOT NULL, actual INTEGER, input_tokens INTEGER, "
                "output_tokens INTEGER, state TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO token_policy VALUES (1,?,?,?)",
                (self.total, self.jury_reserve, self.initial_spent),
            )
            stored = connection.execute(
                "SELECT total,jury_reserve,initial_spent FROM token_policy WHERE id=1"
            ).fetchone()
            if stored != (self.total, self.jury_reserve, self.initial_spent):
                raise ValueError("Token budget policy differs from the existing ledger")
            yield connection

    def reserve(self, model: str, tokens: int) -> TokenReservation:
        if tokens <= 0:
            raise ValueError("Token reservation must be positive")
        with self._connection() as connection:
            used = (
                self.initial_spent
                + connection.execute(
                    "SELECT COALESCE(SUM(COALESCE(actual,held)),0) FROM token_requests"
                ).fetchone()[0]
            )
            limit = self.total if self.use_jury_reserve else self.total - self.jury_reserve
            if used + tokens > limit:
                raise DomainError(
                    "AI_UNAVAILABLE",
                    "LLM token budget limit reached; no API request was sent",
                )
            reservation = TokenReservation(str(uuid4()), tokens)
            connection.execute(
                "INSERT INTO token_requests VALUES (?,?,?,?,NULL,NULL,NULL,'pending')",
                (reservation.id, datetime.now(UTC).isoformat(), model, tokens),
            )
            return reservation

    def settle(
        self,
        reservation: TokenReservation,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        total_tokens: int | None = None,
    ) -> None:
        for value in (input_tokens, output_tokens, total_tokens):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError("Invalid provider token count")
        known = input_tokens is not None and output_tokens is not None
        actual = (
            max(input_tokens + output_tokens, total_tokens or 0)
            if input_tokens is not None and output_tokens is not None
            else None
        )
        with self._connection() as connection:
            changed = connection.execute(
                "UPDATE token_requests SET actual=?,input_tokens=?,output_tokens=?,state=? "
                "WHERE id=? AND state='pending'",
                (
                    actual,
                    input_tokens,
                    output_tokens,
                    "settled" if known else "uncertain",
                    reservation.id,
                ),
            ).rowcount
            if changed != 1:
                if (
                    connection.execute(
                        "SELECT 1 FROM token_requests WHERE id=?", (reservation.id,)
                    ).fetchone()
                    is None
                ):
                    raise ValueError("Unknown token reservation")

    def snapshot(self) -> dict[str, int]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(COALESCE(actual,held)),0),"
                "COALESCE(SUM(CASE WHEN state!='settled' THEN 1 ELSE 0 END),0),"
                "COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0) FROM token_requests"
            ).fetchone()
        used = self.initial_spent + row[1]
        limit = self.total if self.use_jury_reserve else self.total - self.jury_reserve
        return {
            "total": self.total,
            "jury_reserve": self.jury_reserve,
            "initial_spent": self.initial_spent,
            "requests": row[0],
            "accounted_tokens": used,
            "uncertain_requests": row[2],
            "reported_input_tokens": row[3],
            "reported_output_tokens": row[4],
            "remaining_total": max(0, self.total - used),
            "remaining_current_phase": max(0, limit - used),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    with sqlite3.connect(args.path) as connection:
        row = connection.execute(
            "SELECT total,jury_reserve,initial_spent FROM token_policy WHERE id=1"
        ).fetchone()
    print(
        json.dumps(
            TokenBudget(
                args.path, total=row[0], jury_reserve=row[1], initial_spent=row[2]
            ).snapshot(),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
