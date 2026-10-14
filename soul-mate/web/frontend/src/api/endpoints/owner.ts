import { apiGet, apiPost, apiPut, apiRequest, createConfirmation } from '../client';
import type {
  MaterialConclusion,
  MaterialReview,
  MaterialReviewDecision,
  MaterialReviewList,
  PersonaEvolutionEvent,
  PersonaEvolutionState,
  PersonaBaselineDraft,
  PersonaBaselineState,
  ContactRuntime,
  OwnerContact,
  ContactCandidate,
  MomentPost,
  MediaAsset,
  WechatStatus,
  OwnerCharacter,
  PersonaSnapshot,
  WechatBinding,
  OwnerProfileEntry,
  CharacterDisclosure,
  DisclosureState,
  PersonaReviewState,
} from '../types';

export async function listCharacters(signal?: AbortSignal): Promise<OwnerCharacter[]> {
  const raw = await apiGet<{ items?: Array<Record<string, unknown>> }>('/characters', undefined, signal);
  return (raw.items ?? []).map((item) => ({
    id: String(item.id ?? ''),
    displayName: String(item.display_name ?? ''),
    accountId: String(item.account_id ?? ''),
    accountLabel: String(item.account_label ?? ''),
    enabled: Boolean(item.enabled),
    personaStatus: String(item.persona_status ?? ''),
    activeSnapshotVersion: item.active_snapshot_version == null ? null : Number(item.active_snapshot_version),
    activeSnapshotNote: String(item.active_snapshot_note ?? ''),
    enabledContactCount: Number(item.enabled_contact_count ?? 0),
    wechatWindowNickname: String(item.wechat_window_nickname ?? ''),
    wechatId: String(item.wechat_id ?? ''),
    ownerRemark: String(item.owner_remark ?? ''),
    bindingEnabled: Boolean(item.binding_enabled),
    identityConfirmed: Boolean(item.identity_confirmed),
    bindingVersion: Number(item.binding_version ?? 1),
  }));
}

function mapWechatBinding(raw: Record<string, unknown>): WechatBinding {
  return {
    characterId: String(raw.character_id ?? ''),
    displayName: String(raw.display_name ?? ''),
    accountId: String(raw.account_id ?? ''),
    windowNickname: String(raw.window_nickname ?? ''),
    wechatId: String(raw.wechat_id ?? ''),
    ownerRemark: String(raw.owner_remark ?? ''),
    enabled: Boolean(raw.enabled),
    identityConfirmed: Boolean(raw.identity_confirmed),
    bindingVersion: Number(raw.binding_version ?? 1),
    updatedAt: String(raw.updated_at ?? ''),
  };
}

export async function getWechatBinding(characterId: string, signal?: AbortSignal): Promise<WechatBinding> {
  return mapWechatBinding(await apiGet<Record<string, unknown>>(`/characters/${encodeURIComponent(characterId)}/wechat-binding`, undefined, signal));
}

export async function updateWechatBinding(binding: WechatBinding, characterId: string): Promise<WechatBinding> {
  const confirmation = await createConfirmation({
    action: 'character.wechat_binding.update',
    scope: 'character',
    resourceId: characterId,
    reason: `Owner 确认更新${binding.displayName}的微信账号绑定`,
  });
  const raw = await apiRequest<Record<string, unknown>>(`/characters/${encodeURIComponent(characterId)}/wechat-binding`, {
    method: 'PATCH',
    confirmation,
    body: {
      window_nickname: binding.windowNickname,
      wechat_id: binding.wechatId,
      owner_remark: binding.ownerRemark,
      enabled: binding.enabled,
      identity_confirmed: binding.identityConfirmed,
      expected_version: binding.bindingVersion,
    },
  });
  return mapWechatBinding(raw);
}

function mapOwnerProfileEntry(raw: Record<string, unknown>): OwnerProfileEntry {
  return {
    id: String(raw.id ?? ''), profileKey: String(raw.profile_key ?? ''),
    category: String(raw.category ?? 'explicit_fact') as OwnerProfileEntry['category'],
    status: String(raw.status ?? ''), version: Number(raw.version ?? 0), title: String(raw.title ?? ''),
    content: String(raw.content ?? ''), rawQuote: String(raw.raw_quote ?? ''), redacted: Boolean(raw.redacted),
    source: String(raw.source ?? ''), confidence: Number(raw.confidence ?? 0),
    sensitivity: String(raw.sensitivity ?? 'normal') as OwnerProfileEntry['sensitivity'],
    topicTags: Array.isArray(raw.topic_tags) ? raw.topic_tags.map(String) : [],
    validFrom: String(raw.valid_from ?? ''), validUntil: String(raw.valid_until ?? ''), updatedAt: String(raw.updated_at ?? ''),
  };
}

