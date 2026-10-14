import { apiGet, apiPatch, apiPost, apiPut } from '../client';
import { asBoolean, asNullableNumber, asNullableString, asRecord, asString } from '../mappers';
import type { DeviceStatus, ExternalInformationStatus, OwnerDevice, PlatformSettings, RuntimeConfiguration, RuntimeConfigurationUpdate, RuntimeConnectionResult, RuntimeConnectionTarget, RuntimePromptState, RuntimePromptVersion } from '../types';

function mapOwnerDevice(value: unknown): OwnerDevice {
  const record = asRecord(value);
  return {
    id: asString(record.id),
    displayName: asString(record.display_name),
    enabled: asBoolean(record.enabled),
    consent: Object.fromEntries(Object.entries(asRecord(record.consent)).map(([key, item]) => [key, asBoolean(item)])),
    retentionDays: asNullableNumber(record.retention_days) ?? 7,
    createdAt: asString(record.created_at),
    updatedAt: asString(record.updated_at),
    lastSeenAt: asString(record.last_seen_at),
  };
}

function mapDeviceStatus(value: unknown): DeviceStatus {
  const record = asRecord(value);
  return {
    devices: Array.isArray(record.devices) ? record.devices.map(mapOwnerDevice) : [],
    records: Array.isArray(record.records) ? record.records.map(asRecord).map((item) => ({
      id: asString(item.id),
      deviceId: asString(item.device_id),
      deviceName: asString(item.device_name),
      capturedAt: asString(item.captured_at),
      receivedAt: asString(item.received_at),
      data: asRecord(item.data),
    })) : [],
    recordCount: asNullableNumber(record.record_count) ?? 0,
  };
}

export async function fetchDevices(signal?: AbortSignal): Promise<DeviceStatus> {
  return mapDeviceStatus(await apiGet<unknown>('/devices', undefined, signal));
}

export async function createDevice(displayName: string, retentionDays: number): Promise<OwnerDevice & { deviceToken: string }> {
  const record = asRecord(await apiPost<unknown>('/devices', {
    display_name: displayName,
    retention_days: retentionDays,
  }));
  return { ...mapOwnerDevice(record), deviceToken: asString(record.device_token) };
}

export async function updateDevice(
  device: OwnerDevice,
  input: { enabled?: boolean; retentionDays?: number; consent?: Record<string, boolean> },
): Promise<OwnerDevice> {
  return mapOwnerDevice(await apiPatch<unknown>(`/devices/${encodeURIComponent(device.id)}`, {
    enabled: input.enabled,
    retention_days: input.retentionDays,
    consent: input.consent,
  }));
}

export async function rotateDeviceToken(device: OwnerDevice): Promise<string> {
  const record = asRecord(await apiPost<unknown>(`/devices/${encodeURIComponent(device.id)}/rotate-token`));
  return asString(record.device_token);
}

function mapRuntimePromptVersion(value: unknown): RuntimePromptVersion | null {
  if (!value || typeof value !== 'object') return null;
  const record = asRecord(value);
  const id = asString(record.id);
  if (!id) return null;
  return {
    id,
    prompt: asString(record.prompt),
    createdBy: asString(record.created_by),
    createdAt: asString(record.created_at),
    restoredFromId: asNullableString(record.restored_from_id),
    isActive: asBoolean(record.is_active),
  };
}

function mapRuntimePromptState(value: unknown): RuntimePromptState {
  const record = asRecord(value);
  return {
    prompt: asString(record.prompt),
    version: mapRuntimePromptVersion(record.version),
  };
}

/** 平台设置：管理员账号与会话超时策略。全局视图。 */
export async function fetchPlatformSettings(signal?: AbortSignal): Promise<PlatformSettings> {
  const raw = await apiGet<unknown>('/settings', undefined, signal);
  const record = asRecord(raw);
  return {
    adminUsername: asString(record.admin_username),
    sessionTimeoutMinutes: asNullableNumber(record.session_timeout_minutes),
    ownerCredentialMode: asString(record.owner_credential_mode),
    deepseekModel: asString(record.deepseek_model),
    embeddingModel: asString(record.embedding_model),
    embeddingProvider: asString(record.embedding_provider),
    logPath: asString(record.log_path),
    logEncoding: asString(record.log_encoding),
    logRetentionFiles: asNullableNumber(record.log_retention_files),
    pwaChatPath: asString(record.pwa_chat_path),
    pwaTokenConfigured: asBoolean(record.pwa_token_configured),
    lanAccessEnabled: asBoolean(record.lan_access_enabled),
  };
}

