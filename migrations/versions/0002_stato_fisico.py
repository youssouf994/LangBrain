"""Tabella `device_state`: l'ultimo stato noto dei dispositivi, per riconciliarlo dopo un riavvio.

Revisione: 0002
Precedente: 0001
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Il modulo `app.core.stato_fisico` crea la tabella anche da solo se serve prima della migrazione: qui non la si rifà.
    if inspect(op.get_bind()).has_table("device_state"):
        return
    op.create_table(
        "device_state",
        sa.Column("device_id", sa.String(255), primary_key=True),
        sa.Column("value", sa.Text()),
        sa.Column("unit", sa.Text()),
        sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'observed'")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp()),
    )


def downgrade() -> None:
    op.drop_table("device_state")
