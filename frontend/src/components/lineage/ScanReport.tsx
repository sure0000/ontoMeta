import {
  ArrowRightOutlined,
  CheckCircleOutlined,
  FileSearchOutlined,
  InboxOutlined,
  NodeIndexOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { Button, Collapse, Table, Tag, Upload } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useMemo } from "react";
import { DOMAIN_FACTS, groupsOf, uncoveredIsolated } from "./prototypeData";
import type { ScanGroup, SqlPackage } from "./prototypeData";

/**
 * 一个代码包的扫描结果。
 *
 * 代码包**没有固定格式**——目录怎么放、语句怎么写都不做要求，所以这一屏的重点
 * 不是"解析成功了"，而是三个结论：能补哪些边、影响哪些表、哪些孤岛还是孤岛。
 * 解析失败的文件必须摊开来讲：野生代码包里存储过程和动态 SQL 一定有，藏起来
 * 会让人以为"扫完了就全了"。
 */

interface Props {
  pkg: SqlPackage | null;
  /** 正在上传新包：主区让位给上传区。 */
  uploading: boolean;
  scanning: boolean;
  onScan: () => void;
  selected: string[];
  onSelectedChange: (keys: string[]) => void;
  frozen: boolean;
  appliedNote?: string;
  onSendToCanvas: (table: string) => void;
}

function countEdges(groups: ScanGroup[]) {
  const all = groups.flatMap((g) => g.edges);
  return {
    total: all.length,
    ok: all.filter((e) => e.state === "ok").length,
    blocked: all.filter((e) => e.state === "blocked").length,
    skipped: all.filter((e) => e.state === "skipped").length,
  };
}

