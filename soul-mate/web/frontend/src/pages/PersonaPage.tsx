import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Badge, PageHeader, QueryBoundary } from '../components';
import {
  batchUpdateMaterialReviews,
  activatePersonaSnapshot,
  activatePersonaBaselineDraft,
  createPersonaSnapshotEdit,
  getPersonaBaseline,
  getPersonaEvolution,
  listMaterialReviews,
  listPersonaSnapshots,
  publishPersonaEvolution,
  rollbackPersonaEvolution,
  setPersonaEvolutionPaused,
  updateMaterialReview,
  listOwnerProfile,
  listCharacterDisclosures,
  revealOwnerProfileEntry,
  updateCharacterDisclosure,
  getPersonaReview,
  ratePersonaReviewCase,
} from '../api';
import type {
  MaterialReview,
  MaterialReviewDecision,
  PersonaBaselineDraft,
  PersonaEvolutionEvent,
  PersonaSnapshot,
  OwnerProfileEntry,
  CharacterDisclosure,
  PersonaReviewState,
  PersonaReviewCase,
  DisclosureState,
} from '../api/types';
import { useCharacter } from '../features/characters';

const DECISION_LABEL: Record<MaterialReviewDecision, string> = {
  pending: '待审核',
  keep: '保留',
  exclude: '排除',
  split: '已拆分',
  corrected: '已修正',
};

const CHANNEL_LABEL: Record<string, string> = {
  chat: '微信聊天',
  moments: '朋友圈',
  identity: '身份设定',
  douyin_reference: '参考素材',
};

const FACET_LABEL: Record<string, string> = {
  identity: '身份信息',
  life_context: '生活背景',
  communication: '沟通风格',
  values: '价值观',
  value: '价值观',
  boundary: '边界',
  preference: '偏好',
  habit: '习惯',
  relationship: '关系',
};

const FIELD_LABEL: Record<string, string> = {
  real_name: '姓名',
  name: '姓名',
  occupation: '职业',
  speaking_style: '说话风格',
  language_style_chat: '微信语言风格',
  language_style_moments: '朋友圈语言风格',
  nickname_for_user: '对你的称呼',
  current_contact_status: '当前联系状态',
  relationship_with_user: '与你的关系',
  relationship_with_du_jiaying: '与杜佳颖的关系',
  relationship_with_ji_yu: '与纪雨的关系',
  relationship_update_rule: '关系信息更新规则',
  relationship_progress_rule: '关系发展规则',
  relationship_boundaries: '关系边界',
  age: '年龄',
  birthday: '生日',
  birth_date: '出生日期',
  location: '所在地',
  hometown: '家乡',
  home_city: '当前城市',
  current_life: '当前生活状态',
  academic: '学业与保研目标',
  education: '学校与专业',
  emotional_history: '情感经历',
  no_relationship_history: '过往恋爱经历',
  social_boundaries: '社交边界',
  close_friends_style: '与亲近朋友的相处方式',
  behavior_rules: '角色行为边界',
  chat_style_profile: '聊天表达风格',
  moments_style_profile: '朋友圈表达风格',
  lol_level: '英雄联盟水平',
  pet: '宠物',
  personality: '性格',
  flaws: '性格缺点',
  values: '价值观',
  appearance: '外貌与气质',
  appearance_attitude: '对外貌的态度',
  aesthetic: '审美偏好',
  voice: '声音特点',
  family: '家庭关系',
  family_background: '家庭背景',
  hobbies: '兴趣爱好',
  preferences: '日常偏好',
  sensory_preferences: '感官偏好',
  social: '社交状态',
  social_life: '自己的社交生活',
  sleep_habit: '睡眠习惯',
  fan_culture: '追星与饭圈态度',
  moments_habit: '朋友圈习惯',
  recent_chat_2026_07_30: '7 月 30 日聊天背景',
  advice_and_praise: '建议与鼓励方式',
  ambiguous_emotion_rule: '模糊情绪的回应方式',
  conflict_style: '发生冲突时的表现',
  emotional_support: '安慰与陪伴方式',
  explicit_support_request: '明确提出需要时的回应',
  independence: '独立生活',
  independence_and_priority: '独立性与事情轻重',
  jealousy: '吃醋时的表现',
  learning_through_shared_life: '从共同生活中自然成长',
  life_goals: '人生目标',
  mature_capable_presence: '成熟与解决问题的能力',
  multiple_sides_for_owner: '对你展示的不同侧面',
  personality_command_response: '面对命令改性格时的回应',
  proactive_contact: '主动联系习惯',
  repair_and_return: '争执后的修复方式',
  request_boundaries: '面对请求时的边界',
  shared_history: '相识经历',
  softness_and_reciprocity: '柔软与相互照顾',
  study_stress_response: '学习压力下的表现',
  uncertain_reply_policy: '不确定时怎样回答',
  unknown_fact_policy: '不了解事实时怎样处理',
  vulnerability: '脆弱的一面',
  wholehearted_care: '真心为你着想的方式',
  initial_knowledge: '刚认识时知道的资料',
  owner_disclosure_boundary: '了解你的资料边界',
  body_expression_openness: '对身体表达与穿搭探索的态度',
  adult_intimacy_boundary: '成人亲密话题的边界',
  no_omniscience: '能力与信息边界',
  wechat_chat: '微信聊天风格',
  moments: '朋友圈风格',
};

const SUPPORT_KIND_LABEL: Record<string, string> = {
  context: '根据上下文推断',
  direct: '原文直接说明',
  quote: '原文证据',
  manual: '人工提供',
};

const EMPTY_PLACEHOLDER_VALUES = new Set(['', '未设置', '未知', '暂无', '待补充', 'null', 'none']);

function friendlyName(value: string, labels: Record<string, string>) {
  return labels[value] ?? '其他人设内容';
}

function parseMaterialContent(content: string) {
  const match = content.match(/^([a-z][a-z0-9_]*)\s*:\s*([\s\S]*)$/i);
  if (!match) return { fieldKey: '', label: '素材原文', value: content.trim() };
  const fieldKey = match[1].toLowerCase();
  return {
    fieldKey,
    label: friendlyName(fieldKey, FIELD_LABEL),
    value: match[2].trim(),
  };
}

