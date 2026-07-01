<script>
  import { onMount } from 'svelte';

  const title = 'i3X API Gateway for OPC UA';
  const description = 'Turn any OPC UA server into a standards-compatible i3X API and MCP endpoint.';

  const baseLinks = [
    { label: 'API Documentation', href: '/docs' },
    { label: 'i3X Server Info', href: '/view?endpoint=/v1/info&label=i3X%20Server%20Info' },
    { label: 'OPC UA Server Status', href: '/view?endpoint=/ua/status&label=OPC%20UA%20Server%20Status' },
    { label: 'OPC UA Connection', href: '/view?endpoint=/ua/connection&label=OPC%20UA%20Connection' },
    { label: 'OPC UA Limits', href: '/view?endpoint=/ua/limits&label=OPC%20UA%20Limits' },
    { label: 'OPC UA Metrics', href: '/view?endpoint=/ua/metrics&label=OPC%20UA%20Metrics' }
  ];
  let links = [...baseLinks];

  onMount(async () => {
    try {
      const response = await fetch('/mcp/tools', { cache: 'no-store' });
      if (response.ok) {
        links = [...baseLinks, { label: 'MCP Tools', href: '/mcp-tools-viewer' }];
      }
    } catch {
      links = [...baseLinks];
    }
  });
</script>

<main class="panel" style="max-width: 920px; margin: 0 auto; overflow: hidden;">
  <section class="hero">
    <img class="logo" src="/static/logo-small.png" alt="i3X logo" />
    <h1>{title}</h1>
    <p class="lead">{description}</p>
  </section>
  <section class="grid">
    {#each links as link}
      <a class="card" href={link.href}>
        <span>{link.label}</span>
        <span class="arrow">&rarr;</span>
      </a>
    {/each}
  </section>
</main>