export function ScanReport({
  pkg,
  uploading,
  scanning,
  onScan,
  selected,
  onSelectedChange,
  frozen,
  appliedNote,
  onSendToCanvas,
}: Props) {
  const groups = useMemo(() => (pkg ? groupsOf(pkg) : []), [pkg]);

  const stats = useMemo(() => {
    const edges = countEdges(groups);
    const tables = new Set<string>();
    groups.forEach((g) => {
      tables.add(g.target);
      g.edges.forEach((e) => tables.add(e.src));
    });
    const resolved = groups.filter((g) => g.isolated).length;
    return { edges, affected: tables.size, resolved, after: DOMAIN_FACTS.isolated - resolved };
  }, [groups]);

  const done = pkg?.applied;
  const uncovered = useMemo(() => uncoveredIsolated(), []);

  if (uploading || !pkg) {
    return (
      <div className="lin-dropzone">
        <Upload.Dragger
          multiple
          showUploadList={false}
          beforeUpload={() => {
            onScan();
            return false;
          }}
          disabled={scanning}
        >
          <p className="lin-dropzone-icon">
            <InboxOutlined />
          </p>
          <p className="lin-dropzone-title">把 SQL 代码包拖到这里</p>
          <p className="lin-dropzone-hint">
            .zip / .tar.gz / 整个目录都行。<b>不要求目录结构</b>
            ——递归扫描包内所有 .sql 文件，逐条语句提取 FROM / JOIN / INSERT / CREATE
            的表引用与关联键。扫完的包会留在左边的历史里。
          </p>
        </Upload.Dragger>
        <div className="lin-dropzone-foot">
          <Button type="primary" icon={<FileSearchOutlined />} loading={scanning} onClick={onScan}>
            用示例代码包扫描
          </Button>
          <span className="lin-muted">原型：不会真的读文件，扫的是内置示例包</span>
        </div>
      </div>
    );
  }

  const columns: ColumnsType<ScanGroup> = [
    {
      title: "目标表（血缘落点）",
      dataIndex: "target",
      key: "target",
      render: (target: string, row) => (
        <div className="lin-cell-table">
          <span className="lin-cell-name">{target}</span>
          {row.isolated && (
            <Tag color="error" variant="filled">
              孤岛
            </Tag>
          )}
        </div>
      ),
    },
    {
      title: "可补的边",
      key: "edges",
      width: 172,
      render: (_, row) => {
        const c = countEdges([row]);
        return (
          <span className="lin-cell-edges">
            <b>{c.ok}</b> 条
            {c.blocked > 0 && (
              <Tag color="warning" variant="filled">
                待映射 {c.blocked}
              </Tag>
            )}
            {c.skipped > 0 && <Tag variant="filled">跳过 {c.skipped}</Tag>}
          </span>
        );
      },
    },
    {
      title: "来源文件",
      dataIndex: "file",
      key: "file",
      width: 226,
      render: (file: string) => <span className="lin-cell-file">{file}</span>,
    },
    {
      title: "补录后",
      key: "after",
      width: 108,
      render: (_, row) =>
        row.isolated ? (
          <Tag color="success" variant="filled">
            脱离孤岛
          </Tag>
        ) : (
          <span className="lin-muted">补充上下游</span>
        ),
    },
  ];

  return (
    <div className="lin-scan">
      <div className="lin-scan-bar">
        <div className="lin-scan-bar-main">
          <FileSearchOutlined />
          <b>{pkg.name}</b>
          <span className="lin-muted">
            {pkg.uploadedAt} · {pkg.size} · {pkg.directories} 个目录 · {pkg.sqlFiles} 个 .sql ·{" "}
            {pkg.statements} 条语句
          </span>
          <Tag color="success" variant="filled">
            解析成功 {pkg.sqlFiles - pkg.failures.length}
          </Tag>
          {pkg.failures.length > 0 && (
            <Tag color="warning" variant="filled">
              失败 {pkg.failures.length}
            </Tag>
          )}
        </div>
        {appliedNote && (
          <span className="lin-scan-applied">
            <CheckCircleOutlined /> {appliedNote}
          </span>
        )}
      </div>

      {/* 三条结论。已上报过的包换一套口径：讲"当时补了什么"，不再讲"能补什么" */}
      <div className="lin-verdicts">
        <div className="lin-verdict">
          <span className="lin-verdict-label">{done ? "已补的血缘" : "能补多少血缘"}</span>
          <b className="lin-verdict-num">{done ? done.edges : stats.edges.ok}</b>
          <span className="lin-verdict-unit">{done ? "条边已写入 DataHub" : "条边可上报"}</span>
          <div className="lin-verdict-foot">
            {done
              ? `${done.at} 上报，重投同一个包不会重复建边`
              : `另有 ${stats.edges.blocked} 条表名对不上 DataHub、${stats.edges.skipped} 条不在本域`}
          </div>
        </div>
        <div className="lin-verdict">
          <span className="lin-verdict-label">影响多少表</span>
          <b className="lin-verdict-num">{stats.affected}</b>
          <span className="lin-verdict-unit">张表将获得血缘</span>
          <div className="lin-verdict-foot">
            覆盖 {groups.length} 个落点，上游表 {stats.affected - groups.length} 张
          </div>
        </div>
        <div className="lin-verdict lin-verdict--key">
          <span className="lin-verdict-label">孤岛怎么变</span>
          {done ? (
            <>
              <b className="lin-verdict-num">{done.resolved}</b>
              <span className="lin-verdict-unit">张表当时脱离孤岛</span>
              <div className="lin-verdict-foot">已生效：这些落点现在都有上下游，不再计入孤岛</div>
            </>
          ) : (
            <>
              <b className="lin-verdict-num">
                {DOMAIN_FACTS.isolated}
                <ArrowRightOutlined className="lin-verdict-arrow" />
                {stats.after}
              </b>
              <span className="lin-verdict-unit">张孤岛表</span>
              <div className="lin-verdict-foot">
                {stats.resolved} 张靠这个包脱离孤岛，剩 {stats.after} 张要手工连
              </div>
            </>
          )}
        </div>
      </div>

      {/* 明细：按落点分组，不逐条列——一个包上百条边，逐条列没人读得完 */}
      <Table<ScanGroup>
        className="lin-scan-table"
        size="small"
        rowKey="target"
        columns={columns}
        dataSource={groups}
        pagination={false}
        rowSelection={{
          selectedRowKeys: selected,
          onChange: (keys) => onSelectedChange(keys as string[]),
          getCheckboxProps: () => ({ disabled: frozen }),
        }}
        expandable={{
          expandedRowRender: (row) => (
            <div className="lin-edge-list">
              {row.edges.map((edge) => (
                <div
                  key={edge.id}
                  className={`lin-edge-line${edge.state !== "ok" ? " lin-edge-line--off" : ""}`}
                >
                  <span className="lin-node">{edge.src}</span>
                  <ArrowRightOutlined className="lin-flow-arrow" />
                  <span className="lin-node lin-node--target">{edge.dst}</span>
                  {edge.key ? (
                    <span className="lin-key">{edge.key}</span>
                  ) : (
                    <Tag variant="filled">无 JOIN 条件 · 仅表级</Tag>
                  )}
                  {edge.state === "blocked" && (
                    <Tag color="warning" variant="filled">
                      {edge.reason}
                    </Tag>
                  )}
                  {edge.state === "skipped" && <Tag variant="filled">{edge.reason}</Tag>}
                </div>
              ))}
            </div>
          ),
        }}
      />

      <div className="lin-scan-tail">
        {/* 第二个结论：所有包都没提到的孤岛表，只能手工连 */}
        <section className="lin-uncovered">
          <div className="lin-uncovered-head">
            <WarningOutlined />
            <b>所有代码包都没覆盖的孤岛表</b>
            <span className="lin-muted">
              一次都没在 SQL 里出现过，没有代码可推——只能在画布上手工连
            </span>
          </div>
          <div className="lin-uncovered-list">
            {uncovered.map((table) => (
              <button
                key={table}
                type="button"
                className="lin-uncovered-item"
                onClick={() => onSendToCanvas(table)}
              >
                <span>{table}</span>
                <NodeIndexOutlined />
                <em>放到画布</em>
              </button>
            ))}
          </div>
        </section>

        {pkg.failures.length > 0 && (
          <Collapse
            size="small"
            className="lin-failures"
            items={[
              {
                key: "failures",
                label: `解析失败的 ${pkg.failures.length} 个文件（不影响其余结果）`,
                children: (
                  <ul className="lin-failure-list">
                    {pkg.failures.map((item) => (
                      <li key={item.file}>
                        <span className="lin-cell-file">{item.file}</span>
                        <span className="lin-muted">{item.reason}</span>
                      </li>
                    ))}
                  </ul>
                ),
              },
            ]}
          />
        )}
      </div>
    </div>
  );
}