function decisionTone(decision: MaterialReviewDecision) {
  if (decision === 'keep' || decision === 'corrected' || decision === 'split') return 'positive' as const;
  if (decision === 'exclude') return 'neutral' as const;
  return 'warning' as const;
}

export function PersonaPage() {
  const { selected: selectedCharacter, selectedId } = useCharacter();
  const queryClient = useQueryClient();
  const [decision, setDecision] = useState('pending');
  const [sourceChannel, setSourceChannel] = useState('');
  const [searchDraft, setSearchDraft] = useState('');
  const [queryText, setQueryText] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [actionError, setActionError] = useState('');
  const [actionMessage, setActionMessage] = useState('');
  const [revealedEntries, setRevealedEntries] = useState<Record<string, OwnerProfileEntry>>({});

  useEffect(() => {
    setActionError('');
    setActionMessage('');
    setRevealedEntries({});
  }, [selectedId]);

  const filters = useMemo(() => ({ decision, sourceChannel, query: queryText, limit: 100 }), [decision, sourceChannel, queryText]);
  const materialsQuery = useQuery({
    queryKey: ['owner-materials', selectedId, filters],
    queryFn: ({ signal }) => listMaterialReviews(filters, selectedId, signal),
    enabled: Boolean(selectedId),
  });
  const evolutionQuery = useQuery({
    queryKey: ['persona-evolution', selectedId],
    queryFn: ({ signal }) => getPersonaEvolution(selectedId, signal),
    enabled: Boolean(selectedId),
  });
  const baselineQuery = useQuery({
    queryKey: ['persona-baseline', selectedId],
    queryFn: ({ signal }) => getPersonaBaseline(selectedId, signal),
    enabled: Boolean(selectedId),
  });
  const snapshotsQuery = useQuery({
    queryKey: ['persona-snapshots', selectedId],
    queryFn: ({ signal }) => listPersonaSnapshots(selectedId, signal),
    enabled: Boolean(selectedId),
  });
  const profileQuery = useQuery({ queryKey: ['owner-profile'], queryFn: ({ signal }) => listOwnerProfile(signal) });
  const disclosureQuery = useQuery({
    queryKey: ['owner-profile-disclosures', selectedId],
    queryFn: ({ signal }) => listCharacterDisclosures(selectedId, signal),
    enabled: Boolean(selectedId),
  });
  const reviewCandidate = snapshotsQuery.data
    ? [...snapshotsQuery.data]
      .sort((left, right) => right.version - left.version)
      .find((item) => !item.isActive && item.note.includes('真人感') && item.note.includes('候选'))
      ?? (selectedId === 'ji-yu'
        ? [...snapshotsQuery.data]
          .sort((left, right) => right.version - left.version)
          .find((item) => !item.isActive && item.note.startsWith('纪雨 v6 候选'))
        : undefined)
    : undefined;
  const reviewCasesQuery = useQuery({
    queryKey: ['persona-review', selectedId, reviewCandidate?.id],
    queryFn: ({ signal }) => getPersonaReview(reviewCandidate!.id, selectedId, signal),
    enabled: Boolean(reviewCandidate?.id),
  });

  const refresh = async () => {
    setSelected(new Set());
    await queryClient.invalidateQueries({ queryKey: ['owner-materials', selectedId] });
  };
  const reviewMutation = useMutation({
    mutationFn: ({ material, input }: {
      material: MaterialReview;
      input: { decision: MaterialReviewDecision; correctedContent?: string; splitContents?: string[]; reviewNote?: string };
    }) => updateMaterialReview(material.id, input, selectedId),
    onSuccess: () => void refresh(),
    onError: (error) => setActionError(error instanceof Error ? error.message : '审核操作失败'),
  });
  const batchMutation = useMutation({
    mutationFn: (nextDecision: 'keep' | 'exclude') => batchUpdateMaterialReviews([...selected], nextDecision, selectedId),
    onSuccess: () => void refresh(),
    onError: (error) => setActionError(error instanceof Error ? error.message : '批量审核失败'),
  });
  const evolutionMutation = useMutation({
    mutationFn: async (action: { kind: 'control'; paused: boolean } | { kind: 'publish' | 'rollback'; event: PersonaEvolutionEvent }) => {
      if (action.kind === 'control') {
        await setPersonaEvolutionPaused(
          action.paused,
          action.paused ? 'Owner 暂停人格自动演变' : 'Owner 确认素材审核完成并解除暂停',
          selectedId,
        );
      } else if (action.kind === 'publish') {
        await publishPersonaEvolution(action.event, selectedId);
      } else {
        await rollbackPersonaEvolution(action.event, selectedId);
      }
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['persona-evolution', selectedId] }),
    onError: (error) => setActionError(error instanceof Error ? error.message : '人格演变操作失败'),
  });
  const baselineMutation = useMutation({
    mutationFn: (draft: PersonaBaselineDraft) => activatePersonaBaselineDraft(draft, selectedId),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['persona-baseline', selectedId] }),
        queryClient.invalidateQueries({ queryKey: ['owner-materials', selectedId] }),
        queryClient.invalidateQueries({ queryKey: ['owner-contacts', selectedId] }),
      ]);
    },
    onError: (error) => setActionError(error instanceof Error ? error.message : '人格基线激活失败'),
  });
  const snapshotEditMutation = useMutation({
    mutationFn: ({ base, stableProfile, note }: { base: PersonaSnapshot; stableProfile: Record<string, string>; note: string }) => (
      createPersonaSnapshotEdit(base, { stableProfile, note }, selectedId)
    ),
    onSuccess: async () => {
      setActionError('');
      await queryClient.invalidateQueries({ queryKey: ['persona-snapshots', selectedId] });
    },
    onError: (error) => setActionError(error instanceof Error ? error.message : '新版本保存失败'),
  });
  const snapshotActivateMutation = useMutation({
    mutationFn: (snapshot: PersonaSnapshot) => activatePersonaSnapshot(snapshot, selectedId),
    onSuccess: async () => {
      setActionError('');
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['persona-snapshots', selectedId] }),
        queryClient.invalidateQueries({ queryKey: ['persona-baseline', selectedId] }),
        queryClient.invalidateQueries({ queryKey: ['characters'] }),
      ]);
    },
    onError: (error) => setActionError(error instanceof Error ? error.message : '人格版本切换失败'),
  });
  const revealMutation = useMutation({
    mutationFn: revealOwnerProfileEntry,
    onSuccess: (entry) => {
      setActionError('');
      setActionMessage('敏感内容已在当前卡片中临时展开，离开页面后会再次隐藏');
      setRevealedEntries((current) => ({ ...current, [entry.id]: entry }));
    },
    onError: (error) => setActionError(error instanceof Error ? error.message : '敏感资料查看失败'),
  });
  const disclosureMutation = useMutation({
    mutationFn: ({ entryId, state, evidence }: { entryId: string; state: DisclosureState; evidence: string }) => updateCharacterDisclosure(entryId, selectedId, state, evidence),
    onSuccess: (_result, variables) => {
      setActionError('');
      setActionMessage(variables.state === 'confirmed_known' ? '已保存：这个人物知道这条资料' : '已保存：这个人物还不知道这条资料');
      void queryClient.invalidateQueries({ queryKey: ['owner-profile-disclosures', selectedId] });
    },
    onError: (error) => setActionError(error instanceof Error ? error.message : '人物知情状态保存失败'),
  });
  const ratingMutation = useMutation({
    mutationFn: ({ item, rating, reasons, note }: { item: PersonaReviewCase; rating: 'realistic' | 'acceptable' | 'artificial'; reasons: string[]; note: string }) => ratePersonaReviewCase(item.id, selectedId, rating, reasons, note),
    onSuccess: (_result, variables) => {
      setActionError('');
      setActionMessage(`第 ${variables.item.sequence} 条场景评价已保存`);
      void queryClient.invalidateQueries({ queryKey: ['persona-review', selectedId, reviewCandidate?.id] });
    },
    onError: (error) => setActionError(error instanceof Error ? error.message : '场景评价保存失败'),
  });

  return (
    <div className="page owner-page admin-page">
      <PageHeader
        title={`${selectedCharacter?.displayName ?? '当前人物'}的人设与素材`}
        description="这里只修改当前所选人物。逐条决定素材能否进入下一版人格，确认激活前机器人继续使用当前版本。"
      />
      <div className="owner-action-feedback" aria-live="polite">
        {actionMessage ? <p className="success-text" role="status">{actionMessage}</p> : null}
        {actionError ? <p className="dialog-error" role="alert">{actionError}</p> : null}
      </div>

      <QueryBoundary query={snapshotsQuery} apiPath="/api/persona/snapshots" isEmpty={(items) => items.length === 0}>
        {(items) => (
          <SnapshotPanel
            snapshots={items}
            busy={snapshotEditMutation.isPending || snapshotActivateMutation.isPending}
            onSave={(base, stableProfile, note) => {
              setActionError('');
              snapshotEditMutation.mutate({ base, stableProfile, note });
            }}
            onActivate={(snapshot) => {
              if (snapshot.id === reviewCandidate?.id && !reviewCasesQuery.data?.summary.passesStrictReview) {
                const total = reviewCasesQuery.data?.summary.total ?? 0;
                window.alert(`这版仍是候选。请先完成 ${total || '全部'} 个场景评价，并把“人机感明显”降到 0 条。`);
                return;
              }
              if (!window.confirm(`确认切换到 v${snapshot.version}？现有版本会保留，可随时切回来。`)) return;
              setActionError('');
              snapshotActivateMutation.mutate(snapshot);
            }}
          />
        )}
      </QueryBoundary>

      {reviewCandidate ? (
        <QueryBoundary query={reviewCasesQuery} apiPath={`/api/persona/reviews/${reviewCandidate.id}`}>
          {(review) => <PersonaReviewPanel characterName={selectedCharacter?.displayName ?? '当前人物'} candidate={reviewCandidate} review={review} busy={ratingMutation.isPending} onRate={(item, rating, reasons, note) => ratingMutation.mutate({ item, rating, reasons, note })} />}
        </QueryBoundary>
      ) : null}

      <QueryBoundary query={profileQuery} apiPath="/api/owner/profile">
        {(entries) => (
          <OwnerProfilePanel
            characterName={selectedCharacter?.displayName ?? '当前人物'}
            entries={entries}
            disclosures={disclosureQuery.data ?? []}
            revealedEntries={revealedEntries}
            busy={revealMutation.isPending || disclosureMutation.isPending}
            onReveal={(entry) => {
              if (!window.confirm('这会在当前页面临时显示敏感原文，确认周围没有其他人后继续。')) return;
              revealMutation.mutate(entry);
            }}
            onDisclosure={(entryId, state, evidence) => disclosureMutation.mutate({ entryId, state, evidence })}
          />
        )}
      </QueryBoundary>

      <section className="material-review-guide" aria-labelledby="material-review-guide-title">
        <div>
          <span className="material-section-kicker">审核说明</span>
          <h2 id="material-review-guide-title">你只需要判断卡片中“你要审核的内容”</h2>
          <p>内容正确就保留；错误、过期或只是空占位就排除；意思基本正确但表述不准就修改；混了多件事再拆分。</p>
        </div>
        <span className="material-review-guide-note">“系统理解”只是辅助参考，不是另一条需要审核的内容。</span>
      </section>

      {baselineQuery.data && (
        <BaselinePanel
          state={baselineQuery.data}
          busy={baselineMutation.isPending}
          onActivate={(draft) => {
            if (!window.confirm('确认已经看完差异并切换到这版人格？当前版本仍可用于回滚。')) return;
            setActionError('');
            baselineMutation.mutate(draft);
          }}
        />
      )}
      {baselineQuery.isError && <p className="dialog-error" role="alert">人格基线预览加载失败</p>}

      {evolutionQuery.data && (
        <EvolutionPanel
          control={evolutionQuery.data.control}
          events={evolutionQuery.data.events}
          busy={evolutionMutation.isPending}
          baselineReady={Boolean(
            baselineQuery.data?.activeSnapshot.id
            && (baselineQuery.data.reviewSummary.pending ?? 0) === 0
          )}
          onControl={(paused) => {
            setActionError('');
            evolutionMutation.mutate({ kind: 'control', paused });
          }}
          onPublish={(event) => {
            if (!window.confirm('确认发布这次人格慢速演变？发布后会生成并启用一个新版本。')) return;
            setActionError('');
            evolutionMutation.mutate({ kind: 'publish', event });
          }}
          onRollback={(event) => {
            if (!window.confirm('确认回滚当前人格演变版本？')) return;
            setActionError('');
            evolutionMutation.mutate({ kind: 'rollback', event });
          }}
        />
      )}
      {evolutionQuery.isError && <p className="dialog-error" role="alert">人格演变状态加载失败</p>}

      <section className="card material-toolbar" aria-label="素材筛选">
        <div className="material-filter-row">
          <label>
            审核状态
            <select value={decision} onChange={(event) => { setDecision(event.target.value); setSelected(new Set()); }}>
              <option value="">全部</option>
              <option value="pending">待审核</option>
              <option value="keep">保留</option>
              <option value="exclude">排除</option>
              <option value="corrected">已修正</option>
              <option value="split">已拆分</option>
            </select>
          </label>
          <label>
            来源
            <select value={sourceChannel} onChange={(event) => { setSourceChannel(event.target.value); setSelected(new Set()); }}>
              <option value="">全部来源</option>
              <option value="chat">微信聊天</option>
              <option value="moments">朋友圈</option>
              <option value="identity">身份设定</option>
              <option value="douyin_reference">参考素材</option>
            </select>
          </label>
          <form
            className="material-search"
            onSubmit={(event) => { event.preventDefault(); setQueryText(searchDraft.trim()); setSelected(new Set()); }}
          >
            <label>
              搜索原文
              <input value={searchDraft} onChange={(event) => setSearchDraft(event.target.value)} placeholder="输入关键词" />
            </label>
            <button className="btn btn-ghost" type="submit">搜索</button>
          </form>
        </div>
        <div className="material-batch-bar" aria-live="polite">
          <span>已选择 {selected.size} 条</span>
          <button type="button" className="btn btn-primary btn-sm" disabled={!selected.size || batchMutation.isPending} onClick={() => batchMutation.mutate('keep')}>批量保留</button>
          <button type="button" className="btn btn-ghost btn-sm" disabled={!selected.size || batchMutation.isPending} onClick={() => batchMutation.mutate('exclude')}>批量排除</button>
        </div>
      </section>

      <QueryBoundary query={materialsQuery} apiPath="/api/materials" isEmpty={(data) => data.items.length === 0}>
        {(data) => (
          <>
            <div className="material-summary" aria-label="审核进度">
              <strong>{data.summary.pending ?? 0}</strong> 条待审核
              <span>共 {data.total} 条符合当前筛选</span>
            </div>
            <div className="material-list">
              {data.items.map((material) => (
                <MaterialCard
                  key={material.id}
                  material={material}
                  selected={selected.has(material.id)}
                  busy={reviewMutation.isPending}
                  onSelected={(checked) => setSelected((current) => {
                    const next = new Set(current);
                    if (checked) next.add(material.id); else next.delete(material.id);
                    return next;
                  })}
                  onReview={(input) => { setActionError(''); reviewMutation.mutate({ material, input }); }}
                />
              ))}
            </div>
          </>
        )}
      </QueryBoundary>
    </div>
  );
}

