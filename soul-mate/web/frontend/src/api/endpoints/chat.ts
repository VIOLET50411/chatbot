import { apiRequest } from '../client';
import type {
  ChatHistoryResponse,
  ChatSendResponse,
  PwaProfile,
  PwaStatus,
} from '../types';

const PWA_TOKEN_STORAGE_KEY = 'soul_mate_pwa_token';

export function getStoredPwaToken(): string {
  return localStorage.getItem(PWA_TOKEN_STORAGE_KEY) ?? '';
}

export function setStoredPwaToken(token: string): void {
  localStorage.setItem(PWA_TOKEN_STORAGE_KEY, token);
}

export function clearStoredPwaToken(): void {
  localStorage.removeItem(PWA_TOKEN_STORAGE_KEY);
}

function pwaHeaders(token: string): Record<string, string> {
  return token ? { 'X-PWA-Token': token } : {};
}

export async function fetchPwaStatus(signal?: AbortSignal): Promise<PwaStatus> {
  const raw = await apiRequest<{ auth_required?: unknown }>('/pwa/status', {
    method: 'GET',
    signal,
  });
  return { authRequired: raw.auth_required === true };
}

export async function fetchPwaProfile(
  token: string,
  characterId: string,
  signal?: AbortSignal,
): Promise<PwaProfile> {
  const raw = await apiRequest<{ id?: unknown; display_name?: unknown }>(
    `/pwa/profile?character_id=${encodeURIComponent(characterId)}`,
    { method: 'GET', headers: pwaHeaders(token), signal },
  );
  if (typeof raw.id !== 'string' || typeof raw.display_name !== 'string') {
    throw new Error('PWA 对话信息响应格式错误');
  }
  return {
    id: raw.id,
    displayName: raw.display_name,
    contactDisplayName: typeof (raw as { contact_display_name?: unknown }).contact_display_name === 'string'
      ? (raw as { contact_display_name: string }).contact_display_name
      : '',
  };
}

export async function fetchPwaProfiles(token: string, signal?: AbortSignal): Promise<PwaProfile[]> {
  const raw = await apiRequest<{ items?: Array<Record<string, unknown>> }>('/pwa/profiles', {
    method: 'GET',
    headers: pwaHeaders(token),
    signal,
  });
  return (raw.items ?? []).map((item) => ({
    id: String(item.id ?? ''),
    displayName: String(item.display_name ?? ''),
    contactDisplayName: String(item.contact_display_name ?? ''),
  })).filter((item) => item.id && item.displayName);
}

export function fetchChatHistory(
  token: string,
  characterId: string,
  signal?: AbortSignal,
  cursor?: { createdAt: string; id: string },
): Promise<ChatHistoryResponse> {
  const params = new URLSearchParams({ character_id: characterId });
  if (cursor) {
    params.set('before_created_at', cursor.createdAt);
    params.set('before_id', cursor.id);
  }
  return apiRequest<ChatHistoryResponse>(`/pwa/history?${params.toString()}`, {
    method: 'GET',
    headers: pwaHeaders(token),
    signal,
  });
}

export function sendChatMessage(
  message: string,
  token: string,
  characterId: string,
  messageId: string,
): Promise<ChatSendResponse> {
  return apiRequest<ChatSendResponse>('/pwa/send', {
    method: 'POST',
    body: { message, character_id: characterId, message_id: messageId },
    headers: pwaHeaders(token),
  });
}
