import React from 'react';
import ReactDOM from 'react-dom/client';
import StudioFlowLive from './StudioFlowLive';
import LibraryPage from './LibraryPage';
import LoginGate from './LoginGate';
import './styles.css';

class ErrorBoundary extends React.Component<React.PropsWithChildren, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('Director Workbench 渲染异常', error, info.componentStack);
  }

  retry = () => this.setState({ error: null });

  render() {
    if (!this.state.error) return this.props.children;
    return <main className="workspace-login" role="alert"><h1>工作台暂时遇到问题</h1><p>页面没有丢失数据。请重试；如果问题持续，请查看浏览器控制台日志。</p><button className="button primary" type="button" onClick={this.retry}>重试</button></main>;
  }
}

// Safari and older Chromium builds expose Web Crypto but do not yet provide
// crypto.randomUUID(). Install a small UUID v4 fallback before React mounts so
// the creation panels can initialize on those browsers as well.
function installRandomUuidFallback() {
  const cryptoApi = globalThis.crypto;
  if (!cryptoApi || typeof cryptoApi.randomUUID === 'function') return;
  const randomUUID = () => {
    const bytes = new Uint8Array(16);
    if (typeof cryptoApi.getRandomValues === 'function') cryptoApi.getRandomValues(bytes);
    else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  };
  try { Object.defineProperty(cryptoApi, 'randomUUID', { value: randomUUID, configurable: true }); }
  catch { try { (cryptoApi as unknown as { randomUUID: () => string }).randomUUID = randomUUID; } catch { /* best effort for locked-down browsers */ } }
}

installRandomUuidFallback();

function useHashRoute() {
  const [hash, setHash] = React.useState(() => window.location.hash);
  React.useEffect(() => {
    const onChange = () => setHash(window.location.hash);
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);
  return hash;
}

// #/library → 全部；#/library/<子库名> → 只开那一个库。返回 null 表示不是库路由。
function parseLibraryRoute(hash: string): string | null {
  if (!hash.startsWith('#/library')) return null;
  const rest = hash.slice('#/library'.length).replace(/^\//, '');
  if (!rest) return '';
  try { return decodeURIComponent(rest); } catch { return ''; }
}

function WorkbenchEntry() {
  const [privateMode, setPrivateMode] = React.useState<boolean | null>(null);
  const [failed, setFailed] = React.useState(false);
  const hash = useHashRoute();
  React.useEffect(() => {
    const controller = new AbortController();
    fetch('/api/auth/config', { signal: controller.signal })
      .then(async response => {
        if (!response.ok) throw new Error('认证配置不可用');
        const config = await response.json();
        if (typeof config.enabled !== 'boolean') throw new Error('认证配置无效');
        if (!controller.signal.aborted) setPrivateMode(config.enabled);
      }).catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, []);
  if (failed) return <main className="workspace-login"><p role="alert">无法连接工作台，请刷新重试。</p></main>;
  if (privateMode === null) return <main className="workspace-login"><p role="status" aria-live="polite">正在连接工作台…</p></main>;
  if (privateMode) return <LoginGate />;
  const libraryCategory = parseLibraryRoute(hash);
  if (libraryCategory !== null) return <LibraryPage key={libraryCategory} initialCategory={libraryCategory} />;
  return <StudioFlowLive username="本机" onLogout={() => window.location.reload()} />;
}

ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><ErrorBoundary><WorkbenchEntry /></ErrorBoundary></React.StrictMode>);
