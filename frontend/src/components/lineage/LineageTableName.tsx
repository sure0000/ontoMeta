import type { ReactNode } from "react";

interface Props {
  name: string;
  className?: string;
  suffix?: ReactNode;
}

const OPAQUE_DATABASE = /^_[0-9a-f]{12,}$/i;
const SYSTEM_DATABASES = new Set(["information_schema", "mysql", "performance_schema", "sys"]);

export function readableDatabaseList(databases: string[]) {
  const visible = databases.filter(
    (database) => !OPAQUE_DATABASE.test(database) && !SYSTEM_DATABASES.has(database.toLowerCase()),
  );
  if (visible.length > 0) return visible.join(" / ");
  return databases.length > 0 ? `${databases.length} 个数据库` : "—";
}

export function LineageJoinKey({ value }: { value: string }) {
  const readable = value.replace(/\b_[0-9a-f]{12,}\./gi, "");
  return (
    <span className="lin-key" title={value}>
      {readable}
    </span>
  );
}

/**
 * 表名的可读展示：保留完整技术名在 title 中，把临时/哈希库前缀从主文本移开。
 * DataHub 与 SQL 仍使用传入的原始名称，组件只负责视觉层的降噪。
 */
export function LineageTableName({ name, className = "", suffix }: Props) {
  const separator = name.lastIndexOf(".");
  const database = separator > 0 ? name.slice(0, separator) : "";
  const table = separator > 0 ? name.slice(separator + 1) : name;
  const system = table.startsWith("__");
  const label = system ? table.slice(2) : table;
  const showDatabase = Boolean(database) && !OPAQUE_DATABASE.test(database);

  return (
    <span className={`lin-table-name${className ? ` ${className}` : ""}`} title={name}>
      <span className="lin-table-name-main">{label || table}</span>
      {system && <span className="lin-table-name-kind">系统表</span>}
      {showDatabase && <span className="lin-table-name-db">{database}</span>}
      {suffix}
    </span>
  );
}
