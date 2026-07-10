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
  search: string,
  params: {
    domainId?: string;
    objectId?: string;
    logicId?: string;
    relationId?: string;
  },
): Promise<Crumb[]> {
  const searchParams = new URLSearchParams(search);

  if (pathname === "/workspace" || pathname === "/workspace/") {
    return [{ label: "本体建模" }];
  }

  if (pathname.startsWith("/workspace/")) {
    const crumbs: Crumb[] = [{ label: "本体建模", path: "/workspace" }];
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
    return [{ label: "本体浏览" }];
  }

  if (pathname.startsWith("/ontology/relations/") && params.relationId) {
    const rel = await api.getRelationType(params.relationId);
    return [{ label: "本体浏览", path: "/ontology" }, { label: rel.display_name }];
  }

  if (pathname.startsWith("/ontology/") && params.objectId) {
    const obj = await api.getObjectType(params.objectId);
    return [{ label: "本体浏览", path: "/ontology" }, { label: obj.display_name }];
  }

  // ---- 业务逻辑 ----

  const BL_BASE: Crumb = { label: "业务逻辑", path: "/business-logic" };

  if (pathname === "/business-logic") {
    return [BL_BASE];
  }

  // /business-logic/category/:categoryId
  if (pathname.startsWith("/business-logic/category/")) {
    const segments = pathname.split("/");
    const catId = segments[3];
    if (catId) {
      const cats = await api.listBusinessLogicCategories();
      const cat = cats.find((c) => c.id === catId);
      return [BL_BASE, { label: cat?.name ?? "分类" }];
    }
    return [BL_BASE, { label: "分类" }];
  }

  // /business-logic/create?domain=xxx&category=yyy
  if (pathname === "/business-logic/create") {
    const catId = searchParams.get("category");
    const crumbs: Crumb[] = [BL_BASE];
    if (catId) {
      const cats = await api.listBusinessLogicCategories();
      const cat = cats.find((c) => c.id === catId);
      if (cat) {
        crumbs.push({ label: cat.name, path: `/business-logic/category/${cat.id}` });
      }
    }
    crumbs.push({ label: "新建" });
    return crumbs;
  }

  // /business-logic/:logicId
  if (pathname.startsWith("/business-logic/") && params.logicId) {
    const logic = await api.getBusinessLogic(params.logicId);
    const crumbs: Crumb[] = [BL_BASE];
    if (logic.category_id && logic.category_name) {
      crumbs.push({
        label: logic.category_name,
        path: `/business-logic/category/${logic.category_id}`,
      });
    }
    crumbs.push({ label: logic.display_name });
    return crumbs;
  }

  if (pathname === "/settings") {
    return [{ label: "设置" }];
  }

  if (pathname.startsWith("/chat-bi")) {
    return [{ label: "智能问数" }];
  }

  return [{ label: "首页", path: "/ontology" }];
}

export function AppBreadcrumb() {
  const location = useLocation();
  const params = useParams();
  const [items, setItems] = useState<ItemType[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);

    resolveBreadcrumbs(location.pathname, location.search, params)
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
          setItems([{ title: <Link to="/ontology">本体浏览</Link> }]);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [location.pathname, location.search, params.domainId, params.objectId, params.logicId, params.relationId]);

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
