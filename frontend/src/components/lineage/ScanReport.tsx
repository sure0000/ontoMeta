import {
  ArrowRightOutlined,
  CheckCircleOutlined,
  FileSearchOutlined,
  InboxOutlined,
  NodeIndexOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { Button, Collapse, Segmented, Table, Tag, Upload } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useMemo, useState } from "react";
import { ScanGraph } from "./ScanGraph";
import type { LineagePackageDetail, LineagePackageGroup } from "../../types";
import { LineageJoinKey, LineageTableName } from "./LineageTableName";

/**
 * 一个代码包的扫描结果。
 *
 * 代码包**没有固定格式**——目录怎么放、语句怎么写都不做要求，所以这一屏的重点
 * 不是"解析成功了"，而是三个结论：能补哪些边、影响哪些表、哪些孤岛还是孤岛。
 * 解析失败的文件必须摊开来讲：野生代码包里存储过程和动态 SQL 一定有，藏起来
 * 会让人以为"扫完了就全了"。
 */

type DetailView = "list" | "graph";

interface Props {
  pkg: LineagePackageDetail | null;
  scanning: boolean;
  onScan: (file: File) => void;
  selected: string[];
  onSelectedChange: (keys: string[]) => void;
  frozen: boolean;
  /** 当前域的孤岛表名，用于图上的红点与"仍是孤岛"的判断。 */
  isolated: Set<string>;
  isolatedTotal: number;
  /** 所有代码包都没提到的孤岛表——只能手工连的那批。 */
  uncovered: string[];
  /** DataHub 家底仍在读取时，避免把空结果误显示成“没有孤岛”。 */
  inventoryLoading: boolean;
  /** 把一张表送进画布：两条路径在这里交接。 */
  onSendToCanvas: (table: string) => void;
  /** 给一张对不上 DataHub 的表指个目标——blocked 边此前只能重扫，而重扫结果一样。 */
  onMapTable: (sqlTable: string) => void;
}

function countEdges(groups: LineagePackageGroup[]) {
  const all = groups.flatMap((group) => group.edges);
  return {
    ok: all.filter((edge) => edge.state === "ok").length,
    blocked: all.filter((edge) => edge.state === "blocked").length,
    skipped: all.filter((edge) => edge.state === "skipped").length,
  };
}

