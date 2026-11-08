<script>
  import { onMount } from 'svelte';

  let rows = [];
  let error = '';

  onMount(async () => {
    try {
      const response = await fetch('/mcp/tools');
      if (!response.ok) {
        error = `MCP tools endpoint returned HTTP ${response.status}. Enable MCP with I3X_ENABLE_MCP=1.`;
        return;
      }
      const payload = await response.json();
      const tools = payload.tools || {};
      const names = Object.keys(tools);

      if (names.length === 0) {
        error = 'No MCP tools available. Enable MCP with I3X_ENABLE_MCP=1.';
        return;
      }

      rows = names.map((name) => {
        const item = tools[name] || {};
        const schema = item.inputSchema || item.input_schema || {};
        return {
          name,
          description: item.description || 'No description available',
          schema: JSON.stringify(schema, null, 2)
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
              <td class="tool-name">{row.name}</td>
              <td>{row.description}</td>
              <td><pre class="schema">{row.schema}</pre></td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>
</div>
