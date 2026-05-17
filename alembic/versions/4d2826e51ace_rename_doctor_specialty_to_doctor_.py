"""rename doctor_specialty to doctor_specialty_name

Renames the appointments.doctor_specialty column to doctor_specialty_name
and widens it from VARCHAR(64) to VARCHAR(128). The semantic change is
that the column now stores the **Spanish display name** from Cigna's
specialty catalog (e.g. "DERMATOLOGÍA") rather than the legacy
Enum value (e.g. "DERMATOLOGIA"). See domain/value_objects/specialty.py.

Autogenerate emitted drop+add (it can't infer renames); rewritten as an
actual rename so any rows that exist survive the migration.

Revision ID: 4d2826e51ace
Revises: 9409aca91a75
Create Date: 2026-05-16 21:35:24.142934
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4d2826e51ace"
down_revision: Union[str, Sequence[str], None] = "9409aca91a75"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("appointments", schema=None) as batch_op:
        batch_op.alter_column(
            "doctor_specialty",
            new_column_name="doctor_specialty_name",
            existing_type=sa.VARCHAR(length=64),
            type_=sa.String(length=128),
            existing_nullable=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("appointments", schema=None) as batch_op:
        batch_op.alter_column(
            "doctor_specialty_name",
            new_column_name="doctor_specialty",
            existing_type=sa.String(length=128),
            type_=sa.VARCHAR(length=64),
            existing_nullable=False,
        )
