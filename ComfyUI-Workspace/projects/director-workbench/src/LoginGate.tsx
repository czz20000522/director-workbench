import { useEffect, useState, type FormEvent } from 'react';
import { Clapperboard } from 'lucide-react';
import App from './App';
import './login.css';
import { bindSessionUser } from './sessionFetch';
import ConnectAgent from './components/ConnectAgent';

type WorkspaceUser = { username: string; workspace: string };

async function responseError(response: Response, fallback: string) {
  const body = await response.json().catch(() => null);
  return typeof body?.detail === 'string' ? body.detail : fallback;
}

export default function LoginGate() {
  const [user, setUser] = useState<WorkspaceUser | null>(null);
  const [checking, setChecking] = useState(true);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    const expire = () => {
      bindSessionUser('');
      setUser(null);
      setPassword('');
      setError('登录已过期，请重新登录。');
    };
    window.addEventListener('workspace-session-expired', expire);
    return () => window.removeEventListener('workspace-session-expired', expire);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    fetch('/api/auth/me', { signal: controller.signal, credentials: 'same-origin' })
      .then(async response => {
        if (response.ok) { const value = await response.json(); if (!controller.signal.aborted) { bindSessionUser(value.username); setUser(value); } }
        else if (response.status !== 401) throw new Error('暂时无法连接工作台，请稍后重试。');
      })
      .catch(err => { if (!controller.signal.aborted) setError(err.message || '无法连接工作台。'); })
      .finally(() => { if (!controller.signal.aborted) setChecking(false); });
    return () => controller.abort();
  }, []);

  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      const response = await fetch('/api/auth/login', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username.trim(), password }),
      });
      if (!response.ok) throw new Error(await responseError(response, '登录失败，请重试。'));
      const value = await response.json();
      bindSessionUser(value.username); setUser(value);
      setPassword('');
    } catch (err) {
      setError(err instanceof Error ? err.message : '无法连接工作台。');
    } finally { setBusy(false); }
  }

  async function logout() {
    setBusy(true);
    setError('');
    try {
      const response = await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' });
      if (!response.ok && response.status !== 401) throw new Error(await responseError(response, '退出失败，请重试。'));
      setUser(null);
      bindSessionUser('');
      setUsername('');
      setPassword('');
    } catch (err) {
      setError(err instanceof Error ? err.message : '无法连接工作台。');
    } finally { setBusy(false); }
  }

  if (checking) return <main className="workspace-login"><p role="status">正在连接工作台…</p></main>;

  if (user) return <App key={user.username} privateMode onLogout={logout} logoutBusy={busy} sessionError={error} />;

  return <main className="workspace-login">
    <section className="workspace-login-card" aria-labelledby="workspace-login-title">
      <div className="workspace-login-mark"><Clapperboard size={25} aria-hidden="true" /></div>
      <h1 id="workspace-login-title">登录导演工作台</h1>
      <p className="workspace-login-intro">进入你的创作空间</p>
      <ConnectAgent />
      <form onSubmit={login}>
        <label htmlFor="workspace-username">用户名</label>
        <input id="workspace-username" name="username" autoComplete="username" autoCapitalize="none" spellCheck={false} required autoFocus value={username} onChange={event => setUsername(event.target.value)} disabled={busy} placeholder="例如 user001" />
        <label htmlFor="workspace-password">密码</label>
        <input id="workspace-password" name="password" type="password" autoComplete="current-password" required value={password} onChange={event => setPassword(event.target.value)} disabled={busy} />
        {error && <p className="workspace-login-error" role="alert">{error}</p>}
        <button className="workspace-login-submit" type="submit" disabled={busy || !username.trim() || !password}>{busy ? '正在登录…' : '进入工作台'}</button>
      </form>
      <p className="workspace-login-note">账号由管理员分配</p>
    </section>
  </main>;
}
