<script>
  import { onMount } from 'svelte';

  const knownViewTargets = {
    '/v1/info': 'i3X Server Info',
    '/ua/status': 'OPC UA Server Status',
    '/ua/connection': 'OPC UA Connection',
    '/ua/limits': 'OPC UA Server Limits',
    '/ua/metrics': 'OPC UA Metrics'
  };

  let viewerTitle = 'Loading...';
  let output = 'Loading...';
  let isError = false;

  const load = async (endpoint) => {
    try {
      const response = await fetch(endpoint, { cache: 'no-store' });
      const payload = await response.json();
      output = JSON.stringify(payload.result, null, 2);
      isError = false;
    } catch (error) {
      output = `Error: ${error instanceof Error ? error.message : String(error)}`;
      isError = true;
    }
  };

  onMount(() => {
    const params = new URLSearchParams(window.location.search);
    const requested = params.get('endpoint') || '/v1/info';
    const endpoint = Object.prototype.hasOwnProperty.call(knownViewTargets, requested)
      ? requested
      : '/v1/info';
    const refreshMs = endpoint.startsWith('/ua/') ? 2000 : 0;

    viewerTitle = knownViewTargets[endpoint] || 'API Result';
    load(endpoint);

    if (refreshMs > 0) {
      const timer = setInterval(() => {
        load(endpoint);
      }, refreshMs);
      return () => clearInterval(timer);
    }

    return undefined;
  });
</script>

<div class="container">
  <div class="panel hero">
    <img class="logo" src="/static/logo-small.png" alt="i3X logo" />
    <h2>i3X API Gateway for OPC UA</h2>
  </div>
  <div class="panel header">
    <h1>{viewerTitle}</h1>
    <a class="back-link" href="/">&larr; Back</a>
  </div>
  <div class="panel code-block">
    <pre class:error={isError}>{output}</pre>
  </div>
</div>
