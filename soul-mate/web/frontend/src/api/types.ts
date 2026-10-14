export interface LoginRequest {
  username: string;
  password: string;
}

export interface SessionInfo {
  authenticated: boolean;
  actor: string;
  username: string;
  expiresAt: string | null;
}

export interface ChatHistoryMessage {
    id?: string;
    role: 'user' | 'assistant' | string;
    content: string;
    created_at?: string;
  }

export interface ChatHistoryResponse {
  messages: ChatHistoryMessage[];
  has_more?: boolean;
  next_before_created_at?: string | null;
  next_before_id?: string | null;
}

export interface ChatSendResponse {
  reply: string | null;
  chunks: string[];
  status: 'ok' | 'skipped' | string;
}

export interface PwaProfile {
  id: string;
  displayName: string;
  contactDisplayName: string;
}

export interface PwaStatus {
  authRequired: boolean;
}

export interface PlatformSettings {
  adminUsername: string;
  sessionTimeoutMinutes: number | null;
  ownerCredentialMode: string;
  deepseekModel: string;
  embeddingModel: string;
  embeddingProvider: string;
  logPath: string;
  logEncoding: string;
  logRetentionFiles: number | null;
  pwaChatPath: string;
  pwaTokenConfigured: boolean;
  lanAccessEnabled: boolean;
}

export interface RuntimeConfiguration {
  dialogue: {
    provider: string;
    apiKeyConfigured: boolean;
    baseUrl: string;
    model: string;
    plannerModel: string;
    rendererModel: string;
  };
  vision: {
    provider: string;
    apiKeyConfigured: boolean;
    baseUrl: string;
    model: string;
  };
  audio: {
    provider: string;
    apiKeyConfigured: boolean;
    usesVisionKeyFallback: boolean;
    baseUrl: string;
    model: string;
    wechatVoiceMode: string;
  };
  externalInformation: {
    rssSources: Array<{ name: string; url: string }>;
    refreshMinutes: number;
    xBearerTokenConfigured: boolean;
    xUsernames: string[];
    socialBrowserProxyUrl: string;
    socialBrowserProxyTokenConfigured: boolean;
    socialBrowserXEnabled: boolean;
    socialBrowserXhsEnabled: boolean;
  };
  proactiveDefaults: {
    activeStartHour: number;
    activeEndHour: number;
    checkInAfterHours: number;
    cooldownHours: number;
    maxDaily: number;
  };
  deployment: {
    currentMode: 'local' | 'vps' | string;
    vpsTargetUrl: string;
    automaticSwitchSupported: boolean;
    reason: string;
  };
  pwa: {
    tokenConfigured: boolean;
    tokenIsStrong: boolean;
    chatPath: string;
  };
  secretStorage: {
    mode: string;
    encryptedAtRest: boolean;
  };
}

export interface RuntimeConfigurationUpdate {
  deepseek_api_key?: string;
  deepseek_base_url?: string;
  deepseek_model?: string;
  deepseek_planner_model?: string;
  deepseek_renderer_model?: string;
  vision_api_key?: string;
  vision_base_url?: string;
  vision_model?: string;
  audio_api_key?: string;
  audio_base_url?: string;
  audio_model?: string;
  rss_sources?: Array<{ name: string; url: string }>;
  external_refresh_minutes?: number;
  x_bearer_token?: string;
  x_usernames?: string[];
  social_browser_proxy_url?: string;
  social_browser_proxy_token?: string;
  social_browser_x_enabled?: boolean;
  social_browser_xhs_enabled?: boolean;
  proactive_active_start_hour?: number;
  proactive_active_end_hour?: number;
  proactive_check_in_after_hours?: number;
  proactive_cooldown_hours?: number;
  proactive_max_daily?: number;
  vps_target_url?: string;
  pwa_chat_token?: string;
  clear_secrets?: string[];
}

export type RuntimeConnectionTarget = 'dialogue' | 'vision' | 'audio' | 'external' | 'vps';

export interface RuntimeConnectionResult {
  target: RuntimeConnectionTarget;
  ok: boolean;
  latencyMs: number;
  message: string;
}

export interface RuntimePromptVersion {
  id: string;
  prompt: string;
  createdBy: string;
  createdAt: string;
  restoredFromId: string | null;
  isActive: boolean;
}

export interface RuntimePromptState {
  prompt: string;
  version: RuntimePromptVersion | null;
}

