"use client";

import { useEffect, useMemo, useState, type FormEvent } from "react";

import ErrorAlert, { describeError } from "@/components/ErrorAlert";
import {
  ApiError,
  editPurchaseOrder,
  listSupplierOffers,
  type CatalogOffer,
  type PurchaseOrder,
} from "@/lib/api";
import { formatCents } from "@/lib/format";
import { promotionClaim } from "@/lib/promotionClaim";
import { formatSize } from "@/lib/units";

type Row = {
  sku: string;
  name: string;
  packSize: number;
  unitPriceCents: number;
  reason: string | null;
  /** Whole cases; converted to units on save. */
  cases: string;
  removed: boolean;
};

function rowsFrom(po: PurchaseOrder): Row[] {
  return po.lines.map((l) => ({
    sku: l.sku,
    name: l.name,
    packSize: l.pack_size,
    unitPriceCents: l.unit_price_cents,
    reason: l.reason,
    cases: String(l.quantity / l.pack_size),
    removed: false,
  }));
}

function unitsOf(row: Row): number {
  return Number(row.cases.trim()) * row.packSize;
}

/** Edit a DRAFT by cases: change quantities, remove lines, or add another product from the
 * same supplier. It sends only SKUs and unit quantities; the server re-prices and checks
 * purchasing rules. Rebate fit is advisory — a broken claim does not block the save. */
export default function DraftEditor({
  po,
  onSaved,
  onCancel,
  onStale,
}: {
  po: PurchaseOrder;
  onSaved: (po: PurchaseOrder) => void;
  onCancel: () => void;
  onStale: () => Promise<void>;
}) {
  const [rows, setRows] = useState<Row[]>(() => rowsFrom(po));
  const [catalog, setCatalog] = useState<CatalogOffer[] | null>(null);
  const [addSku, setAddSku] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    listSupplierOffers(po.supplier.id)
      .then((items) => {
        if (!cancelled) setCatalog(items);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err);
      });
    return () => {
      cancelled = true;
    };
  }, [po.supplier.id]);

  const kept = useMemo(() => rows.filter((r) => !r.removed), [rows]);
  const claim = useMemo(() => {
    if (!po.promotion) return null;
    const lines = kept
      .filter((r) => /^[1-9]\d*$/.test(r.cases.trim()))
      .map((r) => ({ sku: r.sku, quantity: unitsOf(r) }));
    return promotionClaim(po.promotion, lines);
  }, [po.promotion, kept]);

  const addable = useMemo(() => {
    if (!catalog) return [];
    const onOrder = new Set(rows.map((r) => r.sku));
    return catalog.filter((o) => !onOrder.has(o.sku));
  }, [catalog, rows]);

  function update(sku: string, patch: Partial<Row>) {
    setRows((current) => current.map((r) => (r.sku === sku ? { ...r, ...patch } : r)));
  }

  function addProduct() {
    if (!catalog || !addSku) return;
    const offer = catalog.find((o) => o.sku === addSku);
    if (!offer) return;
    setRows((current) => [
      ...current,
      {
        sku: offer.sku,
        name: offer.name,
        packSize: offer.pack_size,
        unitPriceCents: offer.unit_price_cents,
        reason: null,
        cases: "1",
        removed: false,
      },
    ]);
    setAddSku("");
    setFormError(null);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (kept.length === 0) {
      setFormError("An order needs at least one line. To drop the whole order, reject it instead.");
      return;
    }
    const bad = kept.find((r) => !/^[1-9]\d*$/.test(r.cases.trim()));
    if (bad) {
      setFormError(`Cases for ${bad.name} must be a whole number greater than zero.`);
      return;
    }
    setFormError(null);
    setError(null);
    setSaving(true);
    try {
      const saved = await editPurchaseOrder(
        po.id,
        po.version,
        kept.map((r) => ({
          sku: r.sku,
          quantity: unitsOf(r),
          reason: r.reason,
        })),
      );
      onSaved(saved);
    } catch (err) {
      setError(err);
      setSaving(false);
      if (err instanceof ApiError && err.code === "STALE_DRAFT") await onStale();
    }
  }

  return (
    <form onSubmit={handleSubmit} noValidate aria-label="Edit draft order">
      {claim && (
        <div
          role="status"
          className={claim.meetsTerms ? "alert alert-info" : "alert alert-warning"}
        >
          {claim.message}
          {!claim.meetsTerms && (
            <> You can still save and approve this order; it just will not earn the rebate.</>
          )}
        </div>
      )}

      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Product</th>
              <th className="num">Unit price</th>
              <th>Cases</th>
              <th className="num">Bottles</th>
              <th>Remove</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const casesOk = /^[1-9]\d*$/.test(row.cases.trim());
              return (
                <tr key={row.sku} className={row.removed ? "row-removed" : undefined}>
                  <td>
                    {row.name} <code>{row.sku}</code>
                  </td>
                  <td className="num">{formatCents(row.unitPriceCents)}</td>
                  <td>
                    <input
                      aria-label={`Cases of ${row.name}`}
                      inputMode="numeric"
                      value={row.cases}
                      disabled={row.removed || saving}
                      onChange={(e) => update(row.sku, { cases: e.target.value })}
                    />
                    <span className="hint"> × {row.packSize} / case</span>
                  </td>
                  <td className="num">
                    {row.removed || !casesOk ? "—" : unitsOf(row)}
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Remove ${row.name}`}
                      checked={row.removed}
                      disabled={saving}
                      onChange={(e) => update(row.sku, { removed: e.target.checked })}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="actions" style={{ marginTop: "0.75rem" }}>
        <label htmlFor="add-product" className="hint">
          Add a product from {po.supplier.name}
        </label>
        <select
          id="add-product"
          value={addSku}
          disabled={saving || catalog === null || addable.length === 0}
          onChange={(e) => setAddSku(e.target.value)}
        >
          <option value="">
            {catalog === null
              ? "Loading catalogue…"
              : addable.length === 0
                ? "Every product from this supplier is already on the order"
                : "Choose a product…"}
          </option>
          {addable.map((o) => {
            const detail = [o.variant, o.size_ml ? formatSize(o.size_ml) : null]
              .filter(Boolean)
              .join(", ");
            return (
              <option key={o.sku} value={o.sku}>
                {o.name}
                {detail ? ` (${detail})` : ""} — cases of {o.pack_size},{" "}
                {formatCents(o.unit_price_cents)}
              </option>
            );
          })}
        </select>
        <button
          type="button"
          className="btn"
          onClick={addProduct}
          disabled={saving || !addSku}
        >
          Add to order
        </button>
      </div>

      <p className="hint">
        Quantities are whole cases. Saving sends products and bottle counts; the server
        re-prices the order and checks stock limits, the supplier minimum and your budget.
        {po.promotion && " Rebate fit is checked but does not block the save."}
      </p>
      {formError && (
        <p role="alert" className="field-error">
          {formError}
        </p>
      )}
      {error !== null && (
        <ErrorAlert error={error} title={describeError(error) ?? "The draft was not saved."} />
      )}
      <div className="actions">
        <button type="submit" className="btn btn-primary" disabled={saving}>
          {saving ? "Saving…" : "Save changes"}
        </button>
        <button type="button" className="btn" onClick={onCancel} disabled={saving}>
          Discard changes
        </button>
      </div>
    </form>
  );
}
