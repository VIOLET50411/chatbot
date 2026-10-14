import { useQuery } from '@tanstack/react-query';
import { Badge, PageHeader, QueryBoundary } from '../components';
import { getWechatStatus, listMaterialReviews, listOwnerContacts } from '../api';
import { useCharacter } from '../features/characters';

export function OverviewPage() {
  const { selected, selectedId } = useCharacter();
  const statusQuery = useQuery({ queryKey: ['wechat-status', selectedId], queryFn: ({ signal }) => getWechatStatus(selectedId, signal), enabled: Boolean(selectedId), refetchInterval: 10_000 });
  const materialsQuery = useQuery({ queryKey: ['owner-materials-summary', selectedId], queryFn: ({ signal }) => listMaterialReviews({ limit: 1 }, selectedId, signal), enabled: Boolean(selectedId) });
  const contactsQuery = useQuery({ queryKey: ['owner-contacts', selectedId], queryFn: ({ signal }) => listOwnerContacts(selectedId, signal), enabled: Boolean(selectedId) });
  return (
    <div className="page owner-page admin-page">
      <PageHeader title="运行总览" description={`正在查看 ${selected?.displayName ?? '所选人物'}；她的联系人、记忆和微信队列与其他人物完全分开。`} />
      <QueryBoundary query={statusQuery} apiPath="/api/wechat/status">
        {(status) => (
          <section className="card overview-health admin-glass-surface">
            <div>
              <h2 className="card-title">核心与微信链路</h2>
              <p className="card-description">Docker 核心提供调度、数据库和管理台；Windows Plus 代理只执行微信 UI 动作。</p>
            </div>
            <Badge tone={status.bridge.online && status.bridge.windowReady ? 'positive' : 'danger'}>
              {status.bridge.online && status.bridge.windowReady ? '链路正常' : '微信桥接离线'}
            </Badge>
          </section>
        )}
      </QueryBoundary>
      <section className="card-grid">
        <QueryBoundary query={materialsQuery} apiPath="/api/materials">
          {(materials) => <article className="stat-card admin-glass-surface"><p className="stat-card-value">{materials.summary.pending ?? 0}</p><p className="stat-card-label">待审核人设素材</p></article>}
        </QueryBoundary>
        <QueryBoundary query={contactsQuery} apiPath="/api/contacts">
          {(contacts) => <article className="stat-card admin-glass-surface"><p className="stat-card-value">{contacts.filter((item) => item.enabled).length}</p><p className="stat-card-label">启用联系人</p></article>}
        </QueryBoundary>
        <QueryBoundary query={statusQuery} apiPath="/api/wechat/status">
          {(status) => <article className="stat-card admin-glass-surface"><p className="stat-card-value">{status.inboundPending}</p><p className="stat-card-label">入站消息积压</p></article>}
        </QueryBoundary>
        <QueryBoundary query={statusQuery} apiPath="/api/wechat/status">
          {(status) => <article className="stat-card admin-glass-surface"><p className="stat-card-value">{(status.outbox.queued ?? 0) + (status.outbox.retry_wait ?? 0)}</p><p className="stat-card-label">出站动作积压</p></article>}
        </QueryBoundary>
      </section>
    </div>
  );
}
