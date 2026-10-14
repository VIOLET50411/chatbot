import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ArrowLeft, MoreHorizontal, Send, Smile, Plus } from 'lucide-react';
import {
  clearStoredPwaToken,
  fetchChatHistory,
  fetchPwaProfile,
  fetchPwaProfiles,
  fetchPwaStatus,
  getStoredPwaToken,
  sendChatMessage,
  setStoredPwaToken,
} from '../api';
import { loadCachedMessages, saveCachedMessages, type PwaStoredMessage } from '../features/chat/pwaStorage';

type LocalChatMessage = PwaStoredMessage;

let messageSequence = 0;
const PWA_CHARACTER_STORAGE_KEY = 'soul_mate_pwa_character';

function nextMessageId(): string {
  messageSequence += 1;
  return `${Date.now()}-${messageSequence}`;
}

function toLocalMessage(
  item: { id?: string; role: string; content: string; created_at?: string },
  index: number,
): LocalChatMessage {
  return {
    id: item.id || `history-${index}`,
    role: item.role === 'assistant' ? 'assistant' : 'user',
    content: item.content,
    createdAt: item.created_at || '',
  };
}

function PwaTokenGate({
  error,
  onToken,
}: {
  error: string;
  onToken: (token: string) => void;
}) {
  const [value, setValue] = useState('');

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const token = value.trim();
    if (!token) return;
    onToken(token);
  }

  return (
    <div className="pwa-token-page">
      <form className="pwa-token-card" onSubmit={handleSubmit}>
        <h1>Soul Mate 对话</h1>
        <p>输入 PWA 对话口令后即可开始聊天，口令只会保存在这台设备。</p>
        <input
          className="pwa-token-input"
          value={value}
          autoComplete="off"
          aria-label="PWA 对话口令"
          placeholder="对话口令"
          onChange={(event) => setValue(event.target.value)}
        />
        {error && <p className="pwa-token-error" role="alert">{error}</p>}
        <button type="submit" className="pwa-token-submit" disabled={!value.trim()}>
          进入对话
        </button>
      </form>
    </div>
  );
}

/**
 * 独立 PWA 对话页：只依赖 /api/pwa/*，不依赖管理台登录和角色工作台。
 */