function sizeLabel(bytes: number) {
  if (!bytes) return "—";
  const mb = bytes / 1024 / 1024;
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function stamp(value?: string | null) {
  return (value ?? "").replace("T", " ").slice(0, 16);
}

export function ScanReport({
  pkg,
  scanning,
  onScan,
  selected,
  onSelectedChange,
  frozen,
  isolated,
  isolatedTotal,
  uncovered,
  inventoryLoading,
  onSendToCanvas,
  onMapTable,
}: Props) {
  const [view, setView] = useState<DetailView>("list");

  const groups = useMemo(() => pkg?.groups ?? [], [pkg]);
  const stats = useMemo(() => {
    const tables = new Set<string>();
    groups.forEach((group) => {
      tables.add(group.target);
      group.edges.forEach((edge) => tables.add(edge.source_table));
    });
    const resolved = groups.filter((group) => group.isolated).length;
    return {
      edges: countEdges(groups),
      affected: tables.size,
      resolved,
      after: isolatedTotal - resolved,
    };
  }, [groups, isolatedTotal]);

  if (!pkg) {
    return (
      <div className="lin-dropzone">
        <Upload.Dragger
          multiple={false}
          showUploadList={false}
          beforeUpload={(file) => {
            onScan(file as unknown as File);
            return false;
          }}
          disabled={scanning}
        >
          <p className="lin-dropzone-icon">
            <InboxOutlined />
          </p>
          <p className="lin-dropzone-title">把 SQL 代码包拖到这里</p>
          <p className="lin-dropzone-hint">
            .zip / .tar.gz / 单个 .sql 都行。<b>不要求目录结构</b>
            ——递归扫描包内所有 .sql 文件，逐条语句提取 FROM / JOIN / INSERT / CREATE
            的表引用与关联键。扫完的包会留在左边的历史里。
          </p>
        </Upload.Dragger>
        <div className="lin-dropzone-foot">
          {/* 这里原来是个**永远禁用**的按钮，写着「等待选择文件」——占着主行动位却点不动。
              现在它就是文件选择器本身，和上面的拖拽区两条路都通。 */}
          <Upload
            multiple={false}
            showUploadList={false}
            disabled={scanning}
            beforeUpload={(file) => {
              onScan(file as unknown as File);
              return false;
            }}
          >
            <Button type="primary" icon={<FileSearchOutlined />} loading={scanning}>
              {scanning ? "扫描中…" : "选择文件"}
            </Button>
          </Upload>
          <span className="lin-muted">扫描只落库，不写 DataHub；上报是单独一步</span>
        </div>
      </div>
    );
  }

  const done = pkg.applied_edges > 0;

  /** 列宽必须**每一列都写死**（配合 tableLayout=fixed）。
   *
   * 原来只给了几列 width 又用默认的 auto 布局：源文件那一列全是
   * ``gflow/bi-gflow-ry-group-model-risk-score/code/hy_score_ex.sql`` 这种没有断点的
   * 长串，浏览器把它撑到 666px，反过来把「补录后」挤成 30px，于是「补充上下游」
   * 五个字竖着排成五行——**每一行 127px 高**，1211 个落点滚 15 万像素。
   */
  const columns: ColumnsType<LineagePackageGroup> = [
    {
      title: "目标表（血缘落点）",
      dataIndex: "target",
      key: "target",
      ellipsis: true,
      render: (target: string, row) => (
        <div className="lin-cell-table">
          <LineageTableName className="lin-cell-name" name={target} />
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
      width: 150,
      render: (_, row) => {
        const counts = countEdges([row]);
        return (
          <span className="lin-cell-edges">
            {/* 0 条不加粗：这一列扫一眼是找"有多少能补"，把 0 也做成大号数字，
                满屏都是显眼的零，反而看不见真正有边的那几行。 */}
            <b className={counts.ok === 0 ? "lin-cell-zero" : undefined}>{counts.ok}</b> 条
            {counts.blocked > 0 && (
              <Tag color="warning" variant="filled">
                待映射 {counts.blocked}
              </Tag>
            )}
            {counts.skipped > 0 && <Tag variant="filled">跳过 {counts.skipped}</Tag>}
          </span>
        );
      },
    },
    {
      title: "来源文件",
      key: "files",
      width: 300,
      render: (_, row) => (
        <span className="lin-cell-file" title={row.files.join("\n")}>
          {row.files[0]}
          {row.files.length > 1 ? ` 等 ${row.files.length} 个` : ""}
        </span>
      ),
    },
    {
      title: "补录后",
      key: "after",
      width: 104,
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
            {stamp(pkg.uploaded_at)} · {sizeLabel(pkg.size_bytes)} · {pkg.directories} 个目录 ·{" "}
            {pkg.sql_files} 个 .sql · {pkg.statements} 条语句 · 方言 {pkg.dialect}
          </span>
          <Tag color="success" variant="filled">
            解析成功 {pkg.parsed_files}
          </Tag>
          {pkg.failed_files > 0 && (
            <Tag color="warning" variant="filled">
              失败 {pkg.failed_files}
            </Tag>
          )}
        </div>
        {done && (
          <span className="lin-scan-applied">
            <CheckCircleOutlined /> {stamp(pkg.applied_at)} 已上报 {pkg.applied_edges} 条
          </span>
        )}
      </div>

      {/* 三条结论。已上报过的包换一套口径：讲"当时补了什么"，不再讲"能补什么" */}
      <div className="lin-verdicts">
        <div className="lin-verdict">
          <span className="lin-verdict-label">{done ? "已补的血缘" : "能补多少血缘"}</span>
          <b className="lin-verdict-num">{done ? pkg.applied_edges : stats.edges.ok}</b>
          <span className="lin-verdict-unit">{done ? "条边已写入 DataHub" : "条边可上报"}</span>
          <div className="lin-verdict-foot">
            {done
              ? `${stamp(pkg.applied_at)} 上报，重投同一个包不会重复建边`
              : `另有 ${stats.edges.blocked} 条表名对不上 DataHub、${stats.edges.skipped} 条不在本域`}
          </div>
        </div>
        <div className="lin-verdict">
          <span className="lin-verdict-label">影响多少表</span>
          <b className="lin-verdict-num">{stats.affected}</b>
          <span className="lin-verdict-unit">张表将获得血缘</span>
          <div className="lin-verdict-foot">
            覆盖 {groups.length} 个落点，上游表 {Math.max(0, stats.affected - groups.length)} 张
          </div>
        </div>
        <div className="lin-verdict lin-verdict--key">
          <span className="lin-verdict-label">孤岛怎么变</span>
          {done ? (
            <>
              <b className="lin-verdict-num">{pkg.applied_resolved}</b>
              <span className="lin-verdict-unit">张表当时脱离孤岛</span>
              <div className="lin-verdict-foot">已生效：这些落点现在都有上下游，不再计入孤岛</div>
            </>
          ) : (
            <>
              <b className="lin-verdict-num">
                {isolatedTotal}
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

      {/* 明细两种看法：列表用来逐条核对与勾选，图用来看形状（谁喂谁、哪张表是枢纽） */}
      <div className="lin-detail-head">
        <span className="lin-detail-title">扫出的血缘 · {groups.length} 个落点</span>
        <Segmented
          size="small"
          value={view}
          onChange={(value) => setView(value as DetailView)}
          options={[
            { label: "列表", value: "list" },
            { label: "血缘图", value: "graph" },
          ]}
        />
      </div>

      {view === "graph" && <ScanGraph groups={groups} selected={selected} isolated={isolated} />}

      {view === "list" && (
        <Table<LineagePackageGroup>
          className="lin-scan-table"
          size="small"
          rowKey="target"
          columns={columns}
          dataSource={groups}
          // 列宽写死了就必须 fixed，否则 width 只是建议、长文件名照样把版面撑歪。
          tableLayout="fixed"
          /* **必须分页**：实测一个包有 1211 个落点，不分页就是 1211 行同时进 DOM
             （每行还挂着可展开的边清单），滚动条 15 万像素长，人也翻不到底。
             勾选按 rowKey 记在外面，翻页不会丢。 */
          pagination={{
            size: "small",
            defaultPageSize: 20,
            pageSizeOptions: [20, 50, 100],
            showSizeChanger: true,
            showTotal: (total, [from, to]) => `${from}-${to} / 共 ${total} 个落点`,
          }}
          rowSelection={{
            selectedRowKeys: selected,
            onChange: (keys) => onSelectedChange(keys as string[]),
            getCheckboxProps: () => ({ disabled: frozen }),
            /* 全选只勾当前页会让人以为"我已经全选了"，而上报只发这 20 个。
               这里明确给出「选中全部 N 个」与「清空」两个动作。 */
            selections: [
              {
                key: "all",
                text: `选中全部 ${groups.length} 个落点`,
                onSelect: () => onSelectedChange(groups.map((group) => group.target)),
              },
              {
                key: "none",
                text: "清空选择",
                onSelect: () => onSelectedChange([]),
              },
            ],
          }}
          expandable={{
            expandedRowRender: (row) => (
              <div className="lin-edge-list">
                {row.edges.map((edge) => (
                  <div
                    key={edge.id}
                    className={`lin-edge-line${edge.state !== "ok" ? " lin-edge-line--off" : ""}`}
                  >
                    <LineageTableName className="lin-node" name={edge.source_table} />
                    <ArrowRightOutlined className="lin-flow-arrow" />
                    <LineageTableName
                      className="lin-node lin-node--target"
                      name={edge.target_table}
                    />
                    {edge.join_key ? (
                      <LineageJoinKey value={edge.join_key} />
                    ) : (
                      <Tag variant="filled">无 JOIN 条件 · 仅表级</Tag>
                    )}
                    {edge.kind === "relation" && (
                      <Tag color="processing" variant="filled" title="DDL 里声明的外键：这是关联关系不是血缘，不上报 DataHub，只作为关联证据进本体">
                        外键 · 不上报
                      </Tag>
                    )}
                    {edge.state === "blocked" && (
                      <>
                        <Tag color="warning" variant="filled">
                          {edge.reason}
                        </Tag>
                        {/* 对不上 DataHub 的那一端才是要映射的那个 */}
                        <Button
                          size="small"
                          type="link"
                          onClick={() =>
                            onMapTable(
                              edge.reason?.includes("上游")
                                ? edge.source_table
                                : edge.target_table,
                            )
                          }
                        >
                          指定对应表
                        </Button>
                      </>
                    )}
                    {edge.state === "skipped" && <Tag variant="filled">{edge.reason}</Tag>}
                    {edge.applied && (
                      <Tag color="success" variant="filled">
                        已上报
                      </Tag>
                    )}
                    {/* 落点只来自一个文件时不重复印路径：那一行的「来源文件」列已经写了，
                        而这里的路径动辄七八十个字符，每条边都挂一遍就把每条边挤成两行。
                        跨多个文件时才要标出这条边到底出自哪一个。 */}
                    {row.files.length > 1 && (
                      <span className="lin-cell-file" title={edge.source_file}>
                        {edge.source_file}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            ),
          }}
        />
      )}

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
            {uncovered.length === 0 && (
              <span className="lin-muted">
                {inventoryLoading ? "正在同步 DataHub 孤岛清单…" : "没有了"}
              </span>
            )}
            {uncovered.slice(0, 12).map((table) => (
              <button
                key={table}
                type="button"
                className="lin-uncovered-item"
                onClick={() => onSendToCanvas(table)}
              >
                <LineageTableName name={table} />
                <NodeIndexOutlined />
                <em>放到画布</em>
              </button>
            ))}
            {uncovered.length > 12 && (
              <span className="lin-muted">…另 {uncovered.length - 12} 张</span>
            )}
          </div>
        </section>

        {pkg.failures.length > 0 && (
          <Collapse
            size="small"
            className="lin-failures"
            items={[
              {
                key: "failures",
                label: `${pkg.failures.length} 个文件没有产出血缘（解析失败 ${pkg.failed_files} 个）`,
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