const PROFILE_CATEGORY_LABEL: Record<string, string> = {
  explicit_fact: '明确事实', conditional_rule: '按场景变化的规则', stable_preference: '稳定偏好',
  sensitive_self_report: '敏感自述', model_inference: '待确认推测', rejected_inference: '已经否定的推测',
};

function OwnerProfilePanel({
  characterName, entries, disclosures, revealedEntries, busy, onReveal, onDisclosure,
}: {
  characterName: string;
  entries: OwnerProfileEntry[];
  disclosures: CharacterDisclosure[];
  revealedEntries: Record<string, OwnerProfileEntry>;
  busy: boolean;
  onReveal: (entry: OwnerProfileEntry) => void;
  onDisclosure: (entryId: string, state: DisclosureState, evidence: string) => void;
}) {
  const disclosureByEntry = new Map(disclosures.map((item) => [item.entryId, item]));
  const knowledgeEntries = entries.filter((entry) => ['explicit_fact', 'stable_preference', 'sensitive_self_report'].includes(entry.category));
  const responseRuleEntries = entries.filter((entry) => ['conditional_rule', 'rejected_inference'].includes(entry.category));
  const inferenceEntries = entries.filter((entry) => entry.category === 'model_inference');

  const renderEntry = (entry: OwnerProfileEntry, showDisclosure: boolean) => {
    const disclosure = disclosureByEntry.get(entry.id);
    const shown = revealedEntries[entry.id] ?? entry;
    const state = disclosure?.state ?? 'unknown';
    return (
      <details className="owner-profile-entry" key={entry.id}>
        <summary>
          <span><strong>{entry.title}</strong><small>{PROFILE_CATEGORY_LABEL[entry.category] ?? entry.category} · v{entry.version}</small></span>
          {showDisclosure ? (
            <Badge tone={state === 'confirmed_known' ? 'positive' : state === 'context_retrieved' ? 'warning' : 'neutral'}>
              {state === 'confirmed_known' ? '她已经知道' : state === 'context_retrieved' ? '刚在对话中提到，待确认' : '她还不知道'}
            </Badge>
          ) : <Badge tone={entry.category === 'model_inference' ? 'warning' : 'info'}>{entry.category === 'model_inference' ? '仅供你审核' : '运行规则'}</Badge>}
        </summary>
        <div className="owner-profile-entry-body">
          <p>{shown.redacted ? '这条内容已加密隐藏。需要时可以临时查看，普通日志和页面不会显示原文。' : shown.content}</p>
          <p className="meta-line">来源：{entry.source} · 可信度 {Math.round(entry.confidence * 100)}%{entry.validUntil ? ` · 有效至 ${entry.validUntil}` : ''}</p>
          {shown.rawQuote ? <details><summary>查看原始回答</summary><pre>{shown.rawQuote}</pre></details> : null}
          <div className="button-row">
            {entry.redacted && !revealedEntries[entry.id] ? <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={() => onReveal(entry)}>临时查看敏感内容</button> : null}
            {showDisclosure && state !== 'confirmed_known' ? <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => onDisclosure(entry.id, 'confirmed_known', 'Owner 在管理台确认该人物已经从真实聊天中知道')}>确认她已经知道</button> : null}
            {showDisclosure && state !== 'unknown' ? <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={() => onDisclosure(entry.id, 'unknown', 'Owner 纠正：该人物尚未知道')}>改回她还不知道</button> : null}
          </div>
          {showDisclosure && disclosure?.evidence ? <p className="meta-line">知情依据：{disclosure.evidence}</p> : null}
        </div>
      </details>
    );
  };

  return (
    <section className="card owner-profile-panel" aria-labelledby="owner-profile-title">
      <div className="section-heading">
        <div>
          <span className="material-section-kicker">我的资料</span>
          <h2 id="owner-profile-title">关于你的资料与回应规则</h2>
          <p className="muted">“她知道的资料”和“系统怎样回应你”已经分开。只有第一类才有知情状态，两个人的知情进度完全独立。</p>
        </div>
        <Badge tone="info">{entries.length} 条资料</Badge>
      </div>
      <div className="owner-profile-group">
        <div className="owner-profile-group-heading"><div><h3>{characterName}知道的关于你的事</h3><p>事实、偏好和你亲口披露的经历。说过并确认后，以后可以直接记住，不需要反复问。</p></div><Badge tone="neutral">{knowledgeEntries.length} 条</Badge></div>
        <div className="owner-profile-list">{knowledgeEntries.map((entry) => renderEntry(entry, true))}</div>
      </div>
      <div className="owner-profile-group">
        <div className="owner-profile-group-heading"><div><h3>系统怎样更自然地回应你</h3><p>例如先安慰再分析、避免模板句式。这些规则直接约束回复，不代表{characterName}“知道了你的某段经历”。</p></div><Badge tone="info">{responseRuleEntries.length} 条</Badge></div>
        <div className="owner-profile-list">{responseRuleEntries.map((entry) => renderEntry(entry, false))}</div>
      </div>
      {inferenceEntries.length ? (
        <div className="owner-profile-group">
          <div className="owner-profile-group-heading"><div><h3>待你确认的分析</h3><p>模型推测不会自动当成事实，也不会自动写进人物对你的了解。</p></div><Badge tone="warning">{inferenceEntries.length} 条</Badge></div>
          <div className="owner-profile-list">{inferenceEntries.map((entry) => renderEntry(entry, false))}</div>
        </div>
      ) : null}
    </section>
  );
}

