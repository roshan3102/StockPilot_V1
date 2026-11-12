/** 750 -> "750ml", 1500 -> "1.5L". Display only; mirrors backend/app/domain/units.py. */
export function formatSize(ml: number | null): string {
  if (ml === null) return "";
  return ml >= 1000 ? `${ml / 1000}L` : `${ml}ml`;
}
