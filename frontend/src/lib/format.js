/**
 * Display helpers for the model-builder metrics page.
 *
 * They live in a module rather than inside ``+page.svelte`` so ``npm run test`` can reach them:
 * a helper that sorted or formatted wrongly was previously only visible to whoever looked at the
 * rendered page. Node's built-in test runner covers them, so nothing here adds a dependency to the
 * published app.
 */

/**
 * Entries of a count map, ordered by count descending; equal counts keep names in alphabetical
 * order so the rendered list is stable across reloads.
 *
 * @param {Record<string, number> | null | undefined} value
 * @returns {[string, number][]}
 */
export function sortedEntries(value) {
  return Object.entries(value || {}).sort((a, b) => {
    if (b[1] === a[1]) {
      return a[0].localeCompare(b[0]);
    }
    return b[1] - a[1];
  });
}

/**
 * A percentage with one decimal place, as the panel renders it. A total that cannot divide a
 * numerator reports zero instead of ``NaN`` or ``Infinity``.
 *
 * @param {number} numerator
 * @param {number | null | undefined} total
 * @returns {string}
 */
export function qualityPct(numerator, total) {
  if (!total) return '0.0';
  return ((numerator / total) * 100).toFixed(1);
}

/**
 * A timestamp in the locale's format; a value that cannot be parsed is shown as it arrived, and a
 * missing value shows ``n/a``, so the panel never prints ``Invalid Date``.
 *
 * @param {string | null | undefined} value
 * @returns {string}
 */
export function formatUtcTimestamp(value) {
  if (!value) return 'n/a';
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return value;
  return dt.toLocaleString();
}
