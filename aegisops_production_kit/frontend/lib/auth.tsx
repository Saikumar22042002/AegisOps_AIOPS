"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";

import { api, API_BASE, ApiError } from "./api";
import type { User } from "./types";

interface AuthState {
  user: User | null;
  loading: boolean;
  error: string | null;
  login: (email: string, password: string) => Promise<void>;
  ssoLogin: () => void;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const { user } = await api.get<{ user: User }>("/auth/me");
      setUser(user);
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const login = useCallback(async (email: string, password: string) => {
    setError(null);
    try {
      const { user } = await api.post<{ user: User }>("/auth/login", { email, password });
      setUser(user);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : "Sign-in failed";
      setError(msg);
      throw e;
    }
  }, []);

  const ssoLogin = useCallback(() => {
    window.location.href = `${API_BASE}/auth/sso/login`;
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.post("/auth/logout");
    } finally {
      setUser(null);
    }
  }, []);

  return (
    <AuthContext.Provider value={{ user, loading, error, login, ssoLogin, logout, refresh }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