const REVIEW_REASON_OPTIONS = [
  '不合时宜地笑', '断句太碎', '断句太整', '客服式总结', '机械共情',
  '不合时宜的词', '用了“不是…而是…”', '人称不自然', '过度迎合',
  '称呼生硬', '关系阶段不对', '两个人物无法区分', '无脑套用语料',
];

function PersonaReviewPanel({ characterName, candidate, review, busy, onRate }: {
  characterName: string;
  candidate: PersonaSnapshot;
  review: PersonaReviewState;
  busy: boolean;
  onRate: (item: PersonaReviewCase, rating: 'realistic' | 'acceptable' | 'artificial', reasons: string[], note: string) => void;
}) {
  return (
    <section className="card persona-review-panel" aria-labelledby="persona-review-title">
      <div className="section-heading">
        <div>
          <span className="material-section-kicker">候选，尚未启用</span>
          <h2 id="persona-review-title">{characterName} v{candidate.version} 真人感审核</h2>
          <p className="muted">逐个展开场景，判断这句话像不像真实的{characterName}。必须完成全部 {review.summary.total} 条，并且“人机感明显”为 0，才允许最终确认激活。</p>
        </div>
        <Badge tone={review.summary.passesStrictReview ? 'positive' : 'warning'}>{review.summary.rated} / {review.summary.total} 已评价</Badge>
      </div>
      <div className="review-progress" aria-label="真人感审核进度">
        <strong>{review.summary.passesStrictReview
          ? '严格通过'
          : review.summary.artificial > 0
            ? `${review.summary.artificial} 条待改`
            : `${review.summary.total - review.summary.rated} 条待评`}</strong>
        <span>像真人 {review.summary.realistic} · 可接受 {review.summary.acceptable} · 人机感明显 {review.summary.artificial}</span>
      </div>
      <details className="candidate-evidence">
        <summary>查看问卷依据与分析取舍（{review.evidence.length} 项）</summary>
        <div className="candidate-evidence-list">
          {review.evidence.map((item) => <article key={item.itemKey}><Badge tone={item.state === 'accepted' ? 'positive' : item.state === 'rejected' ? 'danger' : 'warning'}>{item.state === 'accepted' ? '采用' : item.state === 'rejected' ? '否定' : '保留为推测'}</Badge><strong>{item.conclusion}</strong><p>{item.evidence}</p></article>)}
        </div>
      </details>
      <div className="persona-review-cases">
        {review.items.map((item) => <PersonaReviewCaseCard key={item.id} characterName={characterName} item={item} busy={busy} onRate={onRate} />)}
      </div>
    </section>
  );
}

