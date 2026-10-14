import type { UseQueryResult } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { ApiError } from '../api/client';
import { EmptyState, ErrorState, LoadingState, NotImplementedState } from './StatusStates';

interface QueryBoundaryProps<T> {
  query: UseQueryResult<T>;
  /** 规划中的 API 路径，用于 NotImplementedState 展示；仅在 404/410/501 时使用。 */
  apiPath: string;
  /** 判断数据是否为空（例如空数组）；为空时渲染 emptyState。 */
  isEmpty?: (data: T) => boolean;
  emptyState?: ReactNode;
  children: (data: T) => ReactNode;
}

/**
 * 统一的加载 / 空 / 错误（含“接口未部署”）边界。
 * 所有页面通过它接入真实 `/api/*` 路由，不显示假数据。
 */
export function QueryBoundary<T>({
  query,
  apiPath,
  isEmpty,
  emptyState,
  children,
}: QueryBoundaryProps<T>) {
  if (query.isLoading) {
    return <LoadingState />;
  }

  if (query.isError) {
    const error = query.error;
    if (error instanceof ApiError && error.notImplemented) {
      return <NotImplementedState apiPath={apiPath} />;
    }
    const description = error instanceof Error ? error.message : '发生未知错误';
    return <ErrorState description={description} onRetry={() => void query.refetch()} />;
  }

  if (query.data === undefined) {
    return <EmptyState />;
  }

  if (isEmpty?.(query.data)) {
    return <>{emptyState ?? <EmptyState />}</>;
  }

  return <>{children(query.data)}</>;
}
