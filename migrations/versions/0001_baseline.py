"""baseline

Empty on purpose: the starting point every later migration builds on. Tables
arrive with the code that uses them.

Revision ID: 0001
Revises:
Create Date: 2026-10-29 18:31:35.763575
"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
