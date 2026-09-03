import {
  ArrowRightOutlined,
  FileSearchOutlined,
  InboxOutlined,
  NodeIndexOutlined,
  ReloadOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { Button, Collapse, Empty, Table, Tag, Upload } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useMemo } from "react";
import {
  DOMAIN_FACTS,
  SCAN_FAILURES,
  SCAN_GROUPS,
  SCAN_PACKAGE,
  UNCOVERED_ISOLATED,
} from "./prototypeData";
import type { ScanGroup } from "./prototypeData";

/**
 * 路径 A：扔一个 SQL 代码包进来，递归扫所有 .sql，把能推出的血缘全捞出来。
 *
 * 代码包**没有固定格式**——目录怎么放、语句怎么写都不做要求，所以这一屏的重点
 * 不是"解析成功了"，而是三个结论：能补哪些边、影响哪些表、哪些孤岛还是孤岛。
 * 解析失败的文件必须摊开来讲：野生代码包里存储过程和动态 SQL 一定有，藏起来
 * 会让人以为"扫完了就全了"。
 */

interface Props {
  scanned: boolean;
  scanning: boolean;
  onScan: () => void;
  onReset: () => void;
  selected: string[];
  onSelectedChange: (keys: string[]) => void;
  frozen: boolean;
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
  scanned,
  scanning,
  onScan,
  onReset,
  selected,
  onSelectedChange,
  frozen,
  onSendToCanvas,
}: Props) {
  const stats = useMemo(() => {
    const edges = countEdges(SCAN_GROUPS);
    const tables = new Set<string>();
    SCAN_GROUPS.forEach((g) => {
      tables.add(g.target);
      g.edges.forEach((e) => tables.add(e.src));
    });
    const resolved = SCAN_GROUPS.filter((g) => g.isolated).map((g) => g.target);
    return {
      edges,
      affected: tables.size,
      resolved,
      stillIsolated: DOMAIN_FACTS.isolated - resolved.length,
    };
  }, []);

  if (!scanned) {
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
            的表引用与关联键。
          </p>
        </Upload.Dragger>
        <div className="lin-dropzone-foot">
          <Button type="primary" icon={<FileSearchOutlined />} loading={scanning} onClick={onScan}>
            用示例代码包扫描
          </Button>
          <span className="lin-muted">原型：示例包 = {SCAN_PACKAGE.name}</span>
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
      width: 176,
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
      width: 230,
      render: (file: string) => <span className="lin-cell-file">{file}</span>,
    },
    {
      title: "补录后",
      key: "after",
      width: 120,
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
      {/* 摘要：扫了什么 */}
      <div className="lin-scan-bar">
        <div className="lin-scan-bar-main">
          <FileSearchOutlined />
          <b>{SCAN_PACKAGE.name}</b>
          <span className="lin-muted">
            {SCAN_PACKAGE.size} · {SCAN_PACKAGE.directories} 个目录 · {SCAN_PACKAGE.sqlFiles} 个
            .sql · {SCAN_PACKAGE.statements} 条语句
          </span>
          <Tag color="success" variant="filled">
            解析成功 {SCAN_PACKAGE.sqlFiles - SCAN_FAILURES.length}
          </Tag>
          <Tag color="warning" variant="filled">
            失败 {SCAN_FAILURES.length}
          </Tag>
        </div>
        <Button size="small" icon={<ReloadOutlined />} onClick={onReset} disabled={frozen}>
          换一个包
        </Button>
      </div>

      {/* 三条结论 */}
      <div className="lin-verdicts">
        <div className="lin-verdict">
          <span className="lin-verdict-label">能补多少血缘</span>
          <b className="lin-verdict-num">{stats.edges.ok}</b>
          <span className="lin-verdict-unit">条边可上报</span>
          <div className="lin-verdict-foot">
            另有 {stats.edges.blocked} 条表名对不上 DataHub、{stats.edges.skipped} 条不在本域
          </div>
        </div>
        <div className="lin-verdict">
          <span className="lin-verdict-label">影响多少表</span>
          <b className="lin-verdict-num">{stats.affected}</b>
          <span className="lin-verdict-unit">张表将获得血缘</span>
          <div className="lin-verdict-foot">
            覆盖 {SCAN_GROUPS.length} 个落点，上游表 {stats.affected - SCAN_GROUPS.length} 张
          </div>
        </div>
        <div className="lin-verdict lin-verdict--key">
          <span className="lin-verdict-label">孤岛怎么变</span>
          <b className="lin-verdict-num">
            {DOMAIN_FACTS.isolated}
            <ArrowRightOutlined className="lin-verdict-arrow" />
            {stats.stillIsolated}
          </b>
          <span className="lin-verdict-unit">张孤岛表</span>
          <div className="lin-verdict-foot">
            {stats.resolved.length} 张靠这个包脱离孤岛，剩 {stats.stillIsolated} 张要手工连
          </div>
        </div>
      </div>

      {/* 明细：按落点分组，不逐条列——一个包上百条边，逐条列没人读得完 */}
      <Table<ScanGroup>
        className="lin-scan-table"
        size="small"
        rowKey="target"
        columns={columns}
        dataSource={SCAN_GROUPS}
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

      {/* 第二个结论：这些孤岛表包里根本没提到，只能手工连 */}
      <div className="section-card lin-uncovered">
        <div className="section-card-head">
          <span className="section-card-head-title">
            <WarningOutlined /> 代码包没覆盖的孤岛表
          </span>
          <span className="section-card-count">{stats.stillIsolated}</span>
        </div>
        <div className="lin-uncovered-body">
          <p className="lin-muted">
            这些表在整个包里一次都没出现——没有 SQL 可推，只能在画布上手工连。
          </p>
          <div className="lin-uncovered-list">
            {UNCOVERED_ISOLATED.map((table) => (
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
            <span className="lin-muted">
              …另 {stats.stillIsolated - UNCOVERED_ISOLATED.length} 张
            </span>
          </div>
        </div>
      </div>

      <Collapse
        size="small"
        className="lin-failures"
        items={[
          {
            key: "failures",
            label: `解析失败的 ${SCAN_FAILURES.length} 个文件（不影响其余结果）`,
            children:
              SCAN_FAILURES.length === 0 ? (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="全部解析成功" />
              ) : (
                <ul className="lin-failure-list">
                  {SCAN_FAILURES.map((item) => (
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
    </div>
  );
}
