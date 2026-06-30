
class Component extends DCLogic {
  state = {
    authed: false,
    theme: 'dark',          // dark | light | system
    systemDark: true,
    artifactOpen: true,
    mobileNavOpen: false,
    activeArtifact: 'timeline',
    timelineOpen: false,
    approval: 'pending',
    activeNav: 'workspace',
    input: '',
    messages: [],
    streaming: false,
    cmdkOpen: false,
    cmdkQuery: '',
    menu: null,             // org | cloud | model | role | notif | profile | theme | null
    org: 'Northwind Financial',
    env: 'Production',
    cloud: 'AWS',
    region: 'us-east-1',
    model: 'Claude Sonnet 4.5',
    role: 'Platform Admin',
    feedback: {},           // messageId -> 'up' | 'down'
    overviewOpen: true,
  };

  componentDidMount() {
    this._key = (e) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) { e.preventDefault(); if (this.state.authed) this.setState(s => ({ cmdkOpen: !s.cmdkOpen })); }
      if (e.key === 'Escape') this.setState({ cmdkOpen: false, menu: null });
    };
    window.addEventListener('keydown', this._key);
    if (typeof window !== 'undefined' && window.innerWidth <= 860) this.setState({ artifactOpen: false });
    if (window.matchMedia) {
      this._mq = window.matchMedia('(prefers-color-scheme: dark)');
      this.setState({ systemDark: this._mq.matches });
      this._mqL = (e) => this.setState({ systemDark: e.matches });
      this._mq.addEventListener ? this._mq.addEventListener('change', this._mqL) : this._mq.addListener(this._mqL);
    }
  }
  componentWillUnmount() {
    window.removeEventListener('keydown', this._key);
    if (this._mq) { this._mq.removeEventListener ? this._mq.removeEventListener('change', this._mqL) : this._mq.removeListener(this._mqL); }
    if (this._timer) clearInterval(this._timer); if (this._tl) clearInterval(this._tl);
  }

  resolvedTheme() { return this.state.theme === 'system' ? (this.state.systemDark ? 'dark' : 'light') : this.state.theme; }
  toggleMenu(m) { this.setState(s => ({ menu: s.menu === m ? null : m })); }
  themeGlyph(t) {
    if (t === 'light') return React.createElement('svg', { width: 14, height: 14, viewBox: '0 0 24 24', fill: 'none' }, React.createElement('circle', { cx: 12, cy: 12, r: 4, style: { stroke: 'currentColor' }, strokeWidth: 1.7 }), React.createElement('path', { d: 'M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4', style: { stroke: 'currentColor' }, strokeWidth: 1.7, strokeLinecap: 'round' }));
    return React.createElement('svg', { width: 14, height: 14, viewBox: '0 0 24 24', fill: 'none' }, React.createElement('path', { d: 'M20 14.5A8 8 0 0 1 9.5 4 7 7 0 1 0 20 14.5Z', style: { stroke: 'currentColor' }, strokeWidth: 1.7, strokeLinejoin: 'round' }));
  }
  cloudColor(n) { return ({ AWS: 'var(--amber)', Azure: 'var(--cyan)', GCP: 'var(--red)', Kubernetes: 'var(--accent-2)', VMware: 'var(--green)' })[n] || 'var(--accent-2)'; }
  modelColor(n) { if (/Claude/.test(n)) return 'var(--amber)'; if (/GPT|OpenAI/.test(n)) return 'var(--green)'; if (/Gemini/.test(n)) return 'var(--cyan)'; if (/Azure/.test(n)) return 'var(--accent-2)'; return 'var(--violet)'; }

  navStyle(key) {
    const active = this.state.activeNav === key;
    const base = 'display:flex; align-items:center; gap:11px; width:100%; padding:10px 9px; border-radius:9px; border:none; font-size:13px; font-weight:500; cursor:pointer; text-align:left; transition:background .12s;';
    if (active) return base + 'background:rgba(99,102,241,.12); color:var(--text-navactive); box-shadow:inset 2px 0 0 var(--accent-2);';
    return base + 'background:transparent; color:var(--text-3);';
  }
  tabStyle(key) {
    const active = this.state.activeArtifact === key;
    const base = 'padding:9px 13px; border:none; background:transparent; font-size:12.5px; font-weight:500; cursor:pointer; transition:color .12s; white-space:nowrap;';
    if (active) return base + 'color:var(--text); box-shadow:inset 0 -2px 0 var(--accent-2);';
    return base + 'color:var(--text-4);';
  }

  openArtifact(which) { this.setState({ artifactOpen: true, activeArtifact: which }); }

  icon(kind, color) {
    const c = color || 'var(--text-3)';
    if (kind === 'check') return React.createElement('svg', { width: 12, height: 12, viewBox: '0 0 24 24', fill: 'none' }, React.createElement('path', { d: 'm5 12 5 5 9-11', style: { stroke: c }, strokeWidth: 2.6, strokeLinecap: 'round', strokeLinejoin: 'round' }));
    if (kind === 'spin') return React.createElement('svg', { width: 13, height: 13, viewBox: '0 0 24 24', fill: 'none', style: { animation: 'ao-spin 1s linear infinite' } }, React.createElement('path', { d: 'M12 3a9 9 0 1 0 9 9', style: { stroke: c }, strokeWidth: 2.4, strokeLinecap: 'round' }));
    if (kind === 'x') return React.createElement('svg', { width: 11, height: 11, viewBox: '0 0 24 24', fill: 'none' }, React.createElement('path', { d: 'M6 6l12 12M18 6 6 18', style: { stroke: c }, strokeWidth: 2.4, strokeLinecap: 'round' }));
    return React.createElement('span', { style: { width: '6px', height: '6px', borderRadius: '99px', background: c } });
  }
  navIcon(d, color) { return React.createElement('svg', { width: 15, height: 15, viewBox: '0 0 24 24', fill: 'none' }, React.createElement('path', { d, style: { stroke: color || 'var(--text-3)' }, strokeWidth: 1.6, strokeLinecap: 'round', strokeLinejoin: 'round' })); }

  workflowNodes() {
    const approved = this.state.approval === 'approved';
    const rejected = this.state.approval === 'rejected';
    const defs = [
      { title: 'Router', detail: 'Classified → provisioning intent', time: '0.3s', status: 'done' },
      { title: 'Planner', detail: 'Decomposed into 4 sub-tasks', time: '0.6s', status: 'done' },
      { title: 'CloudOps Agent', detail: 'Discovered VPC, drafted plan', time: '3.4s', status: 'done' },
      { title: 'Policy Evaluation', detail: '6 of 6 checks passed', time: '0.4s', status: 'done' },
      { title: 'Human Approval', detail: rejected ? 'Rejected — halted' : (approved ? 'Approved by you' : 'Awaiting your decision'), time: approved ? '—' : (rejected ? '—' : '···'), status: rejected ? 'rejected' : (approved ? 'done' : 'pending') },
      { title: 'Terraform Apply', detail: approved ? 'Applying 14 resources…' : 'Queued', time: approved ? '···' : '—', status: approved ? 'running' : (rejected ? 'cancelled' : 'queued') },
      { title: 'Verification', detail: 'Pending apply', time: '—', status: rejected ? 'cancelled' : 'queued', last: true },
    ];
    const palette = {
      done: { bg: 'rgba(52,211,153,.13)', ring: 'var(--green)', titleColor: 'var(--text)', line: 'rgba(52,211,153,.4)', icon: this.icon('check', 'var(--green)') },
      running: { bg: 'rgba(129,140,248,.16)', ring: 'var(--accent-2)', titleColor: 'var(--text)', line: 'rgba(129,140,248,.4)', icon: this.icon('spin', 'var(--accent-2)') },
      pending: { bg: 'rgba(251,191,36,.16)', ring: 'var(--amber)', titleColor: 'var(--text)', line: 'var(--border-2)', icon: React.createElement('span', { style: { width: '7px', height: '7px', borderRadius: '99px', background: 'var(--amber)', animation: 'ao-pulse 1.4s infinite' } }) },
      queued: { bg: 'var(--surface-3)', ring: 'var(--border-3)', titleColor: 'var(--text-3)', line: 'var(--border-2)', icon: this.icon('dim', 'var(--text-5)') },
      rejected: { bg: 'rgba(248,113,113,.16)', ring: 'var(--red)', titleColor: 'var(--text)', line: 'rgba(248,113,113,.3)', icon: this.icon('x', 'var(--red)') },
      cancelled: { bg: 'var(--surface-2)', ring: 'var(--border-2)', titleColor: 'var(--text-5)', line: 'var(--border)', icon: this.icon('dim', 'var(--text-dim)') },
    };
    return defs.map(d => { const p = palette[d.status]; return { title: d.title, detail: d.detail, time: d.time, bg: p.bg, ring: p.ring, titleColor: p.titleColor, lineColor: p.line, icon: p.icon, hasLine: !d.last }; });
  }

  moduleMeta() {
    const m = {
      projects: { eyebrow: 'Workspaces', title: 'Projects', icon: 'P', desc: 'Every project carries its own infrastructure, conversations, memory and governance. Open one to work inside its context — Terraform, resources and incidents all surface here.', listTitle: 'Active projects',
        stats: [{ label: 'Projects', value: '12', delta: '+2 this quarter', deltaColor: 'var(--green)' }, { label: 'Cloud accounts', value: '7', delta: 'AWS · Azure · GCP', deltaColor: 'var(--text-3)' }, { label: 'Open conversations', value: '34', delta: '6 awaiting you', deltaColor: 'var(--amber)' }, { label: 'Monthly spend', value: '$48.2k', delta: '−4.1% vs last mo', deltaColor: 'var(--green)' }],
        rows: [{ dot: 'var(--red)', name: 'payments-platform', meta: 'Production · EKS provisioning in progress', value: '$18.4k/mo' }, { dot: 'var(--green)', name: 'orders-api', meta: 'Production · healthy · 8 replicas', value: '$9.1k/mo' }, { dot: 'var(--amber)', name: 'data-lakehouse', meta: 'Staging · drift detected on 2 resources', value: '$12.6k/mo' }, { dot: 'var(--green)', name: 'identity-service', meta: 'Production · healthy', value: '$5.3k/mo' }] },
      infrastructure: { eyebrow: 'CloudOps · multi-cloud', title: 'Infrastructure', icon: 'I', desc: 'Every resource the agents discover across AWS, Azure, GCP, Kubernetes and VMware — explorable as a live graph. Open any resource to inspect, plan, or remediate it conversationally.', listTitle: 'Tracked resources',
        stats: [{ label: 'Resources', value: '2,418', delta: '+312 (90d)', deltaColor: 'var(--green)' }, { label: 'Clusters', value: '8', delta: 'EKS · AKS · GKE', deltaColor: 'var(--text-3)' }, { label: 'Drift detected', value: '2', delta: 'data-lakehouse', deltaColor: 'var(--amber)' }, { label: 'Monthly spend', value: '$48.2k', delta: '−4.1% MoM', deltaColor: 'var(--green)' }],
        rows: [{ dot: 'var(--cyan)', name: 'payments-prod-use1', meta: 'EKS cluster · us-east-1 · 3 AZ', value: 'healthy' }, { dot: 'var(--green)', name: 'vpc-0a91c4f2', meta: 'Prod VPC · 6 subnets · 2 NAT', value: 'healthy' }, { dot: 'var(--amber)', name: 'data-lakehouse-rds', meta: 'Aurora PostgreSQL · drift', value: 'drift' }, { dot: 'var(--green)', name: 'orders-api', meta: 'ECS service · 8 tasks', value: 'healthy' }] },
      incidents: { eyebrow: 'SRE · incident management', title: 'Incidents', icon: '!', desc: 'AI-triaged incidents correlated with deploys, metrics and traces. Every incident opens a context graph with RCA, timeline and ServiceNow linkage — investigate it conversationally.', listTitle: 'Active & recent incidents',
        stats: [{ label: 'Open', value: '1', delta: 'P3 · checkout latency', deltaColor: 'var(--amber)' }, { label: 'MTTR (30d)', value: '14m', delta: '−6m vs prev', deltaColor: 'var(--green)' }, { label: 'Deploys today', value: '23', delta: '22 succeeded', deltaColor: 'var(--green)' }, { label: 'False positives', value: '6%', delta: 'AI-triaged', deltaColor: 'var(--text-3)' }],
        rows: [{ dot: 'var(--amber)', name: 'INC-2291 · checkout latency', meta: 'P3 · correlating with 14:20 deploy · SR linked CHG0040021', value: 'open' }, { dot: 'var(--green)', name: 'INC-2287 · eu-west-1 pod restarts', meta: '5 pods rescheduled · auto-resolved', value: 'resolved' }, { dot: 'var(--green)', name: 'INC-2280 · RDS failover staging', meta: 'RCA published · runbook updated', value: 'resolved' }, { dot: 'var(--cyan)', name: 'deploy orders-api v4.2.1', meta: 'GitHub Actions · prod · no incident', value: 'succeeded' }] },
      knowledge: { eyebrow: 'Semantic search', title: 'Knowledge Center', icon: 'K', desc: 'Runbooks, RCAs, architecture docs and conversation summaries — searchable semantically and cited automatically inside the AI Workspace.', listTitle: 'Recently used',
        stats: [{ label: 'Documents', value: '1,284', delta: '+47 this month', deltaColor: 'var(--green)' }, { label: 'Runbooks', value: '96', delta: '12 auto-generated', deltaColor: 'var(--text-3)' }, { label: 'RCAs', value: '38', delta: 'linked to incidents', deltaColor: 'var(--text-3)' }, { label: 'Avg relevance', value: '91%', delta: 'citation accuracy', deltaColor: 'var(--green)' }],
        rows: [{ dot: 'var(--accent-3)', name: 'EKS Production Hardening', meta: 'Runbook · cited 94% relevance', value: 'updated 2d' }, { dot: 'var(--cyan)', name: 'Payments Platform Architecture v3', meta: 'Design doc', value: 'updated 1w' }, { dot: 'var(--green)', name: 'RCA: checkout outage 06-19', meta: 'Root cause analysis', value: '8d ago' }, { dot: 'var(--violet)', name: 'Incident response playbook', meta: 'SOP · on-call', value: 'updated 3w' }] },
      analytics: { eyebrow: 'Executive view', title: 'Analytics', icon: 'A', desc: 'Engineering, SRE, DevOps and cloud KPIs in one place. Every chart is explainable — ask the AI why a number moved and get a grounded answer.', listTitle: 'Key metrics',
        stats: [{ label: 'Cloud spend (mo)', value: '$48.2k', delta: '−4.1% MoM', deltaColor: 'var(--green)' }, { label: 'Deploy frequency', value: '23/day', delta: '+18% QoQ', deltaColor: 'var(--green)' }, { label: 'Change failure', value: '2.1%', delta: 'elite tier', deltaColor: 'var(--green)' }, { label: 'Agent success', value: '97.4%', delta: 'across 1.2k runs', deltaColor: 'var(--text-3)' }],
        rows: [{ dot: 'var(--green)', name: 'Infrastructure growth', meta: '+312 resources tracked (90d)', value: '↑ 12%' }, { dot: 'var(--amber)', name: 'Cost by environment', meta: 'Production 64% · Staging 21%', value: '$48.2k' }, { dot: 'var(--cyan)', name: 'MTBF', meta: 'Mean time between failures', value: '41 days' }, { dot: 'var(--accent-3)', name: 'AI productivity', meta: 'Tasks completed via AI', value: '8,140' }] },
      admin: { eyebrow: 'Governance & identity', title: 'Administration', icon: 'S', desc: 'Organizations, RBAC, approval policies, audit and MCP servers. Governance recommendations are surfaced by the AI rather than buried in config screens.', listTitle: 'Governance overview',
        stats: [{ label: 'Members', value: '184', delta: '12 teams', deltaColor: 'var(--text-3)' }, { label: 'Approval policies', value: '14', delta: '3 require review', deltaColor: 'var(--amber)' }, { label: 'Audit events (24h)', value: '6,402', delta: 'all signed', deltaColor: 'var(--green)' }, { label: 'MCP servers', value: '9', delta: '9 healthy', deltaColor: 'var(--green)' }],
        rows: [{ dot: 'var(--green)', name: 'prod-change-control', meta: 'Approval policy · 6 checks', value: 'active' }, { dot: 'var(--amber)', name: 'Permission review', meta: '3 over-privileged roles flagged by AI', value: 'review' }, { dot: 'var(--green)', name: 'Keycloak SSO', meta: 'OIDC · Azure AD · MFA enforced', value: 'healthy' }, { dot: 'var(--cyan)', name: 'terraform-mcp', meta: 'MCP server · 142 tools · 38ms', value: 'healthy' }] },
      settings: { eyebrow: 'Personal', title: 'Profile & Settings', icon: '⚙', desc: 'Your preferences, notification rules, connected accounts, and personal guardrails. AegisOps adapts its behavior to the defaults you set here.', listTitle: 'Preferences',
        stats: [{ label: 'Theme', value: 'Dark', delta: 'system default', deltaColor: 'var(--text-3)' }, { label: 'Approval mode', value: 'Required', delta: 'for production', deltaColor: 'var(--amber)' }, { label: 'Cost alert', value: '$500', delta: 'per change', deltaColor: 'var(--text-3)' }, { label: 'Sessions', value: '3', delta: 'active devices', deltaColor: 'var(--green)' }],
        rows: [{ dot: 'var(--green)', name: 'Notification rules', meta: 'Slack · email · approval requests', value: 'configured' }, { dot: 'var(--cyan)', name: 'Connected accounts', meta: 'GitHub · AWS · Azure AD', value: '3 linked' }, { dot: 'var(--accent-3)', name: 'Default agent mode', meta: 'Approval required in production', value: 'enabled' }, { dot: 'var(--amber)', name: 'Cost guardrail', meta: 'Flag changes above $500/mo', value: '$500' }] },
    };
    return m[this.state.activeNav] || m.projects;
  }

  sendText(text) {
    const t = (text || '').trim();
    if (!t || this.state.streaming) return;
    const aiId = 'ai' + Date.now();
    const steps = [
      { label: 'Understood intent' }, { label: 'Retrieved memory & context' },
      { label: 'Queried AWS · us-east-1' }, { label: 'Searched knowledge base' },
      { label: 'Evaluated policies' }, { label: 'Composed response' },
    ];
    this.setState(s => ({
      input: '', streaming: true,
      messages: [...s.messages,
        { id: 'u' + Date.now(), isUser: true, text: t },
        { id: aiId, isAI: true, text: '', streaming: true, showTimeline: true, stepIdx: 0, steps },
      ],
    }));

    if (this._tl) clearInterval(this._tl);
    let idx = 0;
    this._tl = setInterval(() => {
      idx++;
      this.setState(s => ({ messages: s.messages.map(m => m.id === aiId ? { ...m, stepIdx: idx } : m) }));
      if (idx >= steps.length) {
        clearInterval(this._tl); this._tl = null;
        setTimeout(() => this.streamReply(aiId), 350);
      }
    }, 480);
  }

  streamReply(aiId) {
    this.setState(s => ({ messages: s.messages.map(m => m.id === aiId ? { ...m, showTimeline: false } : m) }));
    const reply = "I pulled the live state for payments-platform in Production and correlated it against recent deploys and your knowledge base. Across orders-api and payments everything sits within SLO, with one P3 worth a look: checkout p95 latency rose ~12% right after the 14:20 deploy. I can open a root-cause investigation, draft a Terraform plan, or pull the relevant runbook — just say the word.";
    const words = reply.split(' ');
    const speed = Number(this.props.streamSpeed) || 24;
    let i = 0;
    if (this._timer) clearInterval(this._timer);
    this._timer = setInterval(() => {
      i++;
      const done = i >= words.length;
      this.setState(s => ({ messages: s.messages.map(m => m.id === aiId ? { ...m, text: words.slice(0, i).join(' '), streaming: !done } : m), streaming: !done }));
      if (done) { clearInterval(this._timer); this._timer = null; }
    }, speed);
  }

  renderVals() {
    const s = this.state;
    const requiresApproval = (this.props.agentMode ?? 'Approval required') !== 'Autonomous';
    const meta = this.moduleMeta();
    const isWorkspace = s.activeNav === 'workspace';
    const panelVisible = s.artifactOpen && isWorkspace;

    const liveMessages = s.messages.map(m => {
      if (!m.isAI || !m.steps) return m;
      const timeline = m.steps.map((st, i) => {
        const done = i < m.stepIdx, active = i === m.stepIdx;
        return { label: st.label, isDone: done, isActive: active, isPending: !done && !active, color: done ? 'var(--text-2)' : (active ? 'var(--text)' : 'var(--text-4)') };
      });
      return { ...m, timeline };
    });

    const navTo = (nav) => () => this.setState({ activeNav: nav, cmdkOpen: false, mobileNavOpen: false });

    const opt = (field, value, sub) => ({ label: value, sub: sub || '', active: s[field] === value, run: () => this.setState({ [field]: value, menu: null }) });
    const fb = s.feedback || {};

    return {
      // ── auth ──
      authed: s.authed, notAuthed: !s.authed,
      resolvedTheme: this.resolvedTheme(),
      signIn: () => this.setState({ authed: true }),
      signOut: () => this.setState({ authed: false, menu: null }),

      // ── theme ──
      cycleTheme: () => this.setState(st => ({ theme: st.theme === 'dark' ? 'light' : (st.theme === 'light' ? 'system' : 'dark') })),
      themeLabel: ({ dark: 'Dark', light: 'Light', system: 'System' })[s.theme],
      themeIcon: this.themeGlyph(this.resolvedTheme()),
      menuTheme: s.menu === 'theme',
      toggleThemeMenu: () => this.toggleMenu('theme'),
      setThemeDark: () => this.setState({ theme: 'dark', menu: null }),
      setThemeLight: () => this.setState({ theme: 'light', menu: null }),
      setThemeSystem: () => this.setState({ theme: 'system', menu: null }),
      isThemeDark: s.theme === 'dark', isThemeLight: s.theme === 'light', isThemeSystem: s.theme === 'system',

      // ── top-nav context selectors ──
      org: s.org, env: s.env, cloud: s.cloud, region: s.region, role: s.role,
      cloudDot: this.cloudColor(s.cloud),
      modelDot: this.modelColor(s.model),
      menuOrg: s.menu === 'org', menuEnv: s.menu === 'env', menuCloud: s.menu === 'cloud', menuRegion: s.menu === 'region', menuModel: s.menu === 'model', menuRole: s.menu === 'role', menuNotif: s.menu === 'notif', menuProfile: s.menu === 'profile',
      toggleOrg: () => this.toggleMenu('org'), toggleEnv: () => this.toggleMenu('env'), toggleCloud: () => this.toggleMenu('cloud'), toggleRegion: () => this.toggleMenu('region'), toggleModelMenu: () => this.toggleMenu('model'), toggleRole: () => this.toggleMenu('role'), toggleNotif: () => this.toggleMenu('notif'), toggleProfile: () => this.toggleMenu('profile'),
      closeMenus: () => this.setState({ menu: null }),
      mobileNavOpen: s.mobileNavOpen,
      toggleMobileNav: () => this.setState(st => ({ mobileNavOpen: !st.mobileNavOpen })),
      closeMobileNav: () => this.setState({ mobileNavOpen: false }),
      anyMenu: s.menu !== null,
      orgOptions: [opt('org', 'Northwind Financial', 'Enterprise · 184 members'), opt('org', 'Acme Retail', 'Business · 42 members'), opt('org', 'Globex Health', 'Enterprise · 96 members')],
      envOptions: [opt('env', 'Production'), opt('env', 'Staging'), opt('env', 'Development'), opt('env', 'Sandbox')],
      cloudOptions: [opt('cloud', 'AWS', '12 accounts'), opt('cloud', 'Azure', '4 subscriptions'), opt('cloud', 'GCP', '2 projects'), opt('cloud', 'Kubernetes', '8 clusters'), opt('cloud', 'VMware', 'vSphere')].map(o => ({ ...o, dot: this.cloudColor(o.label) })),
      regionOptions: [opt('region', 'us-east-1', 'N. Virginia'), opt('region', 'us-west-2', 'Oregon'), opt('region', 'eu-west-1', 'Ireland'), opt('region', 'ap-south-1', 'Mumbai')],
      modelOptions: [opt('model', 'Claude Sonnet 4.5', 'Anthropic · default'), opt('model', 'Claude Opus 4.1', 'Anthropic · deep reasoning'), opt('model', 'GPT-4o', 'OpenAI'), opt('model', 'Gemini 2.5 Pro', 'Google'), opt('model', 'Azure OpenAI', 'GPT-4o · private'), opt('model', 'Llama 3.1 70B', 'Ollama · self-hosted')].map(o => ({ ...o, dot: this.modelColor(o.label) })),
      roleOptions: [opt('role', 'Platform Admin', 'Full control'), opt('role', 'Org Admin', 'Org-wide'), opt('role', 'Cloud Architect', 'Design + plan'), opt('role', 'DevOps Engineer', 'Deploy + run'), opt('role', 'SRE', 'Incidents + reliability'), opt('role', 'Developer', 'Initiate requests'), opt('role', 'Auditor', 'Read + audit'), opt('role', 'Read Only', 'View only')],
      notifItems: [
        { title: 'Approval requested · EKS production plan', time: '2m', color: 'var(--amber)' },
        { title: 'INC-2291 checkout latency · assigned to you', time: '14m', color: 'var(--red)' },
        { title: 'deploy orders-api v4.2.1 succeeded', time: '1h', color: 'var(--green)' },
        { title: 'Drift detected · data-lakehouse staging', time: '3h', color: 'var(--cyan)' },
      ],

      // ── feedback (seed message) ──
      seedFbUp: fb.seed === 'up', seedFbDown: fb.seed === 'down',
      seedUpBg: fb.seed === 'up' ? 'rgba(52,211,153,.12)' : 'var(--surface-2)',
      seedUpBorder: fb.seed === 'up' ? 'rgba(52,211,153,.4)' : 'var(--border-2)',
      seedUpColor: fb.seed === 'up' ? 'var(--green)' : 'var(--text-3)',
      seedDownBg: fb.seed === 'down' ? 'rgba(248,113,113,.1)' : 'var(--surface-2)',
      seedDownBorder: fb.seed === 'down' ? 'rgba(248,113,113,.4)' : 'var(--border-2)',
      seedDownColor: fb.seed === 'down' ? 'var(--red-2)' : 'var(--text-3)',
      seedThumbUp: () => this.setState(st => ({ feedback: { ...st.feedback, seed: st.feedback.seed === 'up' ? null : 'up' } })),
      seedThumbDown: () => this.setState(st => ({ feedback: { ...st.feedback, seed: st.feedback.seed === 'down' ? null : 'down' } })),

      // ── overview cards ──
      overviewOpen: s.overviewOpen,
      overviewChevron: s.overviewOpen ? 'rotate(180deg)' : 'rotate(0deg)',
      toggleOverview: () => this.setState(st => ({ overviewOpen: !st.overviewOpen })),
      summaryCards: [
        { label: 'Infrastructure Health', value: '99.97%', sub: 'SLO on track', accent: 'var(--green)', glyph: 'health' },
        { label: 'Active Executions', value: '3', sub: '1 applying · 2 queued', accent: 'var(--accent-2)', glyph: 'exec' },
        { label: 'Pending Approvals', value: '2', sub: '1 assigned to you', accent: 'var(--amber)', glyph: 'approve' },
        { label: 'Open Incidents', value: '1', sub: 'P3 · checkout latency', accent: 'var(--red)', glyph: 'incident' },
        { label: 'Cost Summary', value: '$48.2k', sub: '−4.1% vs last month', accent: 'var(--cyan)', glyph: 'cost' },
        { label: 'Recent Activity', value: '23', sub: 'actions in last 24h', accent: 'var(--violet)', glyph: 'activity' },
      ],

      // artifact panel
      artifactPanelVisible: panelVisible,
      artifactOpen: s.artifactOpen,
      toggleArtifact: () => this.setState(st => ({ artifactOpen: !st.artifactOpen })),
      closeArtifact: () => this.setState({ artifactOpen: false }),
      artifactBtnBg: s.artifactOpen ? 'rgba(99,102,241,.14)' : 'var(--surface-2)',
      artifactBtnColor: s.artifactOpen ? 'var(--accent-3)' : 'var(--text-3)',
      chatMaxWidth: panelVisible ? '100%' : '780px',

      activeArtifact: s.activeArtifact,
      artifactTitle: ({ timeline: 'Workflow Timeline', reasoning: 'Agent Reasoning', terraform: 'Terraform Plan', logs: 'Execution Logs', metrics: 'Metrics', traces: 'Langfuse Traces', references: 'References', approvals: 'Approvals' })[s.activeArtifact] || 'Workflow Timeline',
      artifactTabs: [
        ['timeline', 'Timeline'], ['reasoning', 'Reasoning'], ['terraform', 'Terraform'], ['logs', 'Logs'],
        ['metrics', 'Metrics'], ['traces', 'Traces'], ['references', 'References'], ['approvals', 'Approvals'],
      ].map(([key, label]) => ({ key, label, style: this.tabStyle(key), run: () => this.openArtifact(key) })),
      isTimelineTab: s.activeArtifact === 'timeline',
      isReasoningTab: s.activeArtifact === 'reasoning',
      isTfTab: s.activeArtifact === 'terraform',
      isLogsTab: s.activeArtifact === 'logs',
      isMetricsTab: s.activeArtifact === 'metrics',
      isTracesTab: s.activeArtifact === 'traces',
      isReferencesTab: s.activeArtifact === 'references',
      isApprovalsTab: s.activeArtifact === 'approvals',
      openTerraform: () => this.openArtifact('terraform'),
      openCost: () => this.openArtifact('metrics'),
      openWorkflow: () => this.openArtifact('timeline'),

      // artifact card active highlighting
      tfCardBorder: s.artifactOpen && s.activeArtifact === 'terraform' ? 'rgba(129,140,248,.45)' : 'var(--border-2)',
      tfCardBg: s.artifactOpen && s.activeArtifact === 'terraform' ? 'rgba(99,102,241,.06)' : 'var(--surface)',
      costCardBorder: s.artifactOpen && s.activeArtifact === 'metrics' ? 'rgba(52,211,153,.4)' : 'var(--border-2)',
      costCardBg: s.artifactOpen && s.activeArtifact === 'metrics' ? 'rgba(52,211,153,.05)' : 'var(--surface)',
      openGraph: () => this.setState({ activeNav: 'infrastructure', menu: null }),

      // timeline (seed)
      timelineOpen: s.timelineOpen,
      toggleTimeline: () => this.setState(st => ({ timelineOpen: !st.timelineOpen })),
      timelineChevron: s.timelineOpen ? 'rotate(180deg)' : 'rotate(0deg)',
      seedTimeline: [
        { label: 'Understood intent', detail: 'provision EKS · reuse VPC · 3 AZ', time: '0.3s' },
        { label: 'Retrieved memory', detail: 'approved module · naming convention', time: '0.2s' },
        { label: 'Queried AWS', detail: 'us-east-1 · found prod VPC', time: '1.2s' },
        { label: 'Searched knowledge', detail: '2 runbooks · 1 design doc', time: '0.8s' },
        { label: 'Selected module', detail: 'terraform-aws-eks v20.8', time: '0.1s' },
        { label: 'Checked policies', detail: '6 of 6 passed', time: '0.4s' },
        { label: 'Ran terraform plan', detail: '+14 ~2 -0', time: '3.4s' },
        { label: 'Estimated cost', detail: '+$312/mo · within guardrail', time: '0.2s' },
        { label: 'Composed artifacts', detail: 'plan + cost analysis', time: '0.3s' },
        { label: 'Awaiting approval', detail: 'production change', time: '···' },
      ],

      // approval
      showApprovalPending: s.approval === 'pending',
      showApproved: s.approval === 'approved',
      showRejected: s.approval === 'rejected',
      approve: () => this.setState({ approval: 'approved', activeArtifact: 'timeline', artifactOpen: true }),
      reject: () => this.setState({ approval: 'rejected' }),
      workflowNodes: this.workflowNodes(),
      traceSpans: [
        { name: 'intent.classify', dur: '0.3s', dot: 'var(--green)', indent: '0px', tokens: '1.2k tok' },
        { name: 'agent.route', dur: '0.1s', dot: 'var(--green)', indent: '0px', tokens: '' },
        { name: 'workflow.plan', dur: '0.6s', dot: 'var(--green)', indent: '12px', tokens: '2.1k tok' },
        { name: 'rag.retrieve', dur: '0.8s', dot: 'var(--green)', indent: '12px', tokens: '' },
        { name: 'tool.terraform_plan', dur: '3.4s', dot: 'var(--green)', indent: '24px', tokens: '' },
        { name: 'policy.evaluate', dur: '0.4s', dot: 'var(--green)', indent: '12px', tokens: '' },
        { name: 'approval.gate', dur: '···', dot: 'var(--amber)', indent: '0px', tokens: '' },
      ],
      logLines: [
        { ts: '14:23:41', lvl: 'INFO', lvlColor: 'var(--cyan)', msg: 'intent classified: provisioning (0.98)' },
        { ts: '14:23:41', lvl: 'INFO', lvlColor: 'var(--cyan)', msg: 'routed -> CloudOps agent' },
        { ts: '14:23:42', lvl: 'INFO', lvlColor: 'var(--cyan)', msg: 'discovered vpc-0a91c4f2 · 6 subnets' },
        { ts: '14:23:43', lvl: 'DEBUG', lvlColor: 'var(--text-4)', msg: 'rag: 2 runbooks · 1 design doc' },
        { ts: '14:23:44', lvl: 'INFO', lvlColor: 'var(--cyan)', msg: 'terraform init · backend s3' },
        { ts: '14:23:47', lvl: 'INFO', lvlColor: 'var(--cyan)', msg: 'plan: +14 ~2 -0' },
        { ts: '14:23:47', lvl: 'OK', lvlColor: 'var(--green)', msg: 'policy: 6/6 passed' },
        { ts: '14:23:48', lvl: 'WARN', lvlColor: 'var(--amber)', msg: 'awaiting human approval' },
      ],
      reasoningCards: [
        { title: 'Interpreted intent', conf: '98%', body: 'Provision EKS · reuse existing prod VPC · 3 AZ · managed node groups. No destructive operation detected.' },
        { title: 'Routing decision', conf: '', body: 'Routed to CloudOps agent over DevOps/SRE — request is infrastructure provisioning, not deployment or incident.' },
        { title: 'Workflow selection', conf: '', body: 'Selected eks-provision v3 with terraform-aws-eks v20.8 (org-approved). Rejected raw HCL — violates module policy.' },
        { title: 'Risk evaluation', conf: '', body: 'Production + new IAM + $312/mo → medium risk. prod-change-control requires human approval before apply.' },
        { title: 'Memory applied', conf: '', body: 'Reused naming convention and the $500/mo cost guardrail from prior context.' },
      ],
      workflowElapsed: s.approval === 'approved' ? 'running · 9.1s' : (s.approval === 'rejected' ? 'halted · 4.7s' : 'paused · 4.7s'),

      // nav
      goWorkspace: navTo('workspace'), goProjects: navTo('projects'), goInfrastructure: navTo('infrastructure'), goIncidents: navTo('incidents'),
      goKnowledge: navTo('knowledge'), goAnalytics: navTo('analytics'), goAdmin: navTo('admin'), goSettings: navTo('settings'),
      navWorkspaceStyle: this.navStyle('workspace'), navProjectsStyle: this.navStyle('projects'), navInfrastructureStyle: this.navStyle('infrastructure'), navIncidentsStyle: this.navStyle('incidents'),
      navKnowledgeStyle: this.navStyle('knowledge'), navAnalyticsStyle: this.navStyle('analytics'), navAdminStyle: this.navStyle('admin'), navSettingsStyle: this.navStyle('settings'),
      isWorkspace, isModule: !isWorkspace,
      isAdmin: s.activeNav === 'admin',
      integrations: [
        { name: 'Keycloak', cat: 'Identity · SSO/SAML', mark: 'K', color: 'var(--accent-3)', status: 'connected', statusColor: 'var(--green)' },
        { name: 'LangGraph', cat: 'Agent orchestration', mark: 'LG', color: 'var(--accent-2)', status: 'active', statusColor: 'var(--green)' },
        { name: 'Langfuse', cat: 'LLM observability', mark: 'Lf', color: 'var(--cyan)', status: 'tracing', statusColor: 'var(--green)' },
        { name: 'OpenTelemetry', cat: 'Traces · metrics', mark: 'OT', color: 'var(--violet)', status: 'connected', statusColor: 'var(--green)' },
        { name: 'Prometheus', cat: 'Metrics', mark: 'Pr', color: 'var(--amber)', status: 'scraping', statusColor: 'var(--green)' },
        { name: 'Grafana', cat: 'Dashboards', mark: 'Gf', color: 'var(--amber)', status: 'connected', statusColor: 'var(--green)' },
        { name: 'PostgreSQL', cat: 'Primary datastore', mark: 'Pg', color: 'var(--cyan)', status: 'healthy', statusColor: 'var(--green)' },
        { name: 'Redis', cat: 'Cache · queues', mark: 'Rd', color: 'var(--red)', status: 'healthy', statusColor: 'var(--green)' },
        { name: 'Neo4j', cat: 'Context graph', mark: 'N4', color: 'var(--green)', status: 'connected', statusColor: 'var(--green)' },
        { name: 'Terraform', cat: 'Provisioning', mark: 'Tf', color: 'var(--accent-2)', status: 'connected', statusColor: 'var(--green)' },
        { name: 'Ansible', cat: 'Configuration', mark: 'An', color: 'var(--red)', status: 'connected', statusColor: 'var(--green)' },
        { name: 'GitHub', cat: 'SCM · Actions', mark: 'Gh', color: 'var(--text-2)', status: 'connected', statusColor: 'var(--green)' },
        { name: 'ServiceNow', cat: 'ITSM · SR/CR/INC', mark: 'SN', color: 'var(--green)', status: 'syncing', statusColor: 'var(--green)' },
      ],
      moduleEyebrow: meta.eyebrow, moduleTitle: meta.title, moduleDesc: meta.desc, moduleIcon: meta.icon,
      moduleListTitle: meta.listTitle, moduleStats: meta.stats, moduleRows: meta.rows,

      // composer
      input: s.input,
      onInput: (e) => this.setState({ input: e.target.value }),
      onKey: (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this.sendText(this.state.input); } },
      onSend: () => this.sendText(this.state.input),
      sendBg: s.input.trim() && !s.streaming ? 'var(--accent)' : 'var(--border-2)',
      sendCursor: s.input.trim() && !s.streaming ? 'pointer' : 'default',
      useSuggestion0: () => this.sendText('Show every database running in Production'),
      useSuggestion1: () => this.sendText('Why did checkout latency spike after the 14:20 deploy?'),
      useSuggestion2: () => this.sendText('Compare staging and production infrastructure'),
      newChat: () => this.setState({ messages: [], input: '', activeNav: 'workspace' }),
      messages: liveMessages,
      modelLabel: s.model,
      agentModeLabel: requiresApproval ? 'Approval required' : 'Autonomous mode',

      // command palette
      cmdkOpen: s.cmdkOpen,
      openCmdk: () => this.setState({ cmdkOpen: true }),
      closeCmdk: () => this.setState({ cmdkOpen: false }),
      stop: (e) => { e.stopPropagation(); },
      cmdkQuery: s.cmdkQuery,
      onCmdkInput: (e) => this.setState({ cmdkQuery: e.target.value }),
      cmdkActions: [
        { label: 'New conversation', hint: '⌘N', icon: this.navIcon('M12 5v14M5 12h14', 'var(--accent-3)'), run: () => this.setState({ messages: [], input: '', activeNav: 'workspace', cmdkOpen: false }) },
        { label: 'Open Terraform plan', hint: 'artifact', icon: this.navIcon('m3 8 9-5 9 5-9 5-9-5Z', 'var(--accent-3)'), run: () => this.setState({ artifactOpen: true, activeArtifact: 'terraform', cmdkOpen: false }) },
        { label: 'View workflow timeline', hint: 'artifact', icon: this.navIcon('M6 6 18 9M9 18 8 8', 'var(--accent-3)'), run: () => this.setState({ artifactOpen: true, activeArtifact: 'timeline', cmdkOpen: false }) },
        { label: 'Approve & apply plan', hint: 'action', icon: this.navIcon('m5 12 5 5 9-11', 'var(--green)'), run: () => this.setState({ approval: 'approved', activeArtifact: 'timeline', artifactOpen: true, cmdkOpen: false }) },
      ],
      cmdkNav: [
        { label: 'AI Workspace', icon: this.navIcon('m12 3 1.9 4.6L18.5 9l-3.4 3 .9 4.8L12 14.6 7.9 16.8l.9-4.8L5.5 9l4.6-1.4L12 3Z'), run: navTo('workspace') },
        { label: 'Projects', icon: this.navIcon('M3.5 9h17M8 5V3.5M16 5V3.5'), run: navTo('projects') },
        { label: 'Infrastructure', icon: this.navIcon('M3 12h4l2.5-6 5 13 2.5-7H21'), run: navTo('infrastructure') },
        { label: 'Analytics', icon: this.navIcon('M4 20V4M8 18v-6M14 18V9M20 18V6'), run: navTo('analytics') },
      ],
    };
  }
}
