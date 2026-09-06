import { describe, expect, it } from "vitest";
import {
  compactRelationTerm,
  isEmptyVerb,
  isInferredEvidence,
  normalizeCardinality,
  parseJoinKey,
  validateRelationTerm,
} from "./relation";

/**
 * 关系语义的纯逻辑。挑这一组来测，是因为它们**错了也不会报错**——
 * 只是审核队列里少一条旗标、判据栏少一行连接键，看上去一切正常。
 * 而这几个函数与后端各有一份对应实现（evidence_builder / ontology_projection），
 * 两边口径分叉正是这类静默错误的常见来源。
 */

describe("parseJoinKey", () => {
  // 证据句由后端 evidence_builder 生成，正则与 ontology_projection._FK_IN_PROSE 同源。
  // 后端靠它推 ON 条件，前端靠它告诉复核者「机器是凭哪一列认定这条关系的」。
  it("从证据散文里读回连接键", () => {
    expect(parseJoinKey("订单 通过引用字段 customer_id 关联 客户")).toBe("customer_id");
  });

  it("容忍键名带反引号或引号", () => {
    expect(parseJoinKey("A 通过引用字段 `owner_id` 关联 B")).toBe("owner_id");
    expect(parseJoinKey('A 通过引用字段 "owner_id" 关联 B')).toBe("owner_id");
  });

  it("句式对不上时返回 null，不瞎猜", () => {
    expect(parseJoinKey("两张表看起来有关系")).toBeNull();
    expect(parseJoinKey("")).toBeNull();
    expect(parseJoinKey(null)).toBeNull();
  });
});

describe("isEmptyVerb", () => {
  it("把无信息量的动词认出来", () => {
    // 这几个词等于没说，审核队列要据此提醒人补一个真动词
    for (const verb of ["属于", "引用", "关联", "关系", "连接"]) {
      expect(isEmptyVerb(verb)).toBe(true);
    }
  });

  it("空值也算空动词", () => {
    expect(isEmptyVerb("")).toBe(true);
    expect(isEmptyVerb("   ")).toBe(true);
    expect(isEmptyVerb(null)).toBe(true);
    expect(isEmptyVerb(undefined)).toBe(true);
  });

  it("真业务动词不误判", () => {
    expect(isEmptyVerb("下单")).toBe(false);
    expect(isEmptyVerb("归属于")).toBe(false); // 含「属于」但不等于它
  });
});

describe("compactRelationTerm", () => {
  it("已经干净的短谓词原样保留", () => {
    // 曾经的坑：被抽成单动词会丢掉方向后缀（「对账为」→「对账」）
    expect(compactRelationTerm("对账为")).toBe("对账为");
    expect(compactRelationTerm("汇总为")).toBe("汇总为");
    expect(compactRelationTerm("属于")).toBe("属于");
  });

  it("句子式描述压成动词", () => {
    expect(compactRelationTerm("通过外键加工至目标表").length).toBeLessThanOrEqual(8);
  });

  it("超长文本截断到上限", () => {
    expect(compactRelationTerm("一二三四五六七八九十").length).toBeLessThanOrEqual(8);
  });

  it("空白输入原样返回，不抛", () => {
    expect(compactRelationTerm("   ")).toBe("");
  });
});

describe("validateRelationTerm", () => {
  it("接受简短动词", () => {
    expect(validateRelationTerm("下单")).toBeNull();
    expect(validateRelationTerm("包含")).toBeNull();
  });

  it("拒绝空、超长与整句", () => {
    expect(validateRelationTerm("")).toBeTruthy();
    expect(validateRelationTerm("一二三四五六七八九")).toBeTruthy();
    expect(validateRelationTerm("客户下了很多订单。")).toBeTruthy();
  });
});

describe("normalizeCardinality", () => {
  it("认识的基数原样返回，不认识的不硬塞", () => {
    // 归一化不该把没把握的值猜成某个合法枚举——那是把不确定当成事实
    expect(normalizeCardinality("")).toBeUndefined();
    expect(normalizeCardinality(null)).toBeUndefined();
  });
});

describe("isInferredEvidence", () => {
  it("推断出来的关系要能被认出来（人得自己认一遍）", () => {
    expect(isInferredEvidence("依据命名约定推断的外键")).toBe(true);
    expect(isInferredEvidence("源库声明的外键约束")).toBe(false);
    expect(isInferredEvidence(null)).toBe(false);
  });
});
