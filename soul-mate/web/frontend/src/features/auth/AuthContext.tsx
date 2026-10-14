import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  fetchSession,
  login as loginRequest,
  logout as logoutRequest,
  restoreCsrfTokenFromCookie,
  setCsrfToken,
} from '../../api';
import type { LoginRequest, SessionInfo } from '../../api/types';

interface AuthContextValue {
  session: SessionInfo | null;
  loading: boolean;
  error: string | null;
  login: (payload: LoginRequest) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

const UNAUTHENTICATED: SessionInfo = { authenticated: false, actor: '', username: '', expiresAt: null };

/**
 * 会话上下文：全部状态来自后端 Cookie 会话。
 * 会话状态加载时同步恢复双提交 Cookie 中的 CSRF 值；令牌只保留在内存。
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    restoreCsrfTokenFromCookie();
    try {
      const current = await fetchSession();
      setSession(current);
      setCsrfToken(current.authenticated ? restoreCsrfTokenFromCookie() : null);
      setError(null);
    } catch (err) {
      setCsrfToken(null);
      setSession(null);
      setError(err instanceof Error ? `会话查询失败：${err.message}` : '会话查询失败，请重试');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const login = useCallback(async (payload: LoginRequest) => {
    setError(null);
    try {
      const next = await loginRequest(payload);
      setSession(next);
    } catch (err) {
      const message = err instanceof Error ? err.message : '登录失败';
      setError(message);
      throw err;
    }
  }, []);

  const logout = useCallback(async () => {
    try {
      await logoutRequest();
    } finally {
      setCsrfToken(null);
      setSession(UNAUTHENTICATED);
    }
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ session, loading, error, login, logout, refresh }),
    [session, loading, error, login, logout, refresh],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth 必须在 AuthProvider 内部使用');
  }
  return ctx;
}
