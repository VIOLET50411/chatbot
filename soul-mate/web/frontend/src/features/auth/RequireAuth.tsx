import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from './AuthContext';

/** 会话网络错误不会被降级为未登录，用户可原地重试。 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { session, loading, error, refresh } = useAuth();
  const location = useLocation();

  if (loading) return <div className="auth-pending" role="status">正在确认会话…</div>;

  if (error && session === null) {
    return (
      <div className="auth-retry" role="alert">
        <p>{error}</p>
        <button type="button" className="btn btn-primary" onClick={() => void refresh()}>重新尝试</button>
      </div>
    );
  }

  if (!session?.authenticated) return <Navigate to="/login" replace state={{ from: location }} />;

  return <>{children}</>;
}
