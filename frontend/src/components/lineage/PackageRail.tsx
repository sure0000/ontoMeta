import { CloudUploadOutlined, ReloadOutlined } from "@ant-design/icons";
import { Button, Tag, Tooltip } from "antd";
import { groupsOf } from "./prototypeData";
import type { SqlPackage } from "./prototypeData";

/**
 * 代码包历史。
 *
 * 补录是长期活：同一个域会陆续收到好几个包，有的补投、有的重投，扫完不一定当场
 * 上报。所以包必须留档——什么时候投的、扫出多少边、上报了没有、当时让几张表脱离
 * 孤岛。没有这份历史，"这个包是不是已经补过了"就只能靠人记。
 */

interface Props {
  packages: SqlPackage[];
  currentId: string | null;
  appliedInSession: Record<string, { edges: number; resolved: number }>;
  scanningId: string | null;
  onSelect: (id: string) => void;
  onUpload: () => void;
  onRescan: (id: string) => void;
}

function edgeStats(pkg: SqlPackage) {
  const edges = groupsOf(pkg).flatMap((g) => g.edges);
  return {
    ok: edges.filter((e) => e.state === "ok").length,
    targets: pkg.targets.length,
    isolated: groupsOf(pkg).filter((g) => g.isolated).length,
  };
}

export function PackageRail({
  packages,
  currentId,
  appliedInSession,
  scanningId,
  onSelect,
  onUpload,
  onRescan,
}: Props) {
  return (
    <>
      <div className="lin-rail-controls">
        <Button size="small" type="primary" icon={<CloudUploadOutlined />} block onClick={onUpload}>
          上传新代码包
        </Button>
      </div>

      <ul className="lin-pkg-list">
        {packages.map((pkg) => {
          const stats = edgeStats(pkg);
          const session = appliedInSession[pkg.id];
          const history = session ? { ...session, at: "刚刚" } : pkg.applied;
          const partial = history ? history.edges < stats.ok : false;
          const scanning = scanningId === pkg.id;

          return (
            <li key={pkg.id}>
              <button
                type="button"
                className={`lin-pkg${pkg.id === currentId ? " lin-pkg--on" : ""}`}
                onClick={() => onSelect(pkg.id)}
              >
                <span className="lin-pkg-head">
                  <span className="lin-pkg-name" title={pkg.name}>
                    {pkg.name}
                  </span>
                  {scanning ? (
                    <Tag color="processing" variant="filled">
                      扫描中
                    </Tag>
                  ) : history ? (
                    <Tag color={partial ? "warning" : "success"} variant="filled">
                      {partial ? `部分上报 ${history.edges}/${stats.ok}` : "已上报"}
                    </Tag>
                  ) : (
                    <Tag variant="filled">未上报</Tag>
                  )}
                </span>

                <span className="lin-pkg-meta">
                  {pkg.uploadedAt} · {pkg.sqlFiles} 个 .sql · {pkg.size}
                </span>

                <span className="lin-pkg-nums">
                  <b>{stats.ok}</b> 条边
                  <i />
                  {stats.targets} 个落点
                  {stats.isolated > 0 && (
                    <>
                      <i />
                      {stats.isolated} 张孤岛
                    </>
                  )}
                </span>

                {history && (
                  <span className="lin-pkg-applied">
                    {history.at} 上报 {history.edges} 条 · 当时 {history.resolved} 张脱离孤岛
                  </span>
                )}
              </button>

              <Tooltip title="用当前解析器重新扫一遍">
                <Button
                  className="lin-pkg-rescan"
                  size="small"
                  type="text"
                  icon={<ReloadOutlined />}
                  loading={scanning}
                  onClick={() => onRescan(pkg.id)}
                  aria-label="重新扫描"
                />
              </Tooltip>
            </li>
          );
        })}
      </ul>

      <p className="lin-rail-foot">
        包留档只为回答一件事：这个包补过没有、补了多少。重投同一个包不会重复建边（幂等）。
      </p>
    </>
  );
}
