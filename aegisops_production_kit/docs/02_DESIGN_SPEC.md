# 02 — AegisOps Design Spec

**The HTML is the source of truth.** This doc makes the key facts explicit. Always
cross-check `design-reference/AegisOps_Workspace_v3.SOURCE_OF_TRUTH.html`. Build the UI as a
**pixel-exact** replica — production-grade does **not** mean restyled; it means the same
design backed by real systems.

---

## 1. Design tokens — copy VERBATIM into `frontend/app/globals.css`

The full token set + responsive media queries + keyframes + scrollbar styling is in
`design-reference/DESIGN_REFERENCE.tokens.css`. **Copy that whole file.** Core tokens:

```css
* { margin:0; padding:0; box-sizing:border-box; }

:root, [data-theme="dark"] {
  color-scheme: dark;
  --bg:#0a0a0c; --bg-elev:#0c0c0f; --bg-pop:#131319; --code-bg:#0a0a0d;
  --surface:rgba(255,255,255,.02); --surface-2:rgba(255,255,255,.03); --surface-3:rgba(255,255,255,.05);
  --border:rgba(255,255,255,.07); --border-2:rgba(255,255,255,.085); --border-3:rgba(255,255,255,.14);
  --text:#f4f5f7; --text-navactive:#e7e8f4; --text-2:#c4c7cf; --text-3:#9498a2; --text-4:#6b6f7a; --text-5:#52565f; --text-dim:#3a3e47;
  --code-comment:#5a5e68;
  --accent:#6366f1; --accent-strong:#4f46e5; --accent-2:#818cf8; --accent-3:#a5b4fc; --accent-fg:#dadcff; --accent-border:rgba(129,140,248,.3);
  --green:#34d399; --green-strong:#10b981; --on-green:#04140d;
  --amber:#fbbf24; --red:#f87171; --red-2:#fca5a5; --cyan:#22d3ee; --violet:#c084fc;
  --av-user-bg:#3a2f52; --av-user-fg:#d6c8f0; --av-org-bg:#27314a; --av-org-fg:#aebbd6;
}
[data-theme="light"] {
  color-scheme: light;
  --bg:#f5f6f8; --bg-elev:#ffffff; --bg-pop:#ffffff; --code-bg:#f3f5f8;
  --surface:rgba(15,23,42,.022); --surface-2:rgba(15,23,42,.04); --surface-3:rgba(15,23,42,.06);
  --border:rgba(15,23,42,.10); --border-2:rgba(15,23,42,.14); --border-3:rgba(15,23,42,.20);
  --text:#0f172a; --text-navactive:#312e81; --text-2:#334155; --text-3:#5b6573; --text-4:#8a93a3; --text-5:#aab2bf; --text-dim:#cbd2dc;
  --code-comment:#94a3b8;
  --accent:#4f46e5; --accent-strong:#4338ca; --accent-2:#6366f1; --accent-3:#4f46e5; --accent-fg:#4338ca; --accent-border:rgba(99,102,241,.3);
  --green:#059669; --green-strong:#047857; --on-green:#ffffff;
  --amber:#b45309; --red:#dc2626; --red-2:#b91c1c; --cyan:#0e7490; --violet:#7c3aed;
  --av-user-bg:#ede9fe; --av-user-fg:#6d28d9; --av-org-bg:#dbeafe; --av-org-fg:#1d4ed8;
}
html, body { height:100%; background:var(--bg); }
body { font-family:'IBM Plex Sans', system-ui, sans-serif; -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility; }
```

## 2. Fonts
- UI: **IBM Plex Sans** (400/500/600). Monospace: **IBM Plex Mono** (400) for Terraform diff,
  logs, trace span names, and `--code-bg` regions. Load via `next/font/google` (preferred) or
  the Google Fonts `<link>` set used in the source `helmet`.

## 3. Theme mechanics
Root carries `data-theme="dark|light"`. Three-way state dark/light/system; system resolves via
`prefers-color-scheme` and **updates live** on OS change (replicate the `matchMedia` listener).
Cycle order: dark → light → system → dark.

## 4. Animations / keyframes (in tokens file, copy verbatim)
`ao-blink, ao-spin, ao-pulse, ao-ring, ao-fadeup, ao-fadein, ao-stepin, ao-bar, ao-slidein`.
Use exactly where the template references them.

## 5. Responsive (copy verbatim; keep these ids/classes so media queries apply)
ids: `#ao-sidebar, #ao-panel, #ao-topnav, #ao-hamburger`; classes: `.ao-stat-grid,
.ao-summary-grid, .ao-chat-pad, .ao-composer-pad, .ao-module-pad, .ao-cloud-sel`.
- ≤860px: hamburger shows; sidebar = slide-in overlay (`[data-open="true"]`); artifact panel =
  fixed right drawer (≤440px); top-nav horizontal scroll; cloud selector hidden; grids 2-up.
- ≤460px: grids 1-up.

## 6. Component map (template region → React component)
Port from `DESIGN_REFERENCE.template.html`, preserving inline styles. Same list as the regions
in the source (comment banners): `LoginScreen, AppShell, Sidebar, TopNav (+ ContextMenu),
Workspace (ChatColumn: UserMessage, AiMessage, IntentChip, WorkflowChip,
ConfidentialityBadge, ConversationAnalysisTabs, ArtifactCard, ApprovalReferenceCompact,
FollowUpChips, FeedbackRow, ThinkingTimeline, StreamedText, Composer; ArtifactPanel:
ArtifactTabs, TimelineArtifact, ReasoningArtifact, TerraformArtifact, LogsArtifact,
MetricsArtifact, TracesArtifact, ReferencesArtifact, ApprovalsArtifact), ModuleView (+
IntegrationsGrid), CommandPalette, Overview`. Shared icon helpers port `icon()`, `navIcon()`,
`themeGlyph()` — **keep inline SVGs identical**.

> Note: the design has a `traces` tab labeled "Langfuse Traces" and an `Analysis/References`
> message tab (HLD/AC two-tab requirement). Implement **both** — the per-message Analysis tab
> for reasoning summary+citations, and the panel-level Traces tab for the Langfuse run trace.

## 7. Data the UI expects (shapes from `DESIGN_REFERENCE.logic.js`)
Treat every object in `renderVals()`/`moduleMeta()`/`workflowNodes()` etc. as the **API
response contract**. Your real backend returns the same field names/shapes so components bind
unchanged. Seed values become real DB rows (see backend + setup docs), not constants.

## 8. Color-by-value helpers (port exactly)
- `cloudColor`: AWS→amber, Azure→cyan, GCP→red, Kubernetes→accent-2, VMware→green.
- `modelColor`: Claude→amber, GPT/OpenAI→green, Gemini→cyan, Azure→accent-2, else violet.
- `workflowNodes` palettes: done/running/pending/queued/rejected/cancelled — match exactly.

## 9. Final visual QA (both themes + mobile, side-by-side vs source HTML)
Login, sidebar active states, every dropdown, theme cycle, command palette, full conversation
incl. two-tab + confidentiality + feedback, all 8 artifact tabs, approval approve/reject
states, all 7 modules, integrations grid health, notifications, mobile drawers. Pixel-diff if
possible. The bar: a stakeholder cannot tell the rebuilt app from the source HTML.
