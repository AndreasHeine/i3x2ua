import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [
    sveltekit({
      adapter: adapter({
        pages: '../dist',
        assets: '../dist'
      }),
      prerender: {
        crawl: false,
        // Every page is prerendered (src/routes/+layout.js), and with crawl: false only these
        // entries reach the prerenderer: a page left out ships no index.html at all.
        entries: ['/', '/view', '/mcp-tools-viewer', '/model-builder'],
        handleHttpError: 'warn'
      }
    })
  ]
});