function PersonaReviewCaseCard({ characterName, item, busy, onRate }: {
  characterName: string;
  item: PersonaReviewCase;
  busy: boolean;
  onRate: (item: PersonaReviewCase, rating: 'realistic' | 'acceptable' | 'artificial', reasons: string[], note: string) => void;
}) {
  const [reasons, setReasons] = useState<string[]>(item.reasons);
  const [note, setNote] = useState(item.note);
  return (
    <details className="persona-review-case">
      <summary><span><strong>{String(item.sequence).padStart(2, '0')} · {item.category}</strong><small>{item.userMessage.replaceAll('\n', ' / ')}</small></span><Badge tone={item.rating === 'realistic' ? 'positive' : item.rating === 'artificial' ? 'danger' : item.rating === 'acceptable' ? 'info' : 'neutral'}>{item.rating === 'realistic' ? '像真人' : item.rating === 'acceptable' ? '可接受' : item.rating === 'artificial' ? '人机感明显' : '未评价'}</Badge></summary>
      <div className="persona-review-case-body">
        <p className="meta-line">场景：{item.contextNote}</p>
        <div className="review-dialogue"><div><span>你</span><p>{item.userMessage}</p></div><div><span>{characterName}候选</span><p>{item.candidateReply}</p></div></div>
        <fieldset><legend>如果有人机感，可以勾选原因</legend><div className="review-reason-grid">{REVIEW_REASON_OPTIONS.map((reason) => <label key={reason}><input type="checkbox" checked={reasons.includes(reason)} onChange={(event) => setReasons((current) => event.target.checked ? [...current, reason] : current.filter((value) => value !== reason))} /><span>{reason}</span></label>)}</div></fieldset>
        <label>补充说明（可选）<input value={note} onChange={(event) => setNote(event.target.value)} placeholder="具体哪句话不像真人，或者你希望怎么改" /></label>
        <div className="review-action-grid compact">
          <button type="button" className="review-action-option review-action-keep" disabled={busy} onClick={() => onRate(item, 'realistic', reasons, note)}><strong>像真人</strong><span>可以进入表达示例库</span></button>
          <button type="button" className="review-action-option" disabled={busy} onClick={() => onRate(item, 'acceptable', reasons, note)}><strong>可接受</strong><span>自然度足够，但不作为风格示例</span></button>
          <button type="button" className="review-action-option" disabled={busy} onClick={() => onRate(item, 'artificial', reasons, note)}><strong>人机感明显</strong><span>需要根据原因重新生成</span></button>
        </div>
      </div>
    </details>
  );
}