export async function listOwnerProfile(signal?: AbortSignal): Promise<OwnerProfileEntry[]> {
  const raw = await apiGet<{ items?: Array<Record<string, unknown>> }>('/owner/profile', undefined, signal);
  return (raw.items ?? []).map(mapOwnerProfileEntry);
}

export async function revealOwnerProfileEntry(entry: OwnerProfileEntry): Promise<OwnerProfileEntry> {
  const confirmation = await createConfirmation({
    action: 'owner.profile.reveal', scope: 'owner_profile', resourceId: entry.id,
    reason: 'Owner 确认临时查看这条敏感资料原文',
  });
  const raw = await apiRequest<Record<string, unknown>>(`/owner/profile/${encodeURIComponent(entry.id)}/reveal`, { method: 'POST', confirmation });
  return mapOwnerProfileEntry(raw);
}

export async function listCharacterDisclosures(characterId: string, signal?: AbortSignal): Promise<CharacterDisclosure[]> {
  const raw = await apiGet<{ items?: Array<Record<string, unknown>> }>(`/owner/profile/disclosures/${encodeURIComponent(characterId)}`, undefined, signal);
  return (raw.items ?? []).map((item) => ({
    characterId, entryId: String(item.entry_id ?? ''), profileKey: String(item.profile_key ?? ''),
    title: String(item.title ?? ''), state: String(item.state ?? 'unknown') as DisclosureState,
    evidence: String(item.evidence ?? ''), updatedAt: String(item.disclosure_updated_at ?? ''),
  }));
}

export async function updateCharacterDisclosure(entryId: string, characterId: string, state: DisclosureState, evidence: string): Promise<void> {
  await apiRequest(`/owner/profile/${encodeURIComponent(entryId)}/disclosures/${encodeURIComponent(characterId)}`, {
    method: 'PATCH', body: { state, evidence }, characterId,
  });
}

export async function getPersonaReview(candidateSnapshotId: string, characterId: string, signal?: AbortSignal): Promise<PersonaReviewState> {
  const raw = await apiGet<Record<string, unknown>>(`/persona/reviews/${encodeURIComponent(candidateSnapshotId)}`, characterId, signal);
  const summary = (raw.summary ?? {}) as Record<string, unknown>;
  return {
    candidateSnapshotId: String(raw.candidate_snapshot_id ?? ''),
    items: (Array.isArray(raw.items) ? raw.items : []).map((value) => {
      const item = value as Record<string, unknown>;
      return {
        id: String(item.id ?? ''), candidateSnapshotId: String(item.candidate_snapshot_id ?? ''), sequence: Number(item.sequence ?? 0),
        category: String(item.category ?? ''), userMessage: String(item.user_message ?? ''), contextNote: String(item.context_note ?? ''),
        candidateReply: String(item.candidate_reply ?? ''),
        rating: item.rating ? String(item.rating) as 'realistic' | 'acceptable' | 'artificial' : null,
        reasons: Array.isArray(item.reasons) ? item.reasons.map(String) : [], note: String(item.note ?? ''),
      };
    }),
    evidence: (Array.isArray(raw.evidence) ? raw.evidence : []).map((value) => {
      const item = value as Record<string, unknown>;
      return { itemKey: String(item.item_key ?? ''), conclusion: String(item.conclusion ?? ''), evidence: String(item.evidence ?? ''), state: String(item.state ?? '') };
    }),
    summary: {
      total: Number(summary.total ?? 0), rated: Number(summary.rated ?? 0), realistic: Number(summary.realistic ?? 0),
      acceptable: Number(summary.acceptable ?? 0), artificial: Number(summary.artificial ?? 0),
      acceptablePercent: Number(summary.acceptable_percent ?? 0), passes85Percent: Boolean(summary.passes_85_percent),
      passesStrictReview: Boolean(summary.passes_strict_review),
    },
  };
}

export async function ratePersonaReviewCase(caseId: string, characterId: string, rating: 'realistic' | 'acceptable' | 'artificial', reasons: string[], note: string): Promise<void> {
  await apiPut(`/persona/reviews/cases/${encodeURIComponent(caseId)}/rating`, { rating, reasons, note }, characterId);
}

