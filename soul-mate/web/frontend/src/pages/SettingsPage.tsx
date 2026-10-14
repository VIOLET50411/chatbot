import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  fetchPlatformSettings,
  fetchDevices,
  createDevice,
  updateDevice,
  rotateDeviceToken,
  fetchExternalInformationStatus,
  fetchRuntimePrompt,
  fetchRuntimePromptHistory,
  restoreRuntimePrompt,
  refreshExternalInformation,
  saveRuntimePrompt,
  fetchCharacterRuntimePrompt,
  fetchCharacterRuntimePromptHistory,
  saveCharacterRuntimePrompt,
  restoreCharacterRuntimePrompt,
} from '../api';
import { PageHeader, QueryBoundary } from '../components';
import type { OwnerDevice } from '../api/types';
import { RuntimeConfigurationPanel } from '../features/settings/RuntimeConfigurationPanel';
import { useCharacter } from '../features/characters';

const DEVICE_CONSENT_LABELS: Array<[string, string]> = [
  ['device_status', '电量、充电与专注模式'],
  ['app_activity', '最近打开的 App'],
  ['city', '所在城市（不接收经纬度）'],
  ['health_activity', '步数与活动能量'],
  ['health_sleep', '睡眠时长'],
  ['health_heart', '心率摘要'],
];

function formatTime(value: string): string {
  if (!value) return '时间未知';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN');
}

