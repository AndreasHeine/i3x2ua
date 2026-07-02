<script>
  import { onMount } from 'svelte';

  const endpoint = '/v1/model/metrics';

  let metrics = null;
  let loading = true;
  let error = '';

  const fetchMetrics = async () => {
    try {
      const response = await fetch(endpoint, { cache: 'no-store' });
      const payload = await response.json();
      if (!response.ok || !payload?.success) {
        throw new Error(payload?.error?.message || `Request failed with status ${response.status}`);
      }
      metrics = payload.result;
      error = '';
    } catch (e) {
      error = `Error loading model metrics: ${e instanceof Error ? e.message : String(e)}`;
    } finally {
      loading = false;
    }
  };

  const sortedEntries = (value) =>
    Object.entries(value || {}).sort((a, b) => {
      if (b[1] === a[1]) {
        return a[0].localeCompare(b[0]);
      }
      return b[1] - a[1];
    });

  const qualityPct = (num, total) => {
    if (!total) return '0.0';
    return ((num / total) * 100).toFixed(1);
  };

  const formatUtcTimestamp = (value) => {
    if (!value) return 'n/a';
    const dt = new Date(value);
    if (Number.isNaN(dt.getTime())) return value;
    return dt.toLocaleString();
  };

  onMount(() => {
    fetchMetrics();
    const timer = setInterval(fetchMetrics, 5000);
    return () => clearInterval(timer);
  });
</script>

<div class="container">
  <div class="panel hero">
    <img class="logo" src="/static/logo-small.png" alt="i3X logo" />
    <h2>i3X API Gateway for OPC UA</h2>
    <p class="lead">Model builder dashboard with live mapping and relationship diagnostics.</p>
  </div>

  <div class="panel header">
    <h1>Model Builder</h1>
    <div class="header-actions">
      <a class="back-link" href="/view?endpoint=/v1/model/metrics&label=Model%20Builder%20Metrics">View raw</a>
      <a class="back-link" href="/">&larr; Back</a>
    </div>
  </div>

  {#if error}
    <div class="panel" style="padding: 18px;">
      <div class="error">{error}</div>
    </div>
  {:else if loading || !metrics}
    <div class="panel" style="padding: 18px;">Loading model metrics...</div>
  {:else}
    <section class="metric-grid panel" style="padding: 18px; margin-bottom: 16px;">
      <article class="metric-card">
        <div class="metric-label">Total nodes</div>
        <div class="metric-value">{metrics.volume.totalNodes}</div>
      </article>
      <article class="metric-card">
        <div class="metric-label">Root nodes</div>
        <div class="metric-value">{metrics.volume.rootNodes}</div>
      </article>
      <article class="metric-card">
        <div class="metric-label">Build total (s)</div>
        <div class="metric-value">{metrics.build.totalDurationS.toFixed(3)}</div>
      </article>
      <article class="metric-card">
        <div class="metric-label">Low confidence</div>
        <div class="metric-value">
          {metrics.quality.lowConfidenceNodes}
          <span class="metric-sub">({qualityPct(metrics.quality.lowConfidenceNodes, metrics.volume.totalNodes)}%)</span>
        </div>
      </article>
    </section>

    <section class="detail-grid">
      <div class="panel metric-section">
        <h3>Build Phases</h3>
        <dl class="kv-list">
          <div><dt>Browse phase</dt><dd>{metrics.build.browseDurationS.toFixed(3)} s</dd></div>
          <div><dt>Map phase</dt><dd>{metrics.build.mapDurationS.toFixed(3)} s</dd></div>
          <div><dt>Total build</dt><dd>{metrics.build.totalDurationS.toFixed(3)} s</dd></div>
          <div><dt>Last build time</dt><dd>{formatUtcTimestamp(metrics.build.buildCompletedAtUtc)}</dd></div>
        </dl>
      </div>

      <div class="panel metric-section">
        <h3>Coverage</h3>
        <dl class="kv-list">
          <div><dt>Readable properties</dt><dd>{metrics.coverage.readableProperties}</dd></div>
          <div><dt>Invokable actions</dt><dd>{metrics.coverage.invokableActions}</dd></div>
          <div><dt>Typed instance groups</dt><dd>{metrics.coverage.typedInstanceGroups}</dd></div>
          <div><dt>Total typed instances</dt><dd>{metrics.coverage.typedInstances}</dd></div>
        </dl>
      </div>

      <div class="panel metric-section">
        <h3>Relationship Mix</h3>
        <dl class="kv-list">
          <div><dt>Hierarchy edges</dt><dd>{metrics.relationships.hierarchyEdges}</dd></div>
          <div><dt>Composition edges</dt><dd>{metrics.relationships.compositionEdges}</dd></div>
          <div><dt>Graph edges</dt><dd>{metrics.relationships.graphEdges}</dd></div>
          <div><dt>Graph relationship names</dt><dd>{metrics.relationships.uniqueGraphRelationshipNames}</dd></div>
        </dl>
      </div>

      <div class="panel metric-section">
        <h3>Quality</h3>
        <dl class="kv-list">
          <div><dt>Unknown semantic roles</dt><dd>{metrics.quality.unknownSemanticRoleNodes}</dd></div>
          <div><dt>Nodes with unresolved namespace URI</dt><dd>{metrics.context.nodesWithoutNamespace}</dd></div>
          <div><dt>Nodes without profiles</dt><dd>{metrics.context.nodesWithoutProfiles}</dd></div>
          <div><dt>Low confidence %</dt><dd>{qualityPct(metrics.quality.lowConfidenceNodes, metrics.volume.totalNodes)}%</dd></div>
        </dl>
      </div>
    </section>

    <section class="panel metric-section" style="margin-top: 16px;">
      <h3>Distributions</h3>
      <div class="dist-grid">
        <div>
          <h4>By kind</h4>
          <ul class="metric-list">
            {#each sortedEntries(metrics.volume.byKind) as [name, count]}
              <li><span>{name}</span><strong>{count}</strong></li>
            {/each}
          </ul>
        </div>
        <div>
          <h4>Confidence</h4>
          <ul class="metric-list">
            {#each sortedEntries(metrics.quality.confidence) as [name, count]}
              <li><span>{name}</span><strong>{count}</strong></li>
            {/each}
          </ul>
        </div>
        <div>
          <h4>Semantic role</h4>
          <ul class="metric-list">
            {#each sortedEntries(metrics.quality.semanticRole) as [name, count]}
              <li><span>{name}</span><strong>{count}</strong></li>
            {/each}
          </ul>
        </div>
      </div>
    </section>

    <section class="panel metric-section" style="margin-top: 16px;">
      <h3>Top Metadata Signals</h3>
      <div class="dist-grid two-up">
        <div>
          <h4>Namespaces</h4>
          <ul class="metric-list">
            {#each sortedEntries(metrics.context.namespaceCounts).slice(0, 12) as [name, count]}
              <li><span class="truncate" title={name}>{name}</span><strong>{count}</strong></li>
            {/each}
          </ul>
        </div>
        <div>
          <h4>Applied profiles</h4>
          <ul class="metric-list">
            {#each sortedEntries(metrics.context.appliedProfileCounts).slice(0, 12) as [name, count]}
              <li><span class="truncate" title={name}>{name}</span><strong>{count}</strong></li>
            {/each}
          </ul>
        </div>
      </div>
    </section>

  {/if}
</div>
