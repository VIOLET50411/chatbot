import { Link } from 'react-router-dom';

export function NotFoundPage() {
  return (
    <div className="page">
      <h1 className="page-header-title">页面不存在</h1>
      <p className="page-header-description">请检查地址是否正确，或返回总览页。</p>
      <Link className="btn btn-primary" to="/overview">
        返回总览
      </Link>
    </div>
  );
}
