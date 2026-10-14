import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchRuntimeConfiguration, saveRuntimeConfiguration, testRuntimeConnection } from '../../api';
import { Badge, QueryBoundary } from '../../components';
import type { RuntimeConfigurationUpdate, RuntimeConnectionResult, RuntimeConnectionTarget } from '../../api/types';
import { useCharacter } from '../characters';

const SECRET_OPTIONS: Array<[string, string]> = [
  ['DEEPSEEK_API_KEY', 'DeepSeek Key'],
  ['VISION_API_KEY', '识图 Key'],
  ['AUDIO_API_KEY', '语音 Key'],
  ['X_BEARER_TOKEN', 'X 官方 API Token'],
  ['SOCIAL_BROWSER_PROXY_TOKEN', '社交浏览器代理 Token'],
  ['PWA_CHAT_TOKEN', 'PWA 对话口令'],
];

function generateSecureToken(): string {
  const bytes = new Uint8Array(24);
  window.crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
}

function configured(value: boolean): JSX.Element {
  return <Badge tone={value ? 'positive' : 'neutral'}>{value ? '已配置' : '未配置'}</Badge>;
}

export function RuntimeConfigurationPanel() {
  const { characters, selected, selectedId, select } = useCharacter();
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ['runtime-configuration', selectedId],
    queryFn: ({ signal }) => fetchRuntimeConfiguration(selectedId, signal),
    enabled: Boolean(selectedId),
  });
  const [deepseekKey, setDeepseekKey] = useState('');
  const [deepseekBaseUrl, setDeepseekBaseUrl] = useState('');
  const [deepseekModel, setDeepseekModel] = useState('');
  const [plannerModel, setPlannerModel] = useState('');
  const [rendererModel, setRendererModel] = useState('');
  const [visionKey, setVisionKey] = useState('');
  const [visionBaseUrl, setVisionBaseUrl] = useState('');
  const [visionModel, setVisionModel] = useState('');
  const [audioKey, setAudioKey] = useState('');
  const [audioBaseUrl, setAudioBaseUrl] = useState('');
  const [audioModel, setAudioModel] = useState('');
  const [rssText, setRssText] = useState('');
  const [refreshMinutes, setRefreshMinutes] = useState(30);
  const [xToken, setXToken] = useState('');
  const [xUsernames, setXUsernames] = useState('');
  const [socialUrl, setSocialUrl] = useState('');
  const [socialToken, setSocialToken] = useState('');
  const [socialX, setSocialX] = useState(false);
  const [socialXhs, setSocialXhs] = useState(false);
  const [activeStart, setActiveStart] = useState(8);
  const [activeEnd, setActiveEnd] = useState(24);
  const [checkInHours, setCheckInHours] = useState(8);
  const [cooldownHours, setCooldownHours] = useState(4);
  const [maxDaily, setMaxDaily] = useState(4);
  const [vpsUrl, setVpsUrl] = useState('');
  const [pwaToken, setPwaToken] = useState('');
  const [issuedPwaToken, setIssuedPwaToken] = useState('');
  const [clearSecrets, setClearSecrets] = useState<string[]>([]);
  const [message, setMessage] = useState('');
  const [localError, setLocalError] = useState('');
  const [connectionResults, setConnectionResults] = useState<Partial<Record<RuntimeConnectionTarget, RuntimeConnectionResult>>>({});

  useEffect(() => {
    setConnectionResults({});
    setMessage('');
    setLocalError('');
    setDeepseekKey('');
    setVisionKey('');
    setAudioKey('');
    setXToken('');
    setSocialToken('');
    setPwaToken('');
    setIssuedPwaToken('');
    setClearSecrets([]);
  }, [selectedId]);

  useEffect(() => {
    const data = query.data;
    if (!data) return;
    setDeepseekBaseUrl(data.dialogue.baseUrl);
    setDeepseekModel(data.dialogue.model);
    setPlannerModel(data.dialogue.plannerModel);
    setRendererModel(data.dialogue.rendererModel);
    setVisionBaseUrl(data.vision.baseUrl);
    setVisionModel(data.vision.model);
    setAudioBaseUrl(data.audio.baseUrl);
    setAudioModel(data.audio.model);
    setRssText(data.externalInformation.rssSources.map((item) => `${item.name} | ${item.url}`).join('\n'));
    setRefreshMinutes(data.externalInformation.refreshMinutes);
    setXUsernames(data.externalInformation.xUsernames.join(', '));
    setSocialUrl(data.externalInformation.socialBrowserProxyUrl);
    setSocialX(data.externalInformation.socialBrowserXEnabled);
    setSocialXhs(data.externalInformation.socialBrowserXhsEnabled);
    setActiveStart(data.proactiveDefaults.activeStartHour);
    setActiveEnd(data.proactiveDefaults.activeEndHour);
    setCheckInHours(data.proactiveDefaults.checkInAfterHours);
    setCooldownHours(data.proactiveDefaults.cooldownHours);
    setMaxDaily(data.proactiveDefaults.maxDaily);
    setVpsUrl(data.deployment.vpsTargetUrl);
  }, [query.data]);

  const mutation = useMutation({
    mutationFn: (input: RuntimeConfigurationUpdate) => saveRuntimeConfiguration(input, selectedId),
    onSuccess: async (data) => {
      queryClient.setQueryData(['runtime-configuration', selectedId], data);
      setDeepseekKey('');
      setVisionKey('');
      setAudioKey('');
      setXToken('');
      setSocialToken('');
      setPwaToken('');
      setClearSecrets([]);
      setMessage(`${selected?.displayName ?? '当前人物'}的独立配置已加密保存，新请求会直接使用`);
    },
  });
  const connectionMutation = useMutation({
    mutationFn: ({ target, characterId }: { target: RuntimeConnectionTarget; characterId: string }) => testRuntimeConnection(target, characterId),
    onSuccess: (result, variables) => {
      if (variables.characterId !== selectedId) return;
      setConnectionResults((current) => ({ ...current, [result.target]: result }));
    },
  });

  const connectionButton = (target: RuntimeConnectionTarget, label: string) => {
    const result = connectionResults[target];
    const testing = connectionMutation.isPending
      && connectionMutation.variables?.target === target
      && connectionMutation.variables.characterId === selectedId;
    return (
      <div className="configuration-test-row">
        <button className="btn btn-secondary btn-sm" type="button" disabled={connectionMutation.isPending} onClick={() => connectionMutation.mutate({ target, characterId: selectedId })}>
          {testing ? '测试中…' : label}
        </button>
        {result ? <span className={result.ok ? 'success-text' : 'error-text'} role="status">{result.message} · {result.latencyMs}ms</span> : null}
      </div>
    );
  };

  const submit = () => {
    setMessage('');
    setLocalError('');
    const rssSources: Array<{ name: string; url: string }> = [];
    for (const [index, line] of rssText.split(/\r?\n/).entries()) {
      const normalized = line.trim();
      if (!normalized) continue;
      const splitAt = normalized.indexOf('|');
      if (splitAt <= 0) {
        setLocalError(`RSS 第 ${index + 1} 行格式不对，应写成：名称 | https://地址`);
        return;
      }
      rssSources.push({
        name: normalized.slice(0, splitAt).trim(),
        url: normalized.slice(splitAt + 1).trim(),
      });
    }
    const input: RuntimeConfigurationUpdate = {
      deepseek_base_url: deepseekBaseUrl,
      deepseek_model: deepseekModel,
      deepseek_planner_model: plannerModel,
      deepseek_renderer_model: rendererModel,
      vision_base_url: visionBaseUrl,
      vision_model: visionModel,
      audio_base_url: audioBaseUrl,
      audio_model: audioModel,
      rss_sources: rssSources,
      external_refresh_minutes: refreshMinutes,
      x_usernames: xUsernames.split(/[,，\s]+/).map((item) => item.trim()).filter(Boolean),
      social_browser_proxy_url: socialUrl,
      social_browser_x_enabled: socialX,
      social_browser_xhs_enabled: socialXhs,
      proactive_active_start_hour: activeStart,
      proactive_active_end_hour: activeEnd,
      proactive_check_in_after_hours: checkInHours,
      proactive_cooldown_hours: cooldownHours,
      proactive_max_daily: maxDaily,
      vps_target_url: vpsUrl,
      clear_secrets: clearSecrets,
    };
    if (deepseekKey.trim()) input.deepseek_api_key = deepseekKey.trim();
    if (visionKey.trim()) input.vision_api_key = visionKey.trim();
    if (audioKey.trim()) input.audio_api_key = audioKey.trim();
    if (xToken.trim()) input.x_bearer_token = xToken.trim();
    if (socialToken.trim()) input.social_browser_proxy_token = socialToken.trim();
    if (pwaToken.trim()) input.pwa_chat_token = pwaToken.trim();
    mutation.mutate(input);
  };

  const toggleClear = (key: string, checked: boolean) => {
    setClearSecrets((current) => checked ? [...current.filter((item) => item !== key), key] : current.filter((item) => item !== key));
  };

  return (
    <section className="card runtime-configuration-card" aria-labelledby="runtime-configuration-title">
      <div className="section-heading">
        <div>
          <h2 id="runtime-configuration-title">{selected?.displayName ?? '当前人物'}的模型与外界信息</h2>
          <p className="muted">文字、识图、语音、RSS 和 X 账号按人物独立保存。社交浏览器、PWA、主动联系默认值与 VPS 属于两个人物共用的基础设施。</p>
        </div>
      </div>
      <div className="runtime-character-selector" role="group" aria-label="选择要配置的人物">
        <div><strong>正在编辑人物</strong><span>切换后，下方模型 Key 和资讯订阅会显示该人物自己的配置</span></div>
        <div className="runtime-character-options">
          {characters.map((character) => (
            <button
              type="button"
              key={character.id}
              className={character.id === selectedId ? 'is-selected' : ''}
              aria-pressed={character.id === selectedId}
              onClick={() => select(character.id)}
            >
              {character.displayName}
            </button>
          ))}
        </div>
      </div>
      <QueryBoundary query={query} apiPath="/api/settings/runtime">
        {(settings) => (
          <form className="runtime-configuration-form" onSubmit={(event) => { event.preventDefault(); submit(); }}>
            <fieldset>
              <legend>先看当前还缺什么</legend>
              <p className="field-help">只有文字模型是首次微信文字测试的必需项；其他能力按需要逐项配置，不必一次全部购买。</p>
              <div className="configuration-status-line"><span>文字回复（必需）</span>{configured(settings.dialogue.apiKeyConfigured)}</div>
              <div className="configuration-status-line"><span>看懂图片</span>{configured(settings.vision.apiKeyConfigured)}</div>
              <div className="configuration-status-line"><span>微信语音转文字</span><Badge tone="info">优先使用 Plus，不需要额外 Key</Badge></div>
              <div className="configuration-status-line"><span>PWA / 音频文件转文字</span>{configured(settings.audio.apiKeyConfigured || settings.audio.usesVisionKeyFallback)}</div>
              <div className="configuration-status-line"><span>RSS 新闻</span>{configured(settings.externalInformation.rssSources.length > 0)}</div>
              <div className="configuration-status-line"><span>X 官方 API</span>{configured(settings.externalInformation.xBearerTokenConfigured && settings.externalInformation.xUsernames.length > 0)}</div>
              <div className="configuration-status-line"><span>X / 小红书真人浏览器</span>{configured(settings.externalInformation.socialBrowserProxyTokenConfigured && (settings.externalInformation.socialBrowserXEnabled || settings.externalInformation.socialBrowserXhsEnabled))}</div>
              <div className="configuration-status-line"><span>备用 VPS</span>{configured(Boolean(settings.deployment.vpsTargetUrl))}</div>
            </fieldset>

            <fieldset>
              <legend>文字对话模型</legend>
              <div className="configuration-status-line"><span>DeepSeek API Key</span>{configured(settings.dialogue.apiKeyConfigured)}</div>
              <label>填写新的 DeepSeek API Key<input type="password" value={deepseekKey} onChange={(event) => setDeepseekKey(event.target.value)} autoComplete="new-password" placeholder="留空表示不更换" /></label>
              <div className="form-grid-2">
                <label>API 地址<input value={deepseekBaseUrl} onChange={(event) => setDeepseekBaseUrl(event.target.value)} /></label>
                <label>默认模型<input value={deepseekModel} onChange={(event) => setDeepseekModel(event.target.value)} /></label>
              </div>
              <details>
                <summary>分别指定决策模型和表达模型</summary>
                <div className="form-grid-2 settings-details-grid">
                  <label>隐藏决策模型<input value={plannerModel} onChange={(event) => setPlannerModel(event.target.value)} placeholder="留空则使用默认模型" /></label>
                  <label>自然表达模型<input value={rendererModel} onChange={(event) => setRendererModel(event.target.value)} placeholder="留空则使用默认模型" /></label>
                </div>
              </details>
              {connectionButton('dialogue', '测试文字模型连接')}
            </fieldset>

            <fieldset>
              <legend>图片与语音理解</legend>
              <p className="field-help">DeepSeek V4 Pro 负责文字。识图推荐低成本的 qwen3.7-flash；微信语音优先由 Plus 转成文字，不需要语音 Key；PWA 音频推荐 qwen3-asr-flash。</p>
              <div className="configuration-status-line"><span>识图 Key</span>{configured(settings.vision.apiKeyConfigured)}</div>
              <label>填写新的阿里云百炼 Key<input type="password" value={visionKey} onChange={(event) => setVisionKey(event.target.value)} autoComplete="new-password" placeholder="同一把 Key 可同时供识图和 PWA 语音转文字使用" /></label>
              <div className="form-grid-2">
                <label>识图模型<input value={visionModel} onChange={(event) => setVisionModel(event.target.value)} /></label>
                <label>识图 API 地址<input value={visionBaseUrl} onChange={(event) => setVisionBaseUrl(event.target.value)} /></label>
                <label>语音转文字模型<input value={audioModel} onChange={(event) => setAudioModel(event.target.value)} /></label>
                <label>语音 API 地址<input value={audioBaseUrl} onChange={(event) => setAudioBaseUrl(event.target.value)} /></label>
              </div>
              <div className="configuration-status-line">
                <span>单独的语音 Key</span>
                {configured(settings.audio.apiKeyConfigured || settings.audio.usesVisionKeyFallback)}
              </div>
              <label>可选：填写单独的语音 Key<input type="password" value={audioKey} onChange={(event) => setAudioKey(event.target.value)} autoComplete="new-password" placeholder="留空时沿用识图 Key" /></label>
              {connectionButton('vision', '测试识图连接')}
              {connectionButton('audio', '测试语音服务连接')}
            </fieldset>

            <fieldset>
              <legend>新闻、X 与小红书</legend>
              <div className="external-source-explainer">
                <p><strong>RSS</strong><span>网站主动提供的新闻订阅地址，不需要 Key；每行填写一个真实 feed 地址。</span></p>
                <p><strong>X 官方 API</strong><span>用 X Bearer Token 读取下方指定公开账号的文字内容，Token 与账号列表只属于{selected?.displayName ?? '当前人物'}。</span></p>
                <p><strong>真人浏览器</strong><span>Windows 上另一个只读代理，负责读取已登录的小红书或 X 页面。地址和代理 Token 是两个人物共用的连接凭证；没有启动代理时，勾选平台不会生效。</span></p>
              </div>
              <label>RSS 来源<textarea value={rssText} onChange={(event) => setRssText(event.target.value)} placeholder={'科技新闻 | https://example.com/feed.xml\n学校通知 | https://example.edu/rss'} /></label>
              <p className="field-help">上面的灰字只是格式示例，不是已经配置的来源。请替换成真实 RSS 地址；不需要 RSS 可以留空。</p>
              <div className="form-grid-2">
                <label>刷新间隔（分钟）<input type="number" min={5} max={1440} value={refreshMinutes} onChange={(event) => setRefreshMinutes(Number(event.target.value))} /></label>
                <label>X 公开账号<input value={xUsernames} onChange={(event) => setXUsernames(event.target.value)} placeholder="多个账号用逗号分隔，不写 @ 也可以" /></label>
              </div>
              <div className="configuration-status-line"><span>X 官方 API Token</span>{configured(settings.externalInformation.xBearerTokenConfigured)}</div>
              <label>填写新的 X Bearer Token<input type="password" value={xToken} onChange={(event) => setXToken(event.target.value)} autoComplete="new-password" placeholder="留空表示不更换" /></label>
              <details>
                <summary>真人浏览器读取 X / 小红书（两个人物共用代理）</summary>
                <div className="settings-details-grid">
                  <label>宿主代理地址<input value={socialUrl} onChange={(event) => setSocialUrl(event.target.value)} /></label>
                  <div className="configuration-status-line"><span>代理 Token</span>{configured(settings.externalInformation.socialBrowserProxyTokenConfigured)}</div>
                  <label>填写新的代理 Token<input type="password" value={socialToken} onChange={(event) => setSocialToken(event.target.value)} autoComplete="new-password" placeholder="至少 24 位，留空表示不更换" /></label>
                  <label className="checkbox-line"><input type="checkbox" checked={socialX} onChange={(event) => setSocialX(event.target.checked)} /><span>允许只读 X</span></label>
                  <label className="checkbox-line"><input type="checkbox" checked={socialXhs} onChange={(event) => setSocialXhs(event.target.checked)} /><span>允许只读小红书</span></label>
                </div>
              </details>
              {connectionButton('external', '测试外界信息读取')}
            </fieldset>

            <fieldset>
              <legend>主动联系的默认频率</legend>
              <p className="field-help">只对管理台中已加入白名单、且允许主动联系的人生效。新联系人默认不会主动收到消息。</p>
              <div className="form-grid-3">
                <label>开始时间<input type="number" min={0} max={23} value={activeStart} onChange={(event) => setActiveStart(Number(event.target.value))} /></label>
                <label>结束时间<input type="number" min={1} max={24} value={activeEnd} onChange={(event) => setActiveEnd(Number(event.target.value))} /></label>
                <label>每天最多<input type="number" min={1} max={10} value={maxDaily} onChange={(event) => setMaxDaily(Number(event.target.value))} /></label>
                <label>至少间隔（小时）<input type="number" min={1} max={48} value={cooldownHours} onChange={(event) => setCooldownHours(Number(event.target.value))} /></label>
                <label>多久没聊可问候（小时）<input type="number" min={1} max={168} value={checkInHours} onChange={(event) => setCheckInHours(Number(event.target.value))} /></label>
              </div>
            </fieldset>

            <fieldset>
              <legend>本地与 VPS</legend>
              <div className="configuration-status-line"><span>当前核心</span><Badge tone="info">{settings.deployment.currentMode === 'vps' ? 'VPS' : '本机 Docker'}</Badge></div>
              <label>备用 VPS HTTPS 地址<input value={vpsUrl} onChange={(event) => setVpsUrl(event.target.value)} placeholder="https://你的域名" /></label>
              <p className="field-help">管理台可以保存并检查目标地址；真正切换仍需迁移最新数据库并在 Windows 执行一次安全切换命令，避免本地和 VPS 同时写数据。</p>
              {vpsUrl ? <code className="command-preview">.\scripts\set_core_mode.ps1 -Mode Vps -VpsUrl '{vpsUrl}'</code> : null}
              {connectionButton('vps', '测试备用 VPS')}
            </fieldset>

            <fieldset>
              <legend>PWA 对话口令与 iPhone 访问（两个人物共用）</legend>
              <div className="configuration-status-line">
                <span>PWA 独立口令</span>
                {settings.pwa.tokenConfigured
                  ? <Badge tone={settings.pwa.tokenIsStrong ? 'positive' : 'warning'}>{settings.pwa.tokenIsStrong ? '已配置' : '已配置，但长度不足'}</Badge>
                  : configured(false)}
              </div>
              <p className="field-help">这个口令保护 <code>{settings.pwa.chatPath || '/chat'}</code>。生成后先复制，再保存；密钥只保存加密结果，刷新页面后不会回显原文。</p>
              <label>填写新的 PWA 口令<input type="password" value={pwaToken} onChange={(event) => { setPwaToken(event.target.value); setIssuedPwaToken(''); }} autoComplete="new-password" placeholder="至少 24 位，留空表示不更换" /></label>
              <div className="button-row">
                <button className="btn btn-secondary btn-sm" type="button" onClick={() => { const value = generateSecureToken(); setPwaToken(value); setIssuedPwaToken(value); }}>生成安全口令</button>
                {issuedPwaToken ? <button className="btn btn-ghost btn-sm" type="button" onClick={() => navigator.clipboard.writeText(issuedPwaToken)}>复制刚生成的口令</button> : null}
              </div>
              {issuedPwaToken ? <div className="issued-runtime-secret"><strong>请现在保存到密码管理器</strong><code>{issuedPwaToken}</code></div> : null}
              <div className="tailscale-setup-note">
                <strong>在 iPhone 上从外网安全访问</strong>
                <ol>
                  <li>Windows 安装 Tailscale：<code>winget install Tailscale.Tailscale</code></li>
                  <li>电脑和 iPhone 登录同一个 Tailscale 账号</li>
                  <li>在管理员 PowerShell 执行：<code>.\scripts\configure_tailscale_serve.ps1</code></li>
                  <li>脚本显示的 HTTPS 地址加 <code>/chat</code>，就是 iPhone PWA 地址</li>
                </ol>
                <p>电脑关机、休眠或 Tailscale 停止时，本地 PWA 会暂时不可用，已保存的聊天不会丢。</p>
              </div>
            </fieldset>

            <details className="danger-settings">
              <summary>清除已经保存的密钥</summary>
              <p className="field-help">勾选后保存才会清除。只把输入框留空不会删除旧密钥。</p>
              <div className="secret-clear-grid">
                {SECRET_OPTIONS.map(([key, label]) => (
                  <label className="checkbox-line" key={key}>
                    <input type="checkbox" checked={clearSecrets.includes(key)} onChange={(event) => toggleClear(key, event.target.checked)} />
                    <span>{label}</span>
                  </label>
                ))}
              </div>
            </details>

            <div className="sticky-save-row">
              <button className="btn btn-primary" type="submit" disabled={mutation.isPending}>{mutation.isPending ? '保存中…' : `保存${selected?.displayName ?? '当前人物'}的配置及全局设置`}</button>
              {message ? <span className="success-text" role="status">{message}</span> : null}
              {(localError || mutation.error) ? <span className="error-text" role="alert">{localError || mutation.error?.message}</span> : null}
            </div>
          </form>
        )}
      </QueryBoundary>
    </section>
  );
}