function SnapshotPanel({
  snapshots,
  busy,
  onSave,
  onActivate,
}: {
  snapshots: PersonaSnapshot[];
  busy: boolean;
  onSave: (base: PersonaSnapshot, stableProfile: Record<string, string>, note: string) => void;
  onActivate: (snapshot: PersonaSnapshot) => void;
}) {
  const active = snapshots.find((item) => item.isActive) ?? snapshots[0];
  const prioritizedSnapshots = [...snapshots].sort((left, right) => {
    if (left.isActive !== right.isActive) return left.isActive ? -1 : 1;
    return right.version - left.version;
  });
  const duplicateCountById = new Map<string, number>();
  const seenSnapshotKeys = new Map<string, PersonaSnapshot>();
  const visibleSnapshots: PersonaSnapshot[] = [];
  for (const snapshot of prioritizedSnapshots) {
    const stableEntries = Object.entries(snapshot.stableProfile).sort(([left], [right]) => left.localeCompare(right));
    const key = JSON.stringify([stableEntries, snapshot.channelStyles, snapshot.note.trim()]);
    const first = seenSnapshotKeys.get(key);
    if (first) {
      duplicateCountById.set(first.id, (duplicateCountById.get(first.id) ?? 0) + 1);
      continue;
    }
    seenSnapshotKeys.set(key, snapshot);
    visibleSnapshots.push(snapshot);
  }
  const [editing, setEditing] = useState<PersonaSnapshot | null>(null);
  return (
    <section className="card persona-version-panel" aria-label="当前人设与历史版本">
      <div className="evolution-heading">
        <div>
          <span className="material-section-kicker">现在正在使用</span>
          <h2>人格版本 v{active.version}</h2>
          <p>{active.note || '这一版没有单独备注'}</p>
        </div>
        <Badge tone="positive">当前版本</Badge>
      </div>
      <div className="persona-fact-grid">
        {Object.entries(active.stableProfile).map(([key, value]) => (
          <div key={key}><span>{friendlyName(key, FIELD_LABEL)}</span><p>{String(value)}</p></div>
        ))}
      </div>
      <div className="button-row persona-version-actions">
        <button type="button" className="btn btn-primary btn-sm" onClick={() => setEditing(active)}>基于当前版本编辑</button>
      </div>
      <details className="persona-version-history">
        <summary>查看过去版本（{visibleSnapshots.length} 组{snapshots.length !== visibleSnapshots.length ? `，已折叠 ${snapshots.length - visibleSnapshots.length} 个重复提交` : ''}）</summary>
        <div className="persona-version-list">
          {visibleSnapshots.map((snapshot) => (
            <article key={snapshot.id} className="persona-version-item">
              <div>
                <strong>v{snapshot.version}</strong>
                {snapshot.isActive && <Badge tone="positive">正在使用</Badge>}
                {(duplicateCountById.get(snapshot.id) ?? 0) > 0 && <Badge tone="neutral">另有 {duplicateCountById.get(snapshot.id)} 个完全相同版本，已折叠</Badge>}
                <p>{snapshot.note || '无备注'}</p>
                <small>{snapshot.createdAt ? new Date(snapshot.createdAt).toLocaleString() : ''}</small>
              </div>
              <div className="button-row">
                <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => setEditing(snapshot)}>基于这版编辑</button>
                {!snapshot.isActive && <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={() => onActivate(snapshot)}>切换到这版</button>}
              </div>
            </article>
          ))}
        </div>
      </details>
      {editing && (
        <SnapshotEditor
          key={editing.id}
          snapshot={editing}
          busy={busy}
          onCancel={() => setEditing(null)}
          onSave={(profile, note) => {
            onSave(editing, profile, note);
          }}
        />
      )}
    </section>
  );
}

function SnapshotEditor({
  snapshot,
  busy,
  onCancel,
  onSave,
}: {
  snapshot: PersonaSnapshot;
  busy: boolean;
  onCancel: () => void;
  onSave: (stableProfile: Record<string, string>, note: string) => void;
}) {
  const [profile, setProfile] = useState<Record<string, string>>({ ...snapshot.stableProfile });
  const [note, setNote] = useState('');
  return (
    <form className="persona-version-editor" onSubmit={(event) => {
      event.preventDefault();
      if (!note.trim()) return;
      onSave(profile, note.trim());
    }}>
      <div>
        <span className="material-section-kicker">创建新版本</span>
        <h3>基于 v{snapshot.version} 修改</h3>
        <p className="card-description">历史版本不会被覆盖。这里保存后还不会立即切换，你可以检查新版本后再启用。</p>
      </div>
      <div className="persona-edit-fields">
        {Object.entries(profile).map(([key, value]) => (
          <label key={key}>
            {friendlyName(key, FIELD_LABEL)}
            <textarea
              rows={Math.min(6, Math.max(2, String(value).length > 100 ? 4 : 2))}
              value={value}
              onChange={(event) => setProfile((current) => ({ ...current, [key]: event.target.value }))}
            />
            <small>留空表示在新版本中移除这项，不会删除旧版本。</small>
          </label>
        ))}
      </div>
      <label>本次修改说明<input value={note} onChange={(event) => setNote(event.target.value)} placeholder="例如：补充真实学习方向和关系边界" maxLength={500} /></label>
      <div className="button-row">
        <button type="submit" className="btn btn-primary" disabled={busy || !note.trim()}>保存为新版本</button>
        <button type="button" className="btn btn-ghost" disabled={busy} onClick={onCancel}>取消</button>
      </div>
    </form>
  );
}