function mapRuntimeConfiguration(value: unknown): RuntimeConfiguration {
  const record = asRecord(value);
  const dialogue = asRecord(record.dialogue);
  const vision = asRecord(record.vision);
  const audio = asRecord(record.audio);
  const external = asRecord(record.external_information);
  const proactive = asRecord(record.proactive_defaults);
  const deployment = asRecord(record.deployment);
  const pwa = asRecord(record.pwa);
  const secretStorage = asRecord(record.secret_storage);
  return {
    dialogue: {
      provider: asString(dialogue.provider),
      apiKeyConfigured: asBoolean(dialogue.api_key_configured),
      baseUrl: asString(dialogue.base_url),
      model: asString(dialogue.model),
      plannerModel: asString(dialogue.planner_model),
      rendererModel: asString(dialogue.renderer_model),
    },
    vision: {
      provider: asString(vision.provider),
      apiKeyConfigured: asBoolean(vision.api_key_configured),
      baseUrl: asString(vision.base_url),
      model: asString(vision.model),
    },
    audio: {
      provider: asString(audio.provider),
      apiKeyConfigured: asBoolean(audio.api_key_configured),
      usesVisionKeyFallback: asBoolean(audio.uses_vision_key_fallback),
      baseUrl: asString(audio.base_url),
      model: asString(audio.model),
      wechatVoiceMode: asString(audio.wechat_voice_mode),
    },
    externalInformation: {
      rssSources: Array.isArray(external.rss_sources)
        ? external.rss_sources.map(asRecord).map((item) => ({ name: asString(item.name), url: asString(item.url) }))
        : [],
      refreshMinutes: asNullableNumber(external.refresh_minutes) ?? 30,
      xBearerTokenConfigured: asBoolean(external.x_bearer_token_configured),
      xUsernames: Array.isArray(external.x_usernames) ? external.x_usernames.map(String) : [],
      socialBrowserProxyUrl: asString(external.social_browser_proxy_url),
      socialBrowserProxyTokenConfigured: asBoolean(external.social_browser_proxy_token_configured),
      socialBrowserXEnabled: asBoolean(external.social_browser_x_enabled),
      socialBrowserXhsEnabled: asBoolean(external.social_browser_xhs_enabled),
    },
    proactiveDefaults: {
      activeStartHour: asNullableNumber(proactive.active_start_hour) ?? 8,
      activeEndHour: asNullableNumber(proactive.active_end_hour) ?? 24,
      checkInAfterHours: asNullableNumber(proactive.check_in_after_hours) ?? 8,
      cooldownHours: asNullableNumber(proactive.cooldown_hours) ?? 4,
      maxDaily: asNullableNumber(proactive.max_daily) ?? 4,
    },
    deployment: {
      currentMode: asString(deployment.current_mode),
      vpsTargetUrl: asString(deployment.vps_target_url),
      automaticSwitchSupported: asBoolean(deployment.automatic_switch_supported),
      reason: asString(deployment.reason),
    },
    pwa: {
      tokenConfigured: asBoolean(pwa.token_configured),
      tokenIsStrong: asBoolean(pwa.token_is_strong),
      chatPath: asString(pwa.chat_path, '/chat'),
    },
    secretStorage: {
      mode: asString(secretStorage.mode),
      encryptedAtRest: asBoolean(secretStorage.encrypted_at_rest),
    },
  };
}

export async function fetchRuntimeConfiguration(characterId: string, signal?: AbortSignal): Promise<RuntimeConfiguration> {
  return mapRuntimeConfiguration(await apiGet<unknown>('/settings/runtime', characterId, signal));
}

export async function saveRuntimeConfiguration(input: RuntimeConfigurationUpdate, characterId: string): Promise<RuntimeConfiguration> {
  return mapRuntimeConfiguration(await apiPut<unknown>('/settings/runtime', input, characterId));
}

export async function testRuntimeConnection(target: RuntimeConnectionTarget, characterId: string): Promise<RuntimeConnectionResult> {
  const value = asRecord(await apiPost<unknown>('/settings/runtime/test', { target }, characterId));
  return {
    target: asString(value.target) as RuntimeConnectionTarget,
    ok: asBoolean(value.ok),
    latencyMs: asNullableNumber(value.latency_ms) ?? 0,
    message: asString(value.message),
  };
}

export async function fetchRuntimePrompt(signal?: AbortSignal): Promise<RuntimePromptState> {
  return mapRuntimePromptState(await apiGet<unknown>('/runtime-prompt', undefined, signal));
}