export interface ExternalInformationStatus {
  configuration: {
    rssSources: Array<{ name: string; url: string }>;
    xUsernames: string[];
    xConfigured: boolean;
    socialBrowser: {
      xConfigured: boolean;
      xiaohongshuConfigured: boolean;
      mode: string;
    };
    refreshMinutes: number;
  };
  sourceStates: Array<{
    sourceKey: string;
    status: string;
    lastAttemptAt: string;
    lastSuccessAt: string | null;
    errorSummary: string;
    itemCount: number;
  }>;
  cachedItemCount: number;
  recentItems: Array<{
    id: string;
    provider: string;
    sourceName: string;
    title: string;
    summary: string;
    url: string;
    publishedAt: string;
    fetchedAt: string;
  }>;
}

export interface OwnerCharacter {
  id: string;
  displayName: string;
  accountId: string;
  accountLabel: string;
  enabled: boolean;
  personaStatus: string;
  activeSnapshotVersion: number | null;
  activeSnapshotNote: string;
  enabledContactCount: number;
  wechatWindowNickname: string;
  wechatId: string;
  ownerRemark: string;
  bindingEnabled: boolean;
  identityConfirmed: boolean;
  bindingVersion: number;
}

export interface WechatBinding {
  characterId: string;
  displayName: string;
  accountId: string;
  windowNickname: string;
  wechatId: string;
  ownerRemark: string;
  enabled: boolean;
  identityConfirmed: boolean;
  bindingVersion: number;
  updatedAt: string;
}

export type OwnerProfileCategory = 'explicit_fact' | 'conditional_rule' | 'stable_preference' | 'sensitive_self_report' | 'model_inference' | 'rejected_inference';
export type OwnerProfileSensitivity = 'normal' | 'private' | 'high';
export type DisclosureState = 'unknown' | 'context_retrieved' | 'confirmed_known';

export interface OwnerProfileEntry {
  id: string;
  profileKey: string;
  category: OwnerProfileCategory;
  status: string;
  version: number;
  title: string;
  content: string;
  rawQuote: string;
  redacted: boolean;
  source: string;
  confidence: number;
  sensitivity: OwnerProfileSensitivity;
  topicTags: string[];
  validFrom: string;
  validUntil: string;
  updatedAt: string;
}

export interface CharacterDisclosure {
  characterId: string;
  entryId: string;
  profileKey: string;
  title: string;
  state: DisclosureState;
  evidence: string;
  updatedAt: string;
}

export interface PersonaReviewCase {
  id: string;
  candidateSnapshotId: string;
  sequence: number;
  category: string;
  userMessage: string;
  contextNote: string;
  candidateReply: string;
  rating: 'realistic' | 'acceptable' | 'artificial' | null;
  reasons: string[];
  note: string;
}

export interface PersonaReviewState {
  candidateSnapshotId: string;
  items: PersonaReviewCase[];
  evidence: Array<{ itemKey: string; conclusion: string; evidence: string; state: string }>;
  summary: {
    total: number;
    rated: number;
    realistic: number;
    acceptable: number;
    artificial: number;
    acceptablePercent: number;
    passes85Percent: boolean;
    passesStrictReview: boolean;
  };
}

export type MaterialReviewDecision = 'pending' | 'keep' | 'exclude' | 'split' | 'corrected';

export interface MaterialConclusion {
  claimId: string;
  facet: string;
  key: string;
  value: string;
  confidence: number;
  status: string;
  supportKind: string;
  quoteSpan: Record<string, unknown>;
}

export interface MaterialReview {
  id: string;
  sourceMaterialId: string;
  sourceChannel: string;
  sourceType: string;
  content: string;
  sourceTime: string | null;
  reliability: number;
  decision: MaterialReviewDecision;
  correctedContent: string | null;
  splitContents: string[];
  reviewNote: string;
  reviewedBy: string | null;
  reviewedAt: string | null;
  conclusions: MaterialConclusion[];
}

export interface MaterialReviewList {
  items: MaterialReview[];
  total: number;
  limit: number;
  offset: number;
  summary: Record<string, number>;
}

export interface PersonaEvolutionEvent {
  id: string;
  dimension: string;
  before: unknown;
  after: unknown;
  evidenceIds: string[];
  confidence: number;
  evidenceStartAt: string;
  evidenceEndAt: string;
  status: string;
  reason: string;
  baselineSnapshotId: string;
  resultSnapshotId: string;
  createdAt: string;
  publishedAt: string;
  rolledBackAt: string;
}

export interface PersonaEvolutionState {
  control: {
    paused: boolean;
    reason: string;
    updatedBy: string;
    updatedAt: string;
  };
  events: PersonaEvolutionEvent[];
}

export interface PersonaBaselineDiff {
  key: string;
  before: unknown;
  after: unknown;
}