function readableDiffValue(value: unknown) {
  if (value === undefined || value === null || value === '') return '（无）';
  if (typeof value === 'string') return value;
  return JSON.stringify(value, null, 2);
}

function BaselinePanel({
  state,
  busy,
  onActivate,
}: {
  state: Awaited<ReturnType<typeof getPersonaBaseline>>;
  busy: boolean;
  onActivate: (draft: PersonaBaselineDraft) => void;
}) {
  const draft = state.draft;
  const reviewCount = Object.values(state.reviewSummary).reduce((sum, value) => sum + Number(value || 0), 0);
  return (
    <section className="card evolution-panel baseline-panel" aria-label="审核后人格基线">
      <div className="evolution-heading">
        <div>
          <h2>审核后人格基线</h2>
          <p>
            当前运行版本 v{state.activeSnapshot.version}；原始素材审核 {reviewCount} 条，待审核 {state.reviewSummary.pending ?? 0} 条
            {state.ownerConfirmationCount > 0 ? `；另有 ${state.ownerConfirmationCount} 条 Owner 补充确认` : ''}。
          </p>
        </div>
        <Badge tone={draft?.status === 'draft' ? 'warning' : draft?.status === 'activated' ? 'positive' : 'neutral'}>
          {draft?.status === 'draft' ? '等待你确认' : draft?.status === 'activated' ? '已激活' : draft ? '需要重建预览' : '暂无预览'}
        </Badge>
      </div>
      {!draft ? (
        <p className="muted-copy">尚未生成审核后的差异预览，当前人格不会自动切换。</p>
      ) : (
        <>
          <p className="muted-copy">
            保留既有结论 {draft.selectedClaimCount} 条，新增或修正 {draft.derivedClaimCount} 条，替代旧结论 {draft.supersededClaimCount} 条。
          </p>
          <div className="baseline-diff-groups">
            <BaselineDiffGroup title="人格事实变化" items={draft.profileDiff} />
            <BaselineDiffGroup title="聊天与朋友圈风格变化" items={draft.styleDiff} />
            <BaselineDiffGroup title="当前联系人画像变化" items={draft.contactProfileDiff} />
          </div>
          {draft.status === 'draft' && (
            <div className="evolution-control">
              <span>预览不会影响当前机器人；只有点击右侧按钮后才会切换。</span>
              <button type="button" className="btn btn-primary btn-sm" disabled={busy} onClick={() => onActivate(draft)}>
                我已看完，确认激活
              </button>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function BaselineDiffGroup({ title, items }: { title: string; items: PersonaBaselineDraft['profileDiff'] }) {
  return (
    <details className="evolution-event" open={items.length > 0 && items.length <= 4}>
      <summary>{title}（{items.length} 项）</summary>
      {items.length === 0 ? <p className="muted-copy">没有变化</p> : (
        <div className="baseline-diff-list">
          {items.map((item) => (
            <article key={item.key}>
              <strong>{friendlyName(item.key, FIELD_LABEL)}</strong>
              <div><span>原来</span><pre>{readableDiffValue(item.before)}</pre></div>
              <div><span>预览</span><pre>{readableDiffValue(item.after)}</pre></div>
            </article>
          ))}
        </div>
      )}
    </details>
  );
}

function EvolutionPanel({
  control,
  events,
  busy,
  baselineReady,
  onControl,
  onPublish,
  onRollback,
}: {
  control: { paused: boolean; reason: string; updatedAt: string };
  events: PersonaEvolutionEvent[];
  busy: boolean;
  baselineReady: boolean;
  onControl: (paused: boolean) => void;
  onPublish: (event: PersonaEvolutionEvent) => void;
  onRollback: (event: PersonaEvolutionEvent) => void;
}) {
  return (
    <section className="card evolution-panel" aria-label="人格自动演变">
      <div className="evolution-heading">
        <div>
          <h2>人格慢速演变</h2>
          <p>身份事实永久锁定；自动候选必须满足 3 份独立证据、14 天跨度和 0.85 置信度。</p>
        </div>
        <Badge tone={control.paused ? 'warning' : 'positive'}>{control.paused ? '已暂停' : '运行中'}</Badge>
      </div>
      <div className="evolution-control">
        <span>{control.reason}{control.updatedAt ? ` · ${new Date(control.updatedAt).toLocaleString()}` : ''}</span>
        <button type="button" className="btn btn-ghost btn-sm" disabled={busy || (control.paused && !baselineReady)} onClick={() => onControl(!control.paused)}>
          {control.paused ? (baselineReady ? '解除暂停' : '激活新人格后才能解除') : '暂停自动演变'}
        </button>
      </div>
      {events.length === 0 ? (
        <p className="muted-copy">尚无演变候选。用户命令不会直接生成或发布人格版本。</p>
      ) : (
        <div className="evolution-events">
          {events.map((event) => (
            <article key={event.id} className="evolution-event">
              <div>
                <strong>{event.dimension}</strong>
                <Badge tone={event.status === 'published' ? 'positive' : event.status === 'candidate' ? 'warning' : 'neutral'}>{event.status}</Badge>
              </div>
              <p>{event.reason}</p>
              <small>置信度 {Math.round(event.confidence * 100)}% · {event.evidenceIds.length} 份证据 · {event.evidenceStartAt.slice(0, 10)} 至 {event.evidenceEndAt.slice(0, 10)}</small>
              <details><summary>查看差异</summary><pre>{JSON.stringify({ before: event.before, after: event.after }, null, 2)}</pre></details>
              <div className="review-actions">
                {event.status === 'candidate' && <button type="button" className="btn btn-primary btn-sm" disabled={busy || control.paused} onClick={() => onPublish(event)}>确认发布</button>}
                {event.status === 'published' && <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={() => onRollback(event)}>回滚</button>}
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function MaterialCard({
  material,
  selected,
  busy,
  onSelected,
  onReview,
}: {
  material: MaterialReview;
  selected: boolean;
  busy: boolean;
  onSelected: (checked: boolean) => void;
  onReview: (input: { decision: MaterialReviewDecision; correctedContent?: string; splitContents?: string[]; reviewNote?: string }) => void;
}) {
  const [editor, setEditor] = useState<'corrected' | 'split' | null>(null);
  const [correctedContent, setCorrectedContent] = useState(material.correctedContent ?? material.content);
  const [splitText, setSplitText] = useState(material.splitContents.join('\n---\n'));
  const [reviewNote, setReviewNote] = useState(material.reviewNote);
  const splitContents = splitText.split(/^---$/m).map((part) => part.trim()).filter(Boolean);
  const reviewTarget = parseMaterialContent(material.content);
  const isEmptyPlaceholder = EMPTY_PLACEHOLDER_VALUES.has(reviewTarget.value.trim().toLowerCase());
  const reviewTargetId = `material-review-target-${material.id}`;
  const reviewDecisionId = `material-review-decision-${material.id}`;

  return (
    <article className={`card material-card${selected ? ' material-card-selected' : ''}`}>
      <div className="material-card-heading">
        <div className="material-card-meta">
          <Badge tone={decisionTone(material.decision)}>{DECISION_LABEL[material.decision]}</Badge>
          <Badge tone="info">{CHANNEL_LABEL[material.sourceChannel] ?? material.sourceChannel}</Badge>
          <span>可信度 {Math.round(material.reliability * 100)}%</span>
          {material.sourceTime && <span>{material.sourceTime}</span>}
        </div>
        <label className="material-select">
          <input type="checkbox" checked={selected} onChange={(event) => onSelected(event.target.checked)} />
          <span>加入批量审核</span>
        </label>
      </div>

      <section className="material-review-target" aria-labelledby={reviewTargetId}>
        <span className="material-section-kicker">你要审核的内容</span>
        <h2 id={reviewTargetId}>{reviewTarget.label}</h2>
        <p className="material-content">{reviewTarget.value || '（空内容）'}</p>
        <p className="material-target-help">请判断：这条内容是否正确、仍然有效，并且应该写入角色人设。</p>
        {isEmptyPlaceholder && (
          <p className="material-placeholder-warning" role="note">
            这是一条空占位值，通常应“排除这条”；如果你知道正确内容，也可以“修改后保留”。
          </p>
        )}
      </section>

      {material.conclusions.length > 0 && (
        <details className="material-conclusions">
          <summary>
            <span>系统理解（仅供参考）</span>
            <small>{material.conclusions.length} 条提取结论，不需要单独审核</small>
          </summary>
          <ul>
            {material.conclusions.map((conclusion) => (
              <li key={conclusion.claimId}>
                <strong>{friendlyName(conclusion.facet, FACET_LABEL)} / {friendlyName(conclusion.key, FIELD_LABEL)}</strong>
                <span>{conclusion.value}</span>
                <small>置信度 {Math.round(conclusion.confidence * 100)}% · {SUPPORT_KIND_LABEL[conclusion.supportKind] ?? conclusion.supportKind}</small>
              </li>
            ))}
          </ul>
          <details className="material-technical-details">
            <summary>查看技术详情</summary>
            <ul>
              {material.conclusions.map((conclusion) => (
                <li key={`${conclusion.claimId}-technical`}><code>{conclusion.facet} / {conclusion.key}</code></li>
              ))}
            </ul>
            {reviewTarget.fieldKey && <p>内容分类：{friendlyName(reviewTarget.fieldKey, FIELD_LABEL)}</p>}
          </details>
        </details>
      )}

      <section className="material-review-decision" aria-labelledby={reviewDecisionId}>
        <div className="material-decision-heading">
          <div>
            <span className="material-section-kicker">最后一步</span>
            <h3 id={reviewDecisionId}>请选择处理方式</h3>
          </div>
          <span>点击后会立即保存审核结果</span>
        </div>
        <div className="review-action-grid">
          <button type="button" className="review-action-option review-action-keep" disabled={busy} onClick={() => onReview({ decision: 'keep' })}>
            <strong>保留为人设</strong>
            <span>内容正确，允许进入新人格</span>
          </button>
          <button type="button" className="review-action-option" disabled={busy} onClick={() => onReview({ decision: 'exclude' })}>
            <strong>排除这条</strong>
            <span>内容错误、过期或无效，不进入人格</span>
          </button>
          <button type="button" className="review-action-option" disabled={busy} onClick={() => setEditor('corrected')}>
            <strong>修改后保留</strong>
            <span>意思基本正确，但文案需要修改</span>
          </button>
          <button type="button" className="review-action-option" disabled={busy} onClick={() => setEditor('split')}>
            <strong>拆成多条</strong>
            <span>一条内容中混合了多个独立事实</span>
          </button>
        </div>
      </section>
      {editor && (
        <div className="material-editor">
          {editor === 'corrected' ? (
            <label>修改后的完整内容<textarea rows={4} value={correctedContent} onChange={(event) => setCorrectedContent(event.target.value)} /></label>
          ) : (
            <label>拆分后的内容（用单独一行的 --- 分隔，至少两段）<textarea rows={6} value={splitText} onChange={(event) => setSplitText(event.target.value)} /></label>
          )}
          <label>审核备注（可选）<input value={reviewNote} onChange={(event) => setReviewNote(event.target.value)} /></label>
          <div className="review-actions">
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={busy || (editor === 'corrected' ? !correctedContent.trim() : splitContents.length < 2)}
              onClick={() => onReview(editor === 'corrected'
                ? { decision: 'corrected', correctedContent: correctedContent.trim(), reviewNote }
                : { decision: 'split', splitContents, reviewNote })}
            >保存这次处理</button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setEditor(null)}>取消</button>
          </div>
        </div>
      )}
    </article>
  );
}
