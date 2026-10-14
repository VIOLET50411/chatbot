import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Badge, EmptyState, PageHeader, QueryBoundary } from '../components';
import { bindOwnerContactToPwa, createOwnerContact, getContactRuntime, listContactCandidates, listOwnerContacts, reviewContactCandidate, reviewContactMemory, setOwnerContactEnabled, updateOwnerContactDelivery } from '../api';
import type { ContactCandidate, OwnerContact } from '../api/types';
import { useCharacter } from '../features/characters';

export function ContactsPage() {
  const { selected, selectedId } = useCharacter();
  const queryClient = useQueryClient();
  const [externalId, setExternalId] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [error, setError] = useState('');
  const [selectedContactId, setSelectedContactId] = useState<string | null>(null);
  const contactsQuery = useQuery({
    queryKey: ['owner-contacts', selectedId],
    queryFn: ({ signal }) => listOwnerContacts(selectedId, signal),
    enabled: Boolean(selectedId),
  });
  const candidatesQuery = useQuery({
    queryKey: ['contact-candidates', selectedId],
    queryFn: ({ signal }) => listContactCandidates(selectedId, signal),
    enabled: Boolean(selectedId),
    refetchInterval: 15000,
  });
  useEffect(() => setSelectedContactId(null), [selectedId]);
  const refresh = () => Promise.all([
    queryClient.invalidateQueries({ queryKey: ['owner-contacts', selectedId] }),
    queryClient.invalidateQueries({ queryKey: ['contact-candidates', selectedId] }),
  ]);
  const createMutation = useMutation({
    mutationFn: (input: { externalId: string; displayName: string }) => createOwnerContact(input, selectedId),
    onSuccess: () => { setExternalId(''); setDisplayName(''); setError(''); void refresh(); },
    onError: (cause) => setError(cause instanceof Error ? cause.message : '联系人创建失败'),
  });
  const toggleMutation = useMutation({
    mutationFn: ({ contact, enabled }: { contact: OwnerContact; enabled: boolean }) => setOwnerContactEnabled(contact, enabled, selectedId),
    onSuccess: () => { setError(''); void refresh(); },
    onError: (cause) => setError(cause instanceof Error ? cause.message : '联系人状态修改失败'),
  });
  const pwaBindingMutation = useMutation({
    mutationFn: (contact: OwnerContact) => bindOwnerContactToPwa(contact, selectedId),
    onSuccess: () => { setError(''); void refresh(); },
    onError: (cause) => setError(cause instanceof Error ? cause.message : 'PWA 接续对象设置失败'),
  });
  const deliveryMutation = useMutation({
    mutationFn: (input: {
      contact: OwnerContact;
      proactiveEnabled: boolean;
      maxDaily: number;
      cooldownHours: number;
      activeStartHour: number;
      activeEndHour: number;
      checkInAfterHours: number;
    }) => updateOwnerContactDelivery(input, selectedId),
    onSuccess: () => { setError(''); void refresh(); },
    onError: (cause) => setError(cause instanceof Error ? cause.message : '主动消息设置保存失败'),
  });
  const candidateMutation = useMutation({
    mutationFn: async ({ candidate, bindPwa }: { candidate: ContactCandidate; bindPwa: boolean }) => {
      const contact = await reviewContactCandidate(candidate, 'confirm', selectedId);
      if (bindPwa && contact) await bindOwnerContactToPwa(contact, selectedId);
    },
    onSuccess: () => { setError(''); void refresh(); },
    onError: (cause) => setError(cause instanceof Error ? cause.message : '微信会话确认失败'),
  });
  const ignoreCandidateMutation = useMutation({
    mutationFn: (candidate: ContactCandidate) => reviewContactCandidate(candidate, 'ignore', selectedId),
    onSuccess: () => { setError(''); void refresh(); },
    onError: (cause) => setError(cause instanceof Error ? cause.message : '微信会话忽略失败'),
  });
  const runtimeQuery = useQuery({
    queryKey: ['contact-runtime', selectedId, selectedContactId],
    queryFn: ({ signal }) => getContactRuntime(selectedContactId as string, selectedId, signal),
    enabled: Boolean(selectedId && selectedContactId),
  });
  const memoryMutation = useMutation({
    mutationFn: (input: { contactId: string; memoryId: string; content?: string; status?: string }) => reviewContactMemory(input, selectedId),
    onSuccess: () => {
      setError('');
      void queryClient.invalidateQueries({ queryKey: ['contact-runtime', selectedId, selectedContactId] });
    },
    onError: (cause) => setError(cause instanceof Error ? cause.message : '记忆修改失败'),
  });
  const memoryLayerLabel = (layer: string) => ({ core: '长期资料', daily: '近期记忆', diary: '重要日记' }[layer] ?? layer);

  return (
    <div className="page owner-page admin-page">
      <PageHeader title={`${selected?.displayName ?? '当前人物'}的联系人`} description="每位好友拥有独立的对话、记忆和关系状态；切换人物后会看到另一套完全分开的数据。" />
      <section className="card contact-create-card admin-glass-surface">
        <h2 className="card-title">从 Plus 发现的微信会话中确认</h2>
        <p className="card-description">这里显示微信会话列表顶部能看到的备注或昵称。你只需认名字，不需要寻找微信 ID。</p>
        <QueryBoundary query={candidatesQuery} apiPath="/api/contacts/candidates" isEmpty={(items) => items.length === 0} emptyState={<EmptyState title="还没有发现待确认会话" description="启动对应微信窗口的 Plus 桥接后，最近会话会自动出现在这里。" />}>
          {(items) => (
            <div className="contact-candidate-list">
              {items.map((candidate) => (
                <article className="contact-candidate" key={candidate.id}>
                  <div><strong>{candidate.displayName}</strong><p className="meta-line">最近发现：{new Date(candidate.lastSeenAt).toLocaleString('zh-CN')}</p></div>
                  <div className="button-row">
                    <button type="button" className="btn btn-primary btn-sm" disabled={candidateMutation.isPending} onClick={() => candidateMutation.mutate({ candidate, bindPwa: true })}>这是我，并接续到 PWA</button>
                    <button type="button" className="btn btn-secondary btn-sm" disabled={candidateMutation.isPending} onClick={() => candidateMutation.mutate({ candidate, bindPwa: false })}>加入白名单</button>
                    <button type="button" className="btn btn-ghost btn-sm" disabled={ignoreCandidateMutation.isPending} onClick={() => ignoreCandidateMutation.mutate(candidate)}>忽略</button>
                  </div>
                </article>
              ))}
            </div>
          )}
        </QueryBoundary>
        <details className="manual-contact-entry">
          <summary>Plus 没有发现时，手动添加</summary>
          <form
            className="contact-form"
            onSubmit={(event) => {
              event.preventDefault();
              if (!externalId.trim() || !displayName.trim()) return;
              createMutation.mutate({ externalId: externalId.trim(), displayName: displayName.trim() });
            }}
          >
            <label>系统识别号（自己取一个不重复的英文名即可）<input value={externalId} onChange={(event) => setExternalId(event.target.value)} placeholder="例如 owner-main" autoComplete="off" /></label>
            <label>微信里显示的备注或昵称<input value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="例如你在这个小号里的备注" /></label>
            <button type="submit" className="btn btn-primary" disabled={createMutation.isPending}>添加到白名单</button>
          </form>
        </details>
        {error && <p className="dialog-error" role="alert">{error}</p>}
      </section>
      <QueryBoundary query={contactsQuery} apiPath="/api/contacts" isEmpty={(items) => items.length === 0}>
        {(items) => (
          <section className="contact-list" aria-label="联系人列表">
            {items.map((contact) => (
              <article className="card contact-card" key={contact.id}>
                <div>
                  <div className="contact-card-heading">
                    <h2>{contact.displayName}</h2>
                    <Badge tone={contact.enabled ? 'positive' : 'neutral'}>{contact.enabled ? '监听中' : '已停用'}</Badge>
                    {contact.pwaBound && <Badge tone="info">PWA 接续的是我</Badge>}
                    <Badge tone={contact.proactiveEnabled ? 'positive' : 'neutral'}>{contact.proactiveEnabled ? '会主动联系' : '只回复，不主动发'}</Badge>
                  </div>
                  <p className="card-description">关系、记忆与时间线按此联系人严格隔离。</p>
                </div>
                <button
                  type="button"
                  className={contact.enabled ? 'btn btn-danger btn-sm' : 'btn btn-primary btn-sm'}
                  disabled={toggleMutation.isPending}
                  onClick={() => {
                    if (contact.enabled && !window.confirm(`确定停用“${contact.displayName}”吗？历史不会删除，机器人将停止回复。`)) return;
                    toggleMutation.mutate({ contact, enabled: !contact.enabled });
                  }}
                >
                  {contact.enabled ? '停用' : '重新启用'}
                </button>
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => setSelectedContactId(contact.id)}>
                  查看关系与记忆
                </button>
                {!contact.pwaBound && (
                  <button
                    type="button"
                    className="btn btn-secondary btn-sm"
                    disabled={!contact.enabled || pwaBindingMutation.isPending}
                    onClick={() => {
                      if (window.confirm(`确认让 ${selected?.displayName ?? '当前人物'} 的 PWA 对话接续“${contact.displayName}”这份微信关系和记忆吗？`)) {
                        pwaBindingMutation.mutate(contact);
                      }
                    }}
                  >
                    在 PWA 中接续此人
                  </button>
                )}
                <details className="contact-delivery-settings">
                  <summary>主动消息对象与频率</summary>
                  <form
                    className="contact-delivery-form"
                    key={`${contact.id}-${contact.proactiveEnabled}-${contact.proactiveMaxDaily}-${contact.proactiveCooldownHours}`}
                    onSubmit={(event) => {
                      event.preventDefault();
                      const form = new FormData(event.currentTarget);
                      deliveryMutation.mutate({
                        contact,
                        proactiveEnabled: form.has('proactiveEnabled'),
                        maxDaily: Number(form.get('maxDaily')),
                        cooldownHours: Number(form.get('cooldownHours')),
                        activeStartHour: Number(form.get('activeStartHour')),
                        activeEndHour: Number(form.get('activeEndHour')),
                        checkInAfterHours: Number(form.get('checkInAfterHours')),
                      });
                    }}
                  >
                    <label className="checkbox-line">
                      <input name="proactiveEnabled" type="checkbox" defaultChecked={contact.proactiveEnabled} />
                      <span>允许此人收到主动消息</span>
                    </label>
                    <p className="field-help">关闭后仍会正常回复对方。新加入的联系人默认关闭，避免误发给别人。</p>
                    <div className="form-grid-3">
                      <label>每天最多几次<input name="maxDaily" type="number" min={1} max={10} defaultValue={contact.proactiveMaxDaily} /></label>
                      <label>至少间隔几小时<input name="cooldownHours" type="number" min={1} max={48} defaultValue={contact.proactiveCooldownHours} /></label>
                      <label>多久没聊可问候<input name="checkInAfterHours" type="number" min={1} max={168} defaultValue={contact.proactiveCheckInAfterHours} /></label>
                      <label>最早发送时间<input name="activeStartHour" type="number" min={0} max={23} defaultValue={contact.proactiveActiveStartHour} /></label>
                      <label>最晚发送时间<input name="activeEndHour" type="number" min={1} max={24} defaultValue={contact.proactiveActiveEndHour} /></label>
                    </div>
                    <button type="submit" className="btn btn-primary btn-sm" disabled={deliveryMutation.isPending}>保存此人的主动消息设置</button>
                  </form>
                </details>
              </article>
            ))}
          </section>
        )}
      </QueryBoundary>
      {selectedContactId && (
        <QueryBoundary query={runtimeQuery} apiPath={`/api/contacts/${selectedContactId}/runtime`}>
          {(runtime) => (
            <section className="card contact-runtime-card">
              <div className="bridge-status-heading">
                <div><h2 className="card-title">联系人独立运行状态</h2><p className="card-description">关系阶段只能由互动演进；这里仅允许纠正或归档记忆。</p></div>
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => setSelectedContactId(null)}>关闭</button>
              </div>
              <dl className="wechat-kv-grid">
                <div><dt>阶段</dt><dd>{runtime.relationship.stage}</dd></div>
                <div><dt>信任</dt><dd>{runtime.relationship.trust.toFixed(1)}</dd></div>
                <div><dt>亲密</dt><dd>{runtime.relationship.intimacy.toFixed(1)}</dd></div>
                <div><dt>好感</dt><dd>{runtime.relationship.affection.toFixed(1)}</dd></div>
                <div><dt>舒适度</dt><dd>{runtime.relationship.comfort.toFixed(1)}</dd></div>
                <div><dt>积怨</dt><dd>{runtime.relationship.resentment.toFixed(1)}</dd></div>
              </dl>
              <h3>对话接续</h3>
              {runtime.conversationCheckpoint ? (
                <article className="memory-context-card">
                  <p>{runtime.conversationCheckpoint.summaryText}</p>
                  {runtime.conversationCheckpoint.openTopics.length > 0 && <p className="meta-line">还没聊完：{runtime.conversationCheckpoint.openTopics.join('、')}</p>}
                  {runtime.conversationCheckpoint.promises.length > 0 && <p className="meta-line">约定：{runtime.conversationCheckpoint.promises.join('、')}</p>}
                  <span className="meta-line">最近整理：{runtime.conversationCheckpoint.updatedAt}</span>
                </article>
              ) : <p className="meta-line">聊天还没长到需要整理，系统会保留最近原文。</p>}
              {runtime.pendingTopic && <p className="meta-line">待自然延续的话题：{runtime.pendingTopic.topicText}（到期后仍需有合适内容才会主动联系）</p>}
              {(runtime.memoryAssociations.entities.length > 0 || runtime.memoryAssociations.episodes.length > 0) && (
                <div className="memory-association-list">
                  <p className="meta-line">记忆关联</p>
                  {runtime.memoryAssociations.entities.slice(0, 8).map((entity) => <span className="tag" key={entity.id}>{entity.name}</span>)}
                  {runtime.memoryAssociations.episodes.slice(0, 5).map((episode) => <span className="tag" key={episode.id}>{episode.title}</span>)}
                </div>
              )}
              <h3>记忆</h3>
              {runtime.memories.length === 0 ? <p className="meta-line">尚无联系人记忆</p> : runtime.memories.map((memory) => (
                <article className="material-review-card" key={memory.id}>
                  <div><Badge tone={memory.status === 'published' ? 'positive' : 'neutral'}>{memory.status}</Badge> <span className="meta-line">{memoryLayerLabel(memory.layer)} · {memory.type} · 重要度 {memory.importance} · 被自然想起 {memory.recallCount} 次</span></div>
                  <p>{memory.content}</p>
                  <div className="button-row">
                    <button type="button" className="btn btn-secondary btn-sm" onClick={() => {
                      const corrected = window.prompt('修正这条记忆；不会改变其原始证据：', memory.content);
                      if (corrected && corrected.trim() !== memory.content) memoryMutation.mutate({ contactId: selectedContactId, memoryId: memory.id, content: corrected.trim() });
                    }}>纠正文案</button>
                    {memory.status !== 'published' && <button type="button" className="btn btn-primary btn-sm" onClick={() => memoryMutation.mutate({ contactId: selectedContactId, memoryId: memory.id, status: 'published' })}>确认可用</button>}
                    {memory.status !== 'archived' && <button type="button" className="btn btn-danger btn-sm" onClick={() => {
                      if (window.confirm('确定归档这条记忆吗？归档后不会再被对话召回。')) memoryMutation.mutate({ contactId: selectedContactId, memoryId: memory.id, status: 'archived' });
                    }}>归档</button>}
                  </div>
                </article>
              ))}
              <h3>最近时间线</h3>
              <div className="timeline-list">
                {runtime.timeline.length === 0 ? <p className="meta-line">尚无对话</p> : runtime.timeline.map((item) => (
                  <p key={item.id}><strong>{item.role === 'user' ? '好友' : '角色'}：</strong>{item.content}<span className="meta-line"> {item.createdAt}</span></p>
                ))}
              </div>
            </section>
          )}
        </QueryBoundary>
      )}
    </div>
  );
}
