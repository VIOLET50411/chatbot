/**
 * Soul Mate Owner 平台 · API 客户端
 *
 * 边界约定：
 * 1. 会话完全基于 Cookie（`credentials: 'include'`），不在前端持有或转发密码。
 * 2. 写操作携带 CSRF 头（`X-CSRF-Token`），令牌来自登录响应或 `/api/auth/session`；
 *    令牌缺失时仍发起请求，交由后端判定，前端不伪造令牌。
 * 3. 需要角色作用域的请求必须显式传入 `characterId` 作为查询参数；
 *    这里不维护任何“当前角色”的全局状态，避免隐式角色影响运行路由。
 * 4. 后端尚未实现的 `/api/*` 端点会返回 404/501 等，
 *    这里统一归类为 ApiError，由页面层渲染统一的空/错误态，不在客户端伪造数据。
 */

const API_BASE = '/api';
const CSRF_COOKIE_NAME = 'soul_mate_csrf';

let csrfToken: string | null = null;

/** 记录最新的 CSRF 令牌，供后续写请求使用。仅在内存中保存，不落盘。 */
export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

export function getCsrfToken(): string | null {
  return csrfToken;
}

/** 从服务端设置的非 HttpOnly 双提交 Cookie 恢复 CSRF 值，不写入持久化存储。 */
export function restoreCsrfTokenFromCookie(): string | null {
  const entry = document.cookie
    .split('; ')
    .find((item) => item.startsWith(`${CSRF_COOKIE_NAME}=`));
  const value = entry ? decodeURIComponent(entry.slice(CSRF_COOKIE_NAME.length + 1)) : null;
  setCsrfToken(value || null);
  return value || null;
}

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly notImplemented: boolean;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    // 只有 501 表示当前服务明确不支持该接口；404/410 仍应按资源/接口错误显示。
    this.notImplemented = status === 501;
  }
}

export interface ConfirmationToken {
  id: string;
  token: string;
  expiresAt: string;
}

export interface ConfirmationRequest {
  action: string;
  scope: string;
  resourceId?: string;
  reason: string;
}

export interface RequestOptions {
  method?: HttpMethod;
  body?: unknown;
  /** 附加请求头；PWA 独立对话接口用它携带 X-PWA-Token。 */
  headers?: Record<string, string>;
  /** 请求作用域角色；未提供时不附加，绝不回退到隐式的“当前角色”。 */
  characterId?: string;
  /** 仅用于已获取确认令牌的危险写操作。 */
  confirmation?: Pick<ConfirmationToken, 'id' | 'token'>;
  signal?: AbortSignal;
}

function buildUrl(path: string, characterId?: string): string {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  const url = new URL(`${API_BASE}${normalizedPath}`, window.location.origin);
  if (characterId) {
    url.searchParams.set('character_id', characterId);
  }
  return `${url.pathname}${url.search}`;
}

async function parseErrorBody(response: Response): Promise<string> {
  try {
    const data = (await response.clone().json()) as { detail?: string };
    if (data && typeof data.detail === 'string') {
      return data.detail;
    }
  } catch {
    // 非 JSON 响应，降级到文本。
  }
  try {
    const text = await response.text();
    if (text) return text;
  } catch {
    // 忽略读取失败。
  }
  return response.statusText || `请求失败（${response.status}）`;
}

/** 统一请求入口：cookie 会话 + 显式角色 scope + CSRF 头。 */
export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, headers: extraHeaders, characterId, confirmation, signal } = options;
  const headers: Record<string, string> = {
    Accept: 'application/json',
  };
  if (extraHeaders) {
    Object.assign(headers, extraHeaders);
  }
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }
  if (method !== 'GET' && csrfToken) {
    headers['X-CSRF-Token'] = csrfToken;
  }
  if (confirmation) {
    headers['X-Confirmation-ID'] = confirmation.id;
    headers['X-Confirmation-Token'] = confirmation.token;
  }

  const response = await fetch(buildUrl(path, characterId), {
    method,
    credentials: 'include',
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    signal,
  });

  if (!response.ok) {
    const detail = await parseErrorBody(response);
    throw new ApiError(response.status, detail);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const text = await response.text();
  if (!text) {
    return undefined as T;
  }
  const contentType = response.headers.get('content-type') ?? '';
  if (!contentType.toLowerCase().includes('application/json')) {
    throw new ApiError(
      response.status,
      `API 协议错误：预期 JSON 响应，实际收到 ${contentType || '未声明的内容类型'}。`,
    );
  }

  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError(response.status, 'API 协议错误：响应内容不是有效 JSON。');
  }
}

/** 创建一次性危险操作确认令牌；令牌只应随即用于匹配的目标请求。 */
export async function createConfirmation(request: ConfirmationRequest): Promise<ConfirmationToken> {
  const raw = await apiPost<{ id?: unknown; token?: unknown; expires_at?: unknown }>('/confirmations', {
    action: request.action,
    scope: request.scope,
    resource_id: request.resourceId,
    reason: request.reason,
  });
  if (typeof raw.id !== 'string' || typeof raw.token !== 'string' || typeof raw.expires_at !== 'string') {
    throw new ApiError(200, 'API 协议错误：确认令牌响应缺少必要字段。');
  }
  return { id: raw.id, token: raw.token, expiresAt: raw.expires_at };
}

export function apiGet<T>(path: string, characterId?: string, signal?: AbortSignal): Promise<T> {
  return apiRequest<T>(path, { method: 'GET', characterId, signal });
}

export function apiPost<T>(path: string, body?: unknown, characterId?: string): Promise<T> {
  return apiRequest<T>(path, { method: 'POST', body, characterId });
}

export function apiPut<T>(path: string, body?: unknown, characterId?: string): Promise<T> {
  return apiRequest<T>(path, { method: 'PUT', body, characterId });
}

export function apiPatch<T>(path: string, body?: unknown, characterId?: string): Promise<T> {
  return apiRequest<T>(path, { method: 'PATCH', body, characterId });
}

export function apiDelete<T>(path: string, body?: unknown, characterId?: string): Promise<T> {
  return apiRequest<T>(path, { method: 'DELETE', body, characterId });
}
