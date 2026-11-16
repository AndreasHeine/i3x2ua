/**
 * The model-builder page's display helpers, tested as a module rather than through the page.
 *
 * They were inline in ``+page.svelte``, where nothing could reach them: CI only built the frontend,
 * so a helper that sorted or formatted wrongly shipped unnoticed. Keeping them in a plain module
 * makes them testable with Node's built-in runner, which adds no dependency to the published app.
 *
 * The assertions pin the behaviour the page relies on, not the implementation: entries sort by
 * count descending with names breaking ties, a percentage is always one decimal place, and a
 * timestamp that cannot be parsed is shown as it arrived instead of as ``Invalid Date``.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { formatUtcTimestamp, qualityPct, sortedEntries } from './format.js';

test('sortedEntries orders by count descending', () => {
  assert.deepEqual(sortedEntries({ b: 2, a: 5, c: 1 }), [['a', 5], ['b', 2], ['c', 1]]);
});

test('sortedEntries breaks equal counts by name', () => {
  assert.deepEqual(sortedEntries({ zeta: 3, alpha: 3, mid: 3 }), [['alpha', 3], ['mid', 3], ['zeta', 3]]);
});

test('sortedEntries treats a missing or empty value as no entries', () => {
  assert.deepEqual(sortedEntries(undefined), []);
  assert.deepEqual(sortedEntries(null), []);
  assert.deepEqual(sortedEntries({}), []);
});

test('qualityPct reports one decimal place', () => {
  assert.equal(qualityPct(1, 3), '33.3');
  assert.equal(qualityPct(2, 3), '66.7');
  assert.equal(qualityPct(3, 3), '100.0');
  assert.equal(qualityPct(0, 3), '0.0');
});

test('qualityPct reports zero for a total that cannot divide', () => {
  assert.equal(qualityPct(5, 0), '0.0');
  assert.equal(qualityPct(0, 0), '0.0');
  assert.equal(qualityPct(5, undefined), '0.0');
});

test('formatUtcTimestamp renders a parseable timestamp', () => {
  const formatted = formatUtcTimestamp('2026-11-16T00:00:00Z');
  assert.notEqual(formatted, '2026-11-16T00:00:00Z');
  assert.ok(formatted.includes('2026'), formatted);
});

test('formatUtcTimestamp shows an unparseable value as it arrived', () => {
  assert.equal(formatUtcTimestamp('not-a-timestamp'), 'not-a-timestamp');
  assert.equal(formatUtcTimestamp(''), 'n/a');
  assert.equal(formatUtcTimestamp(undefined), 'n/a');
  assert.equal(formatUtcTimestamp(null), 'n/a');
});
