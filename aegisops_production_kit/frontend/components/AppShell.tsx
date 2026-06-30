"use client";

import { useEffect } from "react";

import { useUI } from "../lib/store";
import { ArtifactPanel } from "./ArtifactPanel";
import { CommandPalette } from "./CommandPalette";
import { ModuleView } from "./ModuleView";
import { Sidebar } from "./Sidebar";
import { TopNav } from "./TopNav";
import { Workspace } from "./Workspace";

export function AppShell() {
  const activeNav = useUI((s) => s.activeNav);
  const artifactOpen = useUI((s) => s.artifactOpen);
  const mobileNavOpen = useUI((s) => s.mobileNavOpen);
  const closeMobileNav = useUI((s) => s.closeMobileNav);

  // ⌘K / Ctrl-K toggles the palette; Escape closes palette + open menus.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        const s = useUI.getState();
        s.cmdkOpen ? s.closeCmdk() : s.openCmdk();
      }
      if (e.key === "Escape") {
        useUI.getState().closeCmdk();
        useUI.getState().closeMenus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // On small screens the artifact panel starts closed (matches the design).
  useEffect(() => {
    if (typeof window !== "undefined" && window.innerWidth <= 860) {
      useUI.getState().closeArtifact();
    }
  }, []);

  // Load the real conversation list + nav counts, and restore the last open thread on reload.
  useEffect(() => {
    void useUI.getState().restoreLast();
  }, []);

  const isWorkspace = activeNav === "workspace";

  return (
    <div style={{ display: "flex", height: "100%", width: "100%" }}>
      <Sidebar />
      {mobileNavOpen && (
        <div onClick={closeMobileNav} style={{ position: "fixed", inset: 0, zIndex: 79, background: "rgba(0,0,0,.5)", animation: "ao-fadein .15s ease" }} />
      )}
      <main style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, height: "100%", background: "var(--bg)" }}>
        <TopNav />
        <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
          {isWorkspace ? <Workspace /> : <ModuleView />}
          {isWorkspace && artifactOpen && <ArtifactPanel />}
        </div>
      </main>
      <CommandPalette />
    </div>
  );
}
