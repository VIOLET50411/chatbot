import { Navigate, Route, Routes } from 'react-router-dom';
import { Shell } from './components';
import { AuthProvider, RequireAuth } from './features/auth';
import { CharacterProvider } from './features/characters';
import {
  ContactsPage,
  LoginPage,
  NotFoundPage,
  OverviewPage,
  PersonaPage,
  PwaChatPage,
  SettingsPage,
  WechatPage,
} from './pages';

export function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/chat" element={<PwaChatPage />} />
        <Route element={<RequireAuth><CharacterProvider><Shell /></CharacterProvider></RequireAuth>}>
          <Route index element={<Navigate to="/overview" replace />} />
          <Route path="overview" element={<OverviewPage />} />
          <Route path="persona" element={<PersonaPage />} />
          <Route path="contacts" element={<ContactsPage />} />
          <Route path="wechat" element={<WechatPage />} />
          <Route path="settings" element={<SettingsPage />} />
        </Route>
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </AuthProvider>
  );
}
