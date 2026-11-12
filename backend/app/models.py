"""Database schema (SQLAlchemy 2.0 declarative models). See docs/MVP_PLAN.md section 4.

Conventions
- Money is an integer number of cents. Never floats.
- Rules that can be checked from a single row are CHECK constraints, and relationships
  between tables are FOREIGN KEYs. The database then rejects bad rows even if a bug in
  our Python code tries to write one. Rules that span several rows or tables (e.g.
  "subtotal equals the sum of the lines") live in one service function and are tested.
"""

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    DDL,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# POStatus is defined next to the state machine (the single source of truth for the lifecycle)
# and imported here so the CHECK constraint below and `from app.models import POStatus` keep
# working. Importing domain -> models is allowed; the reverse is forbidden by a test.
from app.domain.po_state import POStatus


class Base(DeclarativeBase):
    """Parent class for all models; `Base.metadata` is the registry of all tables."""


class PaymentStatus(StrEnum):
    CREATED = "CREATED"
    PENDING = "PENDING"
    CAPTURED = "CAPTURED"
    FAILED = "FAILED"


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


def _sql_list(enum_cls: type[StrEnum]) -> str:
    """Render enum values as `'A', 'B'` for use inside a CHECK ... IN (...) clause."""
    return ", ".join(f"'{member.value}'" for member in enum_cls)


# Statuses where the merchant has approved the PO (so approval fields must be filled in).
_POST_APPROVAL = "'APPROVED', 'PAYMENT_PENDING', 'PAID', 'PAYMENT_FAILED'"
# A payment in one of these states "occupies" the PO: no second payment may be started.
_LIVE_PAYMENT = "'CREATED', 'PENDING', 'CAPTURED'"


