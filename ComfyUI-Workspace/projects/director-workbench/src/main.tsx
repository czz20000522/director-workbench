import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import LoginGate from './LoginGate';
import './styles.css';

function WorkbenchEntry() {
  const [privateMode, setPrivateMode] = React.useState<boolean | null>(null);
  const [failed, setFailed] = React.useState(false);
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
  if (privateMode === null) return <main className="workspace-login"><p role="status">正在连接工作台…</p></main>;
  return privateMode ? <LoginGate /> : <App />;
}

ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><WorkbenchEntry /></React.StrictMode>);
