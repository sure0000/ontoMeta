import { Breadcrumb, Spin } from "antd";
import type { ItemType } from "antd/es/breadcrumb/Breadcrumb";
import { useEffect, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { api } from "../api";

interface Crumb {
  label: string;
  path?: string;
}

async function resolveBreadcrumbs(
  pathname: string,
  params: {
    domainId?: string;
    objectId?: string;
    logicId?: string;
    relationId?: string;
  },
): Promise<Crumb[]> {
  if (pathname === "/workspace" || pathname === "/workspace/") {
    return [{ label: "工作区" }];
  }

  if (pathname.startsWith("/workspace/")) {
    const crumbs: Crumb[] = [{ label: "工作区", path: "/workspace" }];
    if (!params.domainId) return crumbs;

    const domain = await api.getDomain(params.domainId);
    const domainPath = `/workspace/${params.domainId}`;

    if (pathname.endsWith("/executions")) {
      return [
        ...crumbs,
        { label: domain.name, path: domainPath },
        { label: "执行记录" },
      ];
    }

    if (params.relationId && pathname.includes("/relations/")) {
      const rel = await api.getRelationType(params.relationId);
      return [
        ...crumbs,
        { label: domain.name, path: domainPath },
        { label: rel.display_name },
      ];
    }

    if (params.objectId && pathname.includes("/objects/")) {
      const obj = await api.getObjectType(params.objectId);
      return [
        ...crumbs,
        { label: domain.name, path: domainPath },
        { label: obj.display_name },
      ];
    }

    return [...crumbs, { label: domain.name }];
  }

  if (pathname === "/ontology") {
    return [{ label: "本体" }];
  }

  if (pathname.startsWith("/ontology/relations/") && params.relationId) {
    const rel = await api.getRelationType(params.relationId);
    return [{ label: "本体", path: "/ontology" }, { label: rel.display_name }];
  }

  if (pathname.startsWith("/ontology/") && params.objectId) {
    const obj = await api.getObjectType(params.objectId);
    return [{ label: "本体", path: "/ontology" }, { label: obj.display_name }];
  }

  if (pathname === "/business-logic") {
    return [{ label: "业务逻辑" }];
  }

  if (pathname === "/business-logic/create") {
    return [
      { label: "业务逻辑", path: "/business-logic" },
      { label: "新建" },
    ];
  }

  if (pathname.startsWith("/business-logic/") && params.logicId) {
    const logic = await api.getBusinessLogic(params.logicId);
    return [
      { label: "业务逻辑", path: "/business-logic" },
      { label: logic.display_name },
    ];
  }

  if (pathname === "/settings") {
    return [{ label: "设置" }];
  }

  return [{ label: "首页", path: "/workspace" }];
}

export function AppBreadcrumb() {
  const location = useLocation();
  const params = useParams();
  const [items, setItems] = useState<ItemType[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);

    resolveBreadcrumbs(location.pathname, params)
      .then((crumbs) => {
        if (cancelled) return;
        setItems(
          crumbs.map((crumb, index) => {
            const isLast = index === crumbs.length - 1;
            const title =
              crumb.path && !isLast ? <Link to={crumb.path}>{crumb.label}</Link> : crumb.label;
            return { title };
          }),
        );
      })
      .catch(() => {
        if (!cancelled) {
          setItems([{ title: <Link to="/workspace">工作区</Link> }]);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [location.pathname, params.domainId, params.objectId, params.logicId, params.relationId]);

  if (loading && items.length === 0) {
    return (
      <div className="app-breadcrumb">
        <Spin size="small" />
      </div>
    );
  }

  return (
    <div className="app-breadcrumb">
      <Breadcrumb items={items} />
    </div>
  );
}
