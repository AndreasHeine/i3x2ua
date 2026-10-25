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
        entries: ['/', '/view', '/mcp-tools-viewer'],
        handleHttpError: 'warn'
      }
    })
  ]
});