function mapBaselineDraft(raw: Record<string, unknown>): PersonaBaselineDraft {
  const mapDiff = (value: unknown) => Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object')).map((item) => ({
      key: String(item.key ?? ''),
      before: item.before,
      after: item.after,
    }))
    : [];
  return {
    id: String(raw.id ?? ''),
    status: String(raw.status ?? ''),
    baselineSnapshotId: String(raw.baseline_snapshot_id ?? ''),
    resultSnapshotId: String(raw.result_snapshot_id ?? ''),
    note: String(raw.note ?? ''),
    createdAt: String(raw.created_at ?? ''),
    activatedAt: String(raw.activated_at ?? ''),
    profileDiff: mapDiff(raw.profile_diff),
    styleDiff: mapDiff(raw.style_diff),
    contactProfileDiff: mapDiff(raw.contact_profile_diff),
    selectedClaimCount: Number(raw.selected_claim_count ?? 0),
    derivedClaimCount: Number(raw.derived_claim_count ?? 0),
    supersededClaimCount: Number(raw.superseded_claim_count ?? 0),
  };
}

export async function getPersonaBaseline(characterId: string, signal?: AbortSignal): Promise<PersonaBaselineState> {
  const raw = await apiGet<Record<string, unknown>>('/persona/baseline', characterId, signal);
  const active = typeof raw.active_snapshot === 'object' && raw.active_snapshot
    ? raw.active_snapshot as Record<string, unknown>
    : {};
  const draft = typeof raw.draft === 'object' && raw.draft
    ? mapBaselineDraft(raw.draft as Record<string, unknown>)
    : null;
  return {
    reviewSummary: typeof raw.review_summary === 'object' && raw.review_summary
      ? raw.review_summary as Record<string, number>
      : {},
    ownerConfirmationCount: Number(raw.owner_confirmation_count ?? 0),
    activeSnapshot: {
      id: String(active.id ?? ''),
      version: Number(active.version ?? 0),
      kind: String(active.kind ?? ''),
      note: String(active.note ?? ''),
      isActive: Boolean(active.is_active),
    },
    draft,
  };
}

export async function activatePersonaBaselineDraft(draft: PersonaBaselineDraft, characterId: string): Promise<void> {
  const confirmation = await createConfirmation({
    action: 'persona.baseline.activate',
    scope: 'persona_baseline',
    resourceId: draft.id,
    reason: 'Owner 已检查素材审核后的人格差异并确认切换基线',
  });
  await apiRequest(`/persona/baseline/drafts/${encodeURIComponent(draft.id)}/activate`, {
    method: 'POST',
    confirmation,
    characterId,
  });
}

function mapPersonaSnapshot(raw: Record<string, unknown>): PersonaSnapshot {
  return {
    id: String(raw.id ?? ''),
    version: Number(raw.version ?? 0),
    kind: String(raw.kind ?? ''),
    stableProfile: typeof raw.stable_profile === 'object' && raw.stable_profile ? raw.stable_profile as Record<string, string> : {},
    channelStyles: typeof raw.channel_styles === 'object' && raw.channel_styles ? raw.channel_styles as Record<string, unknown> : {},
    note: String(raw.note ?? ''),
    isActive: Boolean(raw.is_active),
    isPublished: Boolean(raw.is_published),
    isReadOnly: Boolean(raw.is_read_only),
    createdAt: String(raw.created_at ?? ''),
    publishedAt: typeof raw.published_at === 'string' ? raw.published_at : null,
    activatedAt: typeof raw.activated_at === 'string' ? raw.activated_at : null,
  };
}

export async function listPersonaSnapshots(characterId: string, signal?: AbortSignal): Promise<PersonaSnapshot[]> {
  const raw = await apiGet<{ items?: Array<Record<string, unknown>> }>('/persona/snapshots', characterId, signal);
  return (raw.items ?? []).map(mapPersonaSnapshot);
}

export async function createPersonaSnapshotEdit(
  base: PersonaSnapshot,
  input: { stableProfile: Record<string, string>; note: string },
  characterId: string,
): Promise<PersonaSnapshot> {
  const raw = await apiPost<Record<string, unknown>>(`/persona/snapshots/${encodeURIComponent(base.id)}/edits`, {
    stable_profile: input.stableProfile,
    channel_styles: base.channelStyles,
    note: input.note,
  }, characterId);
  return mapPersonaSnapshot(raw);
}

export async function activatePersonaSnapshot(snapshot: PersonaSnapshot, characterId: string): Promise<PersonaSnapshot> {
  const confirmation = await createConfirmation({
    action: 'persona.snapshot.activate',
    scope: 'persona_snapshot',
    resourceId: snapshot.id,
    reason: `Owner 确认切换到人格版本 v${snapshot.version}`,
  });
  const raw = await apiRequest<Record<string, unknown>>(`/persona/snapshots/${encodeURIComponent(snapshot.id)}/activate`, {
    method: 'POST',
    confirmation,
    characterId,
  });
  return mapPersonaSnapshot(raw);
}

