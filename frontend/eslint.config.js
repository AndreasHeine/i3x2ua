/**
 * ESLint flat config for the SvelteKit SPA.
 *
 * The rules cover the shipped source only: `node_modules`, `.svelte-kit`, `dist`, and `static` are
 * generated or vendored, and linting them would report upstream code we do not own. Globals are
 * declared here rather than pulled in as a package - the SPA uses browser globals, and the Node
 * test runner uses Node's - so the gate adds no dependency beyond the linter itself.
 */

import js from '@eslint/js';
import { configs as svelteConfigs } from 'eslint-plugin-svelte';

const browserGlobals = {
  clearInterval: 'readonly',
  clearTimeout: 'readonly',
  console: 'readonly',
  document: 'readonly',
  fetch: 'readonly',
  setInterval: 'readonly',
  setTimeout: 'readonly',
  URLSearchParams: 'readonly',
  window: 'readonly',
};

export default [
  { ignores: ['node_modules', '.svelte-kit', 'dist', 'static'] },
  js.configs.recommended,
  ...svelteConfigs.recommended,
  {
    files: ['**/*.js', '**/*.svelte'],
    languageOptions: {
      globals: browserGlobals,
    },
    rules: {
      // The SPA links to gateway endpoints - /docs, and /view/ with a query that names the endpoint
      // to show - which are not SvelteKit routes, so resolve() has nothing to resolve them to.
      'svelte/no-navigation-without-resolve': 'off',
    },
  },
];
