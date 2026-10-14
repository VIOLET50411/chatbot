import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Badge, PageHeader, QueryBoundary } from '../components';
import { dispatchMoment, getWechatBinding, getWechatStatus, importWebMedia, listMediaAssets, listMoments, listOwnerContacts, prepareMoment, reviewMediaAsset, updateWechatBinding } from '../api';
import type { MomentPost, WechatBinding } from '../api/types';
import { useCharacter } from '../features/characters';

const CAPABILITY_LABEL: Record<string, string> = {
  text: '文字',
  image: '图片',
  unicode_emoji: 'Unicode 表情',
  sticker_capture: '表情截图',
  sticker_send: '收藏表情发送',
  moment: '朋友圈',
};

export function WechatPage() {
  const { selected, selectedId } = useCharacter();
  const queryClient = useQueryClient();
  const [momentText, setMomentText] = useState('');
  const [momentTags, setMomentTags] = useState('');
  const [momentError, setMomentError] = useState('');
  const [webContactId, setWebContactId] = useState('');
  const [webSourcePage, setWebSourcePage] = useState('');
  const [webImageUrl, setWebImageUrl] = useState('');
  const [webPlatform, setWebPlatform] = useState<'xiaohongshu' | 'x' | 'other'>('xiaohongshu');
  const [webUsage, setWebUsage] = useState<'chat' | 'moments' | 'both'>('chat');
  const [webPresentation, setWebPresentation] = useState<'source_explicit' | 'private_roleplay'>('private_roleplay');
  const [webAttribution, setWebAttribution] = useState('');
  const [webDescription, setWebDescription] = useState('');
  const bindingQuery = useQuery({
    queryKey: ['wechat-binding', selectedId],
    queryFn: ({ signal }) => getWechatBinding(selectedId, signal),
    enabled: Boolean(selectedId),
  });
  const statusQuery = useQuery({
    queryKey: ['wechat-status', selectedId],
    queryFn: ({ signal }) => getWechatStatus(selectedId, signal),
    enabled: Boolean(selectedId),
    refetchInterval: 10_000,
  });
  const momentsQuery = useQuery({ queryKey: ['moments', selectedId], queryFn: ({ signal }) => listMoments(selectedId, signal), enabled: Boolean(selectedId) });
  const mediaQuery = useQuery({ queryKey: ['media-assets', selectedId], queryFn: ({ signal }) => listMediaAssets(selectedId, signal), enabled: Boolean(selectedId) });
  const contactsQuery = useQuery({ queryKey: ['owner-contacts', selectedId], queryFn: ({ signal }) => listOwnerContacts(selectedId, signal), enabled: Boolean(selectedId) });
  useEffect(() => {
    const enabled = (contactsQuery.data ?? []).filter((contact) => contact.enabled);
    if (!enabled.some((contact) => contact.id === webContactId)) setWebContactId(enabled[0]?.id ?? '');
  }, [contactsQuery.data, selectedId, webContactId]);
  const refreshMoments = () => {
    void queryClient.invalidateQueries({ queryKey: ['moments', selectedId] });
    void queryClient.invalidateQueries({ queryKey: ['wechat-status', selectedId] });
  };
  const prepareMutation = useMutation({
    mutationFn: (input: { text: string; privacyTags: string[] }) => prepareMoment(input, selectedId),
    onSuccess: () => { setMomentText(''); setMomentError(''); refreshMoments(); },
    onError: (cause) => setMomentError(cause instanceof Error ? cause.message : '朋友圈准备失败'),
  });
  const dispatchMutation = useMutation({
    mutationFn: (post: MomentPost) => dispatchMoment(post, selectedId),
    onSuccess: () => { setMomentError(''); refreshMoments(); },
    onError: (cause) => setMomentError(cause instanceof Error ? cause.message : '朋友圈发布入队失败'),
  });
  const mediaMutation = useMutation({
    mutationFn: (input: { id: string; reviewStatus: string }) => reviewMediaAsset(input, selectedId),
    onSuccess: () => {
      setMomentError('');
      void queryClient.invalidateQueries({ queryKey: ['media-assets', selectedId] });
      void queryClient.invalidateQueries({ queryKey: ['wechat-status', selectedId] });
    },
    onError: (cause) => setMomentError(cause instanceof Error ? cause.message : '媒体审核失败'),
  });
  const webMediaMutation = useMutation({
    mutationFn: () => importWebMedia({
      contactId: webContactId,
      sourcePageUrl: webSourcePage.trim(),
      imageUrl: webImageUrl.trim(),
      sourcePlatform: webPlatform,
      attribution: webAttribution.trim(),
      usageScope: webUsage,
      presentationMode: webPresentation,
      description: webDescription.trim(),
    }, selectedId),
    onSuccess: () => {
      setMomentError('');
      setWebSourcePage('');
      setWebImageUrl('');
      setWebAttribution('');
      setWebDescription('');
      void queryClient.invalidateQueries({ queryKey: ['media-assets', selectedId] });
    },
    onError: (cause) => setMomentError(cause instanceof Error ? cause.message : '网络图片导入失败'),
  });
  return (
    <div className="page owner-page admin-page">
      <PageHeader title={`${selected?.displayName ?? '当前人物'}的微信与媒体`} description="这里只显示当前人物绑定的微信账号、发送队列和媒体库；切换人物不会混用数据。" />
      <QueryBoundary query={bindingQuery} apiPath={`/api/characters/${selectedId}/wechat-binding`}>
        {(binding) => <WechatBindingEditor key={`${binding.characterId}-${binding.bindingVersion}`} binding={binding} onSaved={(next) => {
          queryClient.setQueryData(['wechat-binding', selectedId], next);
          void queryClient.invalidateQueries({ queryKey: ['characters'] });
        }} />}
      </QueryBoundary>
      <QueryBoundary query={statusQuery} apiPath="/api/wechat/status">
        {(status) => (
          <>
            <section className="card bridge-status-card">
              <div className="bridge-status-heading">
                <div>
                  <h2 className="card-title">Windows Plus 桥接</h2>
                  <p className="card-description">超过 30 秒未收到心跳即判定离线。</p>
                </div>
                <Badge tone={status.bridge.online && status.bridge.windowReady ? 'positive' : 'danger'}>
                  {status.bridge.online && status.bridge.windowReady ? '在线' : '离线'}
                </Badge>
              </div>
              <dl className="wechat-kv-grid">
                <div><dt>Plus</dt><dd>{status.bridge.plusActive ? '已激活/可用' : '未就绪'}</dd></div>
                <div><dt>微信窗口</dt><dd>{status.bridge.windowReady ? '可操作' : '不可操作'}</dd></div>
                <div><dt>微信版本</dt><dd>{status.bridge.wechatVersion || '未上报'}</dd></div>
                <div><dt>最后心跳</dt><dd>{status.bridge.lastSeenAt || '尚未连接'}</dd></div>
              </dl>
              <div className="capability-list" aria-label="桥接能力">
                {status.bridge.capabilities.length > 0
                  ? status.bridge.capabilities.map((item) => <Badge key={item} tone="info">{CAPABILITY_LABEL[item] ?? item}</Badge>)
                  : <span className="meta-line">当前没有已上报能力</span>}
              </div>
            </section>
            <section className="card-grid">
              <article className="stat-card"><p className="stat-card-value">{status.enabledContacts}</p><p className="stat-card-label">启用联系人</p></article>
              <article className="stat-card"><p className="stat-card-value">{status.inboundPending}</p><p className="stat-card-label">待处理入站消息</p></article>
              <article className="stat-card"><p className="stat-card-value">{(status.outbox.queued ?? 0) + (status.outbox.retry_wait ?? 0)}</p><p className="stat-card-label">待发送动作</p></article>
              <article className="stat-card"><p className="stat-card-value">{status.outbox.delivery_unknown ?? 0}</p><p className="stat-card-label">结果待人工确认</p></article>
              <article className="stat-card"><p className="stat-card-value">{status.attachments.pending ?? 0}</p><p className="stat-card-label">待审核图片/表情</p></article>
              <article className="stat-card"><p className="stat-card-value">{status.stickers.enabled}</p><p className="stat-card-label">启用表情资产</p></article>
              <article className="stat-card"><p className="stat-card-value">{status.moments.delivery_unknown ?? 0}</p><p className="stat-card-label">朋友圈待核验</p></article>
            </section>
            {(status.outbox.delivery_unknown ?? 0) > 0 && (
              <section className="card warning-card" role="status">
                <h2 className="card-title">存在发送结果不确定的动作</h2>
                <p className="card-description">系统已停止自动重发，避免重复消息。请在微信中人工确认后再处理。</p>
              </section>
            )}
          </>
        )}
      </QueryBoundary>
      <section className="card web-media-import-card" aria-labelledby="web-media-import-title">
        <div className="section-heading">
          <div>
            <h2 id="web-media-import-title">保存网上找到的图片</h2>
            <p className="muted">可保存小红书、X 或其他公开网页上的图片。系统内部始终保留来源；你也可以把它设为只发给当前白名单联系人的“私聊世界线图片”，用于增强沉浸感。</p>
          </div>
          <Badge tone="warning">导入后仍需审核</Badge>
        </div>
        <form className="configuration-form" onSubmit={(event) => { event.preventDefault(); webMediaMutation.mutate(); }}>
          <div className="form-grid-3">
            <label>发给哪个联系人
              <select value={webContactId} onChange={(event) => setWebContactId(event.target.value)} required>
                <option value="">请选择白名单联系人</option>
                {(contactsQuery.data ?? []).filter((contact) => contact.enabled).map((contact) => <option key={contact.id} value={contact.id}>{contact.displayName}</option>)}
              </select>
            </label>
            <label>来源平台<select value={webPlatform} onChange={(event) => setWebPlatform(event.target.value as typeof webPlatform)}><option value="xiaohongshu">小红书</option><option value="x">X</option><option value="other">其他公开网页</option></select></label>
            <label>允许用途<select value={webUsage} disabled={webPresentation === 'private_roleplay'} onChange={(event) => setWebUsage(event.target.value as typeof webUsage)}><option value="chat">只用于聊天</option><option value="moments">只用于朋友圈</option><option value="both">聊天和朋友圈</option></select></label>
          </div>
          <label>聊天里怎样呈现
            <select value={webPresentation} onChange={(event) => {
              const mode = event.target.value as typeof webPresentation;
              setWebPresentation(mode);
              if (mode === 'private_roleplay') setWebUsage('chat');
            }}>
              <option value="private_roleplay">私聊世界线图片（只发给所选联系人）</option>
              <option value="source_explicit">明确说是网上看到的图片</option>
            </select>
          </label>
          <p className="field-help">“私聊世界线”允许角色在你们的虚构世界线里自然分享场景图，不会机械补一句“网上找的”；该模式强制只用于私聊，不能进入朋友圈。</p>
          <label>原帖或原网页地址<input type="url" required value={webSourcePage} onChange={(event) => setWebSourcePage(event.target.value)} placeholder="https://www.xiaohongshu.com/explore/..." /></label>
          <label>图片文件直链<input type="url" required value={webImageUrl} onChange={(event) => setWebImageUrl(event.target.value)} placeholder="https://.../image.jpg" /></label>
          <p className="field-help">这里需要能直接打开图片文件的 HTTPS 地址，不是原帖地址。服务器只接受公开网络、10 MB 以内的 JPEG、PNG、WebP 或 GIF。</p>
          <label>图片内容说明<input required minLength={4} maxLength={2000} value={webDescription} onChange={(event) => setWebDescription(event.target.value)} placeholder="例如：重庆傍晚的江边夜景，远处有桥，没有人物和水印" /></label>
          <label>作者或来源标注（可选）<input maxLength={300} value={webAttribution} onChange={(event) => setWebAttribution(event.target.value)} placeholder="例如：小红书 @作者名" /></label>
          <button className="btn btn-primary" type="submit" disabled={webMediaMutation.isPending || !webContactId || !webSourcePage.trim() || !webImageUrl.trim() || webDescription.trim().length < 4}>{webMediaMutation.isPending ? '正在下载并校验…' : '导入到待审核图片库'}</button>
        </form>
      </section>
      <QueryBoundary query={mediaQuery} apiPath="/api/wechat/media" isEmpty={(items) => items.length === 0}>
        {(items) => (
          <section className="card">
            <h2 className="card-title">图片与表情审核库</h2>
            <p className="card-description">只有标记为“可用”的当前联系人资产，才可能被角色选择发送。</p>
            <div className="material-review-list">
              {items.map((asset) => (
                <article className="material-review-card" key={asset.id}>
                  <img className="media-review-thumb" src={`/api/wechat/media/${encodeURIComponent(asset.id)}/content?character_id=${encodeURIComponent(selectedId)}`} alt={asset.visionSummary || (asset.kind === 'image' ? '待审核图片' : '待审核表情')} />
                  <div><Badge tone={asset.reviewStatus === 'approved' ? 'positive' : asset.reviewStatus === 'rejected' ? 'danger' : 'neutral'}>{asset.reviewStatus}</Badge> <span className="meta-line">{asset.kind} · {asset.contactNames || '未知来源'}</span></div>
                  <p>{asset.visionSummary || '尚无视觉描述'}</p>
                  {asset.sourcePageUrl ? <p className="meta-line">网上找到 · {asset.sourcePlatform || '公开网页'} · {asset.presentationMode === 'private_roleplay' ? '私聊世界线' : '明确标注来源'} · 用途 {asset.usageScope || '未指定'}{asset.attribution ? ` · ${asset.attribution}` : ''} · <a href={asset.sourcePageUrl} target="_blank" rel="noreferrer">查看来源</a></p> : null}
                  <div className="button-row">
                    <button type="button" className="btn btn-primary btn-sm" disabled={mediaMutation.isPending} onClick={() => mediaMutation.mutate({ id: asset.id, reviewStatus: 'approved' })}>设为可用</button>
                    <button type="button" className="btn btn-danger btn-sm" disabled={mediaMutation.isPending} onClick={() => mediaMutation.mutate({ id: asset.id, reviewStatus: 'rejected' })}>排除</button>
                  </div>
                </article>
              ))}
            </div>
          </section>
        )}
      </QueryBoundary>
      <section className="card contact-create-card">
        <h2 className="card-title">朋友圈测试发布</h2>
        <p className="card-description">每 48 小时最多准备一条；必须填写微信标签。准备后仍需再次确认才会进入 Plus 发送队列。</p>
        <form
          className="contact-form"
          onSubmit={(event) => {
            event.preventDefault();
            const tags = momentTags.split(/[,，]/).map((item) => item.trim()).filter(Boolean);
            if (!momentText.trim() || tags.length === 0) return;
            prepareMutation.mutate({ text: momentText.trim(), privacyTags: tags });
          }}
        >
          <label>朋友圈正文<textarea value={momentText} onChange={(event) => setMomentText(event.target.value)} maxLength={1500} placeholder="先用低风险测试文案" /></label>
          <label>可见微信标签<input value={momentTags} onChange={(event) => setMomentTags(event.target.value)} placeholder="例如：SoulMate测试（多个用逗号分隔）" /></label>
          <button type="submit" className="btn btn-primary" disabled={prepareMutation.isPending}>准备朋友圈</button>
        </form>
        {momentError && <p className="dialog-error" role="alert">{momentError}</p>}
      </section>
      <QueryBoundary query={momentsQuery} apiPath="/api/wechat/moments" isEmpty={(items) => items.length === 0}>
        {(items) => (
          <section className="contact-list" aria-label="朋友圈发布记录">
            {items.map((post: MomentPost) => (
              <article className="card contact-card" key={post.id}>
                <div>
                  <div className="contact-card-heading"><h2>朋友圈</h2><Badge tone={post.status === 'published' ? 'positive' : post.status === 'delivery_unknown' ? 'danger' : 'info'}>{post.status}</Badge></div>
                  <p className="card-description">{post.text || '纯图片朋友圈'}</p>
                  <p className="meta-line">可见标签：{post.privacyTags.join('、')} · {post.createdAt}</p>
                  {post.errorSummary && <p className="dialog-error">{post.errorSummary}</p>}
                </div>
                {post.status === 'prepared' && (
                  <button
                    type="button"
                    className="btn btn-danger btn-sm"
                    disabled={dispatchMutation.isPending}
                    onClick={() => {
                      if (!window.confirm(`确定只向标签“${post.privacyTags.join('、')}”发布这条朋友圈吗？`)) return;
                      dispatchMutation.mutate(post);
                    }}
                  >确认发布</button>
                )}
              </article>
            ))}
          </section>
        )}
      </QueryBoundary>
    </div>
  );
}

