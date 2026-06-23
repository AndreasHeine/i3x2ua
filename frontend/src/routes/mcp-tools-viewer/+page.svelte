<script>
  import { onMount } from 'svelte';

  let rows = [];
  let error = '';

  const esc = (value) =>
    String(value)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');

  onMount(async () => {
    try {
      const response = await fetch('/mcp/tools');
      const payload = await response.json();
      const tools = payload.tools || {};
      const names = Object.keys(tools);

      if (names.length === 0) {
        error = 'No MCP tools available.';
        return;
      }

      rows = names.map((name) => {
        const item = tools[name] || {};
        const schema = item.inputSchema || item.input_schema || {};
        return {
          name: esc(name),
          description: esc(item.description || 'No description available'),
          schema: esc(JSON.stringify(schema, null, 2))
        };
      });
    } catch (e) {
      error = `Error loading tools: ${e instanceof Error ? e.message : String(e)}`;
    }
  });
</script>

<div class="container">
  <div class="panel hero">
    <img class="logo" src="/static/logo-small.png" alt="i3X logo" />
    <h2>i3X API Gateway for OPC UA</h2>
  </div>
  <div class="panel header">
    <h1>MCP Tools</h1>
    <a class="back-link" href="/">&larr; Back</a>
  </div>
  <div class="panel table-wrap">
    {#if error}
      <div class="error" style="padding: 20px;">{error}</div>
    {:else if rows.length === 0}
      <div style="padding: 20px;">Loading tools...</div>
    {:else}
      <table>
        <thead>
          <tr>
            <th style="width: 24%;">Tool</th>
            <th style="width: 36%;">Description</th>
            <th style="width: 40%;">Input Schema</th>
          </tr>
        </thead>
        <tbody>
          {#each rows as row}
            <tr>
              <td class="tool-name">{@html row.name}</td>
              <td>{@html row.description}</td>
              <td><pre class="schema">{@html row.schema}</pre></td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>
</div>
