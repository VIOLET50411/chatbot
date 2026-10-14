import { useEffect, useRef, useState } from 'react';
import { Activity, ContactRound, Menu, MessageCircleMore, Settings, Sparkles } from 'lucide-react';
import { NavLink, Outlet } from 'react-router-dom';
import { useAuth } from '../features/auth';
import { useCharacter } from '../features/characters';

const NAV_ITEMS = [
  { to: '/overview', label: '运行总览', Icon: Activity },
  { to: '/persona', label: '人设与素材', Icon: Sparkles },
  { to: '/contacts', label: '白名单联系人', Icon: ContactRound },
  { to: '/wechat', label: '微信与媒体', Icon: MessageCircleMore },
  { to: '/settings', label: '日志与设置', Icon: Settings },
];

export function Shell() {
  const { session, logout } = useAuth();
  const { characters, selected, selectedId, select, isLoading, error } = useCharacter();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const sidebarRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const root = document.documentElement;
    const body = document.body;
    const themeColor = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
    const originalThemeColor = themeColor?.getAttribute('content');
    root.classList.add('admin-route');
    body.classList.add('admin-route');
    themeColor?.setAttribute('content', '#f5f0e8');
    return () => {
      root.classList.remove('admin-route');
      body.classList.remove('admin-route');
      if (themeColor) {
        if (originalThemeColor) themeColor.setAttribute('content', originalThemeColor);
        else themeColor.removeAttribute('content');
      }
    };
  }, []);

  useEffect(() => {
    if (!mobileNavOpen) return;
    const previousFocus = document.activeElement as HTMLElement | null;
    sidebarRef.current?.querySelector<HTMLElement>('button, a[href]')?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMobileNavOpen(false);
      if (event.key !== 'Tab' || !sidebarRef.current) return;
      const focusable = Array.from(sidebarRef.current.querySelectorAll<HTMLElement>('button, a[href]'));
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => { document.removeEventListener('keydown', onKeyDown); previousFocus?.focus(); };
  }, [mobileNavOpen]);

  return (
    <div className="shell admin-app-shell">
      <a className="skip-link" href="#main-content">跳到主内容</a>
      <button type="button" className="mobile-nav-toggle" aria-expanded={mobileNavOpen} aria-controls="primary-sidebar" onClick={() => setMobileNavOpen((value) => !value)}>
        <Menu size={18} strokeWidth={2} aria-hidden="true" />菜单
      </button>
      {mobileNavOpen && <button className="sidebar-scrim" type="button" aria-label="关闭菜单" onClick={() => setMobileNavOpen(false)} />}
      <aside ref={sidebarRef} id="primary-sidebar" className={`sidebar${mobileNavOpen ? ' sidebar-open' : ''}`} aria-label="主导航">
        <div className="sidebar-logo">
          <span className="sidebar-logo-icon" aria-hidden="true">✦</span>
          <span className="sidebar-logo-text">Soul<strong>Mate</strong></span>
        </div>
        <div className="character-switcher">
          <label htmlFor="active-character">现在查看的人物</label>
          <select
            id="active-character"
            value={selectedId}
            disabled={isLoading || !characters.length}
            onChange={(event) => select(event.target.value)}
          >
            {characters.map((character) => (
              <option key={character.id} value={character.id}>
                {character.displayName} · v{character.activeSnapshotVersion ?? '—'}
              </option>
            ))}
          </select>
          {selected ? <p>{selected.accountLabel || '微信账号未命名'} · {selected.personaStatus === 'active' ? '正在运行' : '未运行'}</p> : null}
          {error ? <p className="error-text" role="alert">人物列表加载失败</p> : null}
        </div>
        <nav className="sidebar-nav">
          {NAV_ITEMS.map(({ to, label, Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) => `nav-item${isActive ? ' nav-item-active' : ''}`}
              onClick={() => setMobileNavOpen(false)}
            >
              <Icon className="nav-item-icon" size={18} strokeWidth={1.8} aria-hidden="true" />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          {session?.username && <span className="sidebar-actor" aria-label="当前账号">{session.username}</span>}
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => void logout()}>退出</button>
        </div>
      </aside>
      <main className="shell-main" id="main-content"><Outlet /></main>
      <nav className="admin-bottom-nav" aria-label="移动端主导航">
        {NAV_ITEMS.map(({ to, label, Icon }) => (
          <NavLink
            key={`mobile-${to}`}
            to={to}
            className={({ isActive }) => `admin-bottom-nav-item${isActive ? ' admin-bottom-nav-item-active' : ''}`}
            onClick={() => setMobileNavOpen(false)}
          >
            <Icon size={19} strokeWidth={1.9} aria-hidden="true" />
            <span>{label.replace('白名单联系人', '联系人').replace('微信与媒体', '微信').replace('日志与设置', '设置').replace('人设与素材', '人设')}</span>
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