function mapEvolutionEvent(raw: Record<string, unknown>): PersonaEvolutionEvent {
  return {
    id: String(raw.id ?? ''),
    dimension: String(raw.dimension ?? ''),
    before: raw.before,
    after: raw.after,
    evidenceIds: Array.isArray(raw.evidence_ids) ? raw.evidence_ids.map(String) : [],
    confidence: Number(raw.confidence ?? 0),
    evidenceStartAt: String(raw.evidence_start_at ?? ''),
    evidenceEndAt: String(raw.evidence_end_at ?? ''),
    status: String(raw.status ?? ''),
    reason: String(raw.reason ?? ''),
    baselineSnapshotId: String(raw.baseline_snapshot_id ?? ''),
    resultSnapshotId: String(raw.result_snapshot_id ?? ''),
    createdAt: String(raw.created_at ?? ''),
    publishedAt: String(raw.published_at ?? ''),
    rolledBackAt: String(raw.rolled_back_at ?? ''),
  };
}

export async function getPersonaEvolution(characterId: string, signal?: AbortSignal): Promise<PersonaEvolutionState> {
  const raw = await apiGet<Record<string, unknown>>('/persona/evolution', characterId, signal);
  const control = typeof raw.control === 'object' && raw.control ? raw.control as Record<string, unknown> : {};
  const events = Array.isArray(raw.events) ? raw.events as Array<Record<string, unknown>> : [];
  return {
    control: {
      paused: Boolean(control.paused),
      reason: String(control.reason ?? ''),
      updatedBy: String(control.updated_by ?? ''),
      updatedAt: String(control.updated_at ?? ''),
    },
    events: events.map(mapEvolutionEvent),
  };
}

export async function setPersonaEvolutionPaused(paused: boolean, reason: string, characterId: string): Promise<void> {
  await apiRequest('/persona/evolution/control', {
    method: 'PATCH',
    body: { paused, reason },
    characterId,
  });
}

export async function publishPersonaEvolution(event: PersonaEvolutionEvent, characterId: string): Promise<void> {
  const confirmation = await createConfirmation({
    action: 'persona.evolution.publish',
    scope: 'persona_evolution',
    resourceId: event.id,
    reason: 'Owner 确认发布满足证据门槛的人格慢速演变版本',
  });
  await apiRequest(`/persona/evolution/${encodeURIComponent(event.id)}/publish`, {
    method: 'POST',
    confirmation,
    characterId,
  });
}

export async function rollbackPersonaEvolution(event: PersonaEvolutionEvent, characterId: string): Promise<void> {
  const confirmation = await createConfirmation({
    action: 'persona.evolution.rollback',
    scope: 'persona_evolution',
    resourceId: event.id,
    reason: 'Owner 确认回滚当前人格演变版本',
  });
  await apiRequest(`/persona/evolution/${encodeURIComponent(event.id)}/rollback`, {
    method: 'POST',
    confirmation,
    characterId,
  });
}

function mapConclusion(raw: Record<string, unknown>): MaterialConclusion {
  return {
    claimId: String(raw.claim_id ?? ''),
    facet: String(raw.facet ?? ''),
    key: String(raw.key ?? ''),
    value: String(raw.value ?? ''),
    confidence: Number(raw.confidence ?? 0),
    status: String(raw.status ?? ''),
    supportKind: String(raw.support_kind ?? ''),
    quoteSpan: typeof raw.quote_span === 'object' && raw.quote_span ? raw.quote_span as Record<string, unknown> : {},
  };
}

function mapMaterial(raw: Record<string, unknown>): MaterialReview {
  return {
    id: String(raw.id ?? ''),
    sourceMaterialId: String(raw.source_material_id ?? ''),
    sourceChannel: String(raw.source_channel ?? ''),
    sourceType: String(raw.source_type ?? ''),
    content: String(raw.content ?? ''),
    sourceTime: typeof raw.source_time === 'string' ? raw.source_time : null,
    reliability: Number(raw.reliability ?? 0),
    decision: String(raw.decision ?? 'pending') as MaterialReviewDecision,
    correctedContent: typeof raw.corrected_content === 'string' ? raw.corrected_content : null,
    splitContents: Array.isArray(raw.split_contents) ? raw.split_contents.map(String) : [],
    reviewNote: String(raw.review_note ?? ''),
    reviewedBy: typeof raw.reviewed_by === 'string' ? raw.reviewed_by : null,
    reviewedAt: typeof raw.reviewed_at === 'string' ? raw.reviewed_at : null,
    conclusions: Array.isArray(raw.conclusions)
      ? raw.conclusions.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object')).map(mapConclusion)
      : [],
  };
}

