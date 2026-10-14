import type { ReactNode } from 'react';

interface StatusBlockProps {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
}

function StatusBlock({ icon, title, description, action }: StatusBlockProps) {
  return (
    <div className="status-block">
      {icon && <div className="status-block-icon" aria-hidden="true">{icon}</div>}
      <p className="status-block-title">{title}</p>
      {description && <p className="status-block-description">{description}</p>}
      {action}
    </div>
  );
}

export function LoadingState({ label = '正在加载数据…' }: { label?: string }) {
  return (
    <div className="status-block status-block-loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <p className="status-block-title">{label}</p>
    </div>
  );
}

interface EmptyStateProps {
  title?: string;
  description?: string;
  action?: ReactNode;
}

/**
 * 统一空态：用于“接口存在但暂无数据”与“接口尚未部署”两种情况。
 * 调用方通过 description 明确区分，页面绝不为了看起来“有数据”而编造内容。
 */
export function EmptyState({
  title = '暂无数据',
  description = '当前没有可展示的数据。',
  action,
}: EmptyStateProps) {
  return <StatusBlock icon="—" title={title} description={description} action={action} />;
}

interface ErrorStateProps {
  title?: string;
  description: string;
  onRetry?: () => void;
}

export function ErrorState({ title = '加载失败', description, onRetry }: ErrorStateProps) {
  return (
    <div className="status-block status-block-error" role="alert">
      <p className="status-block-title">{title}</p>
      <p className="status-block-description">{description}</p>
      {onRetry && (
        <button type="button" className="btn btn-ghost" onClick={onRetry}>
          重试
        </button>
      )}
    </div>
  );
}

/**
 * “接口尚未部署”专用态：与普通错误区分，用蓝色信息语气而非红色错误语气，
 * 明确告知用户这是规划中的能力，不是运行故障。
 */
export function NotImplementedState({ apiPath }: { apiPath: string }) {
  return (
    <div className="status-block status-block-planned">
      <p className="status-block-title">该功能等待后端接口接入</p>
      <p className="status-block-description">
        规划路径 <code>{apiPath}</code> 尚未部署。界面已就位，接口上线后会自动展示真实数据。
      </p>
    </div>
  );
}
