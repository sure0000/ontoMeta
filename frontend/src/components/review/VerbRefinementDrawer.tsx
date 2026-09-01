import { BulbOutlined, CheckOutlined } from "@ant-design/icons";
import { Alert, Button, Checkbox, Drawer, Empty, Space, Spin, Table, Tag, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";
import { api } from "../../api";
import type { VerbSuggestion } from "../../types";

const { Text } = Typography;

interface Props {
  ontologyId: string;
  open: boolean;
  onClose: () => void;
  /** 采纳后刷新队列与进度（采纳即已复核，这批关系会离开队列）。 */
  onApplied: () => void | Promise<void>;
}

/**
 * 空动词细化：把「属于/引用」这类没有信息量的动词换成按外键列推断的精确动词。
 *
 * 与旧面板的两点不同，都是审核语义上的：
 * 1. **逐条可取舍**（默认全选，反选例外）——旧版只有「提交全部建议」，要么全要要么全不要。
 * 2. **采纳即已复核**——旧版采纳后把关系标回待复核，等于每跑一次就多欠一批债；
 *    而人明明刚刚逐条看过它们。
 */
export function VerbRefinementDrawer({ ontologyId, open, onClose, onApplied }: Props) {
  const [suggestions, setSuggestions] = useState<VerbSuggestion[]>([]);
  const [loading, setLoading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [excluded, setExcluded] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.suggestVerbRefinements(ontologyId);
      setSuggestions(result.suggestions);
      setExcluded([]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "生成动词建议失败");
    } finally {
      setLoading(false);
    }
  }, [ontologyId]);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  const selected = suggestions.filter((s) => !excluded.includes(s.relation_id));

  const apply = async () => {
    if (selected.length === 0) return;
    setApplying(true);
    try {
      const response = await api.applyVerbRefinements(
        ontologyId,
        selected.map((item) => ({
          relation_id: item.relation_id,
          new_verb: item.suggested_verb,
        })),
      );
      message.success(`已采纳 ${response.updated_count} 条动词，这批关系已计为已复核`);
      await onApplied();
      onClose();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "采纳失败");
    } finally {
      setApplying(false);
    }
  };

  const columns: ColumnsType<VerbSuggestion> = [
    {
      title: "",
      key: "select",
      width: 44,
      render: (_, row) => (
        <Checkbox
          checked={!excluded.includes(row.relation_id)}
          onChange={() =>
            setExcluded((prev) =>
              prev.includes(row.relation_id)
                ? prev.filter((x) => x !== row.relation_id)
                : [...prev, row.relation_id],
            )
          }
        />
      ),
    },
    {
      title: "改写后的三元组",
      key: "triple",
      render: (_, row) => (
        <span>
          {row.source_object_name} <Tag>{row.current_verb}</Tag>
          <Text type="secondary">→</Text> <Tag color="blue">{row.suggested_verb}</Tag>{" "}
          {row.target_object_name}
        </span>
      ),
    },
    {
      title: "依据",
      dataIndex: "method",
      key: "method",
      width: 96,
      render: (method: string) => (
        <Tag color={method === "rule" ? "green" : "gold"}>
          {method === "rule" ? "外键列" : "LLM/兜底"}
        </Tag>
      ),
    },
  ];

  return (
    <Drawer
      title="空动词细化"
      open={open}
      onClose={onClose}
      width={640}
      extra={
        <Space>
          <Button icon={<BulbOutlined />} onClick={() => void load()} loading={loading}>
            重新生成
          </Button>
          <Button
            type="primary"
            icon={<CheckOutlined />}
            loading={applying}
            disabled={selected.length === 0}
            onClick={() => void apply()}
          >
            采纳选中 {selected.length} 条
          </Button>
        </Space>
      }
    >
      {error && <Alert type="error" message={error} showIcon style={{ marginBottom: 12 }} />}
      <Text type="secondary">
        默认全选，反选掉不认可的；采纳即视为已复核，这批关系会离开审核队列。
      </Text>
      <Spin spinning={loading}>
        {suggestions.length === 0 && !loading ? (
          <Empty
            style={{ marginTop: 32 }}
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="没有待细化的空泛动词"
          />
        ) : (
          <Table
            className="om-table"
            style={{ marginTop: 12 }}
            rowKey="relation_id"
            size="small"
            columns={columns}
            dataSource={suggestions}
            pagination={{ pageSize: 20, showSizeChanger: false }}
          />
        )}
      </Spin>
    </Drawer>
  );
}
