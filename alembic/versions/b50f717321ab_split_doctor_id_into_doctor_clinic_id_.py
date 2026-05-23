"""split doctor_id into doctor_clinic_id + doctor_practitioner_id

Replaces the legacy `doctor_id` column (which held a synthetic key like
`cigna:<name>:<phone>` from the HTML-scraping era) with two Cigna-native
identifiers:

  doctor_clinic_id        — Cigna's addresses[].id ("this doctor AT this
                            clinic" — what the appointments table actually
                            references for booking)
  doctor_practitioner_id  — Cigna's top-level practitioner id
                            (e.g. "-1402186762") — kept as a denormalized
                            snapshot so we can later aggregate appointments
                            by doctor across clinics

Drop + add (rather than alter_column rename) is intentional here: the old
column's values were synthetic strings that don't map to either new field's
semantics. No production rows exist yet — see ROADMAP.md.

Revision ID: b50f717321ab
Revises: 4d2826e51ace
Create Date: 2026-05-23 10:16:24.122318
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b50f717321ab"
down_revision: Union[str, Sequence[str], None] = "4d2826e51ace"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("appointments", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("doctor_clinic_id", sa.String(length=128), nullable=False)
        )
        batch_op.add_column(
            sa.Column("doctor_practitioner_id", sa.String(length=128), nullable=False)
        )
        batch_op.drop_column("doctor_id")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("appointments", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("doctor_id", sa.VARCHAR(length=128), nullable=False)
        )
        batch_op.drop_column("doctor_practitioner_id")
        batch_op.drop_column("doctor_clinic_id")