export async function fetchRuntimePromptHistory(signal?: AbortSignal): Promise<RuntimePromptVersion[]> {
  const record = asRecord(await apiGet<unknown>('/runtime-prompt/history', undefined, signal));
  return Array.isArray(record.items)
    ? record.items.map(mapRuntimePromptVersion).filter((item): item is RuntimePromptVersion => item !== null)
    : [];
}

export async function saveRuntimePrompt(prompt: string): Promise<RuntimePromptState> {
  return mapRuntimePromptState(await apiPut<unknown>('/runtime-prompt', { prompt }));
}

export async function restoreRuntimePrompt(versionId: string): Promise<RuntimePromptState> {
  return mapRuntimePromptState(await apiPost<unknown>(`/runtime-prompt/${encodeURIComponent(versionId)}/restore`));
}

export async function fetchCharacterRuntimePrompt(characterId: string, signal?: AbortSignal): Promise<RuntimePromptState> {
  return mapRuntimePromptState(await apiGet<unknown>('/runtime-prompt/character/current', characterId, signal));
}

export async function fetchCharacterRuntimePromptHistory(characterId: string, signal?: AbortSignal): Promise<RuntimePromptVersion[]> {
  const record = asRecord(await apiGet<unknown>('/runtime-prompt/character/history', characterId, signal));
  return Array.isArray(record.items)
    ? record.items.map(mapRuntimePromptVersion).filter((item): item is RuntimePromptVersion => item !== null)
    : [];
}

export async function saveCharacterRuntimePrompt(prompt: string, characterId: string): Promise<RuntimePromptState> {
  return mapRuntimePromptState(await apiPut<unknown>('/runtime-prompt/character/current', { prompt }, characterId));
}

export async function restoreCharacterRuntimePrompt(versionId: string, characterId: string): Promise<RuntimePromptState> {
  return mapRuntimePromptState(await apiPost<unknown>(`/runtime-prompt/character/${encodeURIComponent(versionId)}/restore`, undefined, characterId));
}

function mapExternalInformationStatus(value: unknown): ExternalInformationStatus {
  const record = asRecord(value);
  const configuration = asRecord(record.configuration);
  const socialBrowser = asRecord(configuration.social_browser);
  const rssSources = Array.isArray(configuration.rss_sources)
    ? configuration.rss_sources.map(asRecord).map((item) => ({ name: asString(item.name), url: asString(item.url) }))
    : [];
  const sourceStates = Array.isArray(record.source_states)
    ? record.source_states.map(asRecord).map((item) => ({
      sourceKey: asString(item.source_key),
      status: asString(item.status),
      lastAttemptAt: asString(item.last_attempt_at),
      lastSuccessAt: asNullableString(item.last_success_at),
      errorSummary: asString(item.error_summary),
      itemCount: asNullableNumber(item.item_count) ?? 0,
    }))
    : [];
  const recentItems = Array.isArray(record.recent_items)
    ? record.recent_items.map(asRecord).map((item) => ({
      id: asString(item.id),
      provider: asString(item.provider),
      sourceName: asString(item.source_name),
      title: asString(item.title),
      summary: asString(item.summary),
      url: asString(item.url),
      publishedAt: asString(item.published_at),
      fetchedAt: asString(item.fetched_at),
    }))
    : [];
  return {
    configuration: {
      rssSources,
      xUsernames: Array.isArray(configuration.x_usernames) ? configuration.x_usernames.map(String) : [],
      xConfigured: asBoolean(configuration.x_configured),
      socialBrowser: {
        xConfigured: asBoolean(socialBrowser.x_configured),
        xiaohongshuConfigured: asBoolean(socialBrowser.xiaohongshu_configured),
        mode: asString(socialBrowser.mode),
      },
      refreshMinutes: asNullableNumber(configuration.refresh_minutes) ?? 30,
    },
    sourceStates,
    cachedItemCount: asNullableNumber(record.cached_item_count) ?? 0,
    recentItems,
  };
}

export async function fetchExternalInformationStatus(characterId: string, signal?: AbortSignal): Promise<ExternalInformationStatus> {
  return mapExternalInformationStatus(await apiGet<unknown>('/external-information', characterId, signal));
}

export async function refreshExternalInformation(characterId: string): Promise<ExternalInformationStatus> {
  return mapExternalInformationStatus(await apiPost<unknown>('/external-information/refresh', undefined, characterId));
}
