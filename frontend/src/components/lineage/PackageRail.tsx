import { CloudUploadOutlined, DeleteOutlined, ReloadOutlined } from "@ant-design/icons";
import { Button, Popconfirm, Tag, Tooltip } from "antd";
import type { LineagePackageRow } from "../../types";

/**
 * 代码包历史。
 *
 * 补录是长期活：同一个域会陆续收到好几个包，有的补投、有的重投，扫完不一定当场
 * 上报。所以包必须留档——什么时候投的、扫出多少边、上报了没有、当时让几张表脱离
 * 孤岛。没有这份历史，"这个包是不是已经补过了"就只能靠人记。
 */

interface Props {
  packages: LineagePackageRow[];
  currentId: string | null;
  scanningId: string | null;
  uploading: boolean;
  onSelect: (id: string) => void;
  onUpload: () => void;
  onRescan: (id: string) => void;
  onDelete: (id: string) => void;
}

function sizeLabel(bytes: number) {
  if (!bytes) return "—";
  const mb = bytes / 1024 / 1024;
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

export function PackageRail({
  packages,
  currentId,
  scanningId,
  uploading,
  onSelect,
  onUpload,
  onRescan,
  onDelete,
}: Props) {
  return (
    <>
      <div className="lin-rail-controls">
        <Button
          size="small"
          type={uploading ? "default" : "primary"}
          icon={<CloudUploadOutlined />}
          block
          onClick={onUpload}
        >
          上传新代码包
        </Button>
      </div>

      <ul className="lin-pkg-list">
        {packages.map((pkg) => {
          const applied = pkg.applied_edges > 0;
          const partial = pkg.status === "partial";
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
                  ) : applied ? (
                    <Tag color={partial ? "warning" : "success"} variant="filled">
                      {partial
                        ? `部分上报 ${pkg.applied_edges}/${pkg.applied_edges + pkg.edges_ok}`
                        : "已上报"}
                    </Tag>
                  ) : (
                    <Tag variant="filled">未上报</Tag>
                  )}
                </span>

                <span className="lin-pkg-meta">
                  {(pkg.uploaded_at ?? "").replace("T", " ").slice(0, 16)} · {pkg.sql_files} 个 .sql
                  · {sizeLabel(pkg.size_bytes)}
                </span>

                <span className="lin-pkg-nums">
                  <b>{pkg.edges_ok}</b> 条边
                  <i />
                  {pkg.targets} 个落点
                  {pkg.isolated_targets > 0 && (
                    <>
                      <i />
                      {pkg.isolated_targets} 张孤岛
                    </>
                  )}
                </span>

                {applied && (
                  <span className="lin-pkg-applied">
                    {(pkg.applied_at ?? "").replace("T", " ").slice(0, 16)} 上报 {pkg.applied_edges}{" "}
                    条 · 当时 {pkg.applied_resolved} 张脱离孤岛
                  </span>
                )}
              </button>

              <span className="lin-pkg-acts">
                <Tooltip title="用当前解析器重新扫一遍">
                  <Button
                    size="small"
                    type="text"
                    icon={<ReloadOutlined />}
                    loading={scanning}
                    onClick={() => onRescan(pkg.id)}
                    aria-label="重新扫描"
                  />
                </Tooltip>
                <Popconfirm
                  title="删除这条记录？"
                  description={
                    applied
                      ? "已写进 DataHub 的血缘边不会被撤销，只是本地不再留档。"
                      : "只删本地记录与归档包。"
                  }
                  okText="删除"
                  cancelText="取消"
                  onConfirm={() => onDelete(pkg.id)}
                >
                  <Button size="small" type="text" danger icon={<DeleteOutlined />} aria-label="删除" />
                </Popconfirm>
              </span>
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