export async function listMaterialReviews(
  filters: { decision?: string; sourceChannel?: string; query?: string; limit?: number; offset?: number },
  characterId: string,
  signal?: AbortSignal,
): Promise<MaterialReviewList> {
  const params = new URLSearchParams();
  if (filters.decision) params.set('decision', filters.decision);
  if (filters.sourceChannel) params.set('source_channel', filters.sourceChannel);
  if (filters.query) params.set('query', filters.query);
  params.set('limit', String(filters.limit ?? 50));
  params.set('offset', String(filters.offset ?? 0));
  const raw = await apiGet<Record<string, unknown>>(`/materials?${params.toString()}`, characterId, signal);
  const items = Array.isArray(raw.items)
    ? raw.items.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object')).map(mapMaterial)
    : [];
  return {
    items,
    total: Number(raw.total ?? items.length),
    limit: Number(raw.limit ?? 50),
    offset: Number(raw.offset ?? 0),
    summary: typeof raw.summary === 'object' && raw.summary ? raw.summary as Record<string, number> : {},
  };
}

export async function updateMaterialReview(
  id: string,
  input: {
    decision: MaterialReviewDecision;
    correctedContent?: string;
    splitContents?: string[];
    reviewNote?: string;
  },
  characterId: string,
): Promise<MaterialReview> {
  const raw = await apiRequest<Record<string, unknown>>(`/materials/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body: {
      decision: input.decision,
      corrected_content: input.correctedContent,
      split_contents: input.splitContents ?? [],
      review_note: input.reviewNote ?? '',
    },
    characterId,
  });
  return mapMaterial(raw);
}

export function batchUpdateMaterialReviews(ids: string[], decision: 'pending' | 'keep' | 'exclude', characterId: string) {
  return apiPost<{ updated: number }>('/materials/batch', { ids, decision }, characterId);
}

export async function listOwnerContacts(characterId: string, signal?: AbortSignal): Promise<OwnerContact[]> {
  const raw = await apiGet<{ items?: Array<Record<string, unknown>> }>('/contacts', characterId, signal);
  return (raw.items ?? []).map((item) => ({
    id: String(item.id ?? ''),
    externalId: String(item.external_id ?? ''),
    displayName: String(item.display_name ?? ''),
    enabled: Boolean(item.enabled),
    pwaBound: Boolean(item.pwa_bound),
    proactiveEnabled: Boolean(item.proactive_enabled),
    proactiveMaxDaily: Number(item.proactive_max_daily ?? 4),
    proactiveCooldownHours: Number(item.proactive_cooldown_hours ?? 4),
    proactiveActiveStartHour: Number(item.proactive_active_start_hour ?? 8),
    proactiveActiveEndHour: Number(item.proactive_active_end_hour ?? 24),
    proactiveCheckInAfterHours: Number(item.proactive_check_in_after_hours ?? 8),
  }));
}

export async function listContactCandidates(characterId: string, signal?: AbortSignal): Promise<ContactCandidate[]> {
  const raw = await apiGet<{ items?: Array<Record<string, unknown>> }>('/contacts/candidates', characterId, signal);
  return (raw.items ?? []).map((item) => ({
    id: String(item.id ?? ''),
    displayName: String(item.display_name ?? ''),
    status: String(item.status ?? ''),
    seenCount: Number(item.seen_count ?? 0),
    firstSeenAt: String(item.first_seen_at ?? ''),
    lastSeenAt: String(item.last_seen_at ?? ''),
  }));
}

export async function reviewContactCandidate(
  candidate: ContactCandidate,
  decision: 'confirm' | 'ignore',
  characterId: string,
): Promise<OwnerContact | null> {
  const raw = await apiPost<Record<string, unknown>>(
    `/contacts/candidates/${encodeURIComponent(candidate.id)}/review`,
    { decision },
    characterId,
  );
  if (!raw.contact || typeof raw.contact !== 'object') return null;
  const contact = raw.contact as Record<string, unknown>;
  return {
    id: String(contact.id ?? ''),
    externalId: String(contact.external_id ?? ''),
    displayName: String(contact.display_name ?? ''),
    enabled: Boolean(contact.enabled),
    pwaBound: Boolean(contact.pwa_bound),
    proactiveEnabled: Boolean(contact.proactive_enabled),
    proactiveMaxDaily: Number(contact.proactive_max_daily ?? 4),
    proactiveCooldownHours: Number(contact.proactive_cooldown_hours ?? 4),
    proactiveActiveStartHour: Number(contact.proactive_active_start_hour ?? 8),
    proactiveActiveEndHour: Number(contact.proactive_active_end_hour ?? 24),
    proactiveCheckInAfterHours: Number(contact.proactive_check_in_after_hours ?? 8),
  };
}

export async function createOwnerContact(input: { externalId: string; displayName: string }, characterId: string): Promise<OwnerContact> {
  const raw = await apiPost<Record<string, unknown>>('/contacts', {
    external_id: input.externalId,
    display_name: input.displayName,
  }, characterId);
  return {
    id: String(raw.id ?? ''),
    externalId: String(raw.external_id ?? ''),
    displayName: String(raw.display_name ?? ''),
    enabled: Boolean(raw.enabled),
    pwaBound: Boolean(raw.pwa_bound),
    proactiveEnabled: Boolean(raw.proactive_enabled),
    proactiveMaxDaily: Number(raw.proactive_max_daily ?? 4),
    proactiveCooldownHours: Number(raw.proactive_cooldown_hours ?? 4),
    proactiveActiveStartHour: Number(raw.proactive_active_start_hour ?? 8),
    proactiveActiveEndHour: Number(raw.proactive_active_end_hour ?? 24),
    proactiveCheckInAfterHours: Number(raw.proactive_check_in_after_hours ?? 8),
  };
}

export async function bindOwnerContactToPwa(contact: OwnerContact, characterId: string): Promise<void> {
  await apiRequest(`/contacts/${encodeURIComponent(contact.id)}/pwa-binding`, {
    method: 'PUT',
    characterId,
  });
}

export async function setOwnerContactEnabled(contact: OwnerContact, enabled: boolean, characterId: string): Promise<void> {
  if (enabled) {
    await apiRequest(`/contacts/${encodeURIComponent(contact.id)}`, {
      method: 'PATCH',
      body: { enabled: true },
      characterId,
    });
    return;
  }
  const confirmation = await createConfirmation({
    action: 'contact.disable',
    scope: 'contact',
    resourceId: contact.id,
    reason: 'Owner 在白名单联系人页停用该联系人',
  });
  await apiRequest(`/contacts/${encodeURIComponent(contact.id)}`, {
    method: 'PATCH',
    body: { enabled: false },
    confirmation,
    characterId,
  });
}

export async function updateOwnerContactDelivery(input: {
  contact: OwnerContact;
  proactiveEnabled: boolean;
  maxDaily: number;
  cooldownHours: number;
  activeStartHour: number;
  activeEndHour: number;
  checkInAfterHours: number;
}, characterId: string): Promise<void> {
  await apiRequest(`/contacts/${encodeURIComponent(input.contact.id)}`, {
    method: 'PATCH',
    body: {
      proactive_enabled: input.proactiveEnabled,
      proactive_max_daily: input.maxDaily,
      proactive_cooldown_hours: input.cooldownHours,
      proactive_active_start_hour: input.activeStartHour,
      proactive_active_end_hour: input.activeEndHour,
      proactive_check_in_after_hours: input.checkInAfterHours,
    },
    characterId,
  });
}

export async function getContactRuntime(contactId: string, characterId: string, signal?: AbortSignal): Promise<ContactRuntime> {
  const raw = await apiGet<Record<string, unknown>>(`/contacts/${encodeURIComponent(contactId)}/runtime`, characterId, signal);
  const relationship = (raw.relationship ?? {}) as Record<string, unknown>;
  const memories = Array.isArray(raw.memories) ? raw.memories as Array<Record<string, unknown>> : [];
  const timeline = Array.isArray(raw.timeline) ? raw.timeline as Array<Record<string, unknown>> : [];
  const checkpoint = raw.conversation_checkpoint && typeof raw.conversation_checkpoint === 'object'
    ? raw.conversation_checkpoint as Record<string, unknown>
    : null;
  const associations = raw.memory_associations && typeof raw.memory_associations === 'object'
    ? raw.memory_associations as Record<string, unknown>
    : {};
  const pendingTopic = raw.pending_topic && typeof raw.pending_topic === 'object'
    ? raw.pending_topic as Record<string, unknown>
    : null;
  const entities = Array.isArray(associations.entities) ? associations.entities as Array<Record<string, unknown>> : [];
  const episodes = Array.isArray(associations.episodes) ? associations.episodes as Array<Record<string, unknown>> : [];
  return {
    relationship: {
      stage: String(relationship.stage ?? ''),
      trust: Number(relationship.trust ?? 0),
      intimacy: Number(relationship.intimacy ?? 0),
      affection: Number(relationship.affection ?? 0),
      comfort: Number(relationship.comfort ?? 0),
      resentment: Number(relationship.resentment ?? 0),
      lastChatAt: typeof relationship.last_chat_at === 'string' ? relationship.last_chat_at : null,
    },
    profile: typeof raw.profile === 'object' && raw.profile ? raw.profile as Record<string, string> : {},
    memories: memories.map((item) => ({
      id: String(item.id ?? ''),
      type: String(item.type ?? ''),
      content: String(item.content ?? ''),
      importance: Number(item.importance ?? 0),
      status: String(item.status ?? ''),
      layer: String(item.layer ?? 'daily'),
      valence: Number(item.valence ?? 0),
      arousal: Number(item.arousal ?? 0.5),
      recallCount: Number(item.recall_count ?? 0),
      expiresAt: String(item.expires_at ?? ''),
      sourceEventIds: Array.isArray(item.source_event_ids) ? item.source_event_ids.map(String) : [],
      updatedAt: String(item.updated_at ?? ''),
    })),
    timeline: timeline.map((item) => ({
      id: String(item.id ?? ''),
      role: String(item.role ?? ''),
      content: String(item.content ?? ''),
      createdAt: String(item.created_at ?? ''),
    })),
    conversationCheckpoint: checkpoint ? {
      id: String(checkpoint.id ?? ''),
      coveredUntilMessageId: String(checkpoint.covered_until_message_id ?? ''),
      summaryText: String(checkpoint.summary_text ?? ''),
      openTopics: Array.isArray(checkpoint.open_topics) ? checkpoint.open_topics.map(String) : [],
      promises: Array.isArray(checkpoint.promises) ? checkpoint.promises.map(String) : [],
      updatedAt: String(checkpoint.updated_at ?? ''),
    } : null,
    memoryAssociations: {
      memoryIds: Array.isArray(associations.memory_ids) ? associations.memory_ids.map(String) : [],
      entities: entities.map((item) => ({
        id: String(item.id ?? ''),
        type: String(item.type ?? ''),
        name: String(item.name ?? ''),
        status: String(item.status ?? ''),
        confidence: Number(item.confidence ?? 0),
        memoryIds: Array.isArray(item.memory_ids) ? item.memory_ids.map(String) : [],
        updatedAt: String(item.updated_at ?? ''),
      })),
      episodes: episodes.map((item) => ({
        id: String(item.id ?? ''),
        key: String(item.key ?? ''),
        title: String(item.title ?? ''),
        memoryIds: Array.isArray(item.memory_ids) ? item.memory_ids.map(String) : [],
        importance: Number(item.importance ?? 0),
        startAt: String(item.start_at ?? ''),
        endAt: String(item.end_at ?? ''),
        updatedAt: String(item.updated_at ?? ''),
      })),
    },
    pendingTopic: pendingTopic ? {
      sourceMessageId: String(pendingTopic.source_message_id ?? ''),
      topicText: String(pendingTopic.topic_text ?? ''),
      followupAfterAt: String(pendingTopic.followup_after_at ?? ''),
    } : null,
  };
}

export async function reviewContactMemory(input: {
  contactId: string;
  memoryId: string;
  content?: string;
  status?: string;
}, characterId: string): Promise<void> {
  let confirmation;
  if (input.status === 'archived') {
    confirmation = await createConfirmation({
      action: 'contact.memory.archive',
      scope: 'contact_memory',
      resourceId: input.memoryId,
      reason: 'Owner 在联系人详情中归档不应继续召回的记忆',
    });
  }
  await apiRequest(`/contacts/${encodeURIComponent(input.contactId)}/memories/${encodeURIComponent(input.memoryId)}`, {
    method: 'PATCH',
    body: { content: input.content, status: input.status },
    confirmation,
    characterId,
  });
}

export async function getWechatStatus(characterId: string, signal?: AbortSignal): Promise<WechatStatus> {
  const raw = await apiGet<Record<string, unknown>>('/wechat/status', characterId, signal);
  const bridge = (raw.bridge ?? {}) as Record<string, unknown>;
  const stickers = (raw.stickers ?? {}) as Record<string, unknown>;
  return {
    bridge: {
      online: Boolean(bridge.online),
      agentId: typeof bridge.agent_id === 'string' ? bridge.agent_id : null,
      lastSeenAt: typeof bridge.last_seen_at === 'string' ? bridge.last_seen_at : null,
      wechatVersion: String(bridge.wechat_version ?? ''),
      bridgeVersion: String(bridge.bridge_version ?? ''),
      plusActive: Boolean(bridge.plus_active),
      windowReady: Boolean(bridge.window_ready),
      capabilities: Array.isArray(bridge.capabilities) ? bridge.capabilities.map(String) : [],
    },
    enabledContacts: Number(raw.enabled_contacts ?? 0),
    inboundPending: Number(raw.inbound_pending ?? 0),
    outbox: typeof raw.outbox === 'object' && raw.outbox ? raw.outbox as Record<string, number> : {},
    attachments: typeof raw.attachments === 'object' && raw.attachments ? raw.attachments as Record<string, number> : {},
    stickers: { total: Number(stickers.total ?? 0), enabled: Number(stickers.enabled ?? 0) },
    moments: typeof raw.moments === 'object' && raw.moments ? raw.moments as Record<string, number> : {},
  };
}

function mapMoment(raw: Record<string, unknown>): MomentPost {
  return {
    id: String(raw.id ?? ''),
    status: String(raw.status ?? ''),
    text: String(raw.text ?? ''),
    attachmentRefs: Array.isArray(raw.attachment_refs) ? raw.attachment_refs.map(String) : [],
    privacyTags: Array.isArray(raw.privacy_tags) ? raw.privacy_tags.map(String) : [],
    source: String(raw.source ?? ''),
    outboundActionId: typeof raw.outbound_action_id === 'string' ? raw.outbound_action_id : null,
    errorSummary: String(raw.error_summary ?? ''),
    createdAt: String(raw.created_at ?? ''),
    publishedAt: typeof raw.published_at === 'string' ? raw.published_at : null,
  };
}

export async function listMoments(characterId: string, signal?: AbortSignal): Promise<MomentPost[]> {
  const raw = await apiGet<{ items?: Array<Record<string, unknown>> }>('/wechat/moments', characterId, signal);
  return (raw.items ?? []).map(mapMoment);
}

export async function prepareMoment(input: { text: string; privacyTags: string[] }, characterId: string): Promise<MomentPost> {
  const raw = await apiPost<Record<string, unknown>>('/wechat/moments', {
    text: input.text,
    attachment_refs: [],
    privacy_tags: input.privacyTags,
    source: 'manual_test',
  }, characterId);
  return mapMoment(raw);
}

export async function dispatchMoment(post: MomentPost, characterId: string): Promise<MomentPost> {
  const confirmation = await createConfirmation({
    action: 'moment.dispatch',
    scope: 'moment',
    resourceId: post.id,
    reason: 'Owner 确认向指定微信测试标签发布一次朋友圈',
  });
  const raw = await apiRequest<Record<string, unknown>>(`/wechat/moments/${encodeURIComponent(post.id)}/dispatch`, {
    method: 'POST',
    confirmation,
    characterId,
  });
  return mapMoment(raw);
}

function mapMediaAsset(raw: Record<string, unknown>): MediaAsset {
  return {
    id: String(raw.id ?? ''),
    kind: String(raw.kind ?? ''),
    mimeType: String(raw.mime_type ?? ''),
    sizeBytes: Number(raw.size_bytes ?? 0),
    reviewStatus: String(raw.review_status ?? ''),
    visionSummary: String(raw.vision_summary ?? ''),
    contactNames: String(raw.contact_names ?? ''),
    createdAt: String(raw.created_at ?? ''),
    sourcePageUrl: String(raw.source_page_url ?? ''),
    imageUrl: String(raw.image_url ?? ''),
    sourcePlatform: String(raw.source_platform ?? ''),
    attribution: String(raw.attribution ?? ''),
    usageScope: String(raw.usage_scope ?? ''),
    claimMode: String(raw.claim_mode ?? ''),
    presentationMode: String(raw.presentation_mode ?? 'source_explicit'),
  };
}

export async function importWebMedia(input: {
  contactId: string;
  sourcePageUrl: string;
  imageUrl: string;
  sourcePlatform: 'xiaohongshu' | 'x' | 'other';
  attribution: string;
  usageScope: 'chat' | 'moments' | 'both';
  presentationMode: 'source_explicit' | 'private_roleplay';
  description: string;
}, characterId: string): Promise<MediaAsset> {
  const raw = await apiPost<Record<string, unknown>>('/wechat/media/import-web', {
    contact_id: input.contactId,
    source_page_url: input.sourcePageUrl,
    image_url: input.imageUrl,
    source_platform: input.sourcePlatform,
    attribution: input.attribution,
    usage_scope: input.usageScope,
    presentation_mode: input.presentationMode,
    description: input.description,
  }, characterId);
  return mapMediaAsset(raw);
}

export async function listMediaAssets(characterId: string, signal?: AbortSignal): Promise<MediaAsset[]> {
  const raw = await apiGet<{ items?: Array<Record<string, unknown>> }>('/wechat/media', characterId, signal);
  return (raw.items ?? []).map(mapMediaAsset);
}

export async function reviewMediaAsset(input: { id: string; reviewStatus: string }, characterId: string): Promise<void> {
  await apiRequest(`/wechat/media/${encodeURIComponent(input.id)}`, {
    method: 'PATCH',
    body: { review_status: input.reviewStatus },
    characterId,
  });
}
