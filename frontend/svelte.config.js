import adapter from '@sveltejs/adapter-static';

const config = {
  kit: {
    adapter: adapter({
      pages: '../dist',
      assets: '../dist'
    }),
    prerender: {
      crawl: false,
      entries: ['/', '/view', '/mcp-tools-viewer'],
      handleHttpError: 'warn'
    }
  }
};

export default config;