export interface PersonaBaselineDraft {
  id: string;
  status: string;
  baselineSnapshotId: string;
  resultSnapshotId: string;
  note: string;
  createdAt: string;
  activatedAt: string;
  profileDiff: PersonaBaselineDiff[];
  styleDiff: PersonaBaselineDiff[];
  contactProfileDiff: PersonaBaselineDiff[];
  selectedClaimCount: number;
  derivedClaimCount: number;
  supersededClaimCount: number;
}

export interface PersonaBaselineState {
  reviewSummary: Record<string, number>;
  ownerConfirmationCount: number;
  activeSnapshot: {
    id: string;
    version: number;
    kind: string;
    note: string;
    isActive: boolean;
  };
  draft: PersonaBaselineDraft | null;
}

export interface PersonaSnapshot {
  id: string;
  version: number;
  kind: string;
  stableProfile: Record<string, string>;
  channelStyles: Record<string, unknown>;
  note: string;
  isActive: boolean;
  isPublished: boolean;
  isReadOnly: boolean;
  createdAt: string;
  publishedAt: string | null;
  activatedAt: string | null;
}

export interface OwnerContact {
  id: string;
  externalId: string;
  displayName: string;
  enabled: boolean;
  pwaBound: boolean;
  proactiveEnabled: boolean;
  proactiveMaxDaily: number;
  proactiveCooldownHours: number;
  proactiveActiveStartHour: number;
  proactiveActiveEndHour: number;
  proactiveCheckInAfterHours: number;
}

export interface ContactCandidate {
  id: string;
  displayName: string;
  status: string;
  seenCount: number;
  firstSeenAt: string;
  lastSeenAt: string;
}

export interface OwnerDevice {
  id: string;
  displayName: string;
  enabled: boolean;
  consent: Record<string, boolean>;
  retentionDays: number;
  createdAt: string;
  updatedAt: string;
  lastSeenAt: string;
}

export interface DeviceRecord {
  id: string;
  deviceId: string;
  deviceName: string;
  capturedAt: string;
  receivedAt: string;
  data: Record<string, unknown>;
}

export interface DeviceStatus {
  devices: OwnerDevice[];
  records: DeviceRecord[];
  recordCount: number;
}

export interface ContactMemory {
  id: string;
  type: string;
  content: string;
  importance: number;
  status: string;
  layer: string;
  valence: number;
  arousal: number;
  recallCount: number;
  expiresAt: string;
  sourceEventIds: string[];
  updatedAt: string;
}

export interface ContactTimelineItem {
  id: string;
  role: string;
  content: string;
  createdAt: string;
}

export interface ContactRuntime {
  relationship: {
    stage: string;
    trust: number;
    intimacy: number;
    affection: number;
    comfort: number;
    resentment: number;
    lastChatAt: string | null;
  };
  profile: Record<string, string>;
  memories: ContactMemory[];
  timeline: ContactTimelineItem[];
  conversationCheckpoint: {
    id: string;
    coveredUntilMessageId: string;
    summaryText: string;
    openTopics: string[];
    promises: string[];
    updatedAt: string;
  } | null;
  memoryAssociations: {
    memoryIds: string[];
    entities: Array<{ id: string; type: string; name: string; status: string; confidence: number; memoryIds: string[]; updatedAt: string }>;
    episodes: Array<{ id: string; key: string; title: string; memoryIds: string[]; importance: number; startAt: string; endAt: string; updatedAt: string }>;
  };
  pendingTopic: { sourceMessageId: string; topicText: string; followupAfterAt: string } | null;
}

export interface BridgeAgentStatus {
  online: boolean;
  agentId: string | null;
  lastSeenAt: string | null;
  wechatVersion: string;
  bridgeVersion: string;
  plusActive: boolean;
  windowReady: boolean;
  capabilities: string[];
}

export interface WechatStatus {
  bridge: BridgeAgentStatus;
  enabledContacts: number;
  inboundPending: number;
  outbox: Record<string, number>;
  attachments: Record<string, number>;
  stickers: { total: number; enabled: number };
  moments: Record<string, number>;
}

export interface MomentPost {
  id: string;
  status: string;
  text: string;
  attachmentRefs: string[];
  privacyTags: string[];
  source: string;
  outboundActionId: string | null;
  errorSummary: string;
  createdAt: string;
  publishedAt: string | null;
}

export interface MediaAsset {
  id: string;
  kind: string;
  mimeType: string;
  sizeBytes: number;
  reviewStatus: string;
  visionSummary: string;
  contactNames: string;
  createdAt: string;
  sourcePageUrl: string;
  imageUrl: string;
  sourcePlatform: string;
  attribution: string;
  usageScope: string;
  claimMode: string;
  presentationMode: string;
}
