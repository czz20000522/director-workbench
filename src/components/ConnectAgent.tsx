import { useState } from 'react';

export default function ConnectAgent() {
  const [copyMessage, setCopyMessage] = useState('');
  const startUrl = `${window.location.origin}/agent/start`;
  async function copy() {
    try { await navigator.clipboard.writeText(startUrl); setCopyMessage('入口已复制，发给你的 Agent 即可。'); }
    catch { setCopyMessage('请选中下方入口地址，手动复制给你的 Agent。'); }
  }
  return <details className="director-operation-panel" aria-label="连接你的Agent">
    <summary>连接你的 Agent</summary>
    <p className="director-muted">把这个入口发给你的 Agent，让它读取工作台能力和使用说明。需要登录后才能操作私人作品；连接结果以 Agent 的实际检查为准。</p>
    <p style={{ overflowWrap: 'anywhere' }}><a href="/agent/start">{startUrl}</a></p>
    <button type="button" className="button secondary compact" onClick={() => void copy()}>复制 Agent 入口</button>
    {copyMessage && <p role="status">{copyMessage}</p>}
    <p className="director-muted">高级接入使用 MCP <code>/mcp/</code>；通过登录会话的 Bearer 凭据认证。凭据只交给受信任的 Agent，不放进入口地址。</p>
    <nav aria-label="Agent 接入说明" className="director-panel-actions">
      <a href="/.well-known/director-workbench.json">发现清单</a>
      <a href="/agent/start">开始接入</a>
      <a href="/agent/skill/SKILL.md">Agent 使用说明</a>
      <a href="/agent/capabilities?group=creation">创作能力</a>
      <a href="/mcp/">MCP 入口</a>
    </nav>
  </details>;
}
