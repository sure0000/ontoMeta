import { BulbOutlined, CheckOutlined, UndoOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Checkbox,
  Drawer,
  Empty,
  Input,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../api";
import type { VerbSuggestion } from "../../types";
import { RELATION_TERM_MAX_LENGTH, validateRelationTerm } from "../../utils/relation";

const { Text } = Typography;

interface Props {
  ontologyId: string;
  /**
   * 本批要细化的关系 = 审核台当前组的选中项。
   *
   * 范围必须和人刚在屏幕上看过的那一批一致：全本体扫描会把屏幕外的几百条关系
   * 一起改掉，人根本没看过它们，采纳也就不算复核。
   */
  relationIds: string[];
  open: boolean;
  onClose: () => void;
  /** 采纳后刷新队列与进度（采纳即已复核，这批关系会离开队列）。 */
  onApplied: () => void | Promise<void>;
}

/**
 * 空动词细化：把「属于/引用」这类没有信息量的动词换成按外键列（规则）或 LLM 推断的精确动词。
 *
 * 三条审核语义：
 * 1. **按批次**——只处理审核台当前组选中的关系，与「确认这 N 个」同一个工作单元。
 * 2. **逐条可改**——建议词直接在行内编辑；机器给的是初值，不是结论。
 * 3. **采纳即已复核**——人刚逐条看过并改过，再标回待复核等于凭空多欠一批债。
 */
export function VerbRefinementDrawer({ ontologyId, relationIds, open, onClose, onApplied }: Props) {
  const [suggestions, setSuggestions] = useState<VerbSuggestion[]>([]);
  const [loading, setLoading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [excluded, setExcluded] = useState<string[]>([]);
  /** 人工改过的动词：只存被改过的行，没有的行用机器建议值。 */
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [candidateCount, setCandidateCount] = useState(0);
  const [llmStatus, setLlmStatus] = useState<string>("unused");
  const [error, setError] = useState<string | null>(null);

  // 依赖取 join 而不是数组本身：父组件每次渲染都会新建 selectedIds 数组，
  // 直接依赖它会让这个抽屉在打开期间反复重新生成建议（并冲掉人工改的词）。
  const idsKey = relationIds.join(",");

  const load = useCallback(async () => {
    // 空批次绝不发请求：后端把「没给 relation_ids」当成全本体扫描，
    // 空数组一旦漏过去就会变成对几百条关系的批量改词。
    const ids = idsKey ? idsKey.split(",") : [];
    if (ids.length === 0) {
      setSuggestions([]);
      setCandidateCount(0);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const result = await api.suggestVerbRefinements(ontologyId, ids);
      setSuggestions(result.suggestions);
      setCandidateCount(result.candidate_count ?? result.suggestions.length);
      setLlmStatus(result.llm_status ?? "unused");
      setExcluded([]);
      setEdits({});
    } catch (err) {
      setError(err instanceof Error ? err.message : "生成动词建议失败");
    } finally {
      setLoading(false);
    }
  }, [ontologyId, idsKey]);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  /** 一行最终要写下去的动词：改过用改的，没改用机器建议的。 */
  const verbOf = useCallback(
    (row: VerbSuggestion) => edits[row.relation_id] ?? row.suggested_verb,
    [edits],
  );

  const selected = useMemo(
    () => suggestions.filter((s) => !excluded.includes(s.relation_id)),
    [suggestions, excluded],
  );
  /** 只拦选中的行：反选掉的行填错了也不该挡住其余的采纳。 */
  const invalidCount = useMemo(
    () => selected.filter((row) => validateRelationTerm(verbOf(row))).length,
    [selected, verbOf],
  );

  const setVerb = useCallback((relationId: string, value: string) => {
    setEdits((prev) => ({ ...prev, [relationId]: value }));
    // 动手改一行 = 要这一行。改完还得回头勾上，是白让人多点一次。
    setExcluded((prev) => prev.filter((id) => id !== relationId));
  }, []);

  const toggle = useCallback((relationId: string) => {
    setExcluded((prev) =>
      prev.includes(relationId) ? prev.filter((x) => x !== relationId) : [...prev, relationId],
    );
  }, []);

  const apply = async () => {
    if (selected.length === 0 || invalidCount > 0) return;
    setApplying(true);
    try {
      const response = await api.applyVerbRefinements(
        ontologyId,
        selected.map((item) => ({
          relation_id: item.relation_id,
          new_verb: verbOf(item).trim(),
        })),
      );
      if (response.errors.length > 0) {
        // 后端逐条校验，部分失败也会 200：吞掉 errors 就等于谎报采纳条数。
        message.warning(
          `采纳 ${response.updated_count} 条，${response.errors.length} 条被拒：${response.errors[0]}`,
        );
      } else {
        message.success(`已采纳 ${response.updated_count} 条动词，这批关系已计为已复核`);
      }
      await onApplied();
      onClose();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "采纳失败");
    } finally {
      setApplying(false);
    }
  };

  const allSelected = suggestions.length > 0 && excluded.length === 0;
  const columns: ColumnsType<VerbSuggestion> = [
    {
      title: (
        <Checkbox
          checked={allSelected}
          indeterminate={excluded.length > 0 && excluded.length < suggestions.length}
          disabled={suggestions.length === 0}
          onChange={() => setExcluded(allSelected ? suggestions.map((s) => s.relation_id) : [])}
        />
      ),
      key: "select",
      width: 44,
      render: (_, row) => (
        <Checkbox
          checked={!excluded.includes(row.relation_id)}
          onChange={() => toggle(row.relation_id)}
        />
      ),
    },
    {
      title: "改写后的三元组",
      key: "triple",
      render: (_, row) => {
        const value = verbOf(row);
        const invalid = validateRelationTerm(value);
        const edited = value !== row.suggested_verb;
        return (
          <div>
            <div className="verb-suggest-triple">
              <span className="verb-suggest-obj" title={row.source_object_name}>
                {row.source_object_name}
              </span>
              <span className="verb-suggest-arrow">—</span>
              <Input
                size="small"
                value={value}
                status={invalid ? "error" : undefined}
                maxLength={RELATION_TERM_MAX_LENGTH}
                className="verb-suggest-input"
                aria-label="关系动词"
                onChange={(e) => setVerb(row.relation_id, e.target.value)}
              />
              <span className="verb-suggest-arrow">→</span>
              <span className="verb-suggest-obj" title={row.target_object_name}>
                {row.target_object_name}
              </span>
              {edited && (
                <Tooltip title={`还原机器建议「${row.suggested_verb}」`}>
                  <Button
                    type="text"
                    size="small"
                    icon={<UndoOutlined />}
                    aria-label="还原机器建议"
                    onClick={() =>
                      setEdits((prev) => {
                        const next = { ...prev };
                        delete next[row.relation_id];
                        return next;
                      })
                    }
                  />
                </Tooltip>
              )}
            </div>
            <div className="verb-suggest-sub">
              原动词 <b>{row.current_verb || "（空）"}</b>
              {invalid && <span className="verb-suggest-err">{invalid}</span>}
            </div>
          </div>
        );
      },
    },
    {
      title: "依据",
      dataIndex: "method",
      key: "method",
      width: 96,
      render: (method: string) => (
        <Tag color={method === "rule" ? "green" : method === "llm" ? "gold" : "default"}>
          {method === "rule" ? "外键列" : method === "llm" ? "LLM" : "兜底"}
        </Tag>
      ),
    },
  ];

  /** 一条建议都没有时，得说清是「本批没得改」还是「模型没配/没答上」。 */
  const emptyHint =
    candidateCount === 0
      ? "先在左侧勾选要细化的关系"
      : llmStatus === "unavailable"
        ? `本批 ${candidateCount} 条都没被外键规则命中，又没有配置 LLM，给不出更精确的动词`
        : llmStatus === "failed"
          ? `LLM 调用失败，本批 ${candidateCount} 条没能拿到建议，可重新生成`
          : `本批 ${candidateCount} 条的动词已经够具体，没有可改的`;

  return (
    <Drawer
      title={`动词建议 · 本批 ${relationIds.length} 条`}
      open={open}
      onClose={onClose}
      width={720}
      extra={
        <Space>
          <Button icon={<BulbOutlined />} onClick={() => void load()} loading={loading}>
            重新生成
          </Button>
          <Button
            type="primary"
            icon={<CheckOutlined />}
            loading={applying}
            disabled={selected.length === 0 || invalidCount > 0}
            onClick={() => void apply()}
          >
            采纳选中 {selected.length} 条
          </Button>
        </Space>
      }
    >
      {error && <Alert type="error" message={error} showIcon style={{ marginBottom: 12 }} />}
      <Text type="secondary">
        只处理审核台当前这一批关系。动词可直接改写，机器给的是初值不是结论；采纳即视为已复核，
        这些关系会离开审核队列。
      </Text>
      {invalidCount > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 12 }}
          message={`有 ${invalidCount} 条选中的动词不合规，改好或反选后才能采纳`}
        />
      )}
      <Spin spinning={loading}>
        {suggestions.length === 0 && !loading ? (
          <Empty
            style={{ marginTop: 32 }}
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={emptyHint}
          />
        ) : (
          <Table
            className="om-table"
            style={{ marginTop: 12 }}
            rowKey="relation_id"
            size="small"
            columns={columns}
            dataSource={suggestions}
            pagination={false}
            rowClassName={(row) => (excluded.includes(row.relation_id) ? "verb-suggest-off" : "")}
          />
        )}
      </Spin>
    </Drawer>
  );
}