function WechatBindingEditor({ binding, onSaved }: { binding: WechatBinding; onSaved: (binding: WechatBinding) => void }) {
  const [draft, setDraft] = useState(binding);
  const [message, setMessage] = useState('');
  useEffect(() => setDraft(binding), [binding]);
  const mutation = useMutation({
    mutationFn: () => updateWechatBinding(draft, binding.characterId),
    onSuccess: (next) => { setMessage('绑定已保存；宿主桥接下次同步会读取新版本'); onSaved(next); },
  });
  const changed = JSON.stringify({ ...draft, updatedAt: '' }) !== JSON.stringify({ ...binding, updatedAt: '' });
  const canEnable = Boolean(draft.windowNickname.trim() && draft.wechatId.trim() && draft.ownerRemark.trim() && draft.identityConfirmed);
  return (
    <section className="card wechat-binding-card" aria-labelledby="wechat-binding-title">
      <div className="section-heading">
        <div>
          <h2 id="wechat-binding-title">{binding.displayName}绑定的微信账号</h2>
          <p className="muted">内部账号 ID <code>{binding.accountId}</code> 永不随换绑改变，因此聊天、关系和记忆会继续保留。</p>
        </div>
        <Badge tone={binding.enabled && binding.identityConfirmed ? 'positive' : 'warning'}>{binding.enabled ? '允许桥接' : '未启用'}</Badge>
      </div>
      <form className="configuration-form" onSubmit={(event) => {
        event.preventDefault();
        setMessage('');
        if (draft.enabled && !canEnable) return;
        if (!window.confirm(`将${binding.displayName}绑定到微信窗口“${draft.windowNickname}”、微信号“${draft.wechatId}”。错误绑定可能把消息发到另一个账号，确认继续吗？`)) return;
        mutation.mutate();
      }}>
        <div className="form-grid-3">
          <label>微信窗口顶部昵称<input value={draft.windowNickname} onChange={(event) => setDraft((current) => ({ ...current, windowNickname: event.target.value }))} placeholder="例如：Locin" /></label>
          <label>这个机器人的微信号<input value={draft.wechatId} onChange={(event) => setDraft((current) => ({ ...current, wechatId: event.target.value }))} placeholder="例如：Theonlywan1" /></label>
          <label>它看到你的备注<input value={draft.ownerRemark} onChange={(event) => setDraft((current) => ({ ...current, ownerRemark: event.target.value }))} placeholder="例如：VIOLET" /></label>
        </div>
        <label className="checkbox-line"><input type="checkbox" checked={draft.identityConfirmed} onChange={(event) => setDraft((current) => ({ ...current, identityConfirmed: event.target.checked, enabled: event.target.checked ? current.enabled : false }))} /><span>我已在这个微信窗口中核对微信号，确认账号身份正确</span></label>
        <label className="checkbox-line"><input type="checkbox" checked={draft.enabled} disabled={!canEnable} onChange={(event) => setDraft((current) => ({ ...current, enabled: event.target.checked }))} /><span>允许宿主桥接读取这个绑定（仍不会自动开启主动消息或朋友圈）</span></label>
        {!canEnable && draft.enabled ? <p className="error-text">先填写三个字段并确认账号身份，才能启用。</p> : null}
        <div className="button-row">
          <button type="submit" className="btn btn-primary" disabled={!changed || mutation.isPending || (draft.enabled && !canEnable)}>{mutation.isPending ? '保存中…' : '保存微信绑定'}</button>
          <span className="muted">配置版本 {binding.bindingVersion} · {binding.updatedAt ? new Date(binding.updatedAt).toLocaleString() : '未更新'}</span>
          {message ? <span className="success-text" role="status">{message}</span> : null}
          {mutation.error ? <span className="error-text" role="alert">{mutation.error.message}</span> : null}
        </div>
      </form>
    </section>
  );
}