/** 日志、模型与跨角色总提示词。 */
export function SettingsPage() {
  const { selected, selectedId } = useCharacter();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState('');
  const [characterDraft, setCharacterDraft] = useState('');
  const [savedMessage, setSavedMessage] = useState('');
  const [deviceName, setDeviceName] = useState('iPhone 17');
  const [retentionDays, setRetentionDays] = useState(7);
  const [issuedToken, setIssuedToken] = useState('');
  const settingsQuery = useQuery({
    queryKey: ['platform-settings'],
    queryFn: ({ signal }) => fetchPlatformSettings(signal),
  });
  const promptQuery = useQuery({
    queryKey: ['runtime-prompt'],
    queryFn: ({ signal }) => fetchRuntimePrompt(signal),
  });
  const historyQuery = useQuery({
    queryKey: ['runtime-prompt-history'],
    queryFn: ({ signal }) => fetchRuntimePromptHistory(signal),
  });
  const characterPromptQuery = useQuery({
    queryKey: ['character-runtime-prompt', selectedId],
    queryFn: ({ signal }) => fetchCharacterRuntimePrompt(selectedId, signal),
    enabled: Boolean(selectedId),
  });
  const characterPromptHistoryQuery = useQuery({
    queryKey: ['character-runtime-prompt-history', selectedId],
    queryFn: ({ signal }) => fetchCharacterRuntimePromptHistory(selectedId, signal),
    enabled: Boolean(selectedId),
  });
  const externalQuery = useQuery({
    queryKey: ['external-information', selectedId],
    queryFn: ({ signal }) => fetchExternalInformationStatus(selectedId, signal),
    enabled: Boolean(selectedId),
  });
  const devicesQuery = useQuery({
    queryKey: ['owner-devices'],
    queryFn: ({ signal }) => fetchDevices(signal),
    refetchInterval: 30000,
  });

  useEffect(() => {
    if (promptQuery.data) setDraft(promptQuery.data.prompt);
  }, [promptQuery.data]);
  useEffect(() => {
    setCharacterDraft(characterPromptQuery.data?.prompt ?? '');
  }, [characterPromptQuery.data, selectedId]);

  const refreshPrompt = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['runtime-prompt'] }),
      queryClient.invalidateQueries({ queryKey: ['runtime-prompt-history'] }),
    ]);
  };
  const saveMutation = useMutation({
    mutationFn: () => saveRuntimePrompt(draft),
    onSuccess: async () => {
      setSavedMessage('已保存，下一轮所有角色回复都会使用');
      await refreshPrompt();
    },
  });
  const restoreMutation = useMutation({
    mutationFn: (versionId: string) => restoreRuntimePrompt(versionId),
    onSuccess: async (result) => {
      setDraft(result.prompt);
      setSavedMessage('已恢复为所选历史内容');
      await refreshPrompt();
    },
  });
  const refreshCharacterPrompt = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['character-runtime-prompt', selectedId] }),
      queryClient.invalidateQueries({ queryKey: ['character-runtime-prompt-history', selectedId] }),
    ]);
  };
  const saveCharacterPromptMutation = useMutation({
    mutationFn: () => saveCharacterRuntimePrompt(characterDraft, selectedId),
    onSuccess: async () => {
      setSavedMessage(`${selected?.displayName ?? '当前人物'}的补充提示词已保存`);
      await refreshCharacterPrompt();
    },
  });
  const restoreCharacterPromptMutation = useMutation({
    mutationFn: (versionId: string) => restoreCharacterRuntimePrompt(versionId, selectedId),
    onSuccess: async (result) => {
      setCharacterDraft(result.prompt);
      setSavedMessage(`${selected?.displayName ?? '当前人物'}的补充提示词已恢复`);
      await refreshCharacterPrompt();
    },
  });
  const externalRefreshMutation = useMutation({
    mutationFn: () => refreshExternalInformation(selectedId),
    onSuccess: (result) => queryClient.setQueryData(['external-information', selectedId], result),
  });
  const refreshDevices = () => queryClient.invalidateQueries({ queryKey: ['owner-devices'] });
  const createDeviceMutation = useMutation({
    mutationFn: () => createDevice(deviceName.trim(), retentionDays),
    onSuccess: async (device) => {
      setIssuedToken(device.deviceToken);
      await refreshDevices();
    },
  });
  const updateDeviceMutation = useMutation({
    mutationFn: ({ device, update }: { device: OwnerDevice; update: { enabled?: boolean; retentionDays?: number; consent?: Record<string, boolean> } }) => updateDevice(device, update),
    onSuccess: refreshDevices,
  });
  const rotateTokenMutation = useMutation({
    mutationFn: rotateDeviceToken,
    onSuccess: (token) => setIssuedToken(token),
  });
  const changed = draft !== (promptQuery.data?.prompt ?? '');
  const characterPromptChanged = characterDraft !== (characterPromptQuery.data?.prompt ?? '');

  return (
    <div className="page admin-page">
      <PageHeader title="日志与设置" description="设置会直接影响运行，但不会偷偷改写任何人物档案。" />

      <section className="card runtime-prompt-editor" aria-labelledby="runtime-prompt-title">
        <div className="section-heading">
          <div>
            <h2 id="runtime-prompt-title">所有人物共用的总提示词</h2>
            <p className="muted">每轮回答时临时使用，不会写入杜佳颖或纪雨的人设快照。留空就是不额外限制。</p>
          </div>
          <span className="muted">{draft.length} / 12000</span>
        </div>
        <QueryBoundary query={promptQuery} apiPath="/api/runtime-prompt">
          {() => (
            <form
              className="inline-form"
              onSubmit={(event) => {
                event.preventDefault();
                setSavedMessage('');
                saveMutation.mutate();
              }}
            >
              <label htmlFor="global-runtime-prompt">提示词内容</label>
              <textarea
                id="global-runtime-prompt"
                value={draft}
                maxLength={12000}
                onChange={(event) => {
                  setDraft(event.target.value);
                  setSavedMessage('');
                }}
                placeholder="例如：回复时避免客服式总结；不确定实时事实时先核实来源"
              />
              <div className="button-row">
                <button className="btn btn-primary" type="submit" disabled={!changed || saveMutation.isPending}>
                  {saveMutation.isPending ? '保存中…' : '保存并立即生效'}
                </button>
                {savedMessage ? <span className="success-text" role="status">{savedMessage}</span> : null}
                {saveMutation.error ? <span className="error-text" role="alert">{saveMutation.error.message}</span> : null}
              </div>
            </form>
          )}
        </QueryBoundary>

        <details className="runtime-prompt-history">
          <summary>查看过去版本</summary>
          <QueryBoundary query={historyQuery} apiPath="/api/runtime-prompt/history">
            {(items) => items.length ? (
              <ul className="plain-list">
                {items.map((item) => (
                  <li key={item.id}>
                    <div>
                      <strong>{item.isActive ? '当前版本' : formatTime(item.createdAt)}</strong>
                      <p className="muted">{item.prompt ? item.prompt.slice(0, 140) : '（空提示词）'}</p>
                    </div>
                    {!item.isActive ? (
                      <button
                        className="btn btn-ghost btn-sm"
                        type="button"
                        disabled={restoreMutation.isPending}
                        onClick={() => restoreMutation.mutate(item.id)}
                      >
                        恢复这个版本
                      </button>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : <p className="muted">还没有保存过总提示词。</p>}
          </QueryBoundary>
        </details>
      </section>

      <section className="card runtime-prompt-editor" aria-labelledby="character-runtime-prompt-title">
        <div className="section-heading">
          <div>
            <h2 id="character-runtime-prompt-title">只给{selected?.displayName ?? '当前人物'}使用的补充提示词</h2>
            <p className="muted">叠加在总提示词之后，仅影响当前人物的回复，不会写入人格快照，也不会影响另一个人物。</p>
          </div>
          <span className="muted">{characterDraft.length} / 12000</span>
        </div>
        <QueryBoundary query={characterPromptQuery} apiPath="/api/runtime-prompt/character/current">
          {() => (
            <form className="inline-form" onSubmit={(event) => { event.preventDefault(); setSavedMessage(''); saveCharacterPromptMutation.mutate(); }}>
              <label htmlFor="character-runtime-prompt">补充提示词内容</label>
              <textarea
                id="character-runtime-prompt"
                value={characterDraft}
                maxLength={12000}
                onChange={(event) => { setCharacterDraft(event.target.value); setSavedMessage(''); }}
            placeholder={`例如：${selected?.displayName ?? '当前人物'}聊天时优先使用自然短句，避免不合时宜的生僻词`}
              />
              <div className="button-row">
                <button className="btn btn-primary" type="submit" disabled={!characterPromptChanged || saveCharacterPromptMutation.isPending}>保存当前人物补充</button>
                {saveCharacterPromptMutation.error ? <span className="error-text" role="alert">{saveCharacterPromptMutation.error.message}</span> : null}
              </div>
            </form>
          )}
        </QueryBoundary>
        <details className="runtime-prompt-history">
          <summary>查看{selected?.displayName ?? '当前人物'}的过去版本</summary>
          <QueryBoundary query={characterPromptHistoryQuery} apiPath="/api/runtime-prompt/character/history">
            {(items) => items.length ? (
              <ul className="plain-list">
                {items.map((item) => (
                  <li key={item.id}>
                    <div><strong>{item.isActive ? '当前版本' : formatTime(item.createdAt)}</strong><p className="muted">{item.prompt ? item.prompt.slice(0, 140) : '（空提示词）'}</p></div>
                    {!item.isActive ? <button className="btn btn-ghost btn-sm" type="button" disabled={restoreCharacterPromptMutation.isPending} onClick={() => restoreCharacterPromptMutation.mutate(item.id)}>恢复这个版本</button> : null}
                  </li>
                ))}
              </ul>
            ) : <p className="muted">这个人物还没有保存过补充提示词。</p>}
          </QueryBoundary>
        </details>
      </section>

      <RuntimeConfigurationPanel />

      <section className="card device-settings-card" aria-labelledby="device-settings-title">
        <div className="section-heading">
          <div>
            <h2 id="device-settings-title">手机状态与健康数据</h2>
            <p className="muted">由你的 iPhone 快捷指令主动上传。每一类数据都可停用，机器人只能看到你授权且已上传的内容。</p>
          </div>
        </div>
        <QueryBoundary query={devicesQuery} apiPath="/api/devices">
          {(status) => (
            <>
              {status.devices.length === 0 ? (
                <form className="device-create-form" onSubmit={(event) => { event.preventDefault(); if (deviceName.trim()) createDeviceMutation.mutate(); }}>
                  <label>设备名称<input value={deviceName} maxLength={80} onChange={(event) => setDeviceName(event.target.value)} /></label>
                  <label>保留天数<input type="number" min={1} max={90} value={retentionDays} onChange={(event) => setRetentionDays(Number(event.target.value))} /></label>
                  <button type="submit" className="btn btn-primary" disabled={createDeviceMutation.isPending}>生成手机接入口令</button>
                </form>
              ) : (
                <div className="device-list">
                  {status.devices.map((device) => {
                    const latest = status.records.find((record) => record.deviceId === device.id);
                    return (
                      <article className="device-item" key={device.id}>
                        <div>
                          <strong>{device.displayName}</strong>
                          <p className="muted">{device.enabled ? '允许接收' : '已暂停'} · 保存 {device.retentionDays} 天 · {latest ? `最近收到 ${formatTime(latest.capturedAt)}` : '还没收到数据'}</p>
                          <fieldset className="device-consent-grid">
                            <legend>允许机器人看到</legend>
                            {DEVICE_CONSENT_LABELS.map(([key, label]) => (
                              <label key={key}>
                                <input
                                  type="checkbox"
                                  checked={Boolean(device.consent[key])}
                                  disabled={updateDeviceMutation.isPending}
                                  onChange={(event) => updateDeviceMutation.mutate({ device, update: { consent: { ...device.consent, [key]: event.target.checked } } })}
                                />
                                <span>{label}</span>
                              </label>
                            ))}
                          </fieldset>
                        </div>
                        <div className="button-row">
                          <button type="button" className="btn btn-secondary btn-sm" disabled={updateDeviceMutation.isPending} onClick={() => updateDeviceMutation.mutate({ device, update: { enabled: !device.enabled } })}>{device.enabled ? '暂停接收' : '恢复接收'}</button>
                          <button type="button" className="btn btn-ghost btn-sm" disabled={rotateTokenMutation.isPending} onClick={() => { if (window.confirm('更新口令后，旧快捷指令会立即失效。确定继续吗？')) rotateTokenMutation.mutate(device); }}>更新接入口令</button>
                        </div>
                      </article>
                    );
                  })}
                </div>
              )}
              {status.records[0] ? (
                <details className="device-latest-data">
                  <summary>查看最近一次上传的数据</summary>
                  <pre>{JSON.stringify(status.records[0].data, null, 2)}</pre>
                </details>
              ) : null}
            </>
          )}
        </QueryBoundary>
        {issuedToken ? (
          <div className="issued-token" role="status">
            <strong>请现在复制，关闭页面后不再显示</strong>
            <p>快捷指令请求地址：<code>{window.location.origin}/api/device/v1/push</code></p>
            <p>在快捷指令的 JSON 词典里加入 <code>device_token</code>，值为：</p>
            <textarea readOnly value={issuedToken} aria-label="手机接入口令" onFocus={(event) => event.currentTarget.select()} />
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => navigator.clipboard.writeText(issuedToken)}>复制口令</button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setIssuedToken('')}>我已保存，隐藏口令</button>
          </div>
        ) : null}
        {(createDeviceMutation.error || updateDeviceMutation.error || rotateTokenMutation.error) ? <p className="error-text" role="alert">{(createDeviceMutation.error || updateDeviceMutation.error || rotateTokenMutation.error)?.message}</p> : null}
      </section>

      <section className="card external-information-card" aria-labelledby="external-information-title">
        <div className="section-heading">
          <div>
            <h2 id="external-information-title">{selected?.displayName ?? '当前人物'}的外界信息</h2>
            <p className="muted">这里显示当前所选人物的 RSS 与 X 账号配置和读取结果。每条都保留来源与时间，只用于当轮回答，不会变成人物事实。</p>
          </div>
          <button className="btn btn-secondary btn-sm" type="button" disabled={externalRefreshMutation.isPending} onClick={() => externalRefreshMutation.mutate()}>
            {externalRefreshMutation.isPending ? '正在刷新…' : '立即刷新'}
          </button>
        </div>
        <QueryBoundary query={externalQuery} apiPath="/api/external-information">
          {(external) => (
            <>
              <dl className="kv-list">
                <div className="kv-row"><dt>RSS 资讯源</dt><dd>{external.configuration.rssSources.length ? `${external.configuration.rssSources.length} 个` : '尚未配置'}</dd></div>
                <div className="kv-row"><dt>X 公开账号</dt><dd>{external.configuration.xConfigured ? external.configuration.xUsernames.map((item) => `@${item}`).join('、') : '尚未配置 Token 和账号'}</dd></div>
                <div className="kv-row"><dt>真人浏览器读取</dt><dd>{external.configuration.socialBrowser.xConfigured || external.configuration.socialBrowser.xiaohongshuConfigured ? `${[external.configuration.socialBrowser.xConfigured && 'X', external.configuration.socialBrowser.xiaohongshuConfigured && '小红书'].filter(Boolean).join('、')}（只读）` : '尚未连接'}</dd></div>
                <div className="kv-row"><dt>刷新间隔</dt><dd>{external.configuration.refreshMinutes} 分钟</dd></div>
                <div className="kv-row"><dt>已缓存资讯</dt><dd>{external.cachedItemCount} 条</dd></div>
              </dl>
              {externalRefreshMutation.error && <p className="error-text" role="alert">刷新失败：{externalRefreshMutation.error.message}</p>}
              {external.recentItems.length > 0 ? (
                <div className="external-information-list">
                  {external.recentItems.map((item) => (
                    <article key={item.id}>
                      <a href={item.url} target="_blank" rel="noreferrer">{item.title}</a>
                      <p>{item.sourceName} · {formatTime(item.publishedAt)}</p>
                    </article>
                  ))}
                </div>
              ) : <p className="muted">还没有缓存资讯。配置源后点击“立即刷新”即可验证。</p>}
            </>
          )}
        </QueryBoundary>
      </section>

      <section className="card">
        <h2>当前运行配置</h2>
        <QueryBoundary query={settingsQuery} apiPath="/api/settings">
          {(settings) => (
            <dl className="kv-list">
              <div className="kv-row"><dt>管理员账号</dt><dd>{settings.adminUsername || '—'}</dd></div>
              <div className="kv-row"><dt>会话超时</dt><dd>{settings.sessionTimeoutMinutes != null ? `${settings.sessionTimeoutMinutes} 分钟` : '未设置'}</dd></div>
              <div className="kv-row"><dt>密码保护</dt><dd>{settings.ownerCredentialMode === 'scrypt_hash' ? '已安全加密，不保存明文密码' : '已配置'}</dd></div>
              <div className="kv-row"><dt>对话模型</dt><dd>{settings.deepseekModel || '—'}</dd></div>
              <div className="kv-row"><dt>本地记忆检索</dt><dd>{settings.embeddingProvider === 'not-loaded' ? `${settings.embeddingModel || '本地模型'} · 首次需要时自动加载` : `${settings.embeddingModel || '本地模型'} · 正在使用`}</dd></div>
              <div className="kv-row"><dt>核心日志</dt><dd>{settings.logPath || '—'}</dd></div>
              <div className="kv-row"><dt>日志策略</dt><dd>{settings.logEncoding || 'UTF-8'} · 保留 {settings.logRetentionFiles ?? 0} 个滚动文件</dd></div>
              <div className="kv-row"><dt>手机对话</dt><dd><a href={settings.pwaChatPath || '/chat'} target="_blank" rel="noreferrer">打开 PWA 对话</a></dd></div>
              <div className="kv-row"><dt>PWA 保护</dt><dd>{settings.pwaTokenConfigured ? '已设置独立口令' : '未设置口令，请勿开放到局域网'}</dd></div>
              <div className="kv-row"><dt>局域网访问</dt><dd>{settings.lanAccessEnabled ? '已允许，iPhone 可用电脑局域网地址访问' : '仅本机可访问'}</dd></div>
            </dl>
          )}
        </QueryBoundary>
      </section>
    </div>
  );
}
