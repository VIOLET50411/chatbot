import { apiGet, apiPost, setCsrfToken } from '../client';
import { asBoolean, asNullableString, asString } from '../mappers';
import type { LoginRequest, SessionInfo } from '../types';

function mapSession(raw: unknown): SessionInfo {
  const record = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
  return {
    authenticated: asBoolean(record.authenticated),
    actor: asString(record.actor),
    username: asString(record.username),
    expiresAt: asNullableString(record.expires_at),
  };
}

/** 使用账号密码登录；成功后依赖响应 Set-Cookie 建立会话，不在前端保存密码。 */
export async function login(payload: LoginRequest): Promise<SessionInfo> {
  const raw = await apiPost<unknown>('/auth/login', {
    username: payload.username,
    password: payload.password,
  });
  const record = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
  setCsrfToken(asNullableString(record.csrf_token));
  return mapSession(raw);
}

/** 退出登录；依赖后端清除会话 Cookie。 */
export async function logout(): Promise<void> {
  await apiPost<unknown>('/auth/logout');
}

/** 读取当前会话状态，用于路由守卫与页面初始化。 */
export async function fetchSession(signal?: AbortSignal): Promise<SessionInfo> {
  const raw = await apiGet<unknown>('/auth/session', undefined, signal);
  return mapSession(raw);
}