export function PwaChatPage() {
  const scrollRef = useRef<HTMLElement>(null);
  const seededRef = useRef(false);
  const pinnedToBottomRef = useRef(true);
  const aliveRef = useRef(true);
  const replyTimersRef = useRef<number[]>([]);
  const [token, setToken] = useState(() => getStoredPwaToken());
  const [characterId, setCharacterId] = useState(
    () => localStorage.getItem(PWA_CHARACTER_STORAGE_KEY) ?? '',
  );
  const [messages, setMessages] = useState<LocalChatMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [online, setOnline] = useState(() => navigator.onLine);
  const [historyLoadingOlder, setHistoryLoadingOlder] = useState(false);
  const [hasOlderHistory, setHasOlderHistory] = useState(false);
  const [historyCursor, setHistoryCursor] = useState<{ createdAt: string; id: string } | null>(null);

  useEffect(() => {
    const root = document.documentElement;
    const body = document.body;
    const themeColor = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
    const originalThemeColor = themeColor?.getAttribute('content');
    // /chat 已由服务端返回独立 manifest。保留它，避免添加到主屏幕后
    // Safari 把聊天页错误地当成管理台入口；这里只切换当前页的主题色。
    themeColor?.setAttribute('content', '#eee8df');
    const appleMeta: HTMLMetaElement[] = [];
    const addAppleMeta = (name: string, content: string) => {
      const existing = document.querySelector<HTMLMetaElement>(`meta[name="${name}"]`);
      if (existing) {
        existing.setAttribute('content', content);
        return;
      }
      const element = document.createElement('meta');
      element.name = name;
      element.content = content;
      document.head.appendChild(element);
      appleMeta.push(element);
    };
    addAppleMeta('apple-mobile-web-app-capable', 'yes');
    addAppleMeta('apple-mobile-web-app-status-bar-style', 'black-translucent');
    addAppleMeta('apple-mobile-web-app-title', 'Soul Mate');
    root.classList.add('pwa-route');
    body.classList.add('pwa-route');
    const viewport = window.visualViewport;
    const updateViewport = () => {
      const keyboardOffset = viewport
        ? Math.max(0, root.getBoundingClientRect().height - viewport.height - viewport.offsetTop)
        : 0;
      root.style.setProperty('--pwa-keyboard-offset', `${Math.round(keyboardOffset)}px`);
      if (pinnedToBottomRef.current) {
        requestAnimationFrame(() => scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight }));
      }
    };
    updateViewport();
    viewport?.addEventListener('resize', updateViewport);
    viewport?.addEventListener('scroll', updateViewport);
    return () => {
      aliveRef.current = false;
      replyTimersRef.current.forEach((timer) => window.clearTimeout(timer));
      replyTimersRef.current = [];
      viewport?.removeEventListener('resize', updateViewport);
      viewport?.removeEventListener('scroll', updateViewport);
      root.classList.remove('pwa-route');
      body.classList.remove('pwa-route');
      root.style.removeProperty('--pwa-keyboard-offset');
      appleMeta.forEach((element) => element.remove());
      if (themeColor && originalThemeColor) themeColor.setAttribute('content', originalThemeColor);
    };
  }, []);

  useEffect(() => () => {
    aliveRef.current = false;
    replyTimersRef.current.forEach((timer) => window.clearTimeout(timer));
  }, []);

  useEffect(() => {
    const becameOnline = () => setOnline(true);
    const becameOffline = () => setOnline(false);
    window.addEventListener('online', becameOnline);
    window.addEventListener('offline', becameOffline);
    return () => {
      window.removeEventListener('online', becameOnline);
      window.removeEventListener('offline', becameOffline);
    };
  }, []);

  const statusQuery = useQuery({
    queryKey: ['pwa-status'],
    queryFn: ({ signal }) => fetchPwaStatus(signal),
    retry: false,
  });
  const authRequired = statusQuery.data?.authRequired ?? false;

  const profilesQuery = useQuery({
    queryKey: ['pwa-profiles', token],
    queryFn: ({ signal }) => fetchPwaProfiles(token, signal),
    enabled: Boolean(statusQuery.data && (token || !authRequired)),
    retry: false,
  });

  useEffect(() => {
    const profiles = profilesQuery.data;
    if (!profiles || profiles.length === 0) return;
    const nextId = profiles.some((item) => item.id === characterId)
      ? characterId
      : profiles[0].id;
    if (nextId !== characterId) setCharacterId(nextId);
    localStorage.setItem(PWA_CHARACTER_STORAGE_KEY, nextId);
  }, [characterId, profilesQuery.data]);

  const profileQuery = useQuery({
    queryKey: ['pwa-profile', token, characterId],
    queryFn: ({ signal }) => fetchPwaProfile(token, characterId, signal),
    enabled: Boolean(characterId && profilesQuery.data),
    retry: false,
  });
  const characterName = profileQuery.data?.displayName || 'Soul Mate';

  const historyQuery = useQuery({
    queryKey: ['chat-history', token, characterId],
    queryFn: ({ signal }) => fetchChatHistory(token, characterId, signal),
    enabled: Boolean(profileQuery.data),
    retry: false,
  });

  useEffect(() => {
    seededRef.current = false;
    setMessages([]);
    setHistoryLoadingOlder(false);
    setHasOlderHistory(false);
    setHistoryCursor(null);
    pinnedToBottomRef.current = true;
    let cancelled = false;
      void loadCachedMessages(`character:${characterId}`).then((cached) => {
      if (!cancelled && !seededRef.current && cached.length > 0) {
        setMessages(cached);
      }
    });
    return () => { cancelled = true; };
  }, [token, characterId]);

  useEffect(() => {
    if (!historyQuery.data || seededRef.current) return;
    seededRef.current = true;
    setMessages(historyQuery.data.messages.map(toLocalMessage));
    setHasOlderHistory(historyQuery.data.has_more === true);
    setHistoryCursor(
      historyQuery.data.next_before_created_at && historyQuery.data.next_before_id
        ? { createdAt: historyQuery.data.next_before_created_at, id: historyQuery.data.next_before_id }
        : null,
    );
  }, [historyQuery.data]);

  useEffect(() => {
    if (!seededRef.current || messages.length === 0) return;
    void saveCachedMessages(`character:${characterId}`, messages);
  }, [messages, token, characterId]);

  useEffect(() => {
    if (!pinnedToBottomRef.current) return;
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: 'smooth',
    });
  }, [messages, sending]);

  useEffect(() => {
    document.title = `${characterName} · Soul Mate`;
    return () => {
      document.title = 'Soul Mate';
    };
  }, [characterName]);

  function handleToken(tokenValue: string) {
    setStoredPwaToken(tokenValue);
    setToken(tokenValue);
  }

  async function deliverMessage(userMessage: LocalChatMessage) {
    setSending(true);
    setMessages((prev) => prev.map((item) => item.id === userMessage.id ? { ...item, delivery: 'sending', error: '' } : item));

    try {
      const result = await sendChatMessage(userMessage.content, token, characterId, userMessage.id);
      setMessages((prev) => prev.map((item) => item.id === userMessage.id ? { ...item, delivery: 'sent', error: '' } : item));
      const replyChunks = result.chunks.filter((chunk) => chunk.trim());
      if (replyChunks.length > 0) {
        for (const [index, chunk] of replyChunks.entries()) {
          if (index > 0) {
            await new Promise<void>((resolve) => {
              const timer = window.setTimeout(resolve, Math.min(2200, 650 + chunk.length * 18));
              replyTimersRef.current.push(timer);
            });
          }
          if (!aliveRef.current) return;
          setMessages((prev) => {
            if (prev.some((item) => item.role === 'assistant' && item.content === chunk)) return prev;
            return [...prev, {
              id: nextMessageId(),
              role: 'assistant' as const,
              content: chunk,
              createdAt: new Date().toISOString(),
            }];
          });
        }
      } else if (result.reply) {
        const reply = result.reply as string;
        setMessages((prev) => prev.some((item) => item.role === 'assistant' && item.content === reply)
          ? prev
          : [...prev, {
            id: nextMessageId(),
            role: 'assistant',
            content: reply,
            createdAt: new Date().toISOString(),
          }]);
      } else {
        setMessages((prev) => [
          ...prev,
          {
            id: nextMessageId(),
            role: 'system',
            content: '对方暂时没有回复',
            createdAt: new Date().toISOString(),
          },
        ]);
      }
    } catch (error) {
      const detail = error instanceof Error ? error.message : '消息发送失败';
      setMessages((prev) => prev.map((item) => item.id === userMessage.id ? { ...item, delivery: 'failed', error: detail } : item));
    } finally {
      setSending(false);
    }
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const message = draft.trim();
    if (!message || !characterId || (authRequired && !token) || sending) return;
    const userMessage: LocalChatMessage = {
      id: nextMessageId(), role: 'user', content: message, createdAt: new Date().toISOString(), delivery: 'sending',
    };
    setDraft('');
    setMessages((prev) => [...prev, userMessage]);
    await deliverMessage(userMessage);
  }

  async function loadOlderHistory() {
    const element = scrollRef.current;
    if (!element || historyLoadingOlder || !hasOlderHistory || !historyCursor || !online) return;
    const previousHeight = element.scrollHeight;
    const previousTop = element.scrollTop;
    setHistoryLoadingOlder(true);
    try {
      const page = await fetchChatHistory(token, characterId, undefined, historyCursor);
      const older = page.messages.map((item, index) => toLocalMessage(item, index));
      setMessages((current) => {
        const existing = new Set(current.map((item) => item.id));
        return [...older.filter((item) => !existing.has(item.id)), ...current];
      });
      setHasOlderHistory(page.has_more === true);
      setHistoryCursor(
        page.next_before_created_at && page.next_before_id
          ? { createdAt: page.next_before_created_at, id: page.next_before_id }
          : null,
      );
      requestAnimationFrame(() => {
        const next = scrollRef.current;
        if (!next) return;
        next.scrollTop = previousTop + (next.scrollHeight - previousHeight);
      });
    } finally {
      setHistoryLoadingOlder(false);
    }
  }

  if (statusQuery.isLoading) {
    return (
      <div className="pwa-token-page">
        <p className="pwa-token-loading" role="status">正在连接对话…</p>
      </div>
    );
  }

  if (statusQuery.isError) {
    return (
      <PwaTokenGate
        error={
          statusQuery.error instanceof Error
            ? statusQuery.error.message
            : '连接失败'
        }
        onToken={handleToken}
      />
    );
  }

  if (authRequired && !token) {
    return <PwaTokenGate error="" onToken={handleToken} />;
  }

  if (profilesQuery.isLoading || profileQuery.isLoading) {
    return (
      <div className="pwa-token-page">
        <p className="pwa-token-loading" role="status">正在连接对话…</p>
      </div>
    );
  }

  if (profilesQuery.isError) {
    return (
      <PwaTokenGate
        error={profilesQuery.error instanceof Error ? profilesQuery.error.message : '连接失败'}
        onToken={handleToken}
      />
    );
  }

  if (profilesQuery.data?.length === 0) {
    return (
      <div className="pwa-token-page">
        <div className="pwa-token-card">
          <h1>还差一步</h1>
          <p>请先在管理台的“白名单联系人”页面，为至少一个人物选择“在 PWA 中接续此人”。</p>
        </div>
      </div>
    );
  }

  if (profileQuery.isError) {
    return (
      <PwaTokenGate
        error={
          profileQuery.error instanceof Error
            ? profileQuery.error.message
            : '连接失败'
        }
        onToken={handleToken}
      />
    );
  }

  return (
    <div className="pwa-chat-page">
      <div className="pwa-chat">
        <header className="pwa-chat-header">
          <button
            type="button"
            className="pwa-chat-icon-button"
            aria-label="返回"
            onClick={() => {
              if (window.history.length > 1) window.history.back();
              else window.location.assign('/');
            }}
          >
            <ArrowLeft size={22} strokeWidth={1.8} />
          </button>

          <div className="pwa-chat-contact">
            <span className="pwa-chat-contact-avatar" aria-hidden="true">
              {characterName.slice(0, 1)}
            </span>
            <span className="pwa-chat-contact-meta">
              {profilesQuery.data && profilesQuery.data.length > 1 ? (
                <select
                  className="pwa-character-select"
                  aria-label="切换聊天人物"
                  value={characterId}
                  onChange={(event) => {
                    const nextId = event.target.value;
                    localStorage.setItem(PWA_CHARACTER_STORAGE_KEY, nextId);
                    setCharacterId(nextId);
                  }}
                >
                  {profilesQuery.data.map((profile) => (
                    <option key={profile.id} value={profile.id}>{profile.displayName}</option>
                  ))}
                </select>
              ) : <strong>{characterName}</strong>}
              <small>{!online ? '离线，消息可稍后重试' : sending ? '对方正在输入…' : '已连接'}</small>
            </span>
          </div>

          <div className="pwa-chat-actions">
            <button
              type="button"
              className="pwa-chat-icon-button"
              aria-expanded={menuOpen}
              aria-label="更多操作"
              onClick={() => setMenuOpen((prev) => !prev)}
            >
              <MoreHorizontal size={22} strokeWidth={1.8} />
            </button>
            {menuOpen && (
              <div className="pwa-chat-menu">
                <button
                  type="button"
                  onClick={() => {
                    clearStoredPwaToken();
                    setToken('');
                    setMenuOpen(false);
                  }}
                >
                  清除对话口令
                </button>
              </div>
            )}
          </div>
        </header>

        <main
          className="pwa-chat-scroll"
          ref={scrollRef}
          aria-live="polite"
          onScroll={() => {
            const element = scrollRef.current;
            if (!element) return;
            pinnedToBottomRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 96;
            if (element.scrollTop < 72) void loadOlderHistory();
          }}
        >
          {historyLoadingOlder && <div className="pwa-history-loading" role="status">正在加载更早的消息</div>}
          <div className="pwa-chat-date">
            <span>今天</span>
          </div>
          {historyQuery.isError && (
            <div className="pwa-chat-date">
              <span>历史消息加载失败，发送仍可用</span>
            </div>
          )}
          {messages.map((message, index) => {
            const previous = messages[index - 1];
            const grouped = Boolean(previous && previous.role === message.role && message.role !== 'system');
            return (
            <div key={message.id} className={`pwa-msg pwa-msg-${message.role}${grouped ? ' pwa-msg-grouped' : ''}`}>
              {message.role !== 'user' && (
                <span className="pwa-avatar" aria-hidden="true">
                  {characterName.slice(0, 1)}
                </span>
              )}
              {message.role === 'system' ? (
                <p className="pwa-system-text">{message.content}</p>
              ) : (
                <div className="pwa-message-content">
                  <div className="pwa-bubble">{message.content}</div>
                  {message.role === 'user' && message.delivery ? (
                    <div className={`pwa-delivery pwa-delivery-${message.delivery}`}>
                      <span>{message.delivery === 'sending' ? '发送中' : message.delivery === 'sent' ? '已发送' : message.error || '发送失败'}</span>
                      {message.delivery === 'failed' ? <button type="button" disabled={sending || !online} onClick={() => void deliverMessage(message)}>重试</button> : null}
                    </div>
                  ) : null}
                </div>
              )}
            </div>
            );
          })}
          {sending && (
            <div className="pwa-msg pwa-msg-assistant">
              <span className="pwa-avatar" aria-hidden="true">
                {characterName.slice(0, 1)}
              </span>
              <div className="pwa-bubble pwa-typing" aria-label="正在输入">
                <span />
                <span />
                <span />
              </div>
            </div>
          )}
        </main>

        <form className="pwa-input-bar" onSubmit={handleSubmit}>
          <button
            type="button"
            className="pwa-input-icon"
            aria-label="更多功能"
            disabled
          >
            <Plus size={24} />
          </button>
          <textarea
            className="pwa-chat-input"
            value={draft}
            rows={1}
            maxLength={4000}
            aria-label="输入消息"
            autoComplete="off"
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
          />
          <button
            type="button"
            className="pwa-input-icon"
            aria-label="表情"
            disabled
          >
            <Smile size={24} />
          </button>
          <button
            type="submit"
            className="pwa-send-button"
            aria-label="发送"
            disabled={!draft.trim() || !characterId || (authRequired && !token) || sending || !online}
          >
            <Send size={20} />
          </button>
        </form>
      </div>
    </div>
  );
}
