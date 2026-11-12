"""initial KaziFlow multi-tenant domain

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-31
"""

from alembic import op

from app.db import models  # noqa: F401 - register model metadata
from app.db.base import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

TENANT_TABLES = [
    "items",
    "customers",
    "orders",
    "order_items",
    "booking_slots",
    "payment_ledgers",
    "audit_events",
]


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    if bind.dialect.name != "postgresql":
        return
    # Queries against tenant tables see nothing until the application executes
    # set_config('app.current_merchant', merchant_id, true) for the transaction.
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON {table} "
            "USING (merchant_id = current_setting('app.current_merchant', true)) "
            "WITH CHECK (merchant_id = current_setting('app.current_merchant', true))"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in TENANT_TABLES:
            op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    Base.metadata.drop_all(bind=bind)
