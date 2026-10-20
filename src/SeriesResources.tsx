import { useEffect, useState } from 'react';
import { ChevronLeft, FolderOpen, Music2, Package, RefreshCw, Users } from 'lucide-react';

type PackAsset = { asset_id: string; role?: string; view?: string; filename?: string };
type Pack = { id: string; kind: string; title: string; version: number; status: string; assets?: PackAsset[] };
type Episode = { id: string; title: string };

const labels: Record<string, string> = { character: '角色', scene: '场景', prop: '道具', sound: '声音' };
const icons: Record<string, typeof Users> = { character: Users, scene: FolderOpen, prop: Package, sound: Music2 };

function assetUrl(series: string, pack: Pack, asset: PackAsset) {
  return `/api/series/${encodeURIComponent(series)}/resource-packs/${encodeURIComponent(pack.id)}/assets/${encodeURIComponent(asset.asset_id)}`;
}

function isImage(asset: PackAsset) {
  return /\.(png|jpe?g|webp|gif)$/i.test(asset.filename || '');
}

export default function SeriesResources({ series, episodes, onBack, onOpen }: {
  series: string; episodes: Episode[]; onBack: () => void; onOpen: (projectId: string) => void;
}) {
  const [packs, setPacks] = useState<Pack[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = async () => {
    setLoading(true); setError('');
    try {
      const response = await fetch(`/api/series/${encodeURIComponent(series)}/resource-packs`, { credentials: 'same-origin' });
      const body = await response.json().catch(() => null);
      if (!response.ok) throw new Error(body?.detail?.message || body?.detail || `请求失败（${response.status}）`);
      setPacks(body.packs || []);
    } catch (err) { setError(err instanceof Error ? err.message : '无法读取资源包'); }
    finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, [series]);

  return <div className="sf-series-page">
    <button className="sf-back" onClick={onBack}><ChevronLeft size={15} /> 全部系列</button>
    <div className="sf-series-head">
      <div><p className="sf-step-label">系列</p><h1>{series}</h1><p className="sf-lede">一个系列包含多集作品；角色、场景、道具和声音资源在这里复用。</p></div>
      <button className="sf-secondary" onClick={() => void load()} disabled={loading}><RefreshCw size={15} /> 刷新</button>
    </div>
    {loading && <div className="sf-empty-card"><p>正在读取资源包…</p></div>}
    {error && <div className="sf-empty-card"><p className="sf-error">{error}</p><button className="sf-secondary" onClick={() => void load()}>重试</button></div>}
    {!loading && !error && <section className="sf-section">
      <div className="sf-section-head"><h2>资源包</h2><span>{packs.length} 个</span></div>
      {packs.length === 0 ? <div className="sf-empty-card"><Package size={20} /><div><h3>还没有资源包</h3><p>让 Agent 把角色、场景或声音资产登记到这个系列；发布后所有集都能复用。</p></div></div> : <div className="sf-series-packs">
        {packs.map(pack => {
          const Icon = icons[pack.kind] || Package;
          return <article className="sf-series-pack" key={pack.id}>
            <div className="sf-series-pack-head"><Icon size={18} /><strong>{pack.title}</strong><span>{labels[pack.kind] || pack.kind} · v{pack.version} · {pack.status}</span></div>
            <div className="sf-series-assets">
              {(pack.assets || []).length === 0 ? <p className="sf-muted">暂无资产</p> : (pack.assets || []).map(asset =>
                isImage(asset) ? <figure key={asset.asset_id}><img src={assetUrl(series, pack, asset)} alt={asset.role || asset.filename || '资源'} /><figcaption>{asset.role || asset.view || '资产'}</figcaption></figure>
                  : <div className="sf-series-file" key={asset.asset_id}><Music2 size={16} /><span>{asset.role || asset.filename || '资产'}</span></div>)}
            </div>
          </article>;
        })}
      </div>}
    </section>}
    <section className="sf-section">
      <div className="sf-section-head"><h2>这一系列的作品</h2><span>{episodes.length} 集</span></div>
      {episodes.length === 0 ? <div className="sf-empty-card"><p>这一系列还没有作品。</p></div> : <div className="sf-series-works">
        {episodes.map(episode => <button className="sf-work-row" key={episode.id} onClick={() => onOpen(episode.id)}>
          <span className="sf-work-mark">{episode.title.slice(0, 1)}</span><span className="sf-work-main"><strong>{episode.title}</strong><small>打开这一集</small></span><span className="sf-work-step">作品</span>
        </button>)}
      </div>}
    </section>
  </div>;
}

