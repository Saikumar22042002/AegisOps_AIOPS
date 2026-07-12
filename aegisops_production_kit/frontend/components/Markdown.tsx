"use client";

// Assistant-message markdown renderer (Phase 8 / N-04) — ChatGPT/Claude-grade formatting with
// the design system's tokens. Fenced code gets a copy control. User messages stay plain text
// (never rendered as markdown — no injection surface).

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

function CodeBlock({ children, className }: { children?: React.ReactNode; className?: string }) {
  const [copied, setCopied] = useState(false);
  const text = String(children ?? "").replace(/\n$/, "");
  const lang = (className ?? "").replace("language-", "");
  return (
    <div style={{ position: "relative", margin: "10px 0" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between",
                    background: "var(--surface-3)", border: "1px solid var(--border)", borderBottom: "none",
                    borderRadius: "10px 10px 0 0", padding: "5px 12px" }}>
        <span style={{ fontSize: 10.5, color: "var(--text-4)", fontFamily: "'IBM Plex Mono',monospace" }}>{lang || "code"}</span>
        <button
          aria-label="Copy code"
          onClick={() => { void navigator.clipboard?.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1500); }}
          style={{ border: "none", background: "transparent", color: copied ? "var(--green)" : "var(--text-4)",
                   fontSize: 10.5, cursor: "pointer", fontFamily: "'IBM Plex Mono',monospace" }}>
          {copied ? "copied" : "copy"}
        </button>
      </div>
      <pre style={{ margin: 0, padding: "11px 13px", background: "var(--surface)", border: "1px solid var(--border)",
                    borderRadius: "0 0 10px 10px", overflowX: "auto" }}>
        <code style={{ fontFamily: "'IBM Plex Mono',monospace", fontSize: 12.5, color: "var(--text-2)", background: "none" }}>{text}</code>
      </pre>
    </div>
  );
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="ao-md" style={{ fontSize: 15, color: "var(--text-2)", lineHeight: 1.78 }}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: ({ children }) => <div style={{ fontSize: 17, fontWeight: 600, color: "var(--text)", margin: "14px 0 6px" }}>{children}</div>,
          h2: ({ children }) => <div style={{ fontSize: 16, fontWeight: 600, color: "var(--text)", margin: "13px 0 5px" }}>{children}</div>,
          h3: ({ children }) => <div style={{ fontSize: 15, fontWeight: 600, color: "var(--text)", margin: "12px 0 4px" }}>{children}</div>,
          p: ({ children }) => <p style={{ margin: "6px 0" }}>{children}</p>,
          strong: ({ children }) => <strong style={{ color: "var(--text)", fontWeight: 600 }}>{children}</strong>,
          ul: ({ children }) => <ul style={{ margin: "6px 0", paddingLeft: 22 }}>{children}</ul>,
          ol: ({ children }) => <ol style={{ margin: "6px 0", paddingLeft: 22 }}>{children}</ol>,
          li: ({ children }) => <li style={{ margin: "3px 0" }}>{children}</li>,
          a: ({ children, href }) => (
            <a href={href} target="_blank" rel="noreferrer" style={{ color: "var(--accent-2)", textDecoration: "underline" }}>{children}</a>
          ),
          table: ({ children }) => (
            <div style={{ overflowX: "auto", margin: "8px 0" }}>
              <table style={{ borderCollapse: "collapse", fontSize: 13 }}>{children}</table>
            </div>
          ),
          th: ({ children }) => <th style={{ border: "1px solid var(--border-2)", padding: "5px 10px", color: "var(--text)", textAlign: "left" }}>{children}</th>,
          td: ({ children }) => <td style={{ border: "1px solid var(--border-2)", padding: "5px 10px" }}>{children}</td>,
          code: ({ className, children, ...props }) => {
            const inline = !(className ?? "").startsWith("language-") && !String(children).includes("\n");
            if (inline) {
              return (
                <code style={{ fontFamily: "'IBM Plex Mono',monospace", fontSize: 12.5, color: "var(--accent-3)",
                               background: "var(--surface-2)", border: "1px solid var(--border)", borderRadius: 5,
                               padding: "1px 5px" }} {...props}>{children}</code>
              );
            }
            return <CodeBlock className={className}>{children}</CodeBlock>;
          },
          pre: ({ children }) => <>{children}</>, // CodeBlock supplies its own <pre>
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