class MerchantSettings(Base):
    """Single-row table of purchasing policy. The CHECK on `id` makes a second row impossible."""

    __tablename__ = "merchant_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_settings_single_row"),
        CheckConstraint("default_budget_cents >= 0", name="ck_settings_budget_nonneg"),
        CheckConstraint("target_cover_days > 0", name="ck_settings_cover_positive"),
        CheckConstraint("lookback_days > 0", name="ck_settings_lookback_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    default_budget_cents: Mapped[int]
    target_cover_days: Mapped[int]
    lookback_days: Mapped[int]


class Product(Base):
    """A sellable item. `on_hand` lives here (not in a separate inventory table) because it is
    exactly one number per product; a 1:1 table would add a join and no information."""

    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("on_hand >= 0", name="ck_products_on_hand_nonneg"),
        CheckConstraint("max_stock > 0", name="ck_products_max_stock_positive"),
        CheckConstraint("on_hand <= max_stock", name="ck_products_on_hand_le_max"),
        CheckConstraint("size_ml IS NULL OR size_ml > 0", name="ck_products_size_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(40))
    on_hand: Mapped[int]
    max_stock: Mapped[int]
    # Optional catalogue attributes. Promotions select products by brand and size, and count
    # distinct variants (flavors); product search matches on all three.
    brand: Mapped[str | None] = mapped_column(String(60))
    variant: Mapped[str | None] = mapped_column(String(60))
    size_ml: Mapped[int | None]


class Sale(Base):
    """Units sold of one product on one day. Days with no sales simply have no row."""

    __tablename__ = "sales"
    __table_args__ = (
        UniqueConstraint("product_id", "sold_on", name="uq_sales_product_day"),
        CheckConstraint("quantity > 0", name="ck_sales_quantity_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    sold_on: Mapped[date] = mapped_column(Date)
    quantity: Mapped[int]


class Supplier(Base):
    __tablename__ = "suppliers"
    __table_args__ = (
        CheckConstraint("min_order_cents >= 0", name="ck_suppliers_min_order_nonneg"),
        CheckConstraint("lead_time_days >= 0", name="ck_suppliers_lead_time_nonneg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    min_order_cents: Mapped[int]
    lead_time_days: Mapped[int]


class SupplierProduct(Base):
    """One supplier's offer for one product: price per unit and case size (`pack_size`).

    The composite primary key (supplier_id, product_id) means a supplier can list a product
    only once, and lets purchase_order_lines point at "this exact offer" with a foreign key.
    """

    __tablename__ = "supplier_products"
    __table_args__ = (
        CheckConstraint("unit_price_cents > 0", name="ck_offers_price_positive"),
        CheckConstraint("pack_size > 0", name="ck_offers_pack_positive"),
    )

    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    unit_price_cents: Mapped[int]
    pack_size: Mapped[int]

    supplier: Mapped[Supplier] = relationship()
    product: Mapped[Product] = relationship()


class PromotionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class Promotion(Base):
    """A confirmed supplier promotion. Only REBATE exists: the PO is paid at list price and
    the supplier pays `rebate_cents` back later, once per bundle on a PO that meets the terms.

    Terms: whole bundles of `required_cases` cases (of `units_per_case` bottles) of the
    eligible products, at most `max_bundles` (NULL: no limit), covering at least
    `min_distinct_variants` different variants (flavors). `buy_through` is how far ahead the
    merchant wants to stock up on the deal (e.g. through the holidays). Which
    products are eligible is an explicit list (`promotion_products`) the merchant confirmed,
    never a rule re-evaluated later.
    """

    __tablename__ = "promotions"
    __table_args__ = (
        CheckConstraint("kind = 'REBATE'", name="ck_promotions_kind"),
        CheckConstraint(f"status IN ({_sql_list(PromotionStatus)})", name="ck_promotions_status"),
        CheckConstraint("required_cases > 0", name="ck_promotions_cases_positive"),
        CheckConstraint("units_per_case > 0", name="ck_promotions_units_positive"),
        CheckConstraint("rebate_cents > 0", name="ck_promotions_rebate_positive"),
        CheckConstraint(
            "min_distinct_variants >= 1 AND min_distinct_variants <= required_cases",
            name="ck_promotions_variants_range",
        ),
        CheckConstraint(
            "valid_from IS NULL OR valid_until IS NULL OR valid_from <= valid_until",
            name="ck_promotions_dates_ordered",
        ),
        CheckConstraint("max_bundles IS NULL OR max_bundles >= 1", name="ck_promotions_max_bundles"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    title: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20), default="REBATE")
    status: Mapped[str] = mapped_column(String(10), default=PromotionStatus.ACTIVE.value)
    required_cases: Mapped[int]
    units_per_case: Mapped[int]
    rebate_cents: Mapped[int]
    min_distinct_variants: Mapped[int]
    max_bundles: Mapped[int | None]
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
    buy_through: Mapped[date | None] = mapped_column(Date)
    source_text: Mapped[str | None] = mapped_column(Text)  # what the merchant pasted, for the record
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())

    supplier: Mapped[Supplier] = relationship()
    products: Mapped[list["PromotionProduct"]] = relationship(
        back_populates="promotion", order_by="PromotionProduct.product_id"
    )


class PromotionProduct(Base):
    """One eligible product of a promotion. The FK to supplier_products guarantees the
    promotion's supplier actually sells it."""

    __tablename__ = "promotion_products"
    __table_args__ = (
        ForeignKeyConstraint(
            ["supplier_id", "product_id"],
            ["supplier_products.supplier_id", "supplier_products.product_id"],
        ),
    )

    promotion_id: Mapped[int] = mapped_column(ForeignKey("promotions.id"), primary_key=True)
    product_id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int]  # denormalized from the promotion so the composite FK can apply

    promotion: Mapped[Promotion] = relationship(back_populates="products")
    product: Mapped[Product] = relationship(
        primaryjoin="PromotionProduct.product_id == Product.id",
        foreign_keys="[PromotionProduct.product_id]",
    )


class PurchaseOrder(Base):
    """One order to one supplier (plan decision D2). Lifecycle rules live in M3; the schema
    only guarantees that a row can never be in a self-contradictory state."""

    __tablename__ = "purchase_orders"
    __table_args__ = (
        # Redundant as a uniqueness rule (id is already unique) but required: it gives the
        # lines table a composite key (id, supplier_id) to reference. See PurchaseOrderLine.
        UniqueConstraint("id", "supplier_id", name="uq_po_id_supplier"),
        CheckConstraint(f"status IN ({_sql_list(POStatus)})", name="ck_po_status_valid"),
        CheckConstraint("subtotal_cents >= 0", name="ck_po_subtotal_nonneg"),
        CheckConstraint("currency = 'USD'", name="ck_po_currency_usd"),
        # Approval fields must agree with status, and what the merchant approved must be
        # exactly the stored subtotal. Because of this, nobody can change a PO's total after
        # approval without the database refusing the write.
        CheckConstraint(
            f"""
            (status = 'DRAFT' AND approved_total_cents IS NULL AND approved_at IS NULL)
            OR (status = 'CANCELLED'
                AND (approved_total_cents IS NULL) = (approved_at IS NULL)
                AND (approved_total_cents IS NULL OR approved_total_cents = subtotal_cents))
            OR (status IN ({_POST_APPROVAL})
                AND approved_total_cents = subtotal_cents
                AND approved_at IS NOT NULL)
            """,
            name="ck_po_approval_consistent",
        ),
        # An approved PO must carry the frozen record of exactly what was approved, and a
        # draft must not have one. (The snapshot's contents are verified by the service.)
        CheckConstraint(
            f"""
            (status = 'DRAFT' AND approved_snapshot IS NULL AND approved_snapshot_sha256 IS NULL)
            OR (status = 'CANCELLED'
                AND (approved_snapshot IS NULL) = (approved_at IS NULL)
                AND (approved_snapshot_sha256 IS NULL) = (approved_snapshot IS NULL))
            OR (status IN ({_POST_APPROVAL})
                AND approved_snapshot IS NOT NULL
                AND length(approved_snapshot_sha256) = 64)
            """,
            name="ck_po_snapshot_consistent",
        ),
        CheckConstraint("version >= 1", name="ck_po_version_positive"),
        CheckConstraint(
            "budget_cents IS NULL OR budget_cents >= 0", name="ck_po_budget_nonneg"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    status: Mapped[str] = mapped_column(String(20), default=POStatus.DRAFT.value)
    subtotal_cents: Mapped[int]
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    rationale: Mapped[str | None]
    approved_total_cents: Mapped[int | None]
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())

    # Optimistic-concurrency counter: starts at 1 and goes up on every edit. A client sends
    # the version it was looking at; if the draft has moved on, the request is rejected as stale.
    version: Mapped[int] = mapped_column(default=1, server_default="1")
    # The spending cap this order was drafted under. POs of one restock run share it (see
    # `run_id`). NULL means "none recorded", and such an order can never be approved.
    budget_cents: Mapped[int | None]
    # Groups the POs produced by one restock run (restock_runs.id). No FOREIGN KEY yet: tests
    # and the dev smoke runner (app.run_agent) still use run ids that have no restock_runs row.
    run_id: Mapped[int | None]
    # Written exactly once, at approval: canonical JSON of the order as approved, plus its
    # SHA-256. Triggers (below) make both immutable. Payment amounts are taken from here.
    approved_snapshot: Mapped[str | None] = mapped_column(Text)
    approved_snapshot_sha256: Mapped[str | None] = mapped_column(String(64))
    # The rebate promotion this order claims. The order is still paid at list price; the
    # validator checks the lines meet the promotion's terms whenever it checks the order.
    promotion_id: Mapped[int | None] = mapped_column(ForeignKey("promotions.id"))

    supplier: Mapped[Supplier] = relationship()
    promotion: Mapped[Promotion | None] = relationship()
    lines: Mapped[list["PurchaseOrderLine"]] = relationship(
        back_populates="purchase_order", order_by="PurchaseOrderLine.id"
    )
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="purchase_order", order_by="Payment.id"
    )
    events: Mapped[list["POEvent"]] = relationship(
        back_populates="purchase_order", order_by="POEvent.id"
    )


class PurchaseOrderLine(Base):
    """One product on one PO. Price and pack size are *copied* from the supplier's offer at
    draft time so later price changes never rewrite history.

    Two composite foreign keys prevent inconsistent orders:
      1. (po_id, supplier_id) -> purchase_orders(id, supplier_id): the line's supplier must
         be the PO's supplier.
      2. (supplier_id, product_id) -> supplier_products: that supplier must actually offer
         this product.
    Together: a PO to supplier A can never contain a product only supplier B sells.
    """

    __tablename__ = "purchase_order_lines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["po_id", "supplier_id"], ["purchase_orders.id", "purchase_orders.supplier_id"]
        ),
        ForeignKeyConstraint(
            ["supplier_id", "product_id"],
            ["supplier_products.supplier_id", "supplier_products.product_id"],
        ),
        UniqueConstraint("po_id", "product_id", name="uq_po_lines_po_product"),
        CheckConstraint("quantity > 0", name="ck_po_lines_quantity_positive"),
        CheckConstraint("unit_price_cents > 0", name="ck_po_lines_price_positive"),
        CheckConstraint("pack_size > 0", name="ck_po_lines_pack_positive"),
        CheckConstraint("quantity % pack_size = 0", name="ck_po_lines_whole_packs"),
        CheckConstraint(
            "line_total_cents = quantity * unit_price_cents", name="ck_po_lines_total_matches"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int]
    supplier_id: Mapped[int]  # denormalized from the PO on purpose; see the docstring
    product_id: Mapped[int]
    quantity: Mapped[int]
    unit_price_cents: Mapped[int]
    pack_size: Mapped[int]
    line_total_cents: Mapped[int]
    reason: Mapped[str | None]

    # Strings are resolved lazily, after the whole class exists (needed because the columns
    # above are annotation-only and are not plain Python variables inside the class body).
    purchase_order: Mapped[PurchaseOrder] = relationship(
        back_populates="lines",
        foreign_keys="[PurchaseOrderLine.po_id, PurchaseOrderLine.supplier_id]",
    )
    # No FK points straight at `products` (the FK goes via supplier_products), so we tell
    # SQLAlchemy how to join explicitly.
    product: Mapped[Product] = relationship(
        primaryjoin="PurchaseOrderLine.product_id == Product.id",
        foreign_keys="[PurchaseOrderLine.product_id]",
    )


class Payment(Base):
    """One attempt to pay a PO through PayPal. A PO can have several attempts (a declined
    payment is retried with a new PayPal order), hence a separate table rather than columns
    on purchase_orders. Written only by services/payments.py. Deliberately stores no raw PayPal
    response (it contains payer name, email and address); the facts we keep are the columns
    below plus the po_events audit trail."""

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(f"status IN ({_sql_list(PaymentStatus)})", name="ck_payments_status_valid"),
        CheckConstraint("amount_cents > 0", name="ck_payments_amount_positive"),
        CheckConstraint("currency = 'USD'", name="ck_payments_currency_usd"),
        CheckConstraint(
            "status != 'CAPTURED' OR paypal_capture_id IS NOT NULL",
            name="ck_payments_captured_has_capture_id",
        ),
        # Partial unique index: at most ONE row per PO whose status is "live". A FAILED
        # attempt does not count, so retries are allowed; a second live attempt (the
        # double-charge scenario) is rejected by the database itself.
        Index(
            "uq_payments_one_live_per_po",
            "po_id",
            unique=True,
            sqlite_where=text(f"status IN ({_LIVE_PAYMENT})"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"))
    paypal_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(10), default=PaymentStatus.CREATED.value)
    amount_cents: Mapped[int]
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    paypal_capture_id: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="payments")


class POEvent(Base):
    """Append-only audit trail of a PO: one row per creation, edit, or status change.

    `detail_json` is a small JSON object (who/what/why: old and new totals, the snapshot hash
    at approval, a cancellation reason...). Triggers below reject UPDATE and DELETE, so history
    can only grow. Rows are written by services.po_lifecycle, in the same transaction as the
    change they describe, so an event exists if and only if the change was committed.
    """

    __tablename__ = "po_events"
    __table_args__ = (Index("ix_po_events_po_id", "po_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"))
    event: Mapped[str] = mapped_column(String(40))
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="events")


class RestockRun(Base):
    """One merchant request handled by the agent. Written only by services/restock_runs.py.

    `running` is committed before the agent starts, so a server crash leaves evidence that the
    startup cleanup turns into `failed`. A run that hit a step limit is `completed` (its drafts
    passed the validator) with `error_code` explaining why it stopped early.
    """

    __tablename__ = "restock_runs"
    __table_args__ = (
        CheckConstraint(f"status IN ({_sql_list(RunStatus)})", name="ck_runs_status_valid"),
        CheckConstraint("budget_cents >= 0", name="ck_runs_budget_nonneg"),
        CheckConstraint(
            """
            (status = 'running' AND finished_at IS NULL AND summary IS NULL AND error_code IS NULL)
            OR (status = 'completed' AND finished_at IS NOT NULL AND summary IS NOT NULL)
            OR (status = 'failed' AND finished_at IS NOT NULL AND error_code IS NOT NULL)
            """,
            name="ck_runs_status_consistent",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    message: Mapped[str] = mapped_column(Text)
    budget_cents: Mapped[int]
    status: Mapped[str] = mapped_column(String(10), default=RunStatus.RUNNING.value)
    summary: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(Text)  # safe to show to the merchant
    model: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    steps: Mapped[list["AgentStepRecord"]] = relationship(order_by="AgentStepRecord.seq")


class AgentStepRecord(Base):
    """One line of a run's tool trace: a model turn, a tool call, or a tool result."""

    __tablename__ = "agent_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_agent_steps_run_seq"),
        CheckConstraint(
            "kind IN ('assistant', 'tool_call', 'tool_result')", name="ck_agent_steps_kind_valid"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("restock_runs.id"))
    seq: Mapped[int]
    kind: Mapped[str] = mapped_column(String(12))
    name: Mapped[str | None] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())


# ---------------------------------------------------------------------------------------------
# Immutability triggers (SQLite).
#
# CHECK constraints can only look at the row being written, so they cannot say "this row may
# never change once approved". A trigger can: it runs inside the database on every write, from
# any code path, and RAISE(ABORT, ...) cancels the statement. These are the last line of
# defence behind the service layer, which also refuses such changes with friendlier errors.
# `IS NOT` is SQLite's NULL-safe "different from" (plain `<>` returns NULL when either side is NULL).
# ---------------------------------------------------------------------------------------------

_TRIGGERS: dict[str, list[str]] = {
    "po_events": [
        """CREATE TRIGGER po_events_append_only_update BEFORE UPDATE ON po_events
           BEGIN SELECT RAISE(ABORT, 'po_events is append-only: rows cannot be updated'); END""",
        """CREATE TRIGGER po_events_append_only_delete BEFORE DELETE ON po_events
           BEGIN SELECT RAISE(ABORT, 'po_events is append-only: rows cannot be deleted'); END""",
    ],
    "purchase_orders": [
        """CREATE TRIGGER po_approval_frozen BEFORE UPDATE ON purchase_orders
           WHEN OLD.approved_snapshot IS NOT NULL AND (
                NEW.approved_snapshot IS NOT OLD.approved_snapshot
                OR NEW.approved_snapshot_sha256 IS NOT OLD.approved_snapshot_sha256
                OR NEW.approved_total_cents IS NOT OLD.approved_total_cents
                OR NEW.approved_at IS NOT OLD.approved_at
                OR NEW.supplier_id <> OLD.supplier_id
                OR NEW.currency <> OLD.currency
                OR NEW.version <> OLD.version
                OR NEW.budget_cents IS NOT OLD.budget_cents
                OR NEW.run_id IS NOT OLD.run_id
                OR NEW.promotion_id IS NOT OLD.promotion_id)
           BEGIN SELECT RAISE(ABORT, 'the approved order snapshot is frozen'); END""",
        """CREATE TRIGGER po_approved_not_deletable BEFORE DELETE ON purchase_orders
           WHEN OLD.approved_snapshot IS NOT NULL
           BEGIN SELECT RAISE(ABORT, 'an approved purchase order cannot be deleted'); END""",
    ],
    "purchase_order_lines": [
        """CREATE TRIGGER po_lines_frozen_insert BEFORE INSERT ON purchase_order_lines
           WHEN (SELECT status FROM purchase_orders WHERE id = NEW.po_id) <> 'DRAFT'
           BEGIN SELECT RAISE(ABORT, 'lines are frozen once a purchase order leaves DRAFT'); END""",
        """CREATE TRIGGER po_lines_frozen_update BEFORE UPDATE ON purchase_order_lines
           WHEN (SELECT status FROM purchase_orders WHERE id = OLD.po_id) <> 'DRAFT'
             OR (SELECT status FROM purchase_orders WHERE id = NEW.po_id) <> 'DRAFT'
           BEGIN SELECT RAISE(ABORT, 'lines are frozen once a purchase order leaves DRAFT'); END""",
        """CREATE TRIGGER po_lines_frozen_delete BEFORE DELETE ON purchase_order_lines
           WHEN (SELECT status FROM purchase_orders WHERE id = OLD.po_id) <> 'DRAFT'
           BEGIN SELECT RAISE(ABORT, 'lines are frozen once a purchase order leaves DRAFT'); END""",
    ],
}

for _table_name, _statements in _TRIGGERS.items():
    for _statement in _statements:
        # "after_create" fires right after `create_all` creates that table. Dropping a table
        # drops its triggers too, so `reset_schema` needs no extra cleanup.
        event.listen(
            Base.metadata.tables[_table_name],
            "after_create",
            DDL(_statement).execute_if(dialect="sqlite"),
        )
