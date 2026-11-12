"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState, type FormEvent } from "react";

import ErrorAlert from "@/components/ErrorAlert";
import {
  createPromotion,
  listSupplierOffers,
  listSuppliers,
  parsePromotion,
  type CatalogOffer,
  type PromotionDraft,
  type Supplier,
} from "@/lib/api";
import { formatCents, parseDollarsToCents } from "@/lib/format";
import { formatSize } from "@/lib/units";

// Mirrors MAX_PROMOTION_TEXT_CHARS in backend/app/agent/promotion_extraction.py.
const MAX_TEXT_CHARS = 2000;

const EXAMPLE =
  "Harbor Wine & Spirits has a rebate of 10 cases of Svedka mix and match with any 3 different " +
  "flavors at 2 different volumes (750ml and 1.5L) for $500.";

type Form = {
  title: string;
  supplierId: string;
  requiredCases: string;
  unitsPerCase: string;
  rebate: string;
  minFlavors: string;
  validFrom: string;
  validUntil: string;
  buyThrough: string;
  maxBundles: string;
  skus: Set<string>;
  brand: string | null;
  sourceText: string | null;
};

function formFrom(draft: PromotionDraft | null): Form {
  return {
    title: draft?.title ?? "",
    supplierId: draft?.supplier_id ? String(draft.supplier_id) : "",
    requiredCases: draft?.required_cases ? String(draft.required_cases) : "",
    unitsPerCase: String(draft?.units_per_case ?? 12),
    rebate: draft?.rebate_cents ? (draft.rebate_cents / 100).toFixed(2) : "",
    minFlavors: draft?.min_distinct_variants ? String(draft.min_distinct_variants) : "",
    validFrom: draft?.valid_from ?? "",
    validUntil: draft?.valid_until ?? "",
    buyThrough: "",
    maxBundles: draft?.max_bundles ? String(draft.max_bundles) : "",
    skus: new Set(draft?.eligible_skus ?? []),
    brand: draft?.brand ?? null,
    sourceText: draft?.source_text ?? null,
  };
}

function positiveInt(value: string): number | null {
  return /^[1-9]\d{0,3}$/.test(value.trim()) ? Number(value.trim()) : null;
}

/** Step 1: paste the supplier's text and let the AI read it (nothing is saved). Step 2: check
 * and correct every term against the supplier's real catalog, then save. */
