import { useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../features/auth';

/**
 * 登录页：账号密码通过 Cookie 会话建立，前端不持久化密码。
 * 登录接口由 `/api/auth/login` 提供；失败时以明确的错误提示呈现，
 * 不假装登录成功。
 */
export function LoginPage() {
  const { login, error } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: { pathname?: string; search?: string; hash?: string } } | null)?.from;
  const destination = from?.pathname ? `${from.pathname}${from.search ?? ''}${from.hash ?? ''}` : '/overview';
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!username.trim() || !password) return;
    setSubmitting(true);
    try {
      await login({ username: username.trim(), password });
      navigate(destination, { replace: true });
    } catch {
      // 错误信息已经写入 AuthContext.error，这里不重复处理。
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={handleSubmit}>
        <div className="login-logo" aria-hidden="true">
          ✦
        </div>
        <h1 className="login-title">Soul Mate · Owner 平台</h1>
        <p className="login-subtitle">私有管理台，仅限授权账号访问</p>

        <label className="dialog-field-label" htmlFor="login-username">
          账号
        </label>
        <input
          id="login-username"
          className="dialog-input"
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          autoComplete="username"
          required
        />

        <label className="dialog-field-label" htmlFor="login-password">
          密码
        </label>
        <input
          id="login-password"
          className="dialog-input"
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          autoComplete="current-password"
          required
        />

        {error && (
          <p className="dialog-error" role="alert">
            {error}
          </p>
        )}

        <button type="submit" className="btn btn-primary login-submit" disabled={submitting}>
          {submitting ? '正在登录…' : '登录'}
        </button>
      </form>
    </div>
  );
}
