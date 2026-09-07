import { Alert, Empty, Modal, Select, Spin, Tag } from "antd";
import { useEffect, useState } from "react";
import type { LineageTableMapping, LineageTableRow } from "../../types";

/**
 * 给一张对不上 DataHub 的表指个目标。
 *
 * blocked 边此前的唯一出路是「重扫」——但重扫用的是同一套自动解析，结果一模一样。
 * 真实原因往往只是库名前缀不同、大小写不同、或者代码里写的是视图别名，人一眼就
 * 看得出该对到哪张表，系统却没有地方让他说。
 *
 * 映射是**域级**的：同一个客户的多个代码包里同一个表名指的都是同一张表，说一次就够。
 */

interface Props {
  open: boolean;
  sqlTable: string;
  tables: LineageTableRow[];
  loading: boolean;
  mappings: LineageTableMapping[];
  saving: boolean;
  onCancel: () => void;
  onSave: (targetUrn: string) => void;
}

export function TableMappingModal({
  open,
  sqlTable,
  tables,
  loading,
  mappings,
  saving,
  onCancel,
  onSave,
}: Props) {
  const [targetUrn, setTargetUrn] = useState<string>("");

  useEffect(() => {
    if (!open) return;
    // 已经映射过就回显，让人看得出这是在改而不是新建。
    const existing = mappings.find(
      (item) => item.sql_table === sqlTable.trim().toLowerCase(),
    );
    setTargetUrn(existing?.target_urn ?? "");
  }, [open, sqlTable, mappings]);

  return (
    <Modal
      title="指定对应表"
      open={open}
      onCancel={onCancel}
      onOk={() => onSave(targetUrn)}
      okText="保存并修复"
      okButtonProps={{ disabled: !targetUrn, loading: saving }}
      width={520}
    >
      <p className="lin-map-intro">
        代码包里写的 <code>{sqlTable}</code> 在 DataHub 里对不上。指定它其实是哪张表后，
        本域<b>所有</b>用到这个表名的边会当场重新判定，重扫时也会自动套用。
      </p>

      {loading ? (
        <div style={{ padding: "24px 0", textAlign: "center" }}>
          <Spin />
        </div>
      ) : tables.length === 0 ? (
        <Empty description="DataHub 表清单还没读出来" />
      ) : (
        <Select
          showSearch
          value={targetUrn || undefined}
          onChange={setTargetUrn}
          placeholder="搜索 DataHub 里的表"
          style={{ width: "100%" }}
          optionFilterProp="label"
          options={tables.map((table) => ({
            value: table.urn,
            label: table.name,
          }))}
        />
      )}

      {mappings.length > 0 && (
        <div className="lin-map-list">
          <div className="lin-map-list-head">本域已有映射</div>
          {mappings.map((item) => (
            <div key={item.id} className="lin-map-row">
              <code>{item.sql_table}</code>
              <span>→</span>
              <Tag>{item.target_table}</Tag>
            </div>
          ))}
        </div>
      )}

      <Alert
        type="info"
        showIcon
        style={{ marginTop: 12 }}
        message="映射只影响表名如何对到 DataHub，不改变已经解析出来的边本身。"
      />
    </Modal>
  );
}