export default function PromotionIntake() {
  const router = useRouter();
  const [text, setText] = useState("");
  const [parsing, setParsing] = useState(false);
  const [draft, setDraft] = useState<PromotionDraft | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [error, setError] = useState<unknown>(null);

  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [offers, setOffers] = useState<CatalogOffer[]>([]);
  const [showAll, setShowAll] = useState(false);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    listSuppliers().then(setSuppliers).catch(setError);
  }, []);

  const supplierId = form?.supplierId ? Number(form.supplierId) : null;
  useEffect(() => {
    if (supplierId === null) return;
    let cancelled = false;
    listSupplierOffers(supplierId)
      .then((items) => !cancelled && setOffers(items))
      .catch((err: unknown) => !cancelled && setError(err));
    return () => {
      cancelled = true;
    };
  }, [supplierId]);

  const visibleOffers = useMemo(() => {
    if (supplierId === null) return [];
    if (showAll || !form?.brand) return offers;
    const brand = form.brand.toLowerCase();
    return offers.filter((o) => o.brand?.toLowerCase() === brand || form.skus.has(o.sku));
  }, [offers, showAll, form, supplierId]);

  async function handleParse(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || trimmed.length > MAX_TEXT_CHARS) return;
    setParsing(true);
    setError(null);
    try {
      const parsed = await parsePromotion(trimmed);
      setDraft(parsed);
      setForm(formFrom(parsed));
    } catch (err) {
      setError(err);
    } finally {
      setParsing(false);
    }
  }

  function update<K extends keyof Form>(key: K, value: Form[K]) {
    setForm((f) => (f ? { ...f, [key]: value } : f));
  }

  function toggleSku(sku: string) {
    setForm((f) => {
      if (!f) return f;
      const skus = new Set(f.skus);
      if (skus.has(sku)) skus.delete(sku);
      else skus.add(sku);
      return { ...f, skus };
    });
  }

  async function handleSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!form) return;
    const requiredCases = positiveInt(form.requiredCases);
    const unitsPerCase = positiveInt(form.unitsPerCase);
    const minFlavors = positiveInt(form.minFlavors);
    const rebateCents = parseDollarsToCents(form.rebate);
    const maxBundles = form.maxBundles.trim() ? positiveInt(form.maxBundles) : null;
    const problem =
      !form.title.trim() ? "Give the promotion a title."
      : supplierId === null ? "Choose the supplier."
      : requiredCases === null ? "Enter the number of cases."
      : unitsPerCase === null ? "Enter the bottles per case."
      : rebateCents === null || rebateCents === 0 ? "Enter the rebate as a dollar amount."
      : minFlavors === null ? "Enter the minimum number of different flavors."
      : form.maxBundles.trim() && maxBundles === null ? "Max bundles must be a whole number, or empty for no limit."
      : form.skus.size === 0 ? "Tick the eligible products."
      : null;
    setFormError(problem);
    if (problem || supplierId === null || requiredCases === null || unitsPerCase === null
        || minFlavors === null || rebateCents === null) return;

    setSaving(true);
    setError(null);
    try {
      const saved = await createPromotion({
        supplier_id: supplierId,
        title: form.title.trim(),
        required_cases: requiredCases,
        units_per_case: unitsPerCase,
        rebate_cents: rebateCents,
        min_distinct_variants: minFlavors,
        eligible_skus: [...form.skus],
        valid_from: form.validFrom || null,
        valid_until: form.validUntil || null,
        source_text: form.sourceText,
        max_bundles: maxBundles,
        buy_through: form.buyThrough || null,
      });
      router.push(`/promotions/${saved.id}`);
    } catch (err) {
      setError(err);
      setSaving(false);
    }
  }

  return (
    <div className="stack">
      <form onSubmit={handleParse} noValidate className="card">
        <h2>Add a supplier promotion</h2>
        <label htmlFor="promo-text">Paste the promotion as the supplier wrote it</label>
        <textarea
          id="promo-text"
          rows={3}
          placeholder={EXAMPLE}
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={parsing}
        />
        <div className="actions">
          <button type="submit" className="btn btn-primary" disabled={parsing || !text.trim()}>
            {parsing ? "Reading…" : "Read with AI"}
          </button>
          <button type="button" className="btn" onClick={() => setText(EXAMPLE)} disabled={parsing}>
            Use the Svedka example
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => {
              setDraft(null);
              setForm(formFrom(null));
            }}
            disabled={parsing}
          >
            Enter terms by hand
          </button>
        </div>
        <p className="hint">
          The AI only reads the text into fields. Nothing is saved until you check the terms below.
        </p>
      </form>

      {error !== null && <ErrorAlert error={error} title="Something went wrong." />}

      {form && (
        <form onSubmit={handleSave} noValidate className="card">
          <h2>Check the terms</h2>
          {draft && (draft.warnings.length > 0 || draft.ambiguities.length > 0) && (
            <div className="alert alert-warning">
              <strong>Please check:</strong>
              <ul>
                {draft.warnings.map((w) => (
                  <li key={`w-${w}`}>{w}</li>
                ))}
                {draft.ambiguities.map((a) => (
                  <li key={`a-${a}`}>
                    <span className="ai-label">AI:</span> {a}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <label htmlFor="promo-title">Title</label>
          <input id="promo-title" value={form.title} onChange={(e) => update("title", e.target.value)} />

          <label htmlFor="promo-supplier">Supplier</label>
          <select
            id="promo-supplier"
            value={form.supplierId}
            onChange={(e) => update("supplierId", e.target.value)}
          >
            <option value="">Choose…</option>
            {suppliers.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
          {draft?.supplier_name_as_written && (
            <p className="hint">The text says “{draft.supplier_name_as_written}”.</p>
          )}

          <label htmlFor="promo-cases">Cases required</label>
          <input id="promo-cases" inputMode="numeric" value={form.requiredCases}
                 onChange={(e) => update("requiredCases", e.target.value)} />

          <label htmlFor="promo-units">Bottles per case</label>
          <input id="promo-units" inputMode="numeric" value={form.unitsPerCase}
                 onChange={(e) => update("unitsPerCase", e.target.value)} />

          <label htmlFor="promo-rebate">Rebate in USD (paid back by the supplier)</label>
          <input id="promo-rebate" inputMode="decimal" value={form.rebate}
                 onChange={(e) => update("rebate", e.target.value)} />

          <label htmlFor="promo-flavors">At least this many different flavors</label>
          <input id="promo-flavors" inputMode="numeric" value={form.minFlavors}
                 onChange={(e) => update("minFlavors", e.target.value)} />

          <label htmlFor="promo-from">Valid from (optional)</label>
          <input id="promo-from" type="date" value={form.validFrom}
                 onChange={(e) => update("validFrom", e.target.value)} />
          <label htmlFor="promo-until">Valid until (optional)</label>
          <input id="promo-until" type="date" value={form.validUntil}
                 onChange={(e) => update("validUntil", e.target.value)} />

          <label htmlFor="promo-buy-through">Stock up through (optional)</label>
          <input id="promo-buy-through" type="date" value={form.buyThrough}
                 onChange={(e) => update("buyThrough", e.target.value)} />
          <p className="hint">
            How far ahead this deal should cover, e.g. Dec 31 for the holidays or when you expect
            the next deal. StockPilot reads last year’s sales for those weeks. Empty: the next{" "}
            two weeks.
          </p>

          <label htmlFor="promo-max-bundles">Max bundles per order (optional)</label>
          <input id="promo-max-bundles" inputMode="numeric" value={form.maxBundles}
                 placeholder="No limit"
                 onChange={(e) => update("maxBundles", e.target.value)} />
          <p className="hint">Each full bundle earns another rebate, up to this limit.</p>

          <fieldset>
            <legend>Eligible products ({form.skus.size} ticked)</legend>
            {supplierId === null ? (
              <p className="hint">Choose a supplier to see what it sells.</p>
            ) : (
              <>
                {form.brand && (
                  <label>
                    <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />{" "}
                    Show all of this supplier’s products, not just {form.brand}
                  </label>
                )}
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th />
                        <th>Product</th>
                        <th>Flavor</th>
                        <th className="num">Size</th>
                        <th className="num">Case</th>
                        <th className="num">Unit price</th>
                      </tr>
                    </thead>
                    <tbody>
                      {visibleOffers.map((o) => (
                        <tr key={o.sku}>
                          <td>
                            <input
                              type="checkbox"
                              aria-label={`Eligible: ${o.name}`}
                              checked={form.skus.has(o.sku)}
                              onChange={() => toggleSku(o.sku)}
                            />
                          </td>
                          <td>
                            {o.name} <code>{o.sku}</code>
                          </td>
                          <td>{o.variant ?? "—"}</td>
                          <td className="num">{formatSize(o.size_ml)}</td>
                          <td className="num">{o.pack_size}</td>
                          <td className="num">{formatCents(o.unit_price_cents)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </fieldset>

          {formError && <p className="field-error">{formError}</p>}
          <button type="submit" className="btn btn-primary" disabled={saving}>
            {saving ? "Saving…" : "Save promotion"}
          </button>
          <p className="hint">The server checks the terms against the catalog before saving.</p>
        </form>
      )}
    </div>
  );
}
